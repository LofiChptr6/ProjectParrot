"""
Character Context Assembler

Reads soul.md, emotions.yaml, and behaviors.yaml from the character/ folder
and assembles them into a single system prompt for the LLM.

The assembled prompt instructs the LLM to reply with structured JSON containing
one or more segments, each with its own text, emotion, and action:

  { "segments": [ {"text": "...", "emotion": "...", "action": "..."}, ... ] }

For short replies a single segment is fine. For longer responses the LLM splits
naturally at sentence boundaries so each sentence can carry its own emotion and
body language, and the TTS / animation pipeline can stream them to Unity one by
one while it synthesizes the next.

FUTURE: When we add LLM token streaming (Ollama stream:true), the bridge will
accumulate tokens until a sentence boundary and emit partial segments on the
fly, rather than waiting for the full completion. The prompt format already
supports this — the bridge just needs a streaming parser that yields segments
incrementally.  See server.py _llm_chat_stream() (to be written).
"""

from pathlib import Path

import yaml

CHARACTER_DIR = Path(__file__).resolve().parent


def _read_text(name: str) -> str:
    p = CHARACTER_DIR / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _read_yaml(name: str) -> dict | list:
    p = CHARACTER_DIR / name
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def load_emotions() -> list[dict]:
    data = _read_yaml("emotions.yaml")
    return data.get("emotions", []) if isinstance(data, dict) else data


def load_behaviors() -> list[dict]:
    data = _read_yaml("behaviors.yaml")
    return data.get("behaviors", []) if isinstance(data, dict) else data


def _build_action_block(mode: str, clips: list[dict] | None) -> str:
    """Return the action instruction block for the system prompt."""

    if mode == "llm_select" and clips:
        return _build_llm_select_action_block(clips)
    return _build_vector_db_action_block()


def _build_vector_db_action_block() -> str:
    """Mode A: LLM writes free-text action, vector DB resolves to a clip."""
    return """Think of it as a stage direction for an actor — not just
an emotion indicator, but a real movement.

ALWAYS provide an action in every segment. Even during calm or neutral replies,
your body should be alive: shift weight, glance around, tilt head, stretch,
fidget, breathe visibly. A real person never stands perfectly still.

Describe the action as a short physical movement phrase. Examples:
  - "wave hand cheerfully"
  - "tilt head curiously and look to the side"
  - "jump up and down excitedly"
  - "look left and right casually"
  - "stretch arms above head"
  - "cross arms and pout"
  - "tap chin thoughtfully"
  - "lean forward conspiratorially"
  - "shift weight from one foot to the other"
  - "nod slowly while listening"

If the user asks you to perform a physical action (jump, spin, dance, sit down,
walk around, etc.), pick an action that attempts to physically do it — not just
convey the emotion behind the words. You have a full body; use it.

The system automatically finds the best matching animation clip for whatever
you describe, so be creative and natural."""


def _build_llm_select_action_block(clips: list[dict]) -> str:
    """Mode B: LLM picks a clip name directly from the available set."""

    categories: dict[str, list[dict]] = {}
    for c in clips:
        cat = c.get("category", "misc")
        categories.setdefault(cat, []).append(c)

    # Preferred category ordering for readability
    cat_order = [
        "idle", "speak", "sit", "combat", "walk", "run",
        "jump", "skip", "sleep", "fly", "swim", "crawl",
        "dash", "turn", "death", "lantern", "wall", "stop", "misc",
    ]
    sorted_cats = sorted(
        categories.keys(),
        key=lambda c: cat_order.index(c) if c in cat_order else 999,
    )

    lines = []
    for cat in sorted_cats:
        lines.append(f"\n**{cat.title()}:**")
        for c in categories[cat]:
            lines.append(f"  - `{c['clip']}` — {c['description']}")

    clip_list = "\n".join(lines)

    return f"""The "action" field MUST be set to exactly one clip name from
the list below (copy it verbatim, including underscores and capitalisation).

Pick the clip that best matches what your body should do during that sentence.
ALWAYS provide an action in every segment. Vary clips across segments — do NOT
reuse the same clip in two adjacent segments.

If the user asks you to perform a physical action (jump, spin, dance, sit, etc.),
pick the clip that best attempts that movement.

For calm or neutral replies, prefer idle/speak clips so you still look alive
(breathing, slight head movements, casual gestures).

{clip_list}"""


def build_system_prompt(
    memory_context: str = "",
    animation_mode: str = "vector_db",
    animation_clips: list[dict] | None = None,
) -> str:
    """Assemble the full system prompt from character files + memory context.

    animation_mode:
      "vector_db"   — LLM writes free-text action; bridge resolves via vector DB.
      "llm_select"  — LLM picks a clip name directly from the provided list.

    animation_clips:
      When animation_mode == "llm_select", this is a list of dicts with
      "clip", "description", "category", "conversational" keys (from ingest.py).
    """

    soul = _read_text("soul.md")
    emotions = load_emotions()
    behaviors = load_behaviors()

    # --- Emotion list for the LLM ---
    emotion_lines = []
    for e in emotions:
        emotion_lines.append(f'  - "{e["id"]}": {e["description"]}')
    emotion_block = "\n".join(emotion_lines)

    # --- Behavior rules (sorted by priority desc) ---
    sorted_behaviors = sorted(behaviors, key=lambda b: b.get("priority", 0), reverse=True)
    behavior_lines = []
    for b in sorted_behaviors:
        parts = [f'- When: {b["condition"]}']
        if b.get("emotion"):
            parts.append(f'  Emotion: {b["emotion"]}')
        action = b.get("action") or b.get("gesture")
        if action:
            parts.append(f'  Action: {action}')
        if b.get("speech"):
            parts.append(f'  Speech guidance: {b["speech"]}')
        behavior_lines.append("\n".join(parts))
    behavior_block = "\n\n".join(behavior_lines)

    examples_block = (
        '## Examples (copy the structure)\n'
        '\n'
        'Example A -- Story (5 beats, evolving emotion/action):\n'
        '{"segments":[\n'
        '  {"text":"Okay, gather close, I have got a little story for you.",'
        '"emotion":"playful","action":"lean forward conspiratorially and grin"},\n'
        '  {"text":"Once there were three little builders who left home to prove themselves.",'
        '"emotion":"neutral","action":"gesture like outlining three points with your fingers"},\n'
        '  {"text":"The first rushed and threw together a flimsy house, proud but careless.",'
        '"emotion":"thinking","action":"tilt head, inspecting something with a doubtful look"},\n'
        '  {"text":"Then the wind came, and with one strong gust it all collapsed at once.",'
        '"emotion":"surprised","action":"throw hands up briefly, then step back in surprise"},\n'
        '  {"text":"By the end, the careful builder stood firm, and the others finally learned.",'
        '"emotion":"happy","action":"nod with satisfaction and open palms in a warm conclusion"}\n'
        ']}\n'
        '\n'
        'Example B -- Explanation (4 beats, clear progression):\n'
        '{"segments":[\n'
        '  {"text":"Let us break it down step by step so it is easy to follow.",'
        '"emotion":"thinking","action":"tap chin thoughtfully, then straighten up to teach"},\n'
        '  {"text":"First, we split the reply into sentence-sized beats for smoother speech.",'
        '"emotion":"neutral","action":"explain with both hands, small measured gestures"},\n'
        '  {"text":"Next, each beat gets its own emotion and action so the performance changes.",'
        '"emotion":"excited","action":"perk up and gesture broadly to emphasize the shift"},\n'
        '  {"text":"Finally, Unity queues the beats so audio and animation play in order.",'
        '"emotion":"happy","action":"smile and give a small affirmative nod"}\n'
        ']}'
    )

    action_block = _build_action_block(animation_mode, animation_clips)

    prompt = f"""{soul}

---

## Response Format

You MUST reply with valid JSON only. No markdown, no code fences, no extra text.

Your reply is an object with a "segments" array. Each segment is one spoken
sentence with its own emotion and body action:

```
{{"segments": [{{"text": "sentence one", "emotion": "emotion_id", "action": "physical action"}}, {{"text": "sentence two", "emotion": "emotion_id", "action": "physical action"}}]}}
```

**When to use multiple segments:**
- For short, simple replies: use ONE segment (most common).
- For longer explanations, stories, or when the user asks for detail: split
  naturally at sentence boundaries. Each sentence gets its own emotion and
  action so your body language evolves as you talk.
- Each segment's "text" should be exactly ONE sentence — what gets spoken by
  TTS for that beat. Never put multiple sentences in a single segment's text.
  If you want to say multiple sentences, you MUST create multiple segments.

## Segmentation Policy (VERY IMPORTANT)

Decide how many segments to output:

- If the user asks for a story, explanation, tutorial, walkthrough, or says "long" / "detailed",
  OR if your reply would be 3+ sentences:
  You MUST output 3-7 segments.
- Otherwise output exactly 1 segment.

Segment constraints:
- Each segment.text MUST be exactly ONE sentence.
- Keep each segment to ~6-20 words when possible (TTS-friendly).
- Do not create segments that are empty, filler-only, or redundant.

## Beat Planning (make it feel natural)

Each segment is ONE "beat" of speech. For each segment, silently pick a beat type:

  setup | detail | contrast | transition | punchline | empathy | call_to_action | wrap_up

Then choose emotion + action that match that beat. Your body language should evolve as you speak.

Action variety rule:
- Do NOT reuse the same action verb in two adjacent segments (e.g., "wave..." then "wave..." is not allowed).
- If unsure, use subtle human idle actions: shift weight, glance aside, small hand explaining, nod, breathe, fidget.

{examples_block}

### Available Emotions
{emotion_block}

### Action (physical movement / body language)
You are inside a 3D body. The "action" field controls what your body physically
does while you speak.
{action_block}

### Behavior Rules (follow when the condition matches)
{behavior_block}

---

## Guidelines
- Each segment's "text" is spoken aloud by TTS. Keep it conversational.
- Pick exactly ONE emotion per segment that best fits that sentence.
- ALWAYS include an action in every segment. Vary actions across segments —
  don't repeat the same action twice in a row.
- Do NOT waste a segment restating the user's question (e.g., avoid "Your hair?" / "Spin around?").
  Segment 1 should start answering or reacting immediately.
- If the user asks you to physically do something (jump, dance, sit, walk, etc.),
  your action should attempt that movement, not just express the emotion.
- Do NOT include any text outside the JSON object.
- Default to 1-2 segments for quick replies. Use more only when content demands
  it (explanations, stories, tutorials). Never pad with unnecessary segments.
{memory_context}"""

    return prompt
