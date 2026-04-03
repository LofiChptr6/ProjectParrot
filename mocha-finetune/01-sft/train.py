#!/usr/bin/env python3
"""
01-sft/train.py
SFT fine-tuning using Unsloth + TRL SFTTrainer.
Trains Qwen2.5-32B (or config-specified base) with LoRA on Mocha conversation data.

Usage:
  cd ~/ProjectParrot/mocha-finetune
  python3 01-sft/train.py
  python3 01-sft/train.py --epochs 1  # quick test run
"""

import argparse
import csv
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
os.chdir(ROOT)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--epochs', type=int, default=None)
    parser.add_argument('--dry-run', action='store_true', help='Load model only, no training')
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    cfg = config['training']['sft']
    hw = config['hardware']
    model_id = config['models']['sft_base']
    gpu_id = hw['gpu_id']
    epochs = args.epochs or cfg['epochs']
    output_dir = ROOT / cfg['output_dir']
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Model: {model_id}")
    print(f"GPU: {gpu_id} | Epochs: {epochs} | LR: {cfg['learning_rate']}")
    print(f"Output: {output_dir}")

    # Set GPU
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

    # Load model
    print("Loading model with Unsloth...")
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
        lora_dropout=cfg['lora_dropout'],
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth",
    )

    if args.dry_run:
        print("Dry run — model loaded successfully. Exiting.")
        return

    # Load data
    train_path = ROOT / 'data/sft/train.json'
    with open(train_path) as f:
        raw_data = json.load(f)

    # Convert ShareGPT to text using chat template
    def format_example(ex):
        convs = ex['conversations']
        messages = []
        for c in convs:
            role = 'user' if c['from'] == 'human' else 'assistant'
            messages.append({'role': role, 'content': c['value']})
        return {'text': tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)}

    dataset = Dataset.from_list([format_example(ex) for ex in raw_data])
    print(f"Training examples: {len(dataset)}")

    # Train
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        args=SFTConfig(
            output_dir=str(output_dir),
            num_train_epochs=epochs,
            per_device_train_batch_size=cfg['batch_size'],
            gradient_accumulation_steps=cfg['grad_accumulation'],
            learning_rate=cfg['learning_rate'],
            warmup_ratio=cfg['warmup_ratio'],
            lr_scheduler_type="cosine",
            logging_steps=10,
            save_strategy="epoch",
            bf16=(hw['dtype'] == 'bfloat16'),
            report_to="none",
            dataset_text_field="text",
            max_seq_length=cfg['max_seq_length'],
        ),
    )

    print("Training...")
    trainer_output = trainer.train()

    # Save adapter
    model.save_pretrained(str(output_dir / 'adapter'))
    tokenizer.save_pretrained(str(output_dir / 'adapter'))
    print(f"Adapter saved to {output_dir}/adapter")

    # Log loss
    log_path = output_dir / 'training_log.csv'
    with open(log_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['step', 'loss'])
        for entry in trainer.state.log_history:
            if 'loss' in entry:
                writer.writerow([entry.get('step', ''), entry['loss']])
    print(f"Training log: {log_path}")
    print(f"Final loss: {trainer_output.training_loss:.4f}")


if __name__ == '__main__':
    main()
