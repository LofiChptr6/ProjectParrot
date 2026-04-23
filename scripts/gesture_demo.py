"""Gesture Demo — Type a sentence, hear TTS, see AI-generated upper-body motion.

Usage:
    .venv-gesture/bin/python scripts/gesture_demo.py

Opens http://localhost:8888 in your browser.
"""

import asyncio
import io
import json
import struct
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
import librosa
import edge_tts
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from scipy.spatial.transform import Rotation
import uvicorn

# Add vendor PantoMatrix to path
VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "PantoMatrix"
sys.path.insert(0, str(VENDOR_DIR))

from models.emage_audio import (
    EmageAudioModel, EmageVQVAEConv, EmageVAEConv, EmageVQModel,
)

# Add project root to path for gesture.retarget import
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from gesture.retarget import smplx_to_vrm_upper_body

UPPER_JOINT_INDICES = [3, 6, 9, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]

# Emotion → VRM expression mapping (from character/emotions.yaml)
EMOTION_TO_VRM = {
    "neutral":   {"preset": "neutral",   "weight": 0.0},
    "happy":     {"preset": "happy",     "weight": 0.8},
    "excited":   {"preset": "happy",     "weight": 1.0},
    "thinking":  {"preset": "neutral",   "weight": 0.0},
    "sad":       {"preset": "sad",       "weight": 0.7},
    "surprised": {"preset": "surprised", "weight": 0.9},
    "playful":   {"preset": "happy",     "weight": 0.7},
    "empathetic": {"preset": "sad",      "weight": 0.3},
}

app = FastAPI()

# ── SMPL-X joint indices and names ───────────────────────────────────────────
SMPLX_JOINTS = {
    0: "pelvis", 1: "left_hip", 2: "right_hip", 3: "spine1",
    4: "left_knee", 5: "right_knee", 6: "spine2", 7: "left_ankle",
    8: "right_ankle", 9: "spine3", 10: "left_foot", 11: "right_foot",
    12: "neck", 13: "left_collar", 14: "right_collar", 15: "head",
    16: "left_shoulder", 17: "right_shoulder",
    18: "left_elbow", 19: "right_elbow",
    20: "left_wrist", 21: "right_wrist",
}

# Upper body kinematic chain for stick figure drawing
UPPER_CHAINS = [
    # spine
    [0, 3, 6, 9, 12, 15],
    # left arm
    [9, 13, 16, 18, 20],
    # right arm
    [9, 14, 17, 19, 21],
]

# Joint indices we care about for visualization (upper body + pelvis for root)
VIS_JOINTS = sorted(set(j for chain in UPPER_CHAINS for j in chain))

# ── SMPL-X forward kinematics (simplified) ──────────────────────────────────
# Parent joint for each SMPL-X joint
SMPLX_PARENT = {
    0: -1, 1: 0, 2: 0, 3: 0, 4: 1, 5: 2, 6: 3, 7: 4, 8: 5,
    9: 6, 10: 7, 11: 8, 12: 9, 13: 9, 14: 9, 15: 12,
    16: 13, 17: 14, 18: 16, 19: 17, 20: 18, 21: 19,
}

# Rest-pose bone offsets (approximate SMPL-X T-pose, Y-up)
REST_OFFSETS = {
    0: np.array([0.0, 0.0, 0.0]),
    3: np.array([0.0, 0.12, 0.0]),    # spine1
    6: np.array([0.0, 0.12, 0.0]),    # spine2
    9: np.array([0.0, 0.12, 0.0]),    # spine3
    12: np.array([0.0, 0.10, 0.0]),   # neck
    13: np.array([-0.06, 0.02, 0.0]), # left_collar
    14: np.array([0.06, 0.02, 0.0]),  # right_collar
    15: np.array([0.0, 0.08, 0.0]),   # head
    16: np.array([-0.08, 0.0, 0.0]),  # left_shoulder
    17: np.array([0.08, 0.0, 0.0]),   # right_shoulder
    18: np.array([-0.22, 0.0, 0.0]),  # left_elbow
    19: np.array([0.22, 0.0, 0.0]),   # right_elbow
    20: np.array([-0.22, 0.0, 0.0]),  # left_wrist
    21: np.array([0.22, 0.0, 0.0]),   # right_wrist
}


def forward_kinematics(axis_angles: np.ndarray) -> dict[int, np.ndarray]:
    """Compute world positions for upper-body joints from axis-angle rotations.

    Args:
        axis_angles: (55, 3) axis-angle rotations for all SMPL-X joints.

    Returns:
        Dict of joint_index → 3D position.
    """
    global_rot = {}
    global_pos = {}

    for j in VIS_JOINTS:
        parent = SMPLX_PARENT.get(j, -1)
        local_rot = Rotation.from_rotvec(axis_angles[j])

        if parent == -1 or parent not in global_rot:
            global_rot[j] = local_rot
            global_pos[j] = REST_OFFSETS.get(j, np.zeros(3))
        else:
            global_rot[j] = global_rot[parent] * local_rot
            offset = REST_OFFSETS.get(j, np.zeros(3))
            global_pos[j] = global_pos[parent] + global_rot[parent].apply(offset)

    return global_pos


# ── Model globals ────────────────────────────────────────────────────────────
_model = None
_motion_vq = None
_device = None


def load_models():
    global _model, _motion_vq, _device
    _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Loading EMAGE on {_device}...")

    face_vq = EmageVQVAEConv.from_pretrained("H-Liu1997/emage_audio", subfolder="emage_vq/face").to(_device)
    upper_vq = EmageVQVAEConv.from_pretrained("H-Liu1997/emage_audio", subfolder="emage_vq/upper").to(_device)
    lower_vq = EmageVQVAEConv.from_pretrained("H-Liu1997/emage_audio", subfolder="emage_vq/lower").to(_device)
    hands_vq = EmageVQVAEConv.from_pretrained("H-Liu1997/emage_audio", subfolder="emage_vq/hands").to(_device)
    global_ae = EmageVAEConv.from_pretrained("H-Liu1997/emage_audio", subfolder="emage_vq/global").to(_device)
    _motion_vq = EmageVQModel(
        face_model=face_vq, upper_model=upper_vq,
        lower_model=lower_vq, hands_model=hands_vq,
        global_model=global_ae,
    ).to(_device)
    _motion_vq.eval()

    _model = EmageAudioModel.from_pretrained("H-Liu1997/emage_audio").to(_device)
    _model.eval()
    print(f"EMAGE loaded. SR={_model.cfg.audio_sr}, FPS={_model.cfg.pose_fps}")


@torch.inference_mode()
def generate_motion(audio_np: np.ndarray) -> dict:
    """Run EMAGE inference. Returns motion, expression, and jaw data."""
    audio = torch.from_numpy(audio_np).to(_device).unsqueeze(0)
    speaker_id = torch.zeros(1, 1).long().to(_device)
    trans = torch.zeros(1, 1, 3).to(_device)

    latent_dict = _model.inference(audio, speaker_id, _motion_vq, masked_motion=None, mask=None)

    face_latent = latent_dict["rec_face"] if _model.cfg.lf > 0 and _model.cfg.cf == 0 else None
    upper_latent = latent_dict["rec_upper"] if _model.cfg.lu > 0 and _model.cfg.cu == 0 else None
    hands_latent = latent_dict["rec_hands"] if _model.cfg.lh > 0 and _model.cfg.ch == 0 else None
    lower_latent = latent_dict["rec_lower"] if _model.cfg.ll > 0 and _model.cfg.cl == 0 else None

    face_index = torch.max(F.log_softmax(latent_dict["cls_face"], dim=2), dim=2)[1] if _model.cfg.cf > 0 else None
    upper_index = torch.max(F.log_softmax(latent_dict["cls_upper"], dim=2), dim=2)[1] if _model.cfg.cu > 0 else None
    hands_index = torch.max(F.log_softmax(latent_dict["cls_hands"], dim=2), dim=2)[1] if _model.cfg.ch > 0 else None
    lower_index = torch.max(F.log_softmax(latent_dict["cls_lower"], dim=2), dim=2)[1] if _model.cfg.cl > 0 else None

    all_pred = _motion_vq.decode(
        face_latent=face_latent, upper_latent=upper_latent,
        lower_latent=lower_latent, hands_latent=hands_latent,
        face_index=face_index, upper_index=upper_index,
        lower_index=lower_index, hands_index=hands_index,
        get_global_motion=False, ref_trans=trans[:, 0],
    )

    motion = all_pred["motion_axis_angle"][0].cpu().numpy()  # (T, 165)
    motion = motion.reshape(-1, 55, 3)

    expression = all_pred["expression"][0].cpu().numpy()  # (T, 100)
    jaw_aa = motion[:, 22, :]  # (T, 3) — jaw joint axis-angle

    return {"motion": motion, "expression": expression, "jaw_aa": jaw_aa}


F5_TTS_URL = "http://127.0.0.1:8002"


async def tts_to_wav(text: str, voice: str = "en-US-AriaNeural") -> tuple[bytes, np.ndarray]:
    """Generate TTS audio. Tries local F5-TTS first, falls back to edge-tts."""
    # Try local F5-TTS (much faster)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{F5_TTS_URL}/synthesize", json={"text": text})
            if resp.status_code == 200:
                wav_bytes = resp.content
                audio_np, _ = librosa.load(io.BytesIO(wav_bytes), sr=16000)
                return wav_bytes, audio_np
    except Exception:
        pass  # Fall through to edge-tts

    # Fallback: edge-tts (online, slower)
    communicate = edge_tts.Communicate(text, voice)
    mp3_buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_buf.write(chunk["data"])

    mp3_buf.seek(0)
    audio_np, sr = librosa.load(mp3_buf, sr=16000)

    import soundfile as sf
    wav_buf = io.BytesIO()
    sf.write(wav_buf, audio_np, 16000, format="WAV", subtype="PCM_16")
    return wav_buf.getvalue(), audio_np


@app.on_event("startup")
async def startup():
    load_models()


@app.post("/generate")
async def api_generate(body: dict):
    text = body.get("text", "Hello, how are you doing today?")
    t0 = time.time()

    # 1. TTS
    wav_bytes, audio_np = await tts_to_wav(text)
    tts_ms = (time.time() - t0) * 1000

    # 2. EMAGE inference
    t1 = time.time()
    result = generate_motion(audio_np)
    emage_ms = (time.time() - t1) * 1000

    motion = result["motion"]      # (T, 55, 3)
    jaw_aa = result["jaw_aa"]      # (T, 3)
    T = motion.shape[0]

    # 3. Forward kinematics → 2D positions for stick figure
    frames = []
    for frame_idx in range(T):
        positions = forward_kinematics(motion[frame_idx])
        frame_data = {}
        for j, pos in positions.items():
            frame_data[str(j)] = [float(pos[0]), float(pos[1])]
        # Add jaw open amount for stick figure head animation
        jaw_open = min(1.0, float(np.linalg.norm(jaw_aa[frame_idx])) / 0.5)
        frame_data["jaw_open"] = round(jaw_open, 3)
        frames.append(frame_data)

    # 4. VRM bone quaternions (glTF coordinate system for three.js)
    upper_aa = motion[:, UPPER_JOINT_INDICES, :].reshape(T, -1)  # (T, 39)
    vrm_bones = smplx_to_vrm_upper_body(upper_aa, UPPER_JOINT_INDICES,
                                         coordinate_system="gltf")

    # 5. Face data per frame
    face_data = []
    for t_idx in range(T):
        jaw_open = min(1.0, float(np.linalg.norm(jaw_aa[t_idx])) / 0.5)
        face_data.append({"jawOpen": round(jaw_open, 3)})

    # 6. Encode audio as base64
    import base64
    audio_b64 = base64.b64encode(wav_bytes).decode()

    return JSONResponse({
        "frames": frames,
        "fps": int(_model.cfg.pose_fps),
        "chains": UPPER_CHAINS,
        "joint_names": {str(k): v for k, v in SMPLX_JOINTS.items() if k in VIS_JOINTS},
        "audio_b64": audio_b64,
        "tts_ms": round(tts_ms),
        "emage_ms": round(emage_ms),
        "num_frames": len(frames),
        "duration_s": round(len(frames) / _model.cfg.pose_fps, 2),
        "vrm_bones": vrm_bones,
        "face_data": face_data,
        "emotion_map": EMOTION_TO_VRM,
    })


@app.get("/")
async def index():
    return HTMLResponse(DEMO_HTML)


DEMO_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>EMAGE Gesture Demo</title>
<script type="importmap">
{
  "imports": {
    "three": "https://cdn.jsdelivr.net/npm/three@0.169.0/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.169.0/examples/jsm/"
  }
}
</script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #1a1a2e; color: #eee;
       display: flex; flex-direction: column; align-items: center; min-height: 100vh; padding: 20px; }
h1 { margin-bottom: 8px; font-size: 24px; color: #e94560; }
.subtitle { color: #888; margin-bottom: 20px; font-size: 14px; }
.input-row { display: flex; gap: 10px; margin-bottom: 14px; width: 100%; max-width: 700px; }
input[type=text] { flex: 1; padding: 12px 16px; border: 2px solid #333; border-radius: 8px;
                   background: #16213e; color: #eee; font-size: 16px; outline: none; }
input[type=text]:focus { border-color: #e94560; }
button { padding: 10px 20px; border: none; border-radius: 8px; background: #e94560;
         color: white; font-size: 15px; cursor: pointer; font-weight: 600; }
button:hover { background: #c73652; }
button:disabled { background: #555; cursor: wait; }
.btn-sm { padding: 6px 14px; font-size: 13px; background: #333; }
.btn-sm:hover { background: #444; }
.btn-sm.active { background: #e94560; }
.toolbar { display: flex; gap: 8px; margin-bottom: 10px; }

.canvas-wrap { position: relative; width: 600px; height: 650px; margin: 0 auto; }
#canvas2d { border: 2px solid #333; border-radius: 12px; background: #0f3460; }
#canvas3d { border: 2px solid #333; border-radius: 12px; display: none; position: absolute; top: 0; left: 0; }
#dropZone { position: absolute; top: 0; left: 0; width: 100%; height: 100%;
            display: flex; align-items: center; justify-content: center;
            border: 2px dashed transparent; border-radius: 12px;
            color: #ffffff33; font-size: 14px; pointer-events: none;
            transition: all 0.2s; z-index: 10; }
#dropZone.drag-over { pointer-events: all; border-color: #e94560;
                      background: rgba(233,69,96,0.12); color: #e94560; font-size: 16px; }

.stats { margin-top: 12px; color: #888; font-size: 13px; font-family: monospace; }
.status { margin-top: 8px; color: #e94560; font-size: 14px; min-height: 20px; }
</style>
</head>
<body>
<h1>EMAGE Gesture Demo</h1>
<p class="subtitle">Type a sentence — hear it spoken, see AI-generated upper-body gestures</p>

<div class="input-row">
    <input type="text" id="textInput"
           value="Hey there! Let me tell you something really exciting about this new project we're working on."
           placeholder="Type something to say...">
    <button id="goBtn" onclick="generate()">Generate</button>
</div>

<div class="toolbar">
    <button class="btn-sm active" id="btn2d" onclick="switchView('2d')">Stick Figure</button>
    <button class="btn-sm" id="btn3d" onclick="switchView('3d')">3D VRM</button>
    <button class="btn-sm" id="btnClear" onclick="window._clearVRM()" style="display:none">Clear Model</button>
    <select id="emotionSelect" style="padding:6px 10px;border-radius:8px;background:#333;color:#eee;border:1px solid #555;font-size:13px;">
        <option value="neutral">Neutral</option>
        <option value="happy" selected>Happy</option>
        <option value="excited">Excited</option>
        <option value="thinking">Thinking</option>
        <option value="sad">Sad</option>
        <option value="surprised">Surprised</option>
        <option value="playful">Playful</option>
        <option value="empathetic">Empathetic</option>
    </select>
</div>

<div class="canvas-wrap" id="canvasWrap">
    <canvas id="canvas2d" width="600" height="650"></canvas>
    <canvas id="canvas3d" width="600" height="650"></canvas>
    <div id="dropZone">Drop .vrm file here for 3D view</div>
</div>
<div class="status" id="status"></div>
<div class="stats" id="stats"></div>

<!-- ═══ Stick figure (2D canvas) ═══ -->
<script>
const canvas2d = document.getElementById('canvas2d');
const ctx = canvas2d.getContext('2d');
const statusEl = document.getElementById('status');
const statsEl = document.getElementById('stats');

let animData = null, animFrame = 0, animTimer = null, audioCtx = null;
let currentView = '2d';  // '2d' or '3d'

const JOINT_COLORS = {
    15:'#ff6b6b', 12:'#ffd93d', 9:'#6bcb77', 6:'#4d96ff', 3:'#845ec2', 0:'#845ec2',
    13:'#ff9671', 14:'#ff9671', 16:'#ff6f91', 17:'#ff6f91',
    18:'#ffc75f', 19:'#ffc75f', 20:'#f9f871', 21:'#f9f871',
};

function drawFrame(frameData, chains) {
    ctx.clearRect(0, 0, canvas2d.width, canvas2d.height);
    const cx = canvas2d.width / 2;
    const cy = canvas2d.height * 0.92;
    const scale = 800;
    const toScreen = pos => [cx + pos[0] * scale, cy - pos[1] * scale];

    // Bones (shadow + line)
    for (const chain of chains) {
        for (const [w, c] of [[6,'#4d96ff44'],[3,'#4d96ffcc']]) {
            ctx.beginPath(); ctx.strokeStyle = c; ctx.lineWidth = w;
            ctx.lineCap = 'round'; ctx.lineJoin = 'round';
            let s = false;
            for (const j of chain) {
                const pos = frameData[String(j)]; if (!pos || !Array.isArray(pos)) continue;
                const [sx,sy] = toScreen(pos);
                if (!s) { ctx.moveTo(sx,sy); s=true; } else ctx.lineTo(sx,sy);
            }
            ctx.stroke();
        }
    }

    // Joints
    for (const [jStr, pos] of Object.entries(frameData)) {
        const j = parseInt(jStr); if (isNaN(j) || !Array.isArray(pos)) continue;
        const [sx,sy] = toScreen(pos);
        const color = JOINT_COLORS[j] || '#ffffff';
        const r = j===15 ? 18 : (j<=9 ? 6 : 8);

        ctx.beginPath(); ctx.arc(sx,sy,r+4,0,Math.PI*2); ctx.fillStyle=color+'33'; ctx.fill();
        ctx.beginPath(); ctx.arc(sx,sy,r,0,Math.PI*2); ctx.fillStyle=color; ctx.fill();

        // Head circle + jaw
        if (j === 15) {
            ctx.beginPath(); ctx.arc(sx,sy,18,0,Math.PI*2);
            ctx.strokeStyle=color; ctx.lineWidth=3; ctx.stroke();
            // Jaw indicator
            const jawOpen = frameData.jaw_open || 0;
            if (jawOpen > 0.05) {
                const drop = jawOpen * 10;
                ctx.beginPath(); ctx.arc(sx, sy + drop, 10, 0.15*Math.PI, 0.85*Math.PI);
                ctx.strokeStyle = '#ff6b6b88'; ctx.lineWidth = 2; ctx.stroke();
            }
        }
    }

    if (animData) {
        ctx.fillStyle = '#ffffff44'; ctx.font = '12px monospace';
        ctx.fillText('frame ' + (animFrame+1) + '/' + animData.frames.length, 10, canvas2d.height-10);
    }
}

function stopAnimation() { if (animTimer) { clearInterval(animTimer); animTimer = null; } }

function playAnimation() {
    if (!animData) return;
    if (animFrame >= animData.frames.length) {
        stopAnimation(); statusEl.textContent = 'Done!'; return;
    }
    // Stick figure
    if (currentView === '2d') drawFrame(animData.frames[animFrame], animData.chains);
    // VRM (handled in module script)
    if (currentView === '3d' && window._applyVRMFrame) {
        window._applyVRMFrame(
            animData.vrm_bones ? animData.vrm_bones[animFrame] : null,
            animData.face_data ? animData.face_data[animFrame] : null
        );
    }
    animFrame++;
}

function playAudio(b64) {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    audioCtx.decodeAudioData(bytes.buffer.slice(0), buf => {
        const src = audioCtx.createBufferSource();
        src.buffer = buf;
        // Connect through audio analyser for lip sync (if VRM loaded)
        if (window._setupAudioAnalyser) {
            window._setupAudioAnalyser(audioCtx, src);
        } else {
            src.connect(audioCtx.destination);
        }
        src.onended = () => { if (window._clearAudioAnalyser) window._clearAudioAnalyser(); };
        src.start(0);
    }, err => { console.error('Audio decode failed:', err); });
}

async function generate() {
    const text = document.getElementById('textInput').value.trim();
    if (!text) return;
    const btn = document.getElementById('goBtn');
    btn.disabled = true;
    statusEl.textContent = 'Generating TTS + gesture...';
    statsEl.textContent = '';
    stopAnimation();

    try {
        const resp = await fetch('/generate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text}),
        });
        const data = await resp.json();
        animData = data; animFrame = 0;
        statsEl.textContent = 'TTS: '+data.tts_ms+'ms | EMAGE: '+data.emage_ms+'ms | '+data.num_frames+' frames @ '+data.fps+'fps = '+data.duration_s+'s';
        statusEl.textContent = 'Playing...';
        // Set emotion expression from dropdown
        if (window._setEmotion && data.emotion_map) {
            const emo = document.getElementById('emotionSelect').value;
            window._setEmotion(emo, data.emotion_map);
        }
        const intervalMs = 1000 / data.fps;
        animTimer = setInterval(playAnimation, intervalMs);
        playAudio(data.audio_b64);
    } catch (e) {
        statusEl.textContent = 'Error: ' + e.message;
    } finally { btn.disabled = false; }
}

function switchView(mode) {
    currentView = mode;
    document.getElementById('btn2d').classList.toggle('active', mode==='2d');
    document.getElementById('btn3d').classList.toggle('active', mode==='3d');
    canvas2d.style.display = mode==='2d' ? 'block' : 'none';
    document.getElementById('canvas3d').style.display = mode==='3d' ? 'block' : 'none';
}

// Idle stick figure
drawFrame({
    "0":[0,0],"3":[0,0.12],"6":[0,0.24],"9":[0,0.36],
    "12":[0,0.46],"13":[-0.06,0.38],"14":[0.06,0.38],
    "15":[0,0.54],"16":[-0.14,0.38],"17":[0.14,0.38],
    "18":[-0.36,0.38],"19":[0.36,0.38],
    "20":[-0.58,0.38],"21":[0.58,0.38], "jaw_open":0
}, [[0,3,6,9,12,15],[9,13,16,18,20],[9,14,17,19,21]]);

document.getElementById('textInput').addEventListener('keydown', e => { if (e.key==='Enter') generate(); });
</script>

<!-- ═══ three.js VRM renderer (ES module) ═══ -->
<script type="module">
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// three-vrm loaded dynamically (no importmap entry needed — use CDN directly)
const VRM_CDN = 'https://cdn.jsdelivr.net/npm/@pixiv/three-vrm@3.3.3/lib/three-vrm.module.min.js';
let VRMModule = null;

async function loadVRMLib() {
    if (!VRMModule) VRMModule = await import(VRM_CDN);
    return VRMModule;
}

const BONE_MAP = {
    'Spine':'spine','Chest':'chest','UpperChest':'upperChest',
    'Neck':'neck','Head':'head',
    'LeftShoulder':'leftShoulder','RightShoulder':'rightShoulder',
    'LeftUpperArm':'leftUpperArm','RightUpperArm':'rightUpperArm',
    'LeftLowerArm':'leftLowerArm','RightLowerArm':'rightLowerArm',
    'LeftHand':'leftHand','RightHand':'rightHand',
};

let scene, camera, renderer, controls, vrm = null, renderActive = false;

function initThree() {
    const c3d = document.getElementById('canvas3d');
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0f3460);

    camera = new THREE.PerspectiveCamera(25, 600/650, 0.1, 50);
    camera.position.set(0, 1.25, 3.0);

    renderer = new THREE.WebGLRenderer({ canvas: c3d, antialias: true });
    renderer.setSize(600, 650);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;

    // Lighting
    scene.add(new THREE.AmbientLight(0xffffff, 1.0));
    const dir = new THREE.DirectionalLight(0xffffff, 1.0);
    dir.position.set(2, 3, 4);
    scene.add(dir);
    const back = new THREE.DirectionalLight(0x8888ff, 0.3);
    back.position.set(-2, 1, -3);
    scene.add(back);

    // Orbit controls
    controls = new OrbitControls(camera, c3d);
    controls.target.set(0, 1.0, 0);
    controls.enableDamping = true;
    controls.dampingFactor = 0.1;
    controls.update();
}

function startRenderLoop() {
    if (renderActive) return;
    renderActive = true;
    function loop() {
        if (!renderActive) return;
        requestAnimationFrame(loop);
        controls.update();
        if (vrm) vrm.update(0);
        renderer.render(scene, camera);
    }
    loop();
}

async function loadVRMFile(file) {
    const statusEl = document.getElementById('status');
    statusEl.textContent = 'Loading VRM model...';

    if (!scene) initThree();

    const { VRMLoaderPlugin, VRMUtils } = await loadVRMLib();
    const url = URL.createObjectURL(file);
    const loader = new GLTFLoader();
    loader.register(parser => new VRMLoaderPlugin(parser));

    try {
        const gltf = await loader.loadAsync(url);
        URL.revokeObjectURL(url);

        if (vrm) scene.remove(vrm.scene);
        vrm = gltf.userData.vrm;
        VRMUtils.removeUnnecessaryVertices(vrm.scene);
        VRMUtils.removeUnnecessaryJoints(vrm.scene);
        scene.add(vrm.scene);

        // Log available expressions for debugging
        if (vrm.expressionManager) {
            const names = vrm.expressionManager.expressions.map(e => e.expressionName);
            console.log('VRM expressions available:', names);
        }

        // Auto-switch to 3D view
        window.switchView('3d');
        startRenderLoop();
        document.getElementById('dropZone').style.display = 'none';
        document.getElementById('btnClear').style.display = '';
        statusEl.textContent = 'VRM loaded! Click Generate to see animation.';
    } catch (e) {
        statusEl.textContent = 'VRM load failed: ' + e.message;
        console.error(e);
    }
}

// ── Audio-driven lip sync via Web Audio API ──
let analyser = null, analyserData = null, audioSourceNode = null;
const VISEME_NAMES = ['aa', 'ih', 'ou', 'ee', 'oh'];
// Frequency band ranges (bin indices for 24kHz/2048 FFT → ~11.7Hz per bin)
// Low (0-300Hz) → aa, Mid-low (300-800Hz) → ou, Mid (800-2kHz) → ee,
// Mid-high (2k-4kHz) → ih, Silence threshold
const BAND_RANGES = [[0,25],[25,68],[68,170],[170,340]];

function getVisemeWeightsFromAudio() {
    if (!analyser || !analyserData) return null;
    analyser.getByteFrequencyData(analyserData);
    const weights = [0,0,0,0,0];
    let total = 0;
    for (let b = 0; b < BAND_RANGES.length; b++) {
        const [lo, hi] = BAND_RANGES[b];
        let sum = 0, count = 0;
        for (let i = lo; i < Math.min(hi, analyserData.length); i++) {
            sum += analyserData[i]; count++;
        }
        const avg = count > 0 ? sum / count / 255.0 : 0;
        weights[b] = avg;
        total += avg;
    }
    // Normalize: strongest band gets weight 1.0, scale others relative
    const maxW = Math.max(...weights, 0.01);
    for (let i = 0; i < 4; i++) weights[i] = Math.pow(weights[i] / maxW, 1.5);
    // Gate: if total energy is low, close mouth
    const gate = Math.min(1.0, total / 0.8);
    for (let i = 0; i < 5; i++) weights[i] *= gate;
    return weights;
}

// ── Emotion expression ──
let currentEmotionPreset = null, currentEmotionWeight = 0;
let targetEmotionPreset = null, targetEmotionWeight = 0;

window._setEmotion = function(emotionId, emotionMap) {
    if (!emotionMap || !emotionMap[emotionId]) return;
    targetEmotionPreset = emotionMap[emotionId].preset;
    targetEmotionWeight = emotionMap[emotionId].weight;
};

function applyEmotionToVRM() {
    if (!vrm || !vrm.expressionManager) return;
    // Clear previous emotion
    if (currentEmotionPreset && currentEmotionPreset !== targetEmotionPreset) {
        try { vrm.expressionManager.setValue(currentEmotionPreset, 0); } catch(e) {}
    }
    // Apply target emotion
    if (targetEmotionPreset) {
        try { vrm.expressionManager.setValue(targetEmotionPreset, targetEmotionWeight); } catch(e) {}
        currentEmotionPreset = targetEmotionPreset;
        currentEmotionWeight = targetEmotionWeight;
    }
}

// ── Apply body + lip sync + emotion per frame ──
window._applyVRMFrame = function(boneFrame, faceFrame) {
    if (!vrm) return;
    // Body bones
    if (boneFrame) {
        for (const [pascalName, q] of Object.entries(boneFrame)) {
            const vrmName = BONE_MAP[pascalName];
            if (!vrmName) continue;
            const bone = vrm.humanoid.getNormalizedBoneNode(vrmName);
            if (bone) bone.quaternion.set(q[0], q[1], q[2], q[3]);
        }
    }
    // Lip sync from audio analyser (overrides EMAGE jaw data)
    if (vrm.expressionManager) {
        const visWeights = getVisemeWeightsFromAudio();
        if (visWeights) {
            for (let i = 0; i < VISEME_NAMES.length; i++) {
                try { vrm.expressionManager.setValue(VISEME_NAMES[i], visWeights[i]); } catch(e) {}
            }
        }
        // Emotion expression (applied on top of visemes)
        applyEmotionToVRM();
    }
};

// ── Setup audio analyser on playback ──
window._setupAudioAnalyser = function(audioCtx, sourceNode) {
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    analyser.smoothingTimeConstant = 0.6;
    analyserData = new Uint8Array(analyser.frequencyBinCount);
    sourceNode.connect(analyser);
    analyser.connect(audioCtx.destination);
    return analyser;
};

window._clearAudioAnalyser = function() {
    analyser = null; analyserData = null;
};

// Clear VRM model
window._clearVRM = function() {
    if (vrm) { scene.remove(vrm.scene); vrm = null; }
    document.getElementById('dropZone').style.display = '';
    document.getElementById('btnClear').style.display = 'none';
    window.switchView('2d');
    document.getElementById('status').textContent = '';
};

// ── Drag and drop ──
const wrap = document.getElementById('canvasWrap');
const dropZone = document.getElementById('dropZone');

wrap.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
wrap.addEventListener('dragleave', e => {
    if (!wrap.contains(e.relatedTarget)) dropZone.classList.remove('drag-over');
});
wrap.addEventListener('drop', async e => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (!file || !file.name.toLowerCase().endsWith('.vrm')) {
        document.getElementById('status').textContent = 'Please drop a .vrm file';
        return;
    }
    await loadVRMFile(file);
});

// Init three.js eagerly so the canvas is ready
initThree();
startRenderLoop();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("Starting Gesture Demo at http://localhost:8888")
    uvicorn.run(
        "gesture_demo:app",
        host="0.0.0.0",
        port=8888,
        reload=True,
        app_dir=str(Path(__file__).resolve().parent),
    )
