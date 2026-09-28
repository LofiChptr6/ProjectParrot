// Semantic face choices for the LLM. Custom presets live inside Mocha.vrm.
// Uploaded avatars without those presets retain the closest built-in emotion.
export const EMOTION_TO_VRM = {
    neutral:    { preset: 'neutral', weight: 0 },
    happy:      { preset: 'happy', weight: 0.8 },
    smile:      { preset: 'smile', weight: 0.8, fallback: 'happy' },
    excited:    { preset: 'happy', weight: 1 },
    thinking:   { preset: 'neutral', weight: 0 },
    sad:        { preset: 'sad', weight: 0.7 },
    surprised:  { preset: 'surprised', weight: 0.9 },
    playful:    { preset: 'happy', weight: 0.7 },
    empathetic: { preset: 'sad', weight: 0.3 },
    warm_smile: { preset: 'warm_smile', weight: 1, fallback: 'happy' },
    bright_smile: { preset: 'bright_smile', weight: 1, fallback: 'excited' },
    bashful: { preset: 'bashful', weight: 1, fallback: 'happy' },
    amused: { preset: 'amused', weight: 1, fallback: 'playful' },
    playful_wink: { preset: 'playful_wink', weight: 1, fallback: 'playful' },
    tender: { preset: 'tender', weight: 1, fallback: 'empathetic' },
    pout: { preset: 'pout', weight: 1, fallback: 'neutral' },
    sleepy: { preset: 'sleepy', weight: 1, fallback: 'neutral' },
    kiss: { preset: 'kiss', weight: 1, fallback: 'happy' },
    curious: { preset: 'curious', weight: 1, fallback: 'thinking' },
    laugh_closed: { preset: 'laugh_closed', weight: 0.85, fallback: 'excited' },
};

export function resolveEmotion(id, vrm) {
    const emotion = EMOTION_TO_VRM[id] || EMOTION_TO_VRM.neutral;
    if (!emotion.fallback || vrm.expressionManager.getExpression(emotion.preset)) return emotion;
    return EMOTION_TO_VRM[emotion.fallback] || EMOTION_TO_VRM.neutral;
}
