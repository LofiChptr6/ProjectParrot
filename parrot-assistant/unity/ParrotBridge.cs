using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using NativeWebSocket;

/// <summary>
/// Connects to parrot-assistant bridge WebSocket (WSL) from Unity (Windows).
/// URL: ws://localhost:8000/ws/unity — must match bridge running: ./start.sh
///
/// Segment queue: The bridge sends "speech_segment" messages (one per sentence)
/// each with text, emotion, gesture, and audio.  Segments are queued and played
/// sequentially so body language/emotion shifts naturally across a reply.
///
/// Barge-in: When the user sends new input (text or mic) while the character is
/// still speaking, all queued segments are cancelled, audio stops, and the
/// character returns to idle before the new reply arrives.
///
/// Debug overlay: Receives "debug_state" messages from the bridge with timing
/// and pipeline status info, exposed via public properties for ParrotDebugOverlay.
/// </summary>
public class ParrotBridge : MonoBehaviour
{
    [Header("Connection")]
    [Tooltip("WSL2: usually ws://127.0.0.1:8000/ws/unity")]
    public string bridgeUrl = "ws://127.0.0.1:8000/ws/unity";

    [Header("References")]
    public AudioSource audioSource;
    public Animator characterAnimator;

    [Header("Animation Library")]
    [Tooltip("Drag all animation clips here. The bridge sends clip names and the script looks them up at runtime.")]
    public AnimationClip[] clipLibrary;

    [Tooltip("The placeholder clip used in the Animator's Action state.")]
    public AnimationClip actionPlaceholder;

    [Header("Segment Playback")]
    [Tooltip("Seconds of silence between segments (lets animations settle).")]
    [Range(0f, 0.5f)]
    public float interSegmentGap = 0.05f;

    [Tooltip("Seconds to wait for the next segment before assuming reply is complete (streaming mode).")]
    [Range(0.5f, 5f)]
    public float streamingTimeout = 2f;

    [Header("Debug")]
    [Tooltip("If set, sends this as user text a moment after the socket opens.")]
    public string debugSendOnConnect = "";

    [Tooltip("Log WebSocket lifecycle + message details.")]
    public bool verboseLogging = true;

    // --- Public state for debug overlay ---
    public string CurrentPhase { get; private set; } = "idle";
    public string CurrentEmotion { get; private set; } = "neutral";
    public string CurrentGesture { get; private set; } = "";
    public int SegmentIndex { get; private set; }
    public int SegmentTotal { get; private set; }
    public string RelationshipTier { get; private set; } = "stranger";
    public float LastLlmMs { get; private set; }
    public float LastTtsMs { get; private set; }
    public float LastAnimMs { get; private set; }

    private static readonly Dictionary<string, int> EmotionMap = new()
    {
        { "neutral",    0 },
        { "happy",      1 },
        { "excited",    2 },
        { "thinking",   3 },
        { "sad",        4 },
        { "surprised",  5 },
        { "playful",    6 },
        { "empathetic", 7 },
    };

    private WebSocket ws;
    private Dictionary<string, AnimationClip> _clipLookup;
    private AnimatorOverrideController _overrideController;

    private readonly Queue<SpeechSegment> _segmentQueue = new();
    private bool _isPlayingQueue;
    private Coroutine _playbackCoroutine;
    private bool _gracefulInterruptRequested;

    /// <summary>Expose the WebSocket so ParrotMicInput can send binary audio.</summary>
    public WebSocket GetWebSocket() => ws;

    /// <summary>Whether the character is currently speaking/playing segments.</summary>
    public bool IsSpeaking => _isPlayingQueue;

    // ------------------------------------------------------------------
    //  Barge-in: cancel current playback
    // ------------------------------------------------------------------

    /// <summary>
    /// Request a graceful interruption: finish the current sentence/gesture,
    /// but cancel any remaining queued segments.
    /// </summary>
    public void RequestGracefulInterrupt()
    {
        if (!_isPlayingQueue)
        {
            _segmentQueue.Clear();
            CurrentPhase = "idle";
            return;
        }

        // Finish current segment, drop the rest.
        _gracefulInterruptRequested = true;
        _segmentQueue.Clear();

        if (verboseLogging)
            Debug.Log("[ParrotBridge] Interrupt requested (graceful): will stop after current sentence.");
    }

    /// <summary>
    /// Immediate stop (hard cut). Use only if you really need to abort playback.
    /// </summary>
    public void CancelCurrentPlaybackImmediate()
    {
        if (!_isPlayingQueue && _segmentQueue.Count == 0)
            return;

        if (verboseLogging)
            Debug.Log($"[ParrotBridge] Interrupt (immediate): cancelling {_segmentQueue.Count} queued segment(s)");

        _segmentQueue.Clear();
        _gracefulInterruptRequested = false;

        if (_playbackCoroutine != null)
        {
            StopCoroutine(_playbackCoroutine);
            _playbackCoroutine = null;
        }

        if (audioSource != null && audioSource.isPlaying)
            audioSource.Stop();

        _isPlayingQueue = false;
        SetAnimatorBool("IsTalking", false);
        CurrentPhase = "idle";
    }

    // ------------------------------------------------------------------
    //  Lifecycle
    // ------------------------------------------------------------------

    private async void Start()
    {
        if (audioSource == null)
            Debug.LogWarning("ParrotBridge: AudioSource is not assigned.");
        if (characterAnimator == null)
            Debug.LogWarning("ParrotBridge: Animator is not assigned.");

        BuildClipLookup();
        SetupOverrideController();

        ws = new WebSocket(bridgeUrl);

        ws.OnOpen += () =>
        {
            if (verboseLogging)
                Debug.Log($"[ParrotBridge] Connected: {bridgeUrl}");
            if (!string.IsNullOrWhiteSpace(debugSendOnConnect))
                StartCoroutine(SendDebugAfterFrames());
        };

        ws.OnError += e => Debug.LogError($"[ParrotBridge] WebSocket error: {e}");
        ws.OnClose += e => Debug.Log($"[ParrotBridge] WebSocket closed: {e}");

        ws.OnMessage += bytes =>
        {
            string msg = Encoding.UTF8.GetString(bytes);
            if (verboseLogging)
                Debug.Log($"[ParrotBridge] OnMessage ({msg.Length} chars): {msg.Substring(0, Math.Min(200, msg.Length))}...");

            var data = JsonUtility.FromJson<BridgeMessage>(msg);
            HandleMessage(data);
        };

        await ws.Connect();
    }

    private IEnumerator SendDebugAfterFrames()
    {
        yield return null;
        yield return null;
        SendUserInput(debugSendOnConnect.Trim());
    }

    private void Update()
    {
#if !UNITY_WEBGL || UNITY_EDITOR
        ws?.DispatchMessageQueue();
#endif
    }

    // ------------------------------------------------------------------
    //  Message handling
    // ------------------------------------------------------------------

    private void HandleMessage(BridgeMessage msg)
    {
        if (msg == null || string.IsNullOrEmpty(msg.type))
            return;

        if (msg.type == "speech")
        {
            EnqueueSegment(msg, 0, 1);
            return;
        }

        if (msg.type == "speech_segment")
        {
            EnqueueSegment(msg, msg.index, msg.total);
            return;
        }

        if (msg.type == "idle_action")
        {
            HandleIdleAction(msg);
            return;
        }

        if (msg.type == "interrupt")
        {
            RequestGracefulInterrupt();
            if (verboseLogging)
                Debug.Log("[ParrotBridge] Server-initiated interrupt received");
            return;
        }

        if (msg.type == "debug_state")
        {
            HandleDebugState(msg);
            return;
        }
    }

    private void HandleIdleAction(BridgeMessage msg)
    {
        if (_isPlayingQueue || (audioSource != null && audioSource.isPlaying))
            return;

        ApplyEmotion(msg.emotion ?? "neutral");
        ApplyGesture(msg.gesture);

        if (verboseLogging)
            Debug.Log($"[ParrotBridge] Idle heartbeat: [{msg.emotion}] gesture={msg.gesture}");
    }

    private void HandleDebugState(BridgeMessage msg)
    {
        if (!string.IsNullOrEmpty(msg.phase))
            CurrentPhase = msg.phase;
        if (!string.IsNullOrEmpty(msg.relationship_tier))
            RelationshipTier = msg.relationship_tier;
        if (msg.llm_ms > 0) LastLlmMs = msg.llm_ms;
        if (msg.tts_ms > 0) LastTtsMs = msg.tts_ms;
        if (msg.anim_ms > 0) LastAnimMs = msg.anim_ms;
    }

    private void EnqueueSegment(BridgeMessage msg, int index, int total)
    {
        byte[] audioBytes = null;
        if (!string.IsNullOrEmpty(msg.audio_base64))
        {
            try { audioBytes = Convert.FromBase64String(msg.audio_base64); }
            catch (Exception e) { Debug.LogWarning($"[ParrotBridge] Failed to decode audio: {e.Message}"); }
        }

        var seg = new SpeechSegment
        {
            jobId = msg.job_id,
            index = index,
            total = total,
            text = msg.text,
            emotion = msg.emotion ?? "neutral",
            gesture = msg.gesture,
            audioBytes = audioBytes,
        };

        _segmentQueue.Enqueue(seg);

        if (verboseLogging)
            Debug.Log($"[ParrotBridge] Enqueued segment {index + 1}/{total}: [{seg.emotion}] gesture={seg.gesture} text={seg.text}");

        if (!_isPlayingQueue)
            _playbackCoroutine = StartCoroutine(PlaySegmentQueue());
    }

    // ------------------------------------------------------------------
    //  Sequential segment playback (optimised transitions)
    // ------------------------------------------------------------------

    private IEnumerator PlaySegmentQueue()
    {
        _isPlayingQueue = true;
        CurrentPhase = "speaking";
        SetAnimatorBool("IsTalking", true);
        _gracefulInterruptRequested = false;

        while (_segmentQueue.Count > 0)
        {
            var seg = _segmentQueue.Dequeue();

            SegmentIndex = seg.index;
            SegmentTotal = seg.total;
            CurrentEmotion = seg.emotion;
            CurrentGesture = seg.gesture ?? "";

            ApplyEmotion(seg.emotion);
            ApplyGesture(seg.gesture);

            if (verboseLogging)
                Debug.Log($"[ParrotBridge] Playing segment {seg.index + 1}/{seg.total}: [{seg.emotion}] gesture={seg.gesture} text={seg.text}");

            SendSegmentEvent("segment_play_start", seg.jobId, seg.index, seg.total);

            if (seg.audioBytes != null && seg.audioBytes.Length > 44 && audioSource != null)
            {
                var clip = CreateAudioClip(seg.audioBytes);
                if (clip != null)
                {
                    audioSource.clip = clip;
                    audioSource.Play();

                    float clipLen = clip.length;
                    float overlapMargin = Mathf.Min(0.05f, clipLen * 0.05f);
                    float waitTime = clipLen - overlapMargin;

                    if (waitTime > 0f)
                    {
                        float elapsed = 0f;
                        while (elapsed < waitTime && audioSource.isPlaying)
                        {
                            yield return null;
                            elapsed += Time.deltaTime;
                        }
                    }

                    if (_segmentQueue.Count == 0)
                    {
                        yield return new WaitWhile(() => audioSource.isPlaying);
                    }
                }
            }

            SendSegmentEvent("segment_play_end", seg.jobId, seg.index, seg.total);

            if (_gracefulInterruptRequested)
            {
                _segmentQueue.Clear();
                break;
            }

            if (_segmentQueue.Count > 0 && interSegmentGap > 0f)
                yield return new WaitForSeconds(interSegmentGap);

            if (_segmentQueue.Count == 0 && seg.total == -1)
            {
                float waited = 0f;
                while (_segmentQueue.Count == 0 && waited < streamingTimeout)
                {
                    yield return new WaitForSeconds(0.1f);
                    waited += 0.1f;
                }
            }
        }

        SetAnimatorBool("IsTalking", false);
        ApplyEmotion("neutral");
        _isPlayingQueue = false;
        _playbackCoroutine = null;
        CurrentPhase = "idle";
        _gracefulInterruptRequested = false;
    }

    /// <summary>Send timing feedback to the bridge so the monitor can plot Unity play times.</summary>
    private async void SendSegmentEvent(string eventType, int jobId, int index, int total)
    {
        if (ws == null || ws.State != WebSocketState.Open) return;
        var msg = new SegmentEvent { type = eventType, job_id = jobId, index = index, total = total };
        try { await ws.SendText(JsonUtility.ToJson(msg)); }
        catch (Exception) { /* non-critical */ }
    }

    // ------------------------------------------------------------------
    //  Emotion / Gesture / Audio helpers
    // ------------------------------------------------------------------

    private void ApplyEmotion(string emotion)
    {
        if (string.IsNullOrEmpty(emotion))
            emotion = "neutral";

        if (characterAnimator != null && characterAnimator.runtimeAnimatorController != null)
        {
            int emotionIdx = EmotionMap.ContainsKey(emotion) ? EmotionMap[emotion] : 0;
            characterAnimator.SetInteger("Emotion", emotionIdx);
        }
    }

    private void ApplyGesture(string clipName)
    {
        if (string.IsNullOrEmpty(clipName) || clipName == "null")
            return;

        EnsureAnimationRuntimeReady();

        if (_clipLookup == null || _overrideController == null || actionPlaceholder == null)
        {
            if (verboseLogging)
                Debug.LogWarning($"[ParrotBridge] Cannot play gesture '{clipName}': override controller not ready.");
            return;
        }

        if (!_clipLookup.TryGetValue(clipName, out AnimationClip clip))
        {
            if (verboseLogging)
                Debug.LogWarning($"[ParrotBridge] Clip '{clipName}' not found in clipLibrary ({_clipLookup.Count} clips loaded).");
            return;
        }

        _overrideController[actionPlaceholder] = clip;
        characterAnimator.SetTrigger("DoAction");

        if (verboseLogging)
            Debug.Log($"[ParrotBridge] Playing clip: {clipName} (length={clip.length:F1}s)");
    }

    private void EnsureAnimationRuntimeReady()
    {
        if (_clipLookup == null || _clipLookup.Count == 0)
            BuildClipLookup();

        if (_overrideController == null)
            SetupOverrideController();
    }

    private void BuildClipLookup()
    {
        _clipLookup = new Dictionary<string, AnimationClip>();
        if (clipLibrary == null) return;

        foreach (var clip in clipLibrary)
        {
            if (clip != null && !_clipLookup.ContainsKey(clip.name))
                _clipLookup[clip.name] = clip;
        }

        if (verboseLogging)
            Debug.Log($"[ParrotBridge] Clip library loaded: {_clipLookup.Count} clips.");
    }

    private void SetupOverrideController()
    {
        if (characterAnimator == null || characterAnimator.runtimeAnimatorController == null)
            return;

        _overrideController = new AnimatorOverrideController(characterAnimator.runtimeAnimatorController);
        characterAnimator.runtimeAnimatorController = _overrideController;

        if (verboseLogging)
            Debug.Log("[ParrotBridge] AnimatorOverrideController created.");
    }

    private void SetAnimatorBool(string name, bool value)
    {
        if (characterAnimator != null && characterAnimator.runtimeAnimatorController != null)
            characterAnimator.SetBool(name, value);
    }

    private AudioClip CreateAudioClip(byte[] wavBytes)
    {
        if (wavBytes == null || wavBytes.Length < 44)
            return null;

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
        return clip;
    }

    // ------------------------------------------------------------------
    //  Send (with barge-in)
    // ------------------------------------------------------------------

    public async void SendUserInput(string text)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            Debug.LogWarning("ParrotBridge: SendUserInput called with empty text.");
            return;
        }

        if (ws == null)
        {
            Debug.LogError("ParrotBridge: WebSocket is null.");
            return;
        }

        if (ws.State != WebSocketState.Open)
        {
            Debug.LogError($"ParrotBridge: WebSocket not open (state={ws.State}).");
            return;
        }

        // Barge-in: finish current sentence, cancel the rest
        RequestGracefulInterrupt();
        CurrentPhase = "waiting";

        string json = JsonUtility.ToJson(new UserInput { type = "user_input", text = text.Trim() });
        if (verboseLogging)
            Debug.Log($"[ParrotBridge] SendUserInput: {json}");

        await ws.SendText(json);
    }

    private async void OnDestroy()
    {
        if (ws != null)
            await ws.Close();
    }

    // ------------------------------------------------------------------
    //  Serializable message types
    // ------------------------------------------------------------------

    [Serializable]
    public class BridgeMessage
    {
        public string type;
        public int job_id;
        public string text;
        public string emotion;
        public string gesture;
        public string audio_base64;
        public int index;
        public int total;
        // debug_state fields
        public string phase;
        public string relationship_tier;
        public float llm_ms;
        public float tts_ms;
        public float anim_ms;
    }

    [Serializable]
    public class UserInput
    {
        public string type;
        public string text;
    }

    [Serializable]
    public class SegmentEvent
    {
        public string type;
        public int job_id;
        public int index;
        public int total;
    }

    private class SpeechSegment
    {
        public int jobId;
        public int index;
        public int total;
        public string text;
        public string emotion;
        public string gesture;
        public byte[] audioBytes;
    }
}
