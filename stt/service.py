"""
Whisper STT Service — Speech-to-Text via faster-whisper.

Exposes:
  POST /transcribe        — upload audio file, get transcript
  POST /align             — forced phoneme alignment via torchaudio MMS
  WS   /ws/stream         — stream raw audio chunks, get real-time transcript
  GET  /health            — health check
"""

import io
import asyncio
import logging
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio
import yaml
from fastapi import FastAPI, Form, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stt")

ROOT = Path(__file__).resolve().parent.parent
config = yaml.safe_load((ROOT / "config.yaml").read_text())["stt"]

app = FastAPI(title="Mocha STT")
model: WhisperModel = None
_align_model = None
_align_tokenizer = None
_align_device = None
_ALIGN_SR = 16000


@app.on_event("startup")
async def load_model():
    global model, _align_model, _align_tokenizer, _align_device
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

    # Load torchaudio MMS forced alignment model
    _align_device = torch.device(f"cuda:{gpu_id}" if device != "cpu" and torch.cuda.is_available() else "cpu")
    log.info(f"Loading MMS forced alignment model on {_align_device}...")
    from torchaudio.pipelines import MMS_FA as bundle
    _align_model = bundle.get_model().to(_align_device)
    _align_model.eval()
    _align_tokenizer = bundle.get_tokenizer()
    log.info("MMS alignment model loaded.")


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
        initial_prompt=config.get("initial_prompt"),
        hotwords=config.get("hotwords"),
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


@app.post("/align")
async def align(audio: UploadFile = File(...), text: str = Form(...)):
    """Forced phoneme alignment via torchaudio MMS.

    Accepts an audio file and its known transcript. Returns word-level
    timestamps and viseme data for lip sync.
    """
    try:
        audio_bytes = await audio.read()
        result = await asyncio.to_thread(_run_alignment, audio_bytes, text)
        return result
    except Exception as e:
        log.error(f"Alignment failed: {e}", exc_info=True)
        return {"words": [], "phonemes": [], "viseme_b64": "", "viseme_fps": 30, "viseme_frames": 0}


@torch.inference_mode()
def _run_alignment(audio_bytes: bytes, text: str) -> dict:
    """Run MMS forced alignment (blocking, call via to_thread)."""
    from torchaudio.functional import forced_align
    from stt.viseme_map import phonemes_to_viseme_weights, pack_viseme_b64

    # Load and resample audio to 16kHz mono
    audio_data, sr = sf.read(io.BytesIO(audio_bytes))
    if audio_data.ndim > 1:
        audio_data = audio_data.mean(axis=1)
    waveform = torch.from_numpy(audio_data.astype(np.float32)).unsqueeze(0)
    if sr != _ALIGN_SR:
        waveform = torchaudio.functional.resample(waveform, sr, _ALIGN_SR)
    duration = waveform.shape[1] / _ALIGN_SR

    # Tokenize text → token IDs for MMS model
    # MMS tokenizer expects lowercase text, returns list of token IDs
    tokens = _align_tokenizer(text.lower().strip())
    if not tokens:
        return {"words": [], "phonemes": [], "viseme_b64": "", "viseme_fps": 30, "viseme_frames": 0}

    # Get emission probabilities from MMS model
    emission, _ = _align_model(waveform.to(_align_device))  # (1, T_enc, vocab)

    # Run forced alignment
    token_ids = torch.tensor([tokens], dtype=torch.int32)
    aligned = forced_align(emission, token_ids)
    # aligned is a list of (token_id, start_frame, end_frame) tuples
    # or a Tensor depending on torchaudio version

    # Convert frame indices to timestamps
    # MMS model has a stride of 320 samples at 16kHz → 20ms per frame
    frame_dur = 320 / _ALIGN_SR  # 0.02s

    # Build word and phoneme timelines
    words_out = []
    phonemes_out = []

    # Split text into words to group aligned tokens
    text_words = text.lower().strip().split()
    char_list = list(text.lower().strip().replace(" ", ""))

    # Extract per-token timestamps from alignment result
    token_times = []
    if isinstance(aligned, torch.Tensor):
        # aligned shape: (1, T_enc) — each frame maps to a token index
        aligned_seq = aligned[0].cpu().tolist()
        current_token = -1
        start_frame = 0
        for frame_idx, tok in enumerate(aligned_seq):
            if tok != current_token:
                if current_token >= 0 and current_token < len(char_list):
                    token_times.append({
                        "char": char_list[current_token] if current_token < len(char_list) else "?",
                        "start": round(start_frame * frame_dur, 3),
                        "end": round(frame_idx * frame_dur, 3),
                    })
                current_token = tok
                start_frame = frame_idx
        # Last token
        if current_token >= 0 and current_token < len(char_list):
            token_times.append({
                "char": char_list[current_token],
                "start": round(start_frame * frame_dur, 3),
                "end": round(len(aligned_seq) * frame_dur, 3),
            })
    else:
        # Fallback: aligned is list of tuples (token_id, start, end)
        for item in aligned:
            tok_id, s, e = item[0], item[1], item[2]
            if tok_id < len(char_list):
                token_times.append({
                    "char": char_list[tok_id],
                    "start": round(s * frame_dur, 3),
                    "end": round(e * frame_dur, 3),
                })

    # Group characters into words
    char_idx = 0
    for word in text_words:
        if char_idx >= len(token_times):
            break
        w_start = token_times[char_idx]["start"]
        w_end = token_times[min(char_idx + len(word) - 1, len(token_times) - 1)]["end"]
        words_out.append({"word": word, "start": w_start, "end": w_end})
        char_idx += len(word)

    # Build phoneme timeline for viseme mapping
    # Use characters as phoneme approximation (uppercase for ARPABET-like lookup)
    phoneme_timeline = [
        {"phoneme": t["char"].upper(), "start": t["start"], "end": t["end"]}
        for t in token_times
    ]

    # Generate viseme weights
    viseme_fps = 30
    viseme_frames = phonemes_to_viseme_weights(phoneme_timeline, duration, fps=viseme_fps)
    viseme_b64 = pack_viseme_b64(viseme_frames)

    log.info(f"MMS aligned {len(words_out)} words, {len(token_times)} chars, {len(viseme_frames)} viseme frames")
    return {
        "words": words_out,
        "phonemes": phoneme_timeline,
        "viseme_b64": viseme_b64,
        "viseme_fps": viseme_fps,
        "viseme_frames": len(viseme_frames),
    }


@app.websocket("/ws/stream")
async def stream_transcribe(ws: WebSocket):
    """
    Streaming transcription: client sends arbitrary PCM16 mono 16 kHz chunks.
    Server uses WebRTC VAD + endpointing (same as bridge /ws/live), then Whisper
    per utterance. Emits `{"text": "...", "final": true}` per completed utterance.
    """
    await ws.accept()
    log.info("STT WebSocket connected.")

    try:
        from bridge.audio_utils import pcm16_mono_to_wav
        from bridge.vad_segmenter import VadUtteranceSegmenter
    except ImportError as e:
        log.error("VAD segmenter unavailable: %s", e)
        await ws.close(code=1011)
        return

    seg = VadUtteranceSegmenter(
        sample_rate=16000,
        frame_ms=20,
        aggressiveness=int(config.get("vad_aggressiveness", 2)),
        silence_ms_interim=int(config.get("vad_silence_ms_interim", 350)),
        silence_ms_final=int(config.get("vad_silence_ms_final", 900)),
        min_speech_ms=int(config.get("vad_min_speech_ms", 200)),
        max_utterance_ms=int(config.get("vad_max_utterance_ms", 15000)),
        silero_threshold=float(config.get("silero_threshold", 0.4)),
    )

    async def _transcribe_pcm(pcm: bytes) -> str:
        wav = pcm16_mono_to_wav(pcm, sample_rate=16000)
        audio_data, sample_rate = sf.read(io.BytesIO(wav))
        if audio_data.ndim > 1:
            audio_data = audio_data.mean(axis=1)
        audio_data = audio_data.astype(np.float32)
        language = config.get("language")
        segments, info = model.transcribe(
            audio_data,
            language=language,
            beam_size=3,
            vad_filter=True,
            initial_prompt=config.get("initial_prompt"),
            hotwords=config.get("hotwords"),
        )
        return " ".join(s.text.strip() for s in segments)

    try:
        while True:
            data = await ws.receive_bytes()
            for tag, utt in seg.feed(data):
                text = await _transcribe_pcm(utt)
                if text.strip():
                    is_final = tag == "final"
                    await ws.send_json({"text": text.strip(), "final": is_final})

    except WebSocketDisconnect:
        log.info("STT WebSocket disconnected.")
    except Exception as e:
        log.error(f"STT stream error: {e}")
        await ws.close(code=1011)
    finally:
        for tag, utt in seg.flush():
            try:
                text = await _transcribe_pcm(utt)
                if text.strip():
                    await ws.send_json({"text": text.strip(), "final": True})
            except Exception:
                pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config["host"], port=config["port"])
