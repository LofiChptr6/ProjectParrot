# Gesture Model — Internal Parameterization Reference

> **Model:** EMAGE (Expressive Masked Audio GEsture, CVPR 2024)
> **Weights:** `H-Liu1997/emage_audio` on HuggingFace
> **Service:** `gesture/service.py` on port 8005

---

## 1. What are VQ Codes?

VQ = **Vector Quantization**. Think of it as a learned motion dictionary.

Training taught the model a **codebook of 256 motion "words"**, each a
256-dimensional vector representing a typical short motion pattern (e.g.,
"lean left while raising hand", "nod rhythmically", "shift weight").

At inference:
1. A motion encoder compresses the pose sequence into a continuous latent vector
2. The **quantizer snaps that vector to the nearest word in the dictionary** — like
   rounding 3.7 to 4
3. The decoder maps that dictionary entry back to actual joint rotations

```
Continuous pose  →  Encoder  →  latent (256-dim)
                                     │
                                     ▼  "nearest neighbour in codebook"
                                 VQ token (0–255)
                                     │
                                     ▼
                               Decoder  →  6D rotations  →  axis-angle
```

**How VQ affects axis-angle:** it doesn't directly. VQ is a compression step.
The final axis-angle values come from decoding the codebook vector — the
quantization step just means the motion can only take one of 256 "vocabulary"
shapes per body part per window, keeping it natural-looking and on-manifold
(no physically impossible poses).

**Face uses continuous latent, not VQ** (config: `lf=3, cf=0`). Face is more
subtle — snapping to discrete tokens would cause jarring expression jumps.
Upper/lower/hands all use VQ (`cu=1, cl=1, ch=1`).

---

## 2. How Are Quaternions Applied in Unity?

Each motion frame contains 13 quaternions — one per upper-body bone.
Unity applies them in `ParrotBridge.cs` inside the `PlayMotionData()` coroutine:

```csharp
// Per frame, per bone:
Transform boneTransform = animator.GetBoneTransform(MotionBoneOrder[b]);
boneTransform.localRotation = new Quaternion(qx, qy, qz, qw);
yield return new WaitForSeconds(1f / motionFps);
```

`localRotation` means the rotation is **relative to the bone's parent** in the
skeleton hierarchy — the same convention SMPL-X uses. This is what makes the
mapping clean: no need to reconstruct the full skeleton, just set each joint's
local offset rotation.

The coroutine yields between frames to stay at the configured fps (default 30).
When audio finishes, `StopMotionPlayback()` resets all bones to identity.

---

## 3. Face and Body — Parallel?

**Same forward pass, separate decoders.**

Both share the same audio input and run in a single `model.inference()` call,
but inside that call they branch into completely independent paths:

```
Audio (16 kHz)
    │
    ├─ audio_encoder_face ──────────────────────► Face cross-attn decoder
    │                                                   │ → cls_face → face latent
    │                                                   │ → jaw + expression
    └─ audio_encoder_body ──► Body cross-attn (8L) ──►─┤
                                                        ├─ motion2latent_upper → cls_upper → VQ
                                                        ├─ motion2latent_hands → cls_hands → VQ
                                                        └─ motion2latent_lower → cls_lower → VQ
```

So: **one GPU forward pass produces all parts simultaneously**. In Python they
execute sequentially on the GPU compute stream, but from our perspective it's
one call, one latency hit.

**We currently discard face output.** Only upper-body VQ path is used.
Hands and lower body are also discarded — they're generated but stripped before
sending to Unity.

---

## 4. Frame Rate — 64 vs 30

**64 is the window size, not the frame rate. Frame rate is 30fps.**

| Concept | Value | What it means |
|---------|-------|---------------|
| `pose_fps` | **30** | Frames per second in the output — speed of movement |
| `pose_length` | **64** | Frames per processing window — ~2.1s of motion at once |

The model processes audio in chunks of 64 frames (~2.1 seconds).
Output is always 30 poses per real second of audio.

**Configuring fps** — set in `config.yaml`:
```yaml
gesture:
  pose_fps: 30   # lower = smoother on slow connections, less bandwidth
                 # higher than 30 = no benefit (model is 30fps natively)
```

If you set `pose_fps: 15`, the service resamples 30fps output down to 15fps
before sending to Unity. Unity plays it back at 15fps — same motion,
half the data, slightly less smooth.

---

## 5. What Does "20-Frame Stride" Mean?

For audio longer than ~2.1s (64 frames), the model can't process it all at once.
It slides a 64-frame window across the full sequence with a **step of 20 frames**:

```
Audio:  |────────────────────────────────────────────|  (e.g. 180 frames = 6s)

Win 1:  |████████████████████|                          frames 0-63
Win 2:           |████████████████████|                 frames 20-83
Win 3:                    |████████████████████|        frames 40-103
Win 4:                             |████████████████████|  frames 60-123
...
```

The 44-frame overlap between windows means adjacent windows share context —
the motion at the seam of window 1 and window 2 benefits from both windows'
audio context. This prevents jarring motion cuts every 2.1 seconds.

Stride of 20 = each new window advances 0.67 seconds forward. Overlapping
outputs are averaged/stitched inside EMAGE's inference loop.

---

## 6. How to Start the Fine-tuning Data Collection App

The **gesture recorder** is the first step toward fine-tuning — it collects
paired audio + pose data by recording you speaking the prompts.

```bash
cd /home/tianyizhang/AI\ Projects/ProjectParrot
.venv-gesture/bin/python scripts/gesture_recorder.py
```

**Controls:**
| Key | Action |
|-----|--------|
| `SPACE` or `R` | Start / stop recording |
| `N` or `→` | Next prompt |
| `P` or `←` | Previous prompt |
| `Q` / `Esc` | Quit |

**Workflow:**
1. Read the prompt displayed on screen — speak naturally and expressively
2. Press SPACE to start recording, press SPACE again to stop
3. Skip boring prompts with N, re-do a take by just recording again
4. Aim for ~5-10 hours total (multiple sessions)

**Output** per take saved to `data/recordings/<session>/take_NNN/`:
```
landmarks.npy     — MediaPipe pose, (T, 33, 3) at capture fps
audio.wav         — 16 kHz mono WAV
transcript.json   — text + timing metadata
```

**After recording**, preprocess to normalize and align to 30fps:
```bash
.venv-gesture/bin/python scripts/gesture_preprocess.py
# output → data/processed/
```

**The full fine-tuning pipeline** (training script not yet built):
```
Record (gesture_recorder.py)
    → Preprocess (gesture_preprocess.py)
    → [TODO] SMPL-X fitting (MediaPipe landmarks → SMPL-X params)
    → [TODO] Fine-tune EMAGE on personal data
    → Drop new weights into gesture/service.py
```

The bottleneck is converting MediaPipe landmarks → SMPL-X format, which EMAGE
needs for training. Options: run 4D-Humans on the recorded video, or train only
the audio encoder layers (freeze VQ codebooks) which doesn't need SMPL-X at all.

---

## 7. End-to-end Data Flow

```
TTS audio (WAV, 16 kHz mono)
        │
        ▼
  WavEncoder ×2 (face + body)       1D CNN → 256-dim features at 30fps
        │
        ▼
  Transformer cross-attention        hidden_size=768, 8-layer decoder
        │
        ├─ cls_upper → argmax → token (0-255)
        │       └─ codebook lookup → 78-dim 6D rotations
        │               └─ rotation_6d_to_axis_angle → (T, 13, 3)
        ├─ cls_hands → [discarded]
        ├─ cls_lower → [discarded]
        └─ face latent → [discarded]
        │
        ▼
  _extract_upper_body()              (T, 165) → (T, 39)
        │
        ▼
  [optional resample]                if config pose_fps ≠ 30
        │
        ▼
  smplx_to_vrm_upper_body()
    axis-angle → quaternion (scipy)
    flip qz, qw (right→left handed)
        │
        ▼
  _pack_motion_b64()                 T×13×4 float32 LE → base64
        │
        ▼
  WebSocket speech_segment           motion_b64 + motion_fps + motion_frames
        │
        ▼
  Unity PlayMotionData() coroutine   localRotation per bone per frame @ fps
```

---

## 8. Body Part Decomposition

| Region | SMPL-X joints | VQ input dims | Output dims | Used? |
|--------|--------------|---------------|-------------|-------|
| Upper  | 3,6,9,12-21 (13 joints) | 78 (13×6D) | 39 (13×3 AA) | ✓ |
| Hands  | 25-54 (30 joints) | 180 (30×6D) | 90 (30×3 AA) | ✗ |
| Lower  | 0,1,2,4,5,7,8,10,11 (9 joints) | 54+7=61 | 27 (9×3 AA) | ✗ |
| Face   | jaw (joint 22) + expression | 106 (6+100) | — | ✗ |

All four are generated in every inference call. We only forward upper-body to Unity.

---

## 9. VQ Codebook Details

```
Codebook entries:   256 tokens
Embedding dim:      256
Quantizer lambda:   1.0  (commitment loss weight)
Sequence window:    64 frames
Stride:             20 frames
```

---

## 10. Coordinate Conversion

```
SMPL-X: right-handed, Y-up, Z-backward   (OpenGL)
Unity:  left-handed,  Y-up, Z-forward

Flip:   q_unity = (qx, qy, -qz, -qw)
```

Axis-angle → quaternion via `scipy.spatial.transform.Rotation.from_rotvec().as_quat()`
returns `(x, y, z, w)` order, which Unity's `new Quaternion(x, y, z, w)` accepts directly.

---

## 11. Wire Format

```
Encoding:   Base64
Binary:     T × 13 × 4 × 4 bytes  (float32 little-endian)
Per float:  [qx, qy, qz, qw]
```

**Bone order** (must match `MotionBoneOrder[]` in `ParrotBridge.cs`):

| Index | VRM Bone | SMPL-X Joint |
|-------|----------|-------------|
| 0 | Spine | 3 |
| 1 | Chest | 6 |
| 2 | UpperChest | 9 |
| 3 | Neck | 12 |
| 4 | LeftShoulder | 13 |
| 5 | RightShoulder | 14 |
| 6 | Head | 15 |
| 7 | LeftUpperArm | 16 |
| 8 | RightUpperArm | 17 |
| 9 | LeftLowerArm | 18 |
| 10 | RightLowerArm | 19 |
| 11 | LeftHand | 20 |
| 12 | RightHand | 21 |

---

## 12. Performance

| Metric | Value |
|--------|-------|
| VRAM | ~0.61 GB |
| Latency warm | ~40 ms |
| Latency cold (first call) | ~292 ms |
| Native output fps | 30 |
| Max useful output fps | 30 |

---

## 13. Lip Sync Pipeline

Three independent motion channels drive the VRM avatar:

| Channel | Source | Controls |
|---------|--------|----------|
| Body | EMAGE (audio → upper-body quaternions) | 13 bone `localRotation` |
| Mouth | WhisperX forced alignment → visemes | 5 VRM blend shapes |
| Face | Emotion classifier → expression weights | VRM expression presets |

**Phoneme extraction:** WhisperX runs forced alignment on the TTS audio + known
transcript text, producing per-phoneme start/end timestamps at ~10ms resolution.

**Phoneme → viseme mapping:** Each phoneme maps to one of 5 VRM viseme shapes:
`aa`, `ih`, `ou`, `ee`, `oh`. The mapping table is a static lookup — e.g.,
`AH` → `aa`, `IY` → `ee`, `UW` → `ou`. Between phonemes, weights lerp to zero
(mouth closes).

**Wire format:** `viseme_b64` — same pattern as `motion_b64`:
`T frames x 5 visemes x float32 little-endian`, base64-encoded. Each float is a
blend shape weight in [0, 1]. Unity applies via `SkinnedMeshRenderer.SetBlendShapeWeight()`.
Sent alongside `motion_b64` in the `speech_segment` WebSocket message.

---

## 14. Future: Emotion-Aware TTS (StyleTTS 2)

**Problem:** Current F5-TTS produces natural-sounding speech but has no emotion
control — prosody is always neutral regardless of Mocha's emotional state.

### Evaluated options

| TTS | Emotion control | Voice cloning | Status |
|-----|----------------|---------------|--------|
| F5-TTS (current) | None | Yes (reference audio) | In production |
| Kokoro | None | No | Rejected — no cloning, no emotion tokens |
| **StyleTTS 2** | Continuous style vector | Yes (reference audio) | **Recommended upgrade** |
| CosyVoice 2 | Instruction-based ("speak excitedly") | Yes (zero-shot) | Alternative |
| Azure TTS (cloud) | SSML emotion styles | Yes | Alternative — best native viseme support (returns blend shape weights directly) |

### StyleTTS 2 approach

- **Style vector:** ~128-dimensional continuous embedding that captures pitch contour,
  energy, speaking rate, and emotional tone in a single vector.
- **Voice cloning:** Feed a reference WAV to extract speaker identity, same as F5-TTS.
- **Emotion at inference:** Record 6-8 reference clips per emotion (happy, sad, angry,
  excited, calm, curious, etc.). At inference, select the reference clip matching
  Mocha's current emotion state → style vector inherits that emotion's prosody.
- **Voice-face coupling:** Train a linear mapper from StyleTTS 2 style vector (128d)
  to VRM expression blend weights (8d). This gives continuous voice-to-face synchronization
  — the face subtly mirrors the voice's emotional intensity without discrete emotion
  switching.

---

## 15. Key Files

| File | Purpose |
|------|---------|
| `gesture/service.py` | FastAPI service, inference, fps resampling |
| `gesture/retarget.py` | axis-angle → quaternion, coordinate flip |
| `config.yaml` → `gesture.pose_fps` | Output fps (default 30) |
| `vendor/PantoMatrix/models/emage_audio/modeling_emage_audio.py` | Model architecture |
| `vendor/PantoMatrix/models/emage_audio/processing_emage_audio.py` | Quantizer, encoders |
| `scripts/gesture_recorder.py` | Webcam data collection for fine-tuning |
| `scripts/gesture_preprocess.py` | Normalize + resample recordings |
| `scripts/recorder_prompts/english.txt` | Prompted phrases for recording |
