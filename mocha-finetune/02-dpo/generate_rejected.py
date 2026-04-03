#!/usr/bin/env python3
"""
02-dpo/generate_rejected.py
Generates "rejected" responses for DPO training by calling the base model (Ollama)
with no personality prompt — produces generic/vanilla responses vs Mocha's style.

Usage:
  cd ~/ProjectParrot/mocha-finetune
  python3 02-dpo/generate_rejected.py
  python3 02-dpo/generate_rejected.py --limit 50  # test with fewer examples
"""

import json
import argparse
import time
from pathlib import Path
import requests

ROOT = Path(__file__).parent.parent

def main():
    import yaml
    with open(ROOT / 'config.yaml') as f:
        config = yaml.safe_load(f)

    base_url = config['models']['ollama_base_url']
    model = config['models']['ollama_rejection_model']

    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--input', default=str(ROOT / 'data/dpo/pairs_skeleton.json'))
    parser.add_argument('--output', default=str(ROOT / 'data/dpo/pairs_complete.json'))
    args = parser.parse_args()

    with open(args.input) as f:
        pairs = json.load(f)

    if args.limit:
        pairs = pairs[:args.limit]

    print(f"Generating rejected responses for {len(pairs)} pairs using {model}...")
    print(f"Ollama URL: {base_url}")

    completed = []
    errors = 0

    for i, pair in enumerate(pairs):
        prompt = pair['prompt']
        try:
            resp = requests.post(
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ],
                    "stream": False,
                    "options": {"temperature": 0.7, "max_tokens": 400}
                },
                timeout=60
            )
            rejected = resp.json()['message']['content'].strip()
        except Exception as e:
            print(f"  Error on {i}: {e}")
            rejected = "I'd be happy to help with that!"  # generic fallback
            errors += 1

        completed.append({
            "prompt": prompt,
            "chosen": pair['chosen'],
            "rejected": rejected
        })

        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(pairs)} done ({errors} errors)...")
            time.sleep(0.5)  # be gentle with Ollama

    with open(args.output, 'w') as f:
        json.dump(completed, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(completed)} pairs to {args.output}")
    print(f"Errors: {errors}")
    print("\nSample pair:")
    if completed:
        p = completed[0]
        print(f"  Prompt:   {p['prompt'][:80]}")
        print(f"  Chosen:   {p['chosen'][:120]}")
        print(f"  Rejected: {p['rejected'][:120]}")


if __name__ == '__main__':
    main()
