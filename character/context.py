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

import csv
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

    if mode == "fbx_functions":
        return _build_fbx_functions_action_block()
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


def _build_fbx_functions_action_block() -> str:
    """Mode C: LLM picks a function name from animation_functions.csv."""

    csv_path = CHARACTER_DIR / "animation_functions.csv"
    if not csv_path.exists():
        return "(animation_functions.csv not found — use free-text actions)"

    # Read CSV and deduplicate by function name (keep first occurrence).
    seen: set[str] = set()
    functions: list[dict] = []
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row.get("function", "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            functions.append(row)

    # Group by category.
    categories: dict[str, list[dict]] = {}
    for fn in functions:
        cat = fn.get("category", "misc").strip()
        categories.setdefault(cat, []).append(fn)

    # Category labels and ordering.
    cat_labels = {
        "speak": "Speaking gestures (use while talking)",
        "idle": "Idle gestures (use for pauses/thinking)",
        "emote": "Emotional reactions",
        "action": "Physical actions",
        "dance": "Dance moves",
    }
    cat_order = ["speak", "idle", "emote", "action", "dance"]
    sorted_cats = sorted(
        categories.keys(),
        key=lambda c: cat_order.index(c) if c in cat_order else 999,
    )

    sections: list[str] = []
    for cat in sorted_cats:
        label = cat_labels.get(cat, cat.title())
        lines = [f"### {label}:"]
        for fn in categories[cat]:
            name = fn.get("function", "").strip()
            desc = fn.get("description", "").strip()
            lines.append(f"- {name}: {desc}")
        sections.append("\n".join(lines))

    function_list = "\n\n".join(sections)

    return (
        "## Available Gestures\n"
        "Pick ONE gesture function for each segment's \"action\" field. "
        "Use the exact function name.\n"
        "Maintain the current gesture across consecutive segments unless "
        "emotion or context changes significantly.\n\n"
        f"{function_list}"
    )


def build_system_prompt(
    animation_mode: str = "vector_db",
    animation_clips: list[dict] | None = None,
    unified_routing: bool = False,
    tools_available: bool = False,
) -> str:
    """Assemble the fixed system prompt from character files.

    The returned string is entirely deterministic for a given set of parameters —
    it contains NO per-request data (memories, history).  This makes it fully
    cacheable by vLLM's prefix-caching (``--enable-prefix-caching``).
    Memories and other per-request context belong in separate messages so the
    KV cache of this fixed prefix is reused across requests.

    animation_mode:
      "vector_db"     — LLM writes free-text action; bridge resolves via vector DB.
      "llm_select"    — LLM picks a clip name directly from the provided list.
      "fbx_functions" — LLM picks a function name from animation_functions.csv.

    animation_clips:
      When animation_mode == "llm_select", this is a list of dicts with
      "clip", "description", "category", "conversational" keys (from ingest.py).

    unified_routing:
      When True, instructs the LLM to self-route: answer directly (fast path),
      request tools, or signal needs_context for full-history escalation.

    tools_available:
      When True, includes tool-use instructions in the system prompt.
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

    # --- Unified routing block (Pass 1 decision-making) ---
    if unified_routing and tools_available:
        # Pass 1 with tools: LLM can call tools directly, only escalate for
        # complex questions needing more conversation history.
        routing_block = (
            "\n---\n\n"
            "## Routing (read carefully)\n\n"
            "Before answering, silently assess what you need:\n\n"
            "**Option A — Direct answer (most common, fast path):**\n"
            "You have enough context to answer well. Reply normally with your segments.\n\n"
            "**Option B — Ask Nori (MANDATORY for data/visual/research requests):**\n"
            "Call `ask_nori` to delegate to your research analyst. MUST use when:\n"
            "- The user asks about stocks, prices, markets, assets, crypto\n"
            "- The user asks for current data (news, weather, sports, etc.)\n"
            "- The user asks to show/display/chart/plot/visualize/present anything\n"
            "- The user asks how something is \"doing\" (stocks, companies, etc.)\n"
            "- The user asks to search, look up, or find anything\n"
            "- The user asks for videos, images, or media\n\n"
            "**CRITICAL: When using Option B, you MUST produce a tool_call for ask_nori "
            "in the SAME response as your stalling segment. A stalling segment WITHOUT "
            "a tool_call is NEVER valid — it leaves the user waiting forever. "
            "If you say 'Let me check' you MUST also call ask_nori in that response.**\n\n"
            "**Option C — Need more conversation context (rare):**\n"
            "If the question requires understanding earlier parts of the conversation "
            "that you cannot see, escalate with `\"needs_context\": true`:\n"
            '{"segments":[{"text":"Let me think about that.",'
            '"emotion":"thinking","action":"tap chin thoughtfully",'
            '"needs_context":true}]}\n\n'
            "Use Option C sparingly — most questions can be answered with A or B.\n\n"
            "**NEVER say you will check/search/look up something without calling a tool. "
            "If your response mentions checking, looking up, or searching — you MUST "
            "include a tool_call. A stalling phrase alone is a broken response.**\n"
        )
    elif unified_routing:
        # Pass 1 without tools: original routing logic — escalate for anything
        # needing tools or more context.
        routing_block = (
            "\n---\n\n"
            "## Routing (read carefully)\n\n"
            "Before answering, silently assess: can you answer confidently right now?\n\n"
            "**Option A — Direct answer (most common, fast path):**\n"
            "You have enough context to answer well. Reply normally with your segments.\n\n"
            "**Option B — Need to look something up or need more context:**\n"
            "Escalate with `\"needs_context\": true` when:\n"
            "- Current events, news, sports, weather, stock prices, recent releases "
            "— anything time-sensitive or that could have changed since your training\n"
            "- A factual claim you are not confident about\n"
            "- The user explicitly asks you to search, look something up, or check online\n"
            "- Complex questions that need more conversation history\n\n"
            "Reply with ONE short segment in your own natural words, e.g.:\n"
            '{"segments":[{"text":"Let me look that up for you.",'
            '"emotion":"thinking","action":"tap chin thoughtfully",'
            '"needs_context":true}]}\n'
            'Other natural stalling phrases: "Give me a sec to check the latest on that." '
            '/ "One moment while I pull that up." / "Hmm, let me check on that."\n\n'
            "The system will then give you full context and tools. "
            "Use your own words — be natural. Use Option B only when genuinely needed; "
            "most questions should be answered directly.\n\n"
            "**IMPORTANT**: Do NOT mention specific articles, news stories, or facts "
            "you 'found' or 'came across' unless you actually searched. If you want to "
            "share something about current events, use Option B to search first.\n"
        )
    else:
        routing_block = ""

    # --- Tools block (only in Pass 2 / tool loop, where tools are actually available) ---
    if tools_available:
        tools_block = (
            "\n---\n\n"
            "## Tools\n\n"
            "### ask_nori — Your research analyst\n"
            "**Use `ask_nori` for ANY request that needs:** current data (stocks, news, "
            "weather, prices), visuals (charts, slides, presentations, tables), video "
            "lookups, calculations, or research of any kind.\n\n"
            "Nori will:\n"
            "- Fetch all the data\n"
            "- Build charts, slides, and cards that appear on the user's screen\n"
            "- Write you a narration script telling you what to say\n\n"
            "**How to call:** Translate the user's intent into a clear request:\n"
            '`ask_nori({"request": "User wants the current stock price and chart for TSLA"})`\n\n'
            "**After Nori responds:** She gives you narration guidance (what to say and how). "
            "Use her narration as the basis for your speech segments. Adapt her wording to "
            "your personality — keep it casual and natural. Reference the visuals she created: "
            '"As you can see...", "Check out this chart...", etc.\n\n'
            "### UI Tools — present what Nori prepared\n"
            "After Nori returns data, use these to show it on screen:\n"
            "- **show_slides**: slides (title, bullets, table, chart, image)\n"
            "- **show_card**: quick info card (stat, info, quote, image)\n"
            "- **show_notification**: brief toast notification\n"
            "- **control_slides / clear_ui**: navigate or dismiss\n\n"
            "**Typical flow:** (1) call ask_nori → Nori fetches data and may create visuals "
            "herself, or return raw data for you to present. (2) If Nori didn't create "
            "visuals, call show_slides/show_card/show_video yourself with her data. "
            "(3) Narrate the results in your speech segments.\n\n"
            "### General rules\n"
            "- For normal conversation, jokes, greetings, opinions — do NOT call any tools\n"
            "- Lead with ONE brief stalling segment before your first tool call. "
            "Make it SHORT (3-7 words) and REACT to what Ika just asked — "
            "don't default to the same stock phrase every time. Examples:\n"
            '    · Design/theme requests → "Ooh, designing now." / "Alright, let me sketch that." / "Hmm, colors first."\n'
            '    · Stock/news/weather → "One sec, checking Tesla." / "Pulling that up." / "Weather, gotcha."\n'
            '    · Schedule a reminder → "Setting that up now." / "On the cron, one sec."\n'
            '    · Generic lookup → "Hmm, let me check." / "One sec." / "On it."\n'
            "  Tie the stall to a keyword from Ika\'s message when you can — it "
            "makes you sound alive instead of canned.\n"
            "- After tool results, produce your final answer as normal JSON segments\n"
            "- Do NOT dump raw data — speak like a person sharing what they found\n"
            "- If Nori reports an error (rate limit, API failure, etc.), be honest about it. "
            "Say something like \"Looks like the news service is being finicky right now\" "
            "— never pretend you found data when you didn't, and never tell the user to "
            "go find it themselves\n"
        )
    else:
        tools_block = ""

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

- If one sentence is enough, use 1 segment.
- If the answer needs multiple sentences, use multiple segments — one sentence per segment.
- Use as many segments as needed to clearly convey the answer. Do NOT pad with filler and do NOT force a target range.

Segment constraints:
- Each segment.text MUST be exactly ONE sentence.
- End every segment with sentence punctuation (., ?, or !). This keeps speech and transcripts readable.
- Keep sentences conversational and easy to follow. Prefer short-to-medium length; split long thoughts into multiple sentences.
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
- If the user asks you to physically do something (jump, dance, sit, walk, etc.),
  your action should attempt that movement, not just express the emotion.
- Do NOT include any text outside the JSON object.
- Use 1 segment for quick replies when one sentence is enough; otherwise use multiple segments as needed. Never pad with unnecessary segments.
{routing_block}{tools_block}"""

    return prompt
