# 02-dpo — Direct Preference Optimization

Trains the model to *prefer* Mocha-style responses over generic LLM responses.
More targeted than SFT — explicitly teaches what "wrong" looks like.

## What it does
DPO uses pairs: (prompt, chosen=Mocha response, rejected=generic GLM response).
The model learns the *delta* between Mocha's voice and a vanilla assistant.

## Run (in order)
```bash
cd ~/ProjectParrot/mocha-finetune

# Step 1: Generate rejected responses (runs Ollama with no personality prompt)
python3 02-dpo/generate_rejected.py

# Test with fewer examples first:
python3 02-dpo/generate_rejected.py --limit 20

# Step 2: Train DPO
python3 02-dpo/train.py
```

## Output
```
data/dpo/
├── pairs_skeleton.json   ← prompts + chosen (no rejected yet)
└── pairs_complete.json   ← full DPO pairs (after generate_rejected.py)

outputs/dpo/
└── adapter/              ← DPO-trained LoRA adapter
```

## Eval
```bash
python3 02-dpo/eval.py
# Outputs: outputs/dpo/judge_results.csv
```

## Hyperparams (edit config.yaml)
- `beta: 0.1` — DPO temperature. Higher = stronger preference signal, risk of instability.
- `epochs: 1` — DPO usually needs fewer epochs than SFT.

## Notes
- Make sure Ollama is running before generate_rejected.py
- The rejection model (qwen2.5:32b by default) must be pulled: `ollama pull qwen2.5:32b`
- For best results, run SFT first and use its adapter as the DPO base (change dpo_base in config.yaml)
