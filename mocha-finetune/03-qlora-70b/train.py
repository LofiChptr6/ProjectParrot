#!/usr/bin/env python3
"""
03-qlora-70b/train.py
QLoRA fine-tuning of a 70B model on Mocha data.
Uses 4-bit quantization to fit a 70B model in 96GB VRAM.

Usage:
  cd ~/ProjectParrot/mocha-finetune
  python3 03-qlora-70b/train.py
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

    cfg = config['training']['qlora_70b']
    hw = config['hardware']
    model_id = config['models']['qlora_base']
    gpu_id = hw['gpu_id']
    output_dir = ROOT / cfg['output_dir']
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Model: {model_id} (4-bit QLoRA)")
    print(f"GPU: {gpu_id} | Epochs: {cfg['epochs']} | LR: {cfg['learning_rate']}")

    os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu_id)

    try:
        from unsloth import FastLanguageModel
        from trl import SFTTrainer, SFTConfig
        from datasets import Dataset
        import torch
        import json
    except ImportError as e:
        print(f"Missing dependency: {e}")
        print("Install: pip install unsloth trl datasets")
        sys.exit(1)

    print("Loading 70B model in 4-bit quantization...")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=model_id,
        max_seq_length=cfg['max_seq_length'],
        dtype=None,         # auto-detect
        load_in_4bit=True,  # QLoRA: 4-bit base, full-precision adapters
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=cfg['lora_r'],
        lora_alpha=cfg['lora_alpha'],
        lora_dropout=cfg['lora_dropout'],
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth",
    )

    # Load SFT data (same dataset as SFT but applied to 70B)
    train_path = ROOT / 'data/sft/train.json'
    with open(train_path) as f:
        raw_data = json.load(f)

    def format_example(ex):
        convs = ex['conversations']
        messages = [
            {'role': 'user' if c['from'] == 'human' else 'assistant', 'content': c['value']}
            for c in convs
        ]
        return {'text': tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)}

    dataset = Dataset.from_list([format_example(ex) for ex in raw_data])
    print(f"Training examples: {len(dataset)}")

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        args=SFTConfig(
            output_dir=str(output_dir),
            num_train_epochs=cfg['epochs'],
            per_device_train_batch_size=cfg['batch_size'],
            gradient_accumulation_steps=cfg['grad_accumulation'],
            learning_rate=cfg['learning_rate'],
            warmup_ratio=0.03,
            lr_scheduler_type="cosine",
            logging_steps=10,
            save_strategy="epoch",
            fp16=False,
            bf16=False,  # 4-bit uses its own dtype internally
            report_to="none",
            dataset_text_field="text",
            max_seq_length=cfg['max_seq_length'],
        ),
    )

    print("Training (QLoRA 70B)...")
    trainer.train()

    model.save_pretrained(str(output_dir / 'adapter'))
    tokenizer.save_pretrained(str(output_dir / 'adapter'))
    print(f"QLoRA adapter saved to {output_dir}/adapter")
    print("Note: merge adapter with base model using unsloth.merge_to_gguf() before running with Ollama")


if __name__ == '__main__':
    main()
