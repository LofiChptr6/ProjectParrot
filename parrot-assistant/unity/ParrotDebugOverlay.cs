using UnityEngine;

/// <summary>
/// Toggleable debug overlay that shows the Parrot pipeline state in real time.
/// Reads public properties from ParrotBridge and renders them via OnGUI.
///
/// Setup: Add to any GameObject. Assign the ParrotBridge reference.
/// Press the toggleKey (default: F3) to show/hide.
/// </summary>
public class ParrotDebugOverlay : MonoBehaviour
{
    [Header("References")]
    public ParrotBridge bridge;

    [Header("Controls")]
    [Tooltip("Press to toggle the overlay.")]
    public KeyCode toggleKey = KeyCode.F3;

    [Header("Appearance")]
    [Tooltip("Width of the overlay panel.")]
    public float panelWidth = 340f;

    private bool _visible;
    private GUIStyle _boxStyle;
    private GUIStyle _labelStyle;
    private GUIStyle _headerStyle;
    private GUIStyle _phaseStyle;

    private void Update()
    {
        if (Input.GetKeyDown(toggleKey))
            _visible = !_visible;
    }

    private void OnGUI()
    {
        if (!_visible || bridge == null)
            return;

        EnsureStyles();

        float x = Screen.width - panelWidth - 10f;
        float y = 10f;
        float lineH = 22f;
        float padding = 8f;

        float totalH = padding + lineH * 12 + padding;
        GUI.Box(new Rect(x, y, panelWidth, totalH), "", _boxStyle);

        float cx = x + padding;
        float cy = y + padding;

        GUI.Label(new Rect(cx, cy, panelWidth - 16, lineH), "PARROT DEBUG", _headerStyle);
        cy += lineH + 4;

        string phase = bridge.CurrentPhase ?? "idle";
        Color phaseColor = phase switch
        {
            "idle" => Color.gray,
            "listening" => Color.cyan,
            "thinking" => Color.yellow,
            "synthesizing" => new Color(1f, 0.6f, 0f),
            "speaking" => Color.green,
            "waiting" => new Color(0.8f, 0.8f, 0.2f),
            _ => Color.white,
        };
        _phaseStyle.normal.textColor = phaseColor;
        GUI.Label(new Rect(cx, cy, panelWidth - 16, lineH), $"Phase: {phase.ToUpper()}", _phaseStyle);
        cy += lineH;

        GUI.Label(new Rect(cx, cy, panelWidth - 16, lineH),
            $"Relationship: {bridge.RelationshipTier}", _labelStyle);
        cy += lineH;

        GUI.Label(new Rect(cx, cy, panelWidth - 16, lineH),
            $"Emotion: {bridge.CurrentEmotion}", _labelStyle);
        cy += lineH;

        string gesture = string.IsNullOrEmpty(bridge.CurrentGesture) ? "-" : bridge.CurrentGesture;
        GUI.Label(new Rect(cx, cy, panelWidth - 16, lineH),
            $"Gesture: {gesture}", _labelStyle);
        cy += lineH;

        GUI.Label(new Rect(cx, cy, panelWidth - 16, lineH),
            $"Segment: {bridge.SegmentIndex + 1} / {bridge.SegmentTotal}", _labelStyle);
        cy += lineH;

        cy += 6;
        GUI.Label(new Rect(cx, cy, panelWidth - 16, lineH), "-- Latency --", _headerStyle);
        cy += lineH;

        GUI.Label(new Rect(cx, cy, panelWidth - 16, lineH),
            $"LLM: {bridge.LastLlmMs:F0} ms", _labelStyle);
        cy += lineH;

        GUI.Label(new Rect(cx, cy, panelWidth - 16, lineH),
            $"TTS: {bridge.LastTtsMs:F0} ms", _labelStyle);
        cy += lineH;

        GUI.Label(new Rect(cx, cy, panelWidth - 16, lineH),
            $"Anim: {bridge.LastAnimMs:F0} ms", _labelStyle);
        cy += lineH;

        GUI.Label(new Rect(cx, cy, panelWidth - 16, lineH),
            $"Speaking: {(bridge.IsSpeaking ? "YES" : "no")}", _labelStyle);
    }

    private void EnsureStyles()
    {
        if (_boxStyle != null) return;

        var bgTex = new Texture2D(1, 1);
        bgTex.SetPixel(0, 0, new Color(0f, 0f, 0f, 0.75f));
        bgTex.Apply();

        _boxStyle = new GUIStyle(GUI.skin.box)
        {
            normal = { background = bgTex },
        };

        _labelStyle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 14,
            normal = { textColor = new Color(0.9f, 0.9f, 0.9f) },
            fontStyle = FontStyle.Normal,
        };

        _headerStyle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 14,
            fontStyle = FontStyle.Bold,
            normal = { textColor = Color.white },
        };

        _phaseStyle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 16,
            fontStyle = FontStyle.Bold,
        };
    }
}
