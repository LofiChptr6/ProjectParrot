#!/usr/bin/env python3
"""
Shared eval script (used by all three approaches).
Run inference on eval set, score with LLM judges, compare base vs fine-tuned.

Usage:
  cd ~/ProjectParrot/mocha-finetune
  python3 01-sft/eval.py
  python3 02-dpo/eval.py
  python3 03-qlora-70b/eval.py

Each approach's eval.py imports this and passes the adapter path.
"""

import json
import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_eval(adapter_path: str, approach_name: str, limit: int = 20):
    """
    Load fine-tuned model, run on eval set, score with judges.
    adapter_path: path to saved LoRA adapter
    approach_name: label for output (e.g. "sft", "dpo", "qlora-70b")
    limit: number of eval examples to score (keep small for cost)
    """
    import yaml
    with open(ROOT / 'config.yaml') as f:
        config = yaml.safe_load(f)

    hw = config['hardware']
    os.environ['CUDA_VISIBLE_DEVICES'] = str(hw['gpu_id'])

    eval_path = ROOT / 'data/eval/eval.json'
    output_path = ROOT / f'outputs/{approach_name}/eval_candidates.jsonl'
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from unsloth import FastLanguageModel
        import torch
        print(f"Loading fine-tuned adapter from {adapter_path}...")
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=adapter_path,
            max_seq_length=2048,
            dtype=torch.bfloat16,
            load_in_4bit=False,
        )
        FastLanguageModel.for_inference(model)
    except Exception as e:
        print(f"Could not load model: {e}")
        sys.exit(1)

    with open(eval_path) as f:
        eval_data = json.load(f)

    examples = eval_data[:limit]
    print(f"Running inference on {len(examples)} eval examples...")

    candidates = []
    for ex in examples:
        convs = ex['conversations']
        prompt = next((c['value'] for c in convs if c['from'] == 'human'), '')
        reference = next((c['value'] for c in convs if c['from'] == 'gpt'), '')

        messages = [{'role': 'user', 'content': prompt}]
        inputs = tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, return_tensors='pt'
        ).to(model.device)

        with torch.no_grad():
            out = model.generate(inputs, max_new_tokens=256, temperature=0.7, do_sample=True)

        response = tokenizer.decode(out[0][inputs.shape[1]:], skip_special_tokens=True).strip()
        candidates.append({
            'prompt': prompt,
            'response': response,
            'reference': reference,
            'model': approach_name
        })

    with open(output_path, 'w') as f:
        for c in candidates:
            f.write(json.dumps(c) + '\n')

    print(f"Candidates saved to {output_path}")
    print("Running judge evaluation...")

    judge_out = ROOT / f'outputs/{approach_name}/judge_results.csv'
    result = subprocess.run(
        [sys.executable, str(ROOT / 'scripts/run_judge.py'),
         '--input', str(output_path),
         '--output', str(judge_out)],
        capture_output=True, text=True
    )
    print(result.stdout)
    if result.stderr:
        print("Errors:", result.stderr[:500])

    print(f"\nEval complete for {approach_name}. Results: {judge_out}")
