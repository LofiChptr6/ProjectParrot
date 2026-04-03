#!/usr/bin/env python3
"""
02-dpo/train.py
DPO fine-tuning: trains model to prefer Mocha-style responses over generic ones.
Run AFTER generate_rejected.py has created data/dpo/pairs_complete.json.

Usage:
  cd ~/ProjectParrot/mocha-finetune
  python3 02-dpo/generate_rejected.py    # first
  python3 02-dpo/train.py                # then this
"""

import os
import sys
from pathlib import Path
import yaml

ROOT = Path(__file__).parent.parent
os.chdir(ROOT)


def main():
    with open(ROOT / 'config.yaml') as f:
        config = yaml.safe_load(f)

    cfg = config['training']['dpo']
    hw = config['hardware']
    model_id = config['models']['dpo_base']
    gpu_id = hw['gpu_id']
    output_dir = ROOT / cfg['output_dir']
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Model: {model_id}")
    print(f"GPU: {gpu_id} | Beta: {cfg['beta']} | LR: {cfg['learning_rate']}")

    os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu_id)

    try:
        from unsloth import FastLanguageModel
        from trl import DPOTrainer, DPOConfig
        from datasets import Dataset
        import torch
        import json
    except ImportError as e:
        print(f"Missing dependency: {e}")
        print("Install: pip install unsloth trl datasets")
        sys.exit(1)

    print("Loading model...")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=model_id,
        max_seq_length=cfg['max_seq_length'],
        dtype=torch.bfloat16 if hw['dtype'] == 'bfloat16' else torch.float16,
        load_in_4bit=False,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=cfg['lora_r'],
        lora_alpha=cfg['lora_alpha'],
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth",
    )

    # Load DPO data
    dpo_path = ROOT / 'data/dpo/pairs_complete.json'
    if not dpo_path.exists():
        print(f"ERROR: {dpo_path} not found. Run generate_rejected.py first.")
        sys.exit(1)

    with open(dpo_path) as f:
        raw = json.load(f)

    def format_dpo(ex):
        return {
            "prompt": ex['prompt'],
            "chosen": ex['chosen'],
            "rejected": ex['rejected']
        }

    dataset = Dataset.from_list([format_dpo(ex) for ex in raw])
    print(f"DPO pairs: {len(dataset)}")

    trainer = DPOTrainer(
        model=model,
        ref_model=None,  # uses implicit reference (LoRA base)
        tokenizer=tokenizer,
        train_dataset=dataset,
        args=DPOConfig(
            output_dir=str(output_dir),
            num_train_epochs=cfg['epochs'],
            per_device_train_batch_size=cfg['batch_size'],
            gradient_accumulation_steps=cfg['grad_accumulation'],
            learning_rate=cfg['learning_rate'],
            beta=cfg['beta'],
            logging_steps=10,
            save_strategy="epoch",
            bf16=(hw['dtype'] == 'bfloat16'),
            report_to="none",
            max_length=cfg['max_seq_length'],
        ),
    )

    print("Training DPO...")
    trainer.train()

    model.save_pretrained(str(output_dir / 'adapter'))
    tokenizer.save_pretrained(str(output_dir / 'adapter'))
    print(f"DPO adapter saved to {output_dir}/adapter")


if __name__ == '__main__':
    main()
