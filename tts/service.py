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

app = FastAPI(title="Mocha TTS")
tts_model = None


class SynthRequest(BaseModel):
    text: str
    speed: float = 1.0
    # Optional per-call reference voice. Absolute path OR a path relative to
    # the project root. When omitted/missing, falls back to the global
    # config.yaml `tts.reference_audio`. Used by the bridge to pass each
    # user's active voice from data/users/{uid}/voices/. Adds zero latency
    # — F5-TTS already reads the ref file fresh on every infer() call.
    ref_audio_path: str | None = None
    # Optional per-call reference TEXT — what's spoken in ref_audio. When
    # provided, F5-TTS skips its in-process Whisper ASR step entirely.
    # The bridge supplies this from a sidecar .txt next to the voice file
    # so per-user voices don't need TTS-side transcription on every cold
    # start (which currently breaks when torchcodec can't load).
    ref_text: str | None = None
    # Optional per-call cfg_strength override. When None, uses the server's
    # config.yaml `tts.cfg_strength`. Higher → more matches reference
    # (calmer, less prosody variation). Lower → more free (more swings).
    # F5's default is 2.0; we ship 2.2 to dampen exclamation/ALL-CAPS spikes.
    cfg_strength: float | None = None


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


def _resolve_per_call_ref(raw: str | None) -> str | None:
    """Resolve a per-call ref_audio_path against the project root.

    Returns the absolute path string if the file exists, else None so the
    caller falls back to the global default. Defends against path traversal:
    the resolved path must be inside ROOT.
    """
    if not raw:
        return None
    try:
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / p
        p = p.resolve()
        # Must live under the project root and exist as a real file.
        try:
            p.relative_to(ROOT.resolve())
        except ValueError:
            log.warning("ref_audio_path outside ROOT, ignoring: %s", raw)
            return None
        if not p.is_file():
            log.warning("ref_audio_path missing, falling back: %s", p)
            return None
        return str(p)
    except Exception as exc:
        log.warning("ref_audio_path resolve failed (%s): %s", raw, exc)
        return None


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
    # Per-call voice wins; fall back to the global config default.
    custom_ref = _resolve_per_call_ref(req.ref_audio_path)
    ref_audio = custom_ref or _get_reference_audio()
    # ref_text resolution priority:
    #   1. Per-call ref_text (caller supplied alongside ref_audio_path)
    #   2. Global config.tts.reference_text (only valid when no custom voice)
    #   3. Empty string → F5-TTS runs in-process Whisper ASR (slow, fragile;
    #      breaks if torchcodec can't load — that's why path 1 exists).
    if req.ref_text and req.ref_text.strip():
        ref_text = req.ref_text.strip()
    elif custom_ref:
        ref_text = ""
    else:
        ref_text = (config.get("reference_text") or "").strip()

    # cfg_strength = classifier-free guidance strength. Default in F5-TTS
    # is 2.0; raising to ~2.2-2.5 dampens prosody peaks (e.g. F5's pitch
    # spike on "!" or ALL CAPS) by leaning harder on the calm reference.
    # Server-side default from config.yaml; per-call override accepted.
    cfg_strength = req.cfg_strength
    if cfg_strength is None:
        cfg_strength = float(config.get("cfg_strength", 2.0))

    try:
        wav, sr, _ = tts_model.infer(
            ref_file=ref_audio,
            ref_text=ref_text,
            gen_text=req.text,
            speed=req.speed,
            cfg_strength=cfg_strength,
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


@app.post("/reload")
async def reload():
    """Reload F5-TTS model, re-read config, and clear all caches."""
    global tts_model, config
    # Re-read config so reference_text changes take effect
    config = yaml.safe_load((ROOT / "config.yaml").read_text())["tts"]
    try:
        from f5_tts.infer.utils_infer import _ref_audio_cache, _ref_text_cache
        _ref_audio_cache.clear()
        _ref_text_cache.clear()
    except (ImportError, AttributeError):
        pass
    tts_model = None
    await load_model()
    log.info("F5-TTS model reloaded via /reload endpoint (config re-read)")
    return {"status": "reloaded"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config["host"], port=config["port"])
