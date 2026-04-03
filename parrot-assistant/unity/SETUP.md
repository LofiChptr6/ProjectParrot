# Unity 3D Character Setup

## Overview

The 3D character runs as a Unity application on Windows, connecting to the
Parrot backend services (running in WSL2) via WebSocket.

```
[Unity on Windows] ←WebSocket→ [Bridge in WSL2:8000] ←→ [STT/LLM/TTS/Memory]
```

## Prerequisites

1. **Unity Hub** — https://unity.com/download
2. **Unity 2022.3 LTS** or newer (install via Unity Hub)
3. **UniVRM** — VRM importer for Unity: https://github.com/vrm-c/UniVRM/releases
   - Download the latest `.unitypackage` from releases
4. **A VRM model** — Get one from:
   - VRoid Hub: https://hub.vroid.com/
   - VRoid Studio (make your own): https://vroid.com/studio
   - Booth.pm (paid/free models)

## Unity Project Setup

### 1. Create Project

- Open Unity Hub → New Project → **3D (URP)** template
- Name: `ParrotCharacter`
- Save inside: `D:\Projects\` (or wherever, NOT in WSL filesystem)

### 2. Import UniVRM

- Assets → Import Package → Custom Package → select UniVRM `.unitypackage`
- Import all

### 3. Import Your VRM Model

- Drag `.vrm` file into `Assets/Models/`
- UniVRM auto-creates a prefab
- Drag prefab into scene

### 4. Install Dependencies (Unity Package Manager)

Add these packages via Window → Package Manager → "+" → Add by git URL:

```
https://github.com/vrm-c/UniVRM.git?path=/Assets/VRMShaders
https://github.com/vrm-c/UniVRM.git?path=/Assets/UniGLTF
https://github.com/vrm-c/UniVRM.git?path=/Assets/VRM
https://github.com/vrm-c/UniVRM.git?path=/Assets/VRM10
```

### 5. WebSocket Client (NativeWebSocket)

Add via Package Manager → git URL:
```
https://github.com/endel/NativeWebSocket.git#upm
```

### 6. Add the Bridge Script

Create `Assets/Scripts/ParrotBridge.cs` — this connects to the WSL2 backend:

```csharp
using UnityEngine;
using NativeWebSocket;
using System;
using System.Text;

public class ParrotBridge : MonoBehaviour
{
    [Header("Connection")]
    public string bridgeUrl = "ws://localhost:8000/ws/unity";

    [Header("References")]
    public AudioSource audioSource;
    public Animator characterAnimator;

    private WebSocket ws;

    async void Start()
    {
        ws = new WebSocket(bridgeUrl);

        ws.OnOpen += () => Debug.Log("Connected to Parrot Bridge");
        ws.OnError += (e) => Debug.LogError($"Bridge error: {e}");
        ws.OnClose += (e) => Debug.Log("Disconnected from Bridge");

        ws.OnMessage += (bytes) =>
        {
            string msg = Encoding.UTF8.GetString(bytes);
            var data = JsonUtility.FromJson<BridgeMessage>(msg);
            HandleMessage(data);
        };

        await ws.Connect();
    }

    void Update()
    {
#if !UNITY_WEBGL || UNITY_EDITOR
        ws?.DispatchMessageQueue();
#endif
    }

    void HandleMessage(BridgeMessage msg)
    {
        if (msg.type == "speech")
        {
            // Play audio
            if (!string.IsNullOrEmpty(msg.audio_base64))
            {
                byte[] audioBytes = Convert.FromBase64String(msg.audio_base64);
                PlayAudio(audioBytes);
            }

            // Trigger talking animation
            if (characterAnimator != null)
            {
                characterAnimator.SetBool("IsTalking", true);
            }

            Debug.Log($"Assistant: {msg.text}");
        }
    }

    void PlayAudio(byte[] wavBytes)
    {
        // WAV parsing: skip 44-byte header
        int headerSize = 44;
        int sampleCount = (wavBytes.Length - headerSize) / 2;
        float[] samples = new float[sampleCount];

        for (int i = 0; i < sampleCount; i++)
        {
            short s = BitConverter.ToInt16(wavBytes, headerSize + i * 2);
            samples[i] = s / 32768f;
        }

        AudioClip clip = AudioClip.Create("speech", sampleCount, 1, 24000, false);
        clip.SetData(samples, 0);
        audioSource.clip = clip;
        audioSource.Play();

        Invoke(nameof(StopTalking), clip.length);
    }

    void StopTalking()
    {
        if (characterAnimator != null)
            characterAnimator.SetBool("IsTalking", false);
    }

    public async void SendUserInput(string text)
    {
        if (ws.State == WebSocketState.Open)
        {
            string json = JsonUtility.ToJson(new UserInput { type = "user_input", text = text });
            await ws.SendText(json);
        }
    }

    async void OnDestroy()
    {
        if (ws != null) await ws.Close();
    }

    [Serializable]
    public class BridgeMessage
    {
        public string type;
        public string text;
        public string audio_base64;
    }

    [Serializable]
    public class UserInput
    {
        public string type;
        public string text;
    }
}
```

### 7. Animator Controller Setup (Emotions + Gestures)

The bridge sends **`emotion`** (string like "happy", "sad") and **`gesture`**
(string like "wave", "clap", or null) with every speech message.

**Create the controller:**

1. **Project → right-click → Create → Animator Controller** → name `ParrotController`
2. Assign it to the Animator on your VRM character

**Parameters** (add via the Animator "Parameters" tab):

| Name        | Type    | Purpose                           |
|-------------|---------|-----------------------------------|
| `IsTalking` | Bool    | True while audio is playing       |
| `Emotion`   | Integer | Maps to emotion states (see below)|

**Emotion integer mapping** (must match `ParrotBridge.cs`):

| Value | Emotion     |
|-------|-------------|
| 0     | neutral     |
| 1     | happy       |
| 2     | excited     |
| 3     | thinking    |
| 4     | sad         |
| 5     | surprised   |
| 6     | playful     |
| 7     | empathetic  |

**States:** Create states for each emotion in the Animator. Each plays a looping
animation clip. Add transitions from `Any State` → each emotion state, conditioned
on `Emotion == N`.

**Gesture triggers:** For each gesture in `character/gestures.yaml`, add a
**Trigger** parameter (e.g. `Wave`, `Clap`, `ChinTap`). Add one-shot states that
play the gesture clip, triggered from Any State. The trigger name must match the
gesture's `clip` value in `gestures.yaml`.

**Animation sources:**
- **Mixamo** (free): https://www.mixamo.com — download FBX motions for humanoid
- **Unity Asset Store**: search "idle", "gesture", "emote"
- **Hand-keyframe**: Unity Animation window for simple nods / tilts

### 7b. VRM Facial Expressions (BlendShapes)

VRM models include standard BlendShapePresets: Joy, Angry, Sorrow, Fun, Surprised, Neutral.
`ParrotBridge.cs` has commented-out code to drive these. Uncomment when UniVRM is imported.

### 7c. Lip Sync Options

**Option A: Amplitude-based (simple)**
- Read audio amplitude in real-time
- Map to blend shape weights on the VRM model
- Works out of the box, looks "okay"

**Option B: Viseme-based (better)**
- Use OVRLipSync (Oculus) or uLipSync:
  - uLipSync: https://github.com/hecomi/uLipSync
- Maps phonemes to mouth shapes
- Much more realistic

**Option C: Full face tracking (best)**
- Add ARKit-style blend shapes to VRM
- Drive from emotion cues sent by the bridge
- Requires VRM model with full blend shape set

### 8. Scene Setup

1. Add VRM character to scene
2. Add `AudioSource` component to character
3. Add `ParrotBridge` script to an empty GameObject
4. Assign **Animator Controller** (`ParrotController`) to the VRM's Animator
5. Wire up references (AudioSource, Animator) in ParrotBridge Inspector
6. Add a UI InputField for text input (or mic button for voice)
7. Set camera to face the character

## Network: WSL2 ↔ Windows

The bridge runs on WSL2 at `localhost:8000`. From Windows, WSL2's localhost
is accessible directly as `localhost` (Windows builds of WSL2 forward ports
automatically). If that doesn't work:

```powershell
# Find WSL2 IP
wsl hostname -I

# Use that IP in Unity instead of localhost
ws://172.x.x.x:8000/ws/unity
```

## Running

1. Start backend services in WSL2:
   ```bash
   cd ~/ProjectParrot/parrot-assistant
   ./start.sh all
   ```

2. Open Unity project on Windows, hit Play

3. The character connects via WebSocket and receives speech + animation data

## Input System: `InvalidOperationException` … `UnityEngine.Input`

If the Console says you cannot use `UnityEngine.Input` because the project uses the **new Input System** only, **Parrot Chat UI** no longer uses `Input.GetKeyDown` for Enter (it uses the input field’s **onSubmit**). Update **`ParrotChatUI.cs`** from this repo if you still see that error.

Alternatively: **Edit → Project Settings → Player → Other Settings → Active Input Handling** → **Both** or **Input Manager (Old)** (less ideal if you rely on the new system everywhere).

## Troubleshooting Unity ↔ bridge

**Swagger `/chat` works but Unity “does nothing”**

- The browser tests **HTTP** (`POST /chat`). Unity uses **WebSocket** (`/ws/unity`) — different protocol. Both must succeed independently.
- Connecting alone does **not** run the LLM. The bridge only responds after it receives JSON: `{"type":"user_input","text":"your message"}`. Wire a UI button / `InputField` to `SendUserInput`, or use **`debugSendOnConnect`** on `ParrotBridge` (see `ParrotBridge.cs` in this folder) to auto-send a test line when Play starts.
- Watch the **Console**: you should see `[ParrotBridge] Connected:` and then either an error or `OnMessage` logs. No `SendUserInput` → no reply.

**WebSocket won’t connect (`OnError` / never “Connected”)**

- Confirm WSL: `cd ~/ProjectParrot/parrot-assistant && ./start.sh all` and `curl -s http://127.0.0.1:8000/health`.
- Try **`ws://127.0.0.1:8000/ws/unity`** instead of `localhost` in Unity (same machine, sometimes fewer DNS quirks).
- If it still fails, use the WSL IP from Windows: `wsl hostname -I` → `ws://172.x.x.x:8000/ws/unity`.
- Windows Firewall: allow Python/uvicorn or port **8000** if prompted.

**Connected but no sound / no lip sync**

- Assign **AudioSource** on the character (or child). Without it, TTS audio is dropped.
- **Animator**: assign the VRM’s animator; add a **bool** parameter `IsTalking` in the Animator Controller if you want the talking state (or remove those lines if you don’t have that parameter yet).

**Use repo script**

- Copy **`unity/ParrotBridge.cs`** from this repo into `Assets/Scripts/` so you get logging + optional `debugSendOnConnect`.

## Chat box in Unity (manual typing)

1. **Hierarchy → right‑click → UI → Canvas** (accept EventSystem if prompted).
2. **Input field (pick one):**
   - **TextMesh Pro (Unity 6 default):** **UI → Input Field — TextMeshPro**. If Unity shows **TMP Importer**, click **Import TMP Essentials** first.
   - **Legacy:** **UI → Legacy → Input Field** — wire it to **Legacy Input Field** on Parrot Chat UI.
3. **Right‑click Canvas → UI → Button** (rename e.g. `SendButton`). Edit the button’s label text to `Send`.
4. Create an empty GameObject under Canvas, e.g. `ChatController`, and **Add Component → Parrot Chat UI** (`ParrotChatUI.cs` from this repo’s `unity/` folder).
5. **Parrot Chat UI** Inspector:
   - **Bridge** → drag the GameObject that has **Parrot Bridge** (e.g. `Mocha` or `Root`).
   - **Tmp Input Field** OR **Legacy Input Field** → assign **ChatInput** (only one needs to be set).
   - **Send Button** → drag **SendButton**.
6. Press **Play**, type a message, click **Send** — Console should show WebSocket traffic; if the bridge and Ollama are up, you should hear TTS (with Audio Source wired).

**Without Unity:** open `http://localhost:8000/docs` and use **POST /chat**, or:

```bash
curl -s -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"text":"Hello","include_audio":false}'
```

## GPU Allocation Strategy (Layout B — fastest)

Benchmarked Layout B: Blackwell handles LLM + TTS (50% faster than splitting).

| Service          | GPU              | Why                                    |
|------------------|------------------|----------------------------------------|
| Ollama (LLM)     | GPU 0 (Blackwell)| Needs the most VRAM for large models   |
| F5-TTS (TTS)     | GPU 0 (Blackwell)| Shares with LLM — Blackwell is 2.2x faster for TTS |
| Whisper (STT)    | GPU 1 (5070 Ti)  | Lightweight, doesn't need Blackwell    |
| Unity (3D)       | GPU 1 (5070 Ti)  | Rendering on secondary GPU              |

Set in `config.yaml`: `tts.gpu_id: 0`, `stt.gpu_id: 1`.

## Character Personality System

The `character/` folder defines the assistant's personality, emotions, and behaviors:

```
character/
├── soul.md           ← personality, voice, boundaries (designer writes this)
├── emotions.yaml     ← emotion list with VRM face + body hints
├── behaviors.yaml    ← situational rules (condition → emotion + gesture + speech guidance)
├── gestures.yaml     ← gesture library (maps ids to Unity animation clip names)
└── context.py        ← assembles everything into a structured LLM system prompt
```

The bridge reads these at startup and instructs the LLM to reply with structured JSON
containing one or more **segments** (one per sentence):

```json
{
  "segments": [
    {"text": "First sentence.", "emotion": "happy", "action": "wave hand cheerfully"},
    {"text": "Second sentence.", "emotion": "thinking", "action": "tap chin thoughtfully"}
  ]
}
```

The bridge resolves each segment's `action` to an animation clip name via the
animation vector DB, synthesises TTS per-segment, and sends `speech_segment`
WebSocket messages to Unity one-by-one.  Unity queues them and plays them back
sequentially — so the character's emotion and body language shift naturally
across a multi-sentence reply while TTS for later sentences is still being
generated on the server.

For short replies the LLM returns a single segment (most common).

### WebSocket message: `speech_segment`

```json
{
  "type": "speech_segment",
  "index": 0,
  "total": 2,
  "text": "First sentence.",
  "emotion": "happy",
  "gesture": "KA_Idle16_WaveHands",
  "audio_base64": "<base64 WAV>"
}
```

| Field | Description |
|-------|-------------|
| `index` | 0-based position of this segment in the full reply |
| `total` | Total segment count (-1 when streaming and count unknown yet) |
| `text` | The spoken sentence |
| `emotion` | Emotion id for this segment |
| `gesture` | Resolved animation clip name (from vector DB) |
| `audio_base64` | Base64-encoded WAV audio for this segment (may be null) |

The legacy single-shot `"type": "speech"` message is still accepted by
`ParrotBridge.cs` for backwards compatibility (treated as a single segment).

### FUTURE: LLM token streaming

Currently the bridge waits for the full LLM completion before processing
segments.  A planned improvement is to enable Ollama `stream: true` so the
bridge can detect sentence boundaries in the token stream and emit
`speech_segment` messages as each sentence completes.  This will reduce
first-segment latency significantly.  When streaming, `total` is sent as `-1`
on non-final segments and the actual count on the last one.  The Unity
`PlaySegmentQueue` coroutine already handles this — it waits briefly for new
segments when the queue empties before the final segment arrives.
