"""
TTS Service — Text-to-Speech via F5-TTS.

F5-TTS supports zero-shot voice cloning from a short reference clip.

Exposes:
  POST /synthesize         — text → wav audio
  GET  /health             — health check
"""

import concurrent.futures
import io
import logging
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from tts.torchaudio_soundfile_shim import apply_torchaudio_soundfile_shim

# F5-TTS splits long text into chunks and feeds them through a ThreadPoolExecutor.
# The DiT transformer caches text embeddings (text_cond / text_uncond) on the model
# object itself, so concurrent chunks with different durations corrupt the cache →
# tensor shape mismatch.  Forcing max_workers=1 serialises chunk inference.
_OrigTPE = concurrent.futures.ThreadPoolExecutor
class _SequentialTPE(_OrigTPE):
    def __init__(self, *args, **kwargs):
        kwargs["max_workers"] = 1
        super().__init__(*args, **kwargs)
concurrent.futures.ThreadPoolExecutor = _SequentialTPE  # type: ignore[misc]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tts")

ROOT = Path(__file__).resolve().parent.parent
config = yaml.safe_load((ROOT / "config.yaml").read_text())["tts"]

app = FastAPI(title="Parrot TTS")
tts_model = None


class SynthRequest(BaseModel):
    text: str
    speed: float = 1.0


@app.on_event("startup")
async def load_model():
    global tts_model
    apply_torchaudio_soundfile_shim()

    want = str(config.get("device", "cuda")).lower()
    gpu_id = int(config.get("gpu_id", 0))
    if want == "cpu" or not torch.cuda.is_available():
        device = "cpu"
    else:
        device = f"cuda:{gpu_id}"
    log.info(f"Loading F5-TTS on {device}...")

    from f5_tts.api import F5TTS
    tts_model = F5TTS(device=device)
    log.info("F5-TTS loaded.")


def _get_reference_audio() -> str:
    ref_path = ROOT / config["reference_audio"]
    if not ref_path.exists():
        fallback = ROOT / "audio"
        wavs = list(fallback.glob("*.wav")) + list(fallback.glob("*.mp3"))
        if wavs:
            return str(wavs[0])
        raise FileNotFoundError(
            f"No reference audio found. Place a 6-15s WAV clip at {ref_path}"
        )
    return str(ref_path)


@app.get("/health")
async def health():
    ref_exists = (ROOT / config["reference_audio"]).exists()
    return {
        "status": "ok",
        "engine": "f5-tts",
        "reference_audio_found": ref_exists,
    }


@app.post("/synthesize")
async def synthesize(req: SynthRequest):
    ref_audio = _get_reference_audio()
    ref_text = (config.get("reference_text") or "").strip()

    try:
        wav, sr, _ = tts_model.infer(
            ref_file=ref_audio,
            ref_text=ref_text,
            gen_text=req.text,
            speed=req.speed,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("synthesize failed")
        raise HTTPException(
            status_code=503,
            detail=f"TTS failed: {e!s}. If you see TorchCodec/CUDA errors, set tts.device: cpu in config.yaml or ensure reference_text is set to skip ASR on the reference clip.",
        ) from e

    buf = io.BytesIO()
    sf.write(buf, wav, samplerate=sr, format="WAV")
    buf.seek(0)

    return StreamingResponse(buf, media_type="audio/wav", headers={
        "Content-Disposition": "inline; filename=speech.wav"
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config["host"], port=config["port"])
