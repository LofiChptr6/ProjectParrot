#!/usr/bin/env python3
"""
scripts/prepare_data.py
Processes raw Mocha conversation history into SFT, DPO, and eval datasets.

Input:  data/raw/mocha_raw.json
Output: data/sft/train.json      — ShareGPT format for SFT training
        data/eval/eval.json      — held-out eval set (10%)
        data/dpo/pairs_skeleton.json — DPO pairs (rejected field empty, filled by generate_rejected.py)
"""

import json
import random
import re
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).parent.parent
RAW_PATH = ROOT / "data/raw/mocha_raw.json"
SFT_OUT = ROOT / "data/sft/train.json"
EVAL_OUT = ROOT / "data/eval/eval.json"
DPO_OUT = ROOT / "data/dpo/pairs_skeleton.json"

# Patterns that indicate cron/tool outputs to filter
SKIP_PATTERNS = [
    r'^\[\[reply_to_current\]\]',
    r'^\[cron:',
    r'^HEARTBEAT_OK',
    r'^NO_REPLY',
    r'Successfully wrote \d+ bytes',
    r'^\{.*"ok":\s*true',  # JSON tool responses
]

def is_skip(text: str) -> bool:
    for pat in SKIP_PATTERNS:
        if re.search(pat, text.strip()):
            return True
    # Skip very tool-heavy responses (lots of JSON)
    if text.count('{') > 10 and text.count('"role"') > 3:
        return True
    return False

def clean_text(text: str) -> str:
    # Remove [[reply_to_current]] prefix
    text = re.sub(r'^\[\[reply_to_current\]\]\s*', '', text.strip())
    return text.strip()


def main():
    with open(RAW_PATH) as f:
        raw = json.load(f)

    print(f"Raw messages: {len(raw)}")

    # Group by source file to reconstruct conversation turns
    by_source = defaultdict(list)
    for msg in raw:
        by_source[msg['source']].append(msg)

    pairs = []
    for source, msgs in by_source.items():
        for i in range(len(msgs) - 1):
            curr = msgs[i]
            nxt = msgs[i + 1]

            if curr['role'] != 'user' or nxt['role'] != 'assistant':
                continue

            user_text = clean_text(curr['content'])
            asst_text = clean_text(nxt['content'])

            # Filter
            if is_skip(asst_text):
                continue
            if len(asst_text) < 20 or len(asst_text) > 1000:
                continue
            if len(user_text) < 3:
                continue

            pairs.append({
                'user': user_text,
                'assistant': asst_text,
                'source': source
            })

    print(f"Valid pairs after filtering: {len(pairs)}")

    # Shuffle and split
    random.seed(42)
    random.shuffle(pairs)
    split = int(len(pairs) * 0.9)
    train_pairs = pairs[:split]
    eval_pairs = pairs[split:]

    print(f"Train: {len(train_pairs)}, Eval: {len(eval_pairs)}")

    # SFT format (ShareGPT)
    def to_sharegpt(pair_list):
        return [
            {
                "conversations": [
                    {"from": "human", "value": p['user']},
                    {"from": "gpt", "value": p['assistant']}
                ]
            }
            for p in pair_list
        ]

    with open(SFT_OUT, 'w') as f:
        json.dump(to_sharegpt(train_pairs), f, indent=2, ensure_ascii=False)
    print(f"SFT train saved: {SFT_OUT}")

    with open(EVAL_OUT, 'w') as f:
        json.dump(to_sharegpt(eval_pairs), f, indent=2, ensure_ascii=False)
    print(f"Eval saved: {EVAL_OUT}")

    # DPO skeleton (rejected = "" — filled by generate_rejected.py)
    dpo_skeleton = [
        {
            "prompt": p['user'],
            "chosen": p['assistant'],
            "rejected": ""
        }
        for p in train_pairs
    ]
    with open(DPO_OUT, 'w') as f:
        json.dump(dpo_skeleton, f, indent=2, ensure_ascii=False)
    print(f"DPO skeleton saved: {DPO_OUT}")

    print("\nSample train pair:")
    if train_pairs:
        p = train_pairs[0]
        print(f"  User: {p['user'][:80]}")
        print(f"  Asst: {p['assistant'][:120]}")

    print("\nDone.")


if __name__ == "__main__":
    main()
