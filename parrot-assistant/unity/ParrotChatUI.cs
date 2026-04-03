using UnityEngine;
using UnityEngine.UI;
using TMPro;

/// <summary>
/// Simple chat box: type a message, press Send → calls ParrotBridge.SendUserInput (WebSocket → LLM → TTS).
///
/// Assign ONE input type:
/// - **TMP Input Field** (Unity 6 default): GameObject → UI → Input Field — TextMeshPro. Requires "Import TMP Essentials" once.
/// - **Legacy Input Field**: GameObject → UI → Legacy → Input Field — assign to Legacy Input Field below.
/// </summary>
[RequireComponent(typeof(RectTransform))]
public class ParrotChatUI : MonoBehaviour
{
    [Header("Wiring")]
    public ParrotBridge bridge;

    [Tooltip("Use this if you added UI → Input Field (TextMeshPro).")]
    public TMP_InputField tmpInputField;

    [Tooltip("Use this if you added UI → Legacy → Input Field instead.")]
    public InputField legacyInputField;

    public Button sendButton;

    [Header("Optional")]
    [Tooltip("Send when Enter is pressed (uses input field onSubmit, not UnityEngine.Input).")]
    public bool submitOnEnter = true;

    private void Start()
    {
        if (sendButton != null)
            sendButton.onClick.AddListener(OnSend);
        else
            Debug.LogWarning("ParrotChatUI: Send Button is not assigned — clicks will do nothing.");

        if (bridge == null)
            Debug.LogWarning("ParrotChatUI: Parrot Bridge is not assigned.");

        if (tmpInputField == null && legacyInputField == null)
            Debug.LogWarning("ParrotChatUI: Assign Tmp Input Field OR Legacy Input Field.");

        // Enter-to-send without UnityEngine.Input (avoids errors when Player Settings use "Input System Package" only).
        if (submitOnEnter)
        {
            if (tmpInputField != null)
                tmpInputField.onSubmit.AddListener(_ => OnSend());
            if (legacyInputField != null)
                legacyInputField.onSubmit.AddListener(_ => OnSend());
        }
    }

    /// <summary>Hook this to a Button OnClick() in the Inspector if you prefer.</summary>
    public void OnSend()
    {
        Debug.Log("[ParrotChatUI] Send pressed.");

        if (bridge == null)
        {
            Debug.LogError("ParrotChatUI: assign the Parrot Bridge field (drag the GameObject that has ParrotBridge.cs).");
            return;
        }

        string t = GetInputText();
        if (string.IsNullOrWhiteSpace(t))
        {
            Debug.LogWarning("ParrotChatUI: type a message in the input box first.");
            return;
        }

        bridge.SendUserInput(t.Trim());
        ClearInput();
        FocusInput();
    }

    private string GetInputText()
    {
        if (tmpInputField != null)
            return tmpInputField.text;
        if (legacyInputField != null)
            return legacyInputField.text;
        Debug.LogError("ParrotChatUI: assign TMP Input Field or Legacy Input Field.");
        return "";
    }

    private void ClearInput()
    {
        if (tmpInputField != null)
            tmpInputField.text = "";
        if (legacyInputField != null)
            legacyInputField.text = "";
    }

    private void FocusInput()
    {
        if (tmpInputField != null)
            tmpInputField.ActivateInputField();
        else if (legacyInputField != null)
            legacyInputField.ActivateInputField();
    }
}
