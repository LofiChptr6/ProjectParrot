using System;
using System.IO;
using UnityEngine;
using UnityEngine.UI;
using TMPro;

/// <summary>
/// Push-to-talk microphone input for Parrot Assistant.
///
/// Records audio via Unity's Microphone API, encodes it as 16 kHz mono WAV,
/// and sends the raw bytes over the WebSocket (ParrotBridge handles the rest).
///
/// The bridge receives binary data on /ws/unity, runs STT → LLM → TTS,
/// and sends back the speech response as usual.
///
/// Setup:
///   1. Add this script to the same GameObject (or any) in your scene.
///   2. Assign the ParrotBridge reference.
///   3. Optionally assign a UI Button for tap-to-talk and a status label.
///   4. Set pushToTalkKey (default: Space) for keyboard control.
/// </summary>
public class ParrotMicInput : MonoBehaviour
{
    [Header("Wiring")]
    public ParrotBridge bridge;

    [Header("Controls")]
    [Tooltip("Hold this key to record. Release to send.")]
    public KeyCode pushToTalkKey = KeyCode.Space;

    [Tooltip("If assigned, hold this button to record (touch / click).")]
    public Button micButton;

    [Header("Audio Settings")]
    [Tooltip("Recording sample rate sent to Whisper (16 kHz recommended).")]
    public int sampleRate = 16000;

    [Tooltip("Max recording length in seconds (safety cap).")]
    public int maxRecordSeconds = 30;

    [Header("Optional UI")]
    [Tooltip("Shows recording status text.")]
    public TMP_Text statusLabel;

    private AudioClip _micClip;
    private bool _isRecording;
    private string _micDevice;
    private bool _buttonHeld;

    private void Start()
    {
        if (Microphone.devices.Length == 0)
        {
            Debug.LogError("[ParrotMicInput] No microphone found.");
            if (statusLabel != null) statusLabel.text = "No mic found";
            return;
        }

        _micDevice = Microphone.devices[0];
        Debug.Log($"[ParrotMicInput] Using mic: {_micDevice}");

        if (micButton != null)
        {
            var trigger = micButton.gameObject.AddComponent<UnityEngine.EventSystems.EventTrigger>();

            var pointerDown = new UnityEngine.EventSystems.EventTrigger.Entry
                { eventID = UnityEngine.EventSystems.EventTriggerType.PointerDown };
            pointerDown.callback.AddListener(_ => { _buttonHeld = true; StartRecording(); });
            trigger.triggers.Add(pointerDown);

            var pointerUp = new UnityEngine.EventSystems.EventTrigger.Entry
                { eventID = UnityEngine.EventSystems.EventTriggerType.PointerUp };
            pointerUp.callback.AddListener(_ => { _buttonHeld = false; StopAndSend(); });
            trigger.triggers.Add(pointerUp);
        }
    }

    private void Update()
    {
        if (Input.GetKeyDown(pushToTalkKey) && !_isRecording)
            StartRecording();

        if (Input.GetKeyUp(pushToTalkKey) && _isRecording && !_buttonHeld)
            StopAndSend();
    }

    public void StartRecording()
    {
        if (_isRecording || string.IsNullOrEmpty(_micDevice)) return;

        // Barge-in: finish current sentence, cancel the rest
        if (bridge != null && bridge.IsSpeaking)
            bridge.RequestGracefulInterrupt();

        _micClip = Microphone.Start(_micDevice, false, maxRecordSeconds, sampleRate);
        _isRecording = true;

        if (statusLabel != null) statusLabel.text = "🎤 Recording…";
        Debug.Log("[ParrotMicInput] Recording started.");
    }

    public void StopAndSend()
    {
        if (!_isRecording) return;

        int position = Microphone.GetPosition(_micDevice);
        Microphone.End(_micDevice);
        _isRecording = false;

        if (statusLabel != null) statusLabel.text = "Sending…";
        Debug.Log($"[ParrotMicInput] Recording stopped. Samples: {position}");

        if (position == 0)
        {
            if (statusLabel != null) statusLabel.text = "Too short";
            return;
        }

        float[] samples = new float[position];
        _micClip.GetData(samples, 0);

        byte[] wav = EncodeWav(samples, sampleRate);
        SendAudioTobridge(wav);
    }

    private async void SendAudioTobridge(byte[] wavBytes)
    {
        if (bridge == null)
        {
            Debug.LogError("[ParrotMicInput] ParrotBridge not assigned.");
            return;
        }

        var ws = bridge.GetWebSocket();
        if (ws == null || ws.State != NativeWebSocket.WebSocketState.Open)
        {
            Debug.LogError("[ParrotMicInput] WebSocket not open.");
            if (statusLabel != null) statusLabel.text = "Not connected";
            return;
        }

        Debug.Log($"[ParrotMicInput] Sending {wavBytes.Length} bytes to bridge.");
        await ws.Send(wavBytes);

        if (statusLabel != null) statusLabel.text = "Waiting for reply…";
    }

    private static byte[] EncodeWav(float[] samples, int rate)
    {
        using var ms = new MemoryStream();
        using var w = new BinaryWriter(ms);

        int dataBytes = samples.Length * 2;

        // RIFF header
        w.Write(new char[] { 'R', 'I', 'F', 'F' });
        w.Write(36 + dataBytes);
        w.Write(new char[] { 'W', 'A', 'V', 'E' });

        // fmt chunk
        w.Write(new char[] { 'f', 'm', 't', ' ' });
        w.Write(16);           // chunk size
        w.Write((short)1);     // PCM
        w.Write((short)1);     // mono
        w.Write(rate);
        w.Write(rate * 2);     // byte rate
        w.Write((short)2);     // block align
        w.Write((short)16);    // bits per sample

        // data chunk
        w.Write(new char[] { 'd', 'a', 't', 'a' });
        w.Write(dataBytes);

        foreach (float s in samples)
        {
            float clamped = Mathf.Clamp(s, -1f, 1f);
            w.Write((short)(clamped < 0 ? clamped * 0x8000 : clamped * 0x7FFF));
        }

        return ms.ToArray();
    }
}
