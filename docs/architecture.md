# ProjectParrot — System Architecture

## 1. High-Level Overview

```
                                    EXTERNAL CLIENTS
    +------------------+   +------------------+   +------------------+
    |   Web Browser    |   |   Telegram Bot   |   |   Discord Bot    |
    |  (HTML/JS/VRM)   |   |                  |   |                  |
    +--------+---------+   +--------+---------+   +--------+---------+
             |                      |                      |
        WS /ws/live            POST /channel          POST /channel
        SSE /chat/stream       (JSON req/resp)        (JSON req/resp)
        WS /ws/monitor              |                      |
             |                      |                      |
    =========|======================|======================|===========
             |                      |                      |
             +----------+-----------+----------+-----------+
                        |
              +---------v----------+
              |                    |
              |   BRIDGE SERVER    |    Central orchestrator
              |   :8000 (FastAPI)  |    HTTP + WebSocket + SSE
              |                    |
              +-+--+--+--+--+--+--+
                |  |  |  |  |  |
    +-----------+  |  |  |  |  +------------------+
    |              |  |  |  |                     |
    v              v  |  v  v                     v
+-------+  +-------+ | +-------+ +--------+ +----------+
|  STT  |  |  TTS  | | |Memory | |Animate | |PostgreSQL|
|:8001  |  |:8002  | | |:8003  | |:8004   | |:5432     |
|Whisper|  |F5-TTS | | |Chroma | |VecDB   | |Call Log  |
+-------+  +-------+ | +-------+ +--------+ +----------+
                      |
                      v
                 +--------+
                 |  vLLM  |
                 | :8800  |
                 | Qwen3  |
                 +--------+
```

---

## 2. Service Map

```
 Port   Service            Protocol   Role
 ----   -------            --------   ----
 8000   Bridge             HTTP/WS    Central hub — routes everything
 8001   STT (Whisper)      HTTP/WS    Speech-to-text + forced alignment
 8002   TTS (F5-TTS)       HTTP       Text-to-speech (zero-shot clone)
 8003   Memory (ChromaDB)  HTTP       Semantic memory store/query
 8004   Animation VecDB    HTTP       Action text -> clip name lookup
 8005   Gesture (EMAGE)    HTTP       Audio -> upper-body bone quaternions
 8800   vLLM               HTTP       LLM inference (OpenAI-compatible)
 5432   PostgreSQL         TCP        Write-only call log analytics
```

---

## 3. Bridge Endpoints

```
 HTTP ENDPOINTS
 +-----------+---------------------------+----------------------------------------+
 | Method    | Path                      | Purpose                                |
 +-----------+---------------------------+----------------------------------------+
 | GET       | /health                   | Health check + uptime                  |
 | GET       | /chat/stream?text=&id=    | SSE streaming response (web frontend)  |
 | GET       | /api/conversation?limit=  | Conversation history JSON              |
 | GET       | /api/default-model        | Default VRM model file                 |
 | GET       | /api/animation-functions  | List available animation functions      |
 | POST      | /channel                  | Text pipeline (Telegram/Discord/CLI)   |
 | POST      | /voice                    | Audio in -> transcribe -> LLM -> TTS   |
 | POST      | /api/tts                  | Synthesize speech from text             |
 | POST      | /admin/reload-tools       | Hot-reload custom tools                |
 | POST      | /admin/clear-memory       | Wipe ChromaDB memories                 |
 | POST      | /admin/shiro/toggle       | Enable/disable Shiro agent             |
 | POST      | /admin/tts/restart        | Restart TTS service                    |
 | POST      | /admin/tts/upload-voice   | Upload new voice reference WAV         |
 +-----------+---------------------------+----------------------------------------+

 WEBSOCKET ENDPOINTS
 +------------------+--------------------------------------------+
 | Path             | Purpose                                    |
 +------------------+--------------------------------------------+
 | /ws/live         | Web/phone client (streaming PCM16 + JSON)  |
 | /ws/unity        | Unity client (push-to-talk + JSON)         |
 | /ws/voice-stream | Audio-only streaming transcription         |
 | /ws/monitor      | Dashboard (pipeline metrics + config)      |
 +------------------+--------------------------------------------+
```

---

## 4. Mocha's Execution Flow (Web Frontend)

```
  User types or speaks
         |
         v
  +------+-------+
  | /ws/live      |  Binary PCM16 (mic) or JSON {"type":"user_input","text":"..."}
  | WebSocket     |
  +------+--------+
         |
         v
  +------+--------+     +----------+
  | VAD (Silero)  +---->| STT :8001|  POST /transcribe
  | 900ms silence |     | Whisper  |  -> {"text": "show me stocks today"}
  +------+--------+     +----+-----+
         |                    |
         v                    v
  +------+--------------------+------+
  | Query Memories                   |  POST memory:8003/query
  | Assemble system prompt           |  character/context.py
  | Build message array              |
  +------+---------------------------+
         |
         v
  +------+-----------+
  | COMPLEXITY ROUTER |
  | (Two-Pass Mode)   |
  +---+----------+----+
      |          |
  ----+----  ----+----
  | PASS 1 |  | PASS 2 |    (only if PASS1 escalates via needs_context)
  |--------|  |--------|
  | 8 msgs |  | 20 msgs|
  | Tools? |  | Tools  |
  | No think| | Think  |
  +---+----+  +---+----+
      |           |
      +-----+-----+
            |
            v
    +-------+--------+
    | vLLM :8800     |  POST /v1/chat/completions (streaming)
    | Qwen3-32B      |
    +--+--------+----+
       |        |
   ----+----  --+--------+
   |SEGMENTS|  |TOOL CALLS|   (ReAct loop, max 5 rounds)
   +---+----+  +--+-------+
       |          |
       |    +-----v------+
       |    | Tool Loop  |  tools/executor.py
       |    |            |  execute_tool() -> results -> LLM again
       |    +-----+------+
       |          |
       |          v
       |    +-----+----------+
       |    | UI Tools?      |  create_presentation, show_card, etc.
       |    | _broadcast_to_ |  -> {"type":"ui_command",...}
       |    | unity()        |  -> sent to all /ws/live clients
       |    +----------------+
       |
       v
  +----+-----------+
  | For each segment|
  +----+-----------+
       |
       v
  +----+------+     +-----------+     +----------+
  | TTS :8002 +---->| Align     +---->| Animate  |
  | F5-TTS    |     | STT :8001 |     | :8004    |
  | text->WAV |     | /align    |     | action-> |
  +----+------+     | ->visemes |     | clip name|
       |            +-----+-----+     +----+-----+
       |                  |                 |
       +--------+---------+---------+-------+
                |
                v
  +-------------+--------------+
  | speech_segment             |
  | {                          |
  |   type: "speech_segment",  |
  |   text, emotion, gesture,  |
  |   audio_base64,            |
  |   viseme_b64, viseme_fps   |
  | }                          |
  +-------------+--------------+
                |
                v
  +-------------+--------------+
  | /ws/live -> Browser        |
  | app.js -> segmentQueue     |
  | -> playback with lip-sync  |
  | -> VRM emotion expression  |
  | -> animation clip playback |
  +----------------------------+
```

---

## 5. Complexity Routing (Two-Pass)

```
                   User Input
                       |
                       v
            +----------+----------+
            | complexity_routing   |
            | enabled?             |
            +----+----------+-----+
                 |          |
              YES|          |NO
                 v          v
          +------+----+ +--+----------+
          | PASS 1    | | Single Pass |
          |           | | full history|
          | 8 msgs    | | + tools     |
          | tools?    | | + thinking  |
          | (config)  | +------+------+
          | no think  |        |
          | max 512tok|        |
          +-----+-----+       |
                |              |
       +--------+--------+    |
       |                  |    |
   direct answer    needs_context    |
   (Option A)       (Option C)      |
       |                  |    |
       v                  v    |
   +---+---+       +------+---+----+
   | Done  |       | PASS 2        |
   | yield |       | 20 msgs       |
   | segs  |       | + tools       |
   +-------+       | + thinking    |
                    | full reasoning|
                    +-------+------+
                            |
                            v
                         segments
                         (+ tool calls handled)

  pass1_tools config:
  +-----------+-----------------------------------------------+
  | true      | Tools in PASS1 -> LLM can call them directly  |
  |           | Only escalates if needs more history (rare)    |
  +-----------+-----------------------------------------------+
  | false     | No tools in PASS1 -> must escalate for any    |
  |           | tool usage (news, stocks, weather, etc.)       |
  +-----------+-----------------------------------------------+
```

---

## 6. Tool Call Flow (ReAct Loop)

```
  LLM Response
       |
       v
  +----+-------+
  | Tool calls |    finish_reason == "tool_calls"
  | detected?  |
  +--+---------+
     |YES   |NO
     |       +----> yield segments -> Done
     v
  +--+------------+
  | Round N       |   (max 5 rounds)
  | (1-based)     |
  +--+------------+
     |
     v
  +--+-----------+     +-----------+
  | For each     |     | execute_  |
  | tool_call:   +---->| tool()    |
  | name + args  |     | 30s timeout|
  +--+-----------+     +-----+-----+
     |                       |
     |    +------------------+
     |    |
     v    v
  +--+----+----------+
  | Append results   |   role: "tool", content: result (max 4000 bytes)
  | to messages      |
  +--+---------------+
     |
     v
  +--+-----------+
  | Re-query LLM |   non-streaming, no thinking
  | with results  |
  +--+-----------+
     |
     v
  +--+-----------+
  | More tool    |   YES -> loop (round N+1)
  | calls?       |   NO  -> extract final segments -> Done
  +--+-----------+

  UI Tools (create_presentation, show_card, etc.)
  also broadcast via _broadcast_to_unity() during execution:

  execute_tool("create_presentation", {...})
       |
       +---> return "Created presentation..." (to LLM)
       |
       +---> _broadcast_to_unity({"type":"ui_command",...}) (to browser)
```

---

## 7. Web Frontend Architecture

```
  index.html
  +---------------------------------------------------------------------+
  |                                                                     |
  |  +--canvas-area--(position:relative)--+  +--chat-sidebar--+        |
  |  |                                    |  |                 |        |
  |  |  +--canvas3d--+                    |  |  #chatLog       |        |
  |  |  | Three.js   |                    |  |  (scrollable)   |        |
  |  |  | VRM model  |                    |  |                 |        |
  |  |  | (Mocha)    |                    |  |                 |        |
  |  |  +------------+                    |  |                 |        |
  |  |                                    |  |                 |        |
  |  |  +--presentationPanel--+           |  |                 |        |
  |  |  | (overlay, bottom-L) |           |  |                 |        |
  |  |  | slides / charts /   |           |  |                 |        |
  |  |  | tables / images     |           |  |                 |        |
  |  |  +---------------------+           |  |  +-----------+  |        |
  |  |                                    |  |  | text input|  |        |
  |  |  +--cardContainer--+  (top-R of    |  |  | send  mic |  |        |
  |  |  | info cards      |   canvas)     |  |  +-----------+  |        |
  |  |  | stacked         |              |  |                 |        |
  |  |  +-----------------+              |  +-----------------+        |
  |  +------------------------------------+                             |
  |                                                                     |
  |  +--notifContainer--(fixed, top-right)--+                           |
  |  | toast notifications                  |                           |
  |  +--------------------------------------+                           |
  |                                                                     |
  |  +--gchatPopup--(draggable, bottom-left)--+                         |
  |  | Global Chat (cross-platform messages)  |                         |
  |  +-----------------------------------------+                        |
  |                                                                     |
  |  +--status-bar--(top-right)--+                                      |
  |  | MIC  STT  LLM  TTS  GEST |                                      |
  |  +---------------------------+                                      |
  +---------------------------------------------------------------------+

  JavaScript Modules:
  +-------------------+   +--------------------+   +---------------------+
  | app.js            |   | vrm-renderer.js    |   | animation-          |
  | - WebSocket mgmt  |   | - Three.js scene   |   |   controller.js     |
  | - segment queue   |   | - VRM loader       |   | - FBX->VRM retarget |
  | - audio playback  |   | - applyBones()     |   | - clip playback     |
  | - chat log        |   | - applyVisemes()   |   | - idle state machine|
  | - global chat SSE |   | - applyEmotion()   |   | - gesture queue     |
  | - settings modal  |   | - skeleton debug   |   | - fidget timer      |
  +-------------------+   +--------------------+   +---------------------+
  +-------------------+
  | presentation.js   |
  | - slide renderer  |
  | - Chart.js charts |
  | - card manager    |
  | - toast notifs    |
  | - keyboard nav    |
  +-------------------+
```

---

## 8. WebSocket Message Flow

```
  /ws/live  (Browser <-> Bridge)
  ==============================

  Browser -> Bridge:
  +-------------------------------+
  | Binary: PCM16 audio frames    |   640 bytes = 20ms @ 16kHz mono
  +-------------------------------+
  | {"type":"user_input",         |   typed text (skips STT)
  |  "text":"..."}                |
  +-------------------------------+
  | {"type":"segment_play_start", |   segment playback started
  |  "index":0,"job_id":42}       |
  +-------------------------------+
  | {"type":"segment_play_end",   |   segment playback finished
  |  "index":0,"job_id":42}       |
  +-------------------------------+

  Bridge -> Browser:
  +-------------------------------+
  | {"type":"speech_segment",     |   main response unit
  |  "text":"...",                |
  |  "emotion":"happy",           |
  |  "gesture":"KA_Idle28_Laugh", |
  |  "audio_base64":"...",        |   WAV audio (base64)
  |  "viseme_b64":"...",          |   lip-sync weights (base64 float32)
  |  "viseme_fps":30,             |
  |  "viseme_frames":45,          |
  |  "index":0, "total":3,        |
  |  "job_id":42}                 |
  +-------------------------------+
  | {"type":"speech_end",         |   all segments sent
  |  "job_id":42,                 |
  |  "total_segments":3}          |
  +-------------------------------+
  | {"type":"interrupt"}          |   cancel current playback (barge-in)
  +-------------------------------+
  | {"type":"stt_result",         |   transcription result
  |  "text":"..."}                |
  +-------------------------------+
  | {"type":"idle_action",        |   silent gesture (no audio)
  |  "emotion":"neutral",         |
  |  "gesture":"KA_Idle05"}       |
  +-------------------------------+
  | {"type":"ui_command",         |   Mocha's HTML toolbox
  |  "action":"create_...",       |
  |  "presentation":{...}}        |
  +-------------------------------+
  | {"type":"debug_state",        |   pipeline timing
  |  "phase":"thinking",          |
  |  "llm_ms":234.5}             |
  +-------------------------------+
  | {"type":"echo_mode",          |   mic mute policy
  |  "mode":"room"}              |
  +-------------------------------+
  | {"type":"mic_mute",           |   mic muted state
  |  "muted":true}               |
  +-------------------------------+

  /ws/monitor  (Dashboard <-> Bridge)
  ====================================

  Dashboard -> Bridge:
  +-------------------------------+
  | {"type":"config_update",      |   runtime config change
  |  "key":"llm_temperature",    |
  |  "value":"0.9"}              |
  +-------------------------------+

  Bridge -> Dashboard:
  +-------------------------------+
  | {"type":"thread_status",      |   per-thread state
  |  "thread":"llm",             |
  |  "status":"processing",      |
  |  "elapsed_ms":123.4}         |
  +-------------------------------+
  | {"type":"timeline_event",     |   Gantt chart data
  |  "job_id":42, "label":"tts", |
  |  "action":"start",           |
  |  "offset_ms":234.5}          |
  +-------------------------------+
  | {"type":"chat_entry",         |   conversation log
  |  "entry":{                   |
  |    "user_text":"...",        |
  |    "assistant_text":"...",   |
  |    "source":"web"}}          |
  +-------------------------------+
  | {"type":"config_state",       |   full config snapshot
  |  "llm_temperature":0.8,...}  |
  +-------------------------------+
  | {"type":"shiro_state",        |   Shiro agent status
  |  "running":true}             |
  +-------------------------------+
```

---

## 9. 3D/VRM Rendering Pipeline

```
  LLM Segment
  {"text":"...", "emotion":"happy", "action":"wave cheerfully"}
       |
       +--- emotion -----> vrm-renderer.js
       |                   applyEmotion("happy")
       |                        |
       |                        v
       |                   VRM BlendShapes
       |                   { Joy: 0.8, Neutral: 0.2 }
       |
       +--- action ------> Bridge: Animation VecDB :8004
       |                   POST /query {"text":"wave cheerfully"}
       |                        |
       |                        v
       |                   clip name: "KA_Idle16_WaveHands"
       |
       +--- audio -------> TTS :8002 -> WAV
       |                        |
       +--- visemes -----> STT :8001 /align -> viseme_b64
       |                        |
       v                        v
  speech_segment message -> Browser
       |
       v
  +----+------ Playback Pipeline ------+
  |                                    |
  |  1. Load clip JSON                 |   /static/clips/KA_Idle16_WaveHands.json
  |     animation-controller.js        |   { frames: [{Hips:[qx,qy,qz,qw],...},...] }
  |                                    |
  |  2. FBX -> VRM retarget            |   quaternion axis remapping
  |     _retargetFrame(frame)          |   per-section calibration (core/arm/leg)
  |                                    |
  |  3. Apply bones per frame          |   vrm-renderer.js: _applyBones()
  |     30 fps interpolation           |   VRM humanoid bone.quaternion.set()
  |                                    |
  |  4. Play audio                     |   AudioContext + base64 -> ArrayBuffer
  |                                    |
  |  5. Sync visemes to audio          |   5-channel weights: aa, ih, ou, ee, oh
  |     viseme_b64 -> float32 array    |   applied to VRM blendShapes per frame
  |     30 fps lip-sync                |
  |                                    |
  |  6. Apply emotion expression       |   VRM expression presets
  |     happy -> Joy: 0.8              |   blended over duration
  +------------------------------------+

  Idle State Machine (no user input):
  +-------+   40s    +-----------+   gesture   +---------+
  | IDLE  +--------->| Pick fidget+----------->| PLAYING |
  |       |<---------+ behavior  |             | ONCE    |
  +-------+  done    +-----------+             +----+----+
                                                    |
                                               clip ends
                                                    |
                                               +----v----+
                                               | IDLE    |
                                               +---------+
```

---

## 10. Audio Pipeline

```
  MIC INPUT                              SPEAKER OUTPUT
  --------                               --------------

  Browser mic                            Browser AudioContext
  MediaStream API                        playback queue
       |                                      ^
       v                                      |
  AudioWorklet                           base64 -> ArrayBuffer
  PCM16 16kHz mono                       24kHz WAV decode
  20ms frames (640 bytes)                     ^
       |                                      |
       v                                      |
  /ws/live (binary)                      speech_segment.audio_base64
       |                                      ^
       v                                      |
  +----+---+                             +----+--------+
  | Bridge |                             |  TTS :8002  |
  | VAD    |                             |  F5-TTS     |
  +----+---+                             |  24kHz WAV  |
       |                                 +----+--------+
       | 900ms silence                        ^
       v                                      |
  +----+---+                             text segments
  | STT    |                             from LLM
  | :8001  |
  | Whisper|
  +----+---+
       |
       v
  transcribed text
  -> LLM pipeline

  VISEME LIP-SYNC (parallel to TTS)
  ===================================
  TTS audio + text
       |
       v
  STT :8001 /align
  (MMS forced alignment)
       |
       v
  phoneme timestamps
       |
       v
  phoneme -> viseme mapping
  (ARPAbet categories)
       |
       v
  per-frame weights (30 fps)
  5 channels: aa, ih, ou, ee, oh
       |
       v
  float32 array -> base64
  -> viseme_b64 in speech_segment
```

---

## 11. Agent Architecture (Mocha, Shiro, Nori)

```
  +=====================================================================+
  |                         MOCHA (Main Agent)                          |
  |                                                                     |
  |  Role: Front-end conversational character                           |
  |  Model: Qwen3-32B via vLLM                                         |
  |  Personality: character/soul.md + character/behaviors.yaml          |
  |  Emotions: character/emotions.yaml (8 emotions)                     |
  |                                                                     |
  |  Capabilities:                                                      |
  |  - Natural conversation with voice + face + body                    |
  |  - Tool calling (ReAct loop, max 5 rounds)                         |
  |  - Data fetching (stocks, news, weather, calculator)                |
  |  - HTML UI control (presentations, cards, notifications)            |
  |  - Memory-aware (ChromaDB semantic recall)                          |
  |                                                                     |
  |  Input channels:                                                    |
  |  - Web (/ws/live, /chat/stream)                                     |
  |  - Telegram, Discord (/channel)                                     |
  |  - Voice (/voice, /ws/unity)                                        |
  |  - CLI (/channel)                                                   |
  +=====================================================================+
         |                              |
         | Tool calls via               | Conversations logged
         | tools/custom/*               | to PostgreSQL
         |                              |
  +======+=====+              +=========+=========+
  |    NORI    |              |                   |
  | (Assistant)|              |    PostgreSQL     |
  |            |              |    :5432          |
  | Role:      |              |    llm_call_log   |
  | Backend    |              |                   |
  | data       |              +=========+=========+
  | service    |                        |
  |            |                        | reads logs
  | Lives as   |                        |
  | custom     |              +=========+=========+
  | tools in   |              |     SHIRO         |
  | tools/     |              |  (Meta-Agent)     |
  | custom/    |              |                   |
  |            |              |  Role:            |
  | Tools:     |              |  Coaching agent   |
  | - stocks   |              |  that analyzes    |
  | - news     |              |  Mocha's convos   |
  | - weather  |              |  and proposes     |
  | - calc     |              |  improvements     |
  | - present  |              |                   |
  | - cards    |              |  Can modify:      |
  | - notify   |              |  - soul.md        |
  | - clear    |              |  - behaviors.yaml |
  |            |              |  - tools/custom/* |
  +============+              |                   |
                              |  Reads:           |
                              |  - llm_call_log   |
                              |  - soul.md        |
                              |  - behaviors.yaml |
                              |                   |
                              |  shiro/agent.py   |
                              |  shiro/analyzer.py|
                              |  shiro/pg_reader  |
                              +===================+
```

---

## 12. Nori (Assistant Agent) — Tool Call Detail

```
  Mocha LLM decides to use a tool
           |
           v
  +--------+----------+
  | DATA TOOLS (Nori) |    Fetch real-world data
  +--------+----------+
           |
     +-----+------+------+------+
     |     |      |      |      |
     v     v      v      v      v
  +-----+ +----+ +----+ +----+ +-------+
  |stock| |news| |wthr| |calc| |web_   |
  |data | |    | |    | |    | |search |
  +--+--+ +--+-+ +--+-+ +--+-+ +---+---+
     |       |       |      |       |
     v       v       v      v       v
  yfinance  DDGS   Open-  ast    DDGS
  API       .news  Meteo  safe   .text
                   API    eval

  Results (max 4000 bytes) returned to LLM
           |
           v
  +--------+----------+
  | UI TOOLS (Nori)   |    Manipulate web frontend
  +--------+----------+
           |
     +-----+------+------+------+
     |     |      |      |      |
     v     v      v      v      v
  +-----+ +----+ +----+ +----+ +-----+
  |pres | |ctrl| |card| |ntfy| |clear|
  |ent  | |pres| |    | |    | |ui   |
  +--+--+ +--+-+ +--+-+ +--+-+ +--+--+
     |       |       |      |      |
     +-------+-------+------+------+
                     |
                     v
         _broadcast_to_unity()
                     |
                     v
          {"type":"ui_command",...}
                     |
                     v
         /ws/live -> all connected browsers
                     |
                     v
          presentation.js handles rendering
```

---

## 13. Channel Integration

```
  +---TELEGRAM---+     +---DISCORD---+     +---CLI---+     +---WEB---+
  |              |     |             |     |         |     |         |
  | python-      |     | discord.py  |     | rich    |     | Browser |
  | telegram-bot |     | v2+         |     | REPL    |     | JS/HTML |
  |              |     |             |     |         |     |         |
  +------+-------+     +------+------+     +----+----+     +----+----+
         |                    |                  |              |
         v                    v                  v              |
    POST /channel        POST /channel      POST /channel      |
    source=telegram      source=discord     source=cli         |
         |                    |                  |              |
         +----------+---------+----------+       |
                    |                            |
                    v                            v
          +--------+--------+          +---------+--------+
          | /channel handler|          | /ws/live handler  |
          | (text only)     |          | (streaming audio  |
          | tool_loop()     |          |  + text)          |
          | batch response  |          | _two_pass_stream()|
          +-----------------+          | real-time speech  |
                                       +------------------+

  Key difference:
  /channel  -> Batch: one request, one JSON response, tool loop inline
  /ws/live  -> Streaming: real-time segments, TTS audio, visemes, gestures
```

---

## 14. Memory & Context Assembly

```
  User says something
         |
         v
  +------+--------+
  | Query ChromaDB |    POST memory:8003/query
  | semantic search |    top-5 relevant memories
  +------+---------+
         |
         v
  +------+---------+
  | Build System   |    character/context.py
  | Prompt         |    build_system_prompt()
  +------+---------+
         |
         v
  +------+----------------------------------------------+
  | SYSTEM PROMPT                                       |
  |                                                     |
  | 1. Soul (character/soul.md)                         |
  |    - Identity, personality, voice, boundaries       |
  |                                                     |
  | 2. Emotions (character/emotions.yaml)               |
  |    - 8 emotion definitions + VRM mappings           |
  |                                                     |
  | 3. Behaviors (character/behaviors.yaml)             |
  |    - 24 condition->response rules, priority sorted  |
  |                                                     |
  | 4. Response Format                                  |
  |    - JSON segments schema                           |
  |    - Beat planning rules                            |
  |                                                     |
  | 5. Action/Animation block (mode-dependent)          |
  |    - llm_select: pick from clip list                |
  |    - fbx_functions: pick from function list         |
  |                                                     |
  | 6. Routing block (Pass 1 only)                      |
  |    - Option A: direct answer                        |
  |    - Option B: use tools (if pass1_tools)           |
  |    - Option C: needs_context escalation             |
  |                                                     |
  | 7. Tools block (when tools_available=True)          |
  |    - Built-in tools (web_search, file ops)          |
  |    - Data tools (stocks, news, weather, calc)       |
  |    - UI tools (presentation, cards, notifications)  |
  |    - Anti-hallucination rules                       |
  +-----------------------------------------------------+
         |
         v
  +------+---------+
  | Message Array  |    Sent to vLLM
  +----------------+
  | [0] system     |    <- assembled prompt above
  | [1] memory     |    <- "Relevant memories: ..."
  | [2] user       |    <- history msg 1
  | [3] assistant  |    <- history msg 2
  | ...            |    <- up to MAX_HISTORY (20)
  | [N] user       |    <- current user input
  +----------------+
```

---

## 15. PostgreSQL Call Logging

```
  Every LLM call
       |
       v
  asyncio.create_task(log_call(...))     fire-and-forget, non-blocking
       |
       v
  +----+---+
  | PG     |    DSN: postgresql://parrot:parrot@127.0.0.1:5432/parrot
  | :5432  |    Table: llm_call_log
  +--------+

  Columns logged per call:
  +--------------------+--------------------------------------------+
  | call_id            | UUID                                       |
  | created_at         | timestamp                                  |
  | triggered_by       | chat_stream / channel / voice / shiro / ...|
  | source             | telegram / discord / cli / web              |
  | user_id            | who triggered                              |
  | conversation_id    | groups all calls in one user turn           |
  | pass_number        | 1 (routing) or 2 (full)                    |
  | tool_round         | which tool loop iteration (1-based)        |
  | model              | Qwen/Qwen3-32B                             |
  | temperature        | 0.8 etc                                    |
  | messages           | JSONB: full message array sent              |
  | response_content   | full LLM output text                       |
  | response_tool_calls| JSONB: tool calls if any                   |
  | latency_ms         | wall-clock time                            |
  | ttft_ms            | time to first token (streaming)            |
  | prompt_tokens      | input count                                |
  | completion_tokens  | output count                               |
  +--------------------+--------------------------------------------+

  Consumers:
  - Shiro (reads via shiro/pg_reader.py for conversation analysis)
  - Claude Code (manual SQL queries for debugging)
  - Future dashboards / analytics
```

---

## 16. Idle Behavior System

```
  No user input for 40s (initial_delay)
       |
       v
  +----+-----------+
  | Pick random    |    IDLE_BEHAVIORS list in bridge/server.py
  | idle behavior  |    e.g. "stretch arms", "look around", "yawn"
  +----+-----------+
       |
       v
  +----+---------+      +----------+
  | Resolve clip |----->| Anim DB  |
  | from action  |      | :8004    |
  +----+---------+      +----------+
       |
       v
  +----+-------------------+
  | Broadcast idle_action  |    {"type":"idle_action","emotion":"neutral",
  | (no audio, no TTS)     |     "gesture":"KA_Idle05_Stretch"}
  +----+-------------------+
       |
       v
  Repeat every 15s (with +/- 30% jitter)
  Stop after 300s total silence (max_idle_duration)
  Reset on any user input
```

---

## 17. Config Reference

```yaml
# config.yaml — key sections

bridge:
  host: 0.0.0.0
  port: 8000

llm:
  vllm_base: "http://127.0.0.1:8800/v1"
  model: "Qwen/Qwen3-32B"
  temperature: 0.8
  max_tokens: 1024
  complexity_routing:
    enabled: true
    short_history: 8
    pass1_tools: true          # <-- NEW: tools in Pass 1

stt:
  host: 0.0.0.0
  port: 8001
  model: large-v3

tts:
  host: 0.0.0.0
  port: 8002

memory:
  host: 0.0.0.0
  port: 8003
  short_term_limit: 20

animation:
  host: 0.0.0.0
  port: 8004
  mode: llm_select             # or fbx_functions

tools:
  enabled: true
  allowed: [bash_exec, read_file, write_file, git_status,
            list_dir, web_search,
            get_stock_data, get_news, get_weather, calculate,
            create_presentation, control_presentation,
            show_card, show_notification, clear_ui]
  max_rounds: 5
  timeout: 30

idle:
  enabled: true
  initial_delay: 40
  interval: 15
```

---

## 18. Sequence Diagram — "Show me stocks today"

```
  User          Browser        Bridge         vLLM        Tools        Browser UI
   |               |              |              |           |              |
   |  "show me     |              |              |           |              |
   |   stocks"     |              |              |           |              |
   +-------------->|              |              |           |              |
   |               | user_input   |              |           |              |
   |               +------------->|              |           |              |
   |               |              |              |           |              |
   |               |              | PASS 1       |           |              |
   |               |              | (tools=yes)  |           |              |
   |               |              +------------->|           |              |
   |               |              |              |           |              |
   |               |              |  stalling    |           |              |
   |               |              |  segment +   |           |              |
   |               |              |  tool_call:  |           |              |
   |               |              |  get_stock   |           |              |
   |               |              |<-------------+           |              |
   |               |              |              |           |              |
   |               | speech_seg   |              |           |              |
   |               | "Let me      |              |           |              |
   |               |  check..."   |              |           |              |
   |               |<-------------+              |           |              |
   |               |              |              |           |              |
   |               |              | execute      |           |              |
   |               |              +------------------------->|              |
   |               |              |              |  yfinance |              |
   |               |              |              |  API call |              |
   |               |              |<-------------------------+              |
   |               |              |              |           |              |
   |               |              | round 2:     |           |              |
   |               |              | LLM + results|           |              |
   |               |              +------------->|           |              |
   |               |              |              |           |              |
   |               |              |  tool_call:  |           |              |
   |               |              |  create_     |           |              |
   |               |              |  presentation|           |              |
   |               |              |<-------------+           |              |
   |               |              |              |           |              |
   |               |              | execute      |           |              |
   |               |              +------------------------->|              |
   |               |              |              |           |              |
   |               |              |              |    _broadcast_to_unity() |
   |               |              |              |           +------------->|
   |               |              |              |           | ui_command:  |
   |               |              |              |           | create_pres  |
   |               |              |              |           |   (slides    |
   |               |              |              |           |    render)   |
   |               |              |<-------------------------+              |
   |               |              |              |           |              |
   |               |              | round 3:     |           |              |
   |               |              | LLM + results|           |              |
   |               |              +------------->|           |              |
   |               |              |              |           |              |
   |               |              | final segs   |           |              |
   |               |              | (narration)  |           |              |
   |               |              |<-------------+           |              |
   |               |              |              |           |              |
   |               |  TTS + visemes + gesture    |           |              |
   |               |  speech_segment (x3)        |           |              |
   |               |<-------------+              |           |              |
   |               |              |              |           |              |
   |  Mocha speaks |              |              |           |              |
   |  + slides     |              |              |           |              |
   |  visible      |              |              |           |              |
   |<--------------+              |              |           |              |
```
