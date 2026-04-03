# 01-sft — Supervised Fine-Tuning

Trains Qwen2.5-32B with LoRA to imitate Mocha's response style directly from conversation history.

## What it does
SFT learns to map user inputs → Mocha-style outputs by training on real conversation pairs.
Fastest approach, lowest quality ceiling. Good starting point.

## Run
```bash
cd ~/ProjectParrot/mocha-finetune

# Full training (3 epochs, ~2-4 hours on Blackwell)
python3 01-sft/train.py

# Quick test (1 epoch, verify setup works)
python3 01-sft/train.py --epochs 1

# Dry run (load model only, no training — check VRAM)
python3 01-sft/train.py --dry-run
```

## Output
```
outputs/sft/
├── adapter/         ← LoRA adapter (use this with base model)
└── training_log.csv ← loss per step
```

## Eval
```bash
python3 01-sft/eval.py
# Outputs: outputs/sft/judge_results.csv
```

## Export to Ollama
```python
from unsloth import FastLanguageModel
model, tok = FastLanguageModel.from_pretrained("outputs/sft/adapter")
model.save_pretrained_gguf("outputs/sft/mocha-q8", tok, quantization_method="q8_0")
# ollama create mocha-sft -f outputs/sft/mocha-q8/Modelfile
```

## Hyperparams (edit config.yaml)
- `lora_r: 16` — LoRA rank. Higher = more expressive but more VRAM.
- `epochs: 3` — More epochs = more personality absorption but risk of overfitting small dataset.
- `learning_rate: 2e-4` — Standard for LoRA SFT.

## Notes
- Current dataset: 113 training pairs. More data = better results.
- Run prepare_data.py again after adding more conversation history.
