"""
Whisper STT Service — Speech-to-Text via faster-whisper.

Exposes:
  POST /transcribe        — upload audio file, get transcript
  WS   /ws/stream         — stream raw audio chunks, get real-time transcript
  GET  /health            — health check
"""

import io
import asyncio
import logging
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml
from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stt")

ROOT = Path(__file__).resolve().parent.parent
config = yaml.safe_load((ROOT / "config.yaml").read_text())["stt"]

app = FastAPI(title="Parrot STT")
model: WhisperModel = None


@app.on_event("startup")
async def load_model():
    global model
    gpu_id = int(config.get("gpu_id", 0))
    device = config["device"]
    log.info(f"Loading Whisper {config['model']} on {device}:{gpu_id}...")
    model = WhisperModel(
        config["model"],
        device=device,
        device_index=gpu_id,
        compute_type=config["compute_type"],
    )
    log.info("Whisper model loaded.")


@app.get("/health")
async def health():
    return {"status": "ok", "model": config["model"]}


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    audio_bytes = await file.read()
    audio_data, sample_rate = sf.read(io.BytesIO(audio_bytes))

    if audio_data.ndim > 1:
        audio_data = audio_data.mean(axis=1)
    audio_data = audio_data.astype(np.float32)

    language = config.get("language")
    segments, info = model.transcribe(
        audio_data,
        language=language,
        beam_size=5,
        vad_filter=True,
    )

    text_parts = []
    for seg in segments:
        text_parts.append(seg.text.strip())

    full_text = " ".join(text_parts)
    log.info(f"Transcribed ({info.language}, {info.duration:.1f}s): {full_text[:80]}...")

    return {
        "text": full_text,
        "language": info.language,
        "duration": round(info.duration, 2),
    }


@app.websocket("/ws/stream")
async def stream_transcribe(ws: WebSocket):
    """
    Real-time streaming transcription.
    Client sends raw 16kHz mono PCM16 audio chunks.
    Server sends back partial transcripts as JSON.
    """
    await ws.accept()
    log.info("STT WebSocket connected.")

    buffer = bytearray()
    CHUNK_DURATION = 3  # seconds of audio before transcribing
    SAMPLE_RATE = 16000
    BYTES_PER_SAMPLE = 2
    CHUNK_BYTES = CHUNK_DURATION * SAMPLE_RATE * BYTES_PER_SAMPLE

    try:
        while True:
            data = await ws.receive_bytes()
            buffer.extend(data)

            if len(buffer) >= CHUNK_BYTES:
                audio = np.frombuffer(bytes(buffer), dtype=np.int16).astype(np.float32) / 32768.0
                buffer.clear()

                segments, info = model.transcribe(
                    audio,
                    language=config.get("language"),
                    beam_size=3,
                    vad_filter=True,
                )
                text = " ".join(seg.text.strip() for seg in segments)
                if text:
                    await ws.send_json({"text": text, "final": False})

    except WebSocketDisconnect:
        log.info("STT WebSocket disconnected.")
    except Exception as e:
        log.error(f"STT stream error: {e}")
        await ws.close(code=1011)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config["host"], port=config["port"])
