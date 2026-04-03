# 03-qlora-70b — QLoRA Fine-Tuning (70B)

Fine-tunes a 70B parameter model using 4-bit quantization (QLoRA).
Highest quality ceiling — 70B models have far better instruction following and personality retention.

## What it does
QLoRA quantizes the base model to 4-bit (fits ~40GB on Blackwell) then trains
full-precision LoRA adapters on top. Best personality fidelity but slowest training.

## Requirements
- LLaMA 3.1 70B must be downloaded from HuggingFace:
```bash
pip install huggingface_hub
huggingface-cli download meta-llama/Meta-Llama-3.1-70B-Instruct --local-dir ~/.cache/huggingface/hub/meta-llama-70b
# Requires HuggingFace account + accepting LLaMA license at huggingface.co/meta-llama
```
- Or change `qlora_base` in config.yaml to any available 70B model

## Run
```bash
cd ~/ProjectParrot/mocha-finetune
python3 03-qlora-70b/train.py
```

## Expected VRAM usage
- 70B in 4-bit: ~40GB
- LoRA adapters + optimizer + activations: ~35GB
- Total: ~75GB — fits on Blackwell 96GB with headroom

## Training time
- ~6-12 hours for 2 epochs at batch_size=1, grad_accumulation=16 on Blackwell

## Output
```
outputs/qlora-70b/
└── adapter/    ← QLoRA adapter (merge with base before using with Ollama)
```

## Eval
```bash
python3 03-qlora-70b/eval.py
# Outputs: outputs/qlora-70b/judge_results.csv
```

## Export to Ollama
```python
from unsloth import FastLanguageModel
model, tok = FastLanguageModel.from_pretrained("outputs/qlora-70b/adapter", load_in_4bit=True)
model.save_pretrained_gguf("outputs/qlora-70b/mocha-70b-q4", tok, quantization_method="q4_k_m")
# ollama create mocha-70b -f outputs/qlora-70b/mocha-70b-q4/Modelfile
```

## Notes
- Significantly better than 32B at maintaining Mocha's voice over long conversations
- Same training data as SFT (data/sft/train.json) — more data helps more here
- Change `qlora_base` in config.yaml to use a different 70B model
