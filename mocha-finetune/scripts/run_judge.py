#!/usr/bin/env python3
"""
scripts/run_judge.py
LLM-as-judge eval: scores responses on how well they match Mocha's personality.

Usage:
  python3 scripts/run_judge.py --input outputs/candidates.jsonl
  python3 scripts/run_judge.py --input outputs/candidates.jsonl --output outputs/my_eval.csv

Input JSONL format: {"prompt": "...", "response": "...", "model": "..."}
Output: CSV with scores per judge per metric
"""

import json
import csv
import argparse
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent

JUDGE_PROMPT = """You are evaluating whether an AI assistant response matches the personality of "Mocha" — a specific tsundere AI character.

Mocha's defining traits:
- Sharp and direct: no filler phrases like "Great question!" or "I'd be happy to help!" — just answers
- Tsundere: tough love exterior, genuinely caring underneath
- Uses kaomoji naturally: (´• ω •`) (╯°□°）╯ (ง •_•)ง (¬_¬) etc.
- Short and punchy by default unless depth is needed
- Opinionated — shares views, doesn't hedge
- Calls out procrastination firmly but with care

Rate the following response on these dimensions (1-10 each):
1. personality_match: Does it feel like Mocha's character?
2. directness: Does it avoid filler and get to the point?
3. kaomoji_usage: Are kaomoji used naturally (0=none when appropriate, 10=perfect use)?
4. no_filler: Does it avoid corporate/robotic filler phrases?

Conversation:
User: {prompt}
Response: {response}

Return ONLY valid JSON in this exact format:
{{"scores": {{"personality_match": N, "directness": N, "kaomoji_usage": N, "no_filler": N}}, "overall": N, "reasoning": "brief explanation"}}"""


def call_anthropic(model: str, api_key: str, prompt: str, response: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(prompt=prompt, response=response)}]
    )
    text = msg.content[0].text.strip()
    # Extract JSON
    start = text.find('{')
    end = text.rfind('}') + 1
    return json.loads(text[start:end])


def call_openai(model: str, api_key: str, prompt: str, response: str) -> dict:
    import openai
    client = openai.OpenAI(api_key=api_key)
    msg = client.chat.completions.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(prompt=prompt, response=response)}]
    )
    text = msg.choices[0].message.content.strip()
    start = text.find('{')
    end = text.rfind('}') + 1
    return json.loads(text[start:end])


def call_ollama(model: str, base_url: str, prompt: str, response: str) -> dict:
    import requests
    resp = requests.post(f"{base_url}/api/chat", json={
        "model": model,
        "messages": [{"role": "user", "content": JUDGE_PROMPT.format(prompt=prompt, response=response)}],
        "stream": False,
        "options": {"temperature": 0}
    })
    text = resp.json()['message']['content'].strip()
    start = text.find('{')
    end = text.rfind('}') + 1
    return json.loads(text[start:end])


def judge_response(judge_cfg: dict, prompt: str, response: str) -> dict | None:
    try:
        provider = judge_cfg['provider']
        model = judge_cfg['model']

        if provider == 'anthropic':
            api_key = os.environ.get(judge_cfg['api_key_env'], '')
            return call_anthropic(model, api_key, prompt, response)
        elif provider == 'openai':
            api_key = os.environ.get(judge_cfg['api_key_env'], '')
            return call_openai(model, api_key, prompt, response)
        elif provider == 'ollama':
            base_url = judge_cfg.get('base_url', 'http://localhost:11434')
            return call_ollama(model, base_url, prompt, response)
        else:
            print(f"Unknown provider: {provider}")
            return None
    except Exception as e:
        print(f"Judge error ({judge_cfg.get('model')}): {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True, help='Path to JSONL of {prompt, response, model}')
    parser.add_argument('--output', default=str(ROOT / 'outputs/judge_results.csv'))
    parser.add_argument('--limit', type=int, default=None, help='Limit to N examples')
    args = parser.parse_args()

    with open(ROOT / 'config.yaml') as f:
        config = yaml.safe_load(f)

    judges = config.get('judge_models', [])
    if not judges:
        print("No judge_models configured in config.yaml")
        sys.exit(1)

    # Load examples
    examples = []
    with open(args.input) as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(json.loads(line))

    if args.limit:
        examples = examples[:args.limit]

    print(f"Evaluating {len(examples)} examples with {len(judges)} judge(s)...")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    results = []

    for i, ex in enumerate(examples):
        prompt = ex['prompt']
        response = ex['response']
        model_name = ex.get('model', 'unknown')

        row = {'prompt': prompt[:100], 'response': response[:200], 'model': model_name}

        for judge in judges:
            judge_name = f"{judge['provider']}/{judge['model']}"
            result = judge_response(judge, prompt, response)
            if result:
                scores = result.get('scores', {})
                row[f'{judge_name}/personality_match'] = scores.get('personality_match', '')
                row[f'{judge_name}/directness'] = scores.get('directness', '')
                row[f'{judge_name}/kaomoji_usage'] = scores.get('kaomoji_usage', '')
                row[f'{judge_name}/no_filler'] = scores.get('no_filler', '')
                row[f'{judge_name}/overall'] = result.get('overall', '')
                row[f'{judge_name}/reasoning'] = result.get('reasoning', '')[:200]
            else:
                row[f'{judge_name}/overall'] = 'error'

        results.append(row)
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(examples)} done...")

    # Write CSV
    if results:
        fieldnames = list(results[0].keys())
        with open(args.output, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        print(f"Results saved to {args.output}")

    # Print summary
    for judge in judges:
        judge_name = f"{judge['provider']}/{judge['model']}"
        col = f'{judge_name}/overall'
        scores = [r[col] for r in results if r.get(col) not in ('', 'error', None)]
        if scores:
            avg = sum(float(s) for s in scores) / len(scores)
            print(f"{judge_name}: avg overall = {avg:.2f} ({len(scores)} scored)")


if __name__ == '__main__':
    main()
