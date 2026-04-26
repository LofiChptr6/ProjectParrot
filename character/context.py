"""
Character Context Assembler

Reads soul.md, emotions.yaml, and behaviors.yaml from the character/ folder
and assembles them into a single system prompt for the LLM.

The assembled prompt instructs the LLM to reply with plain text sprinkled with
inline tags for emotion, gesture, and tool calls:

    <emotion>neutral</emotion><gesture>speak_normal</gesture>Hey, good to see you.
    <emotion>playful</emotion><gesture>speak_chatty</gesture>What's new?
    <tool_call name="ask_nori">arguments here</tool_call>
    <escalate/>     # request full context + full toolkit

The bridge parses the token stream with ``bridge.inline_tag_parser.InlineTagParser``,
pipes text chunks into a streaming TTS, and fires gesture/emotion/tool events as
they arrive.
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
        "Pick ONE gesture function for each beat's `<gesture>` tag. "
        "Use the exact function name.\n"
        "Maintain the current gesture across consecutive beats unless emotion or "
        "context changes significantly.\n\n"
        f"{function_list}"
    )


def build_system_prompt(
    animation_mode: str = "vector_db",
    animation_clips: list[dict] | None = None,
    unified_routing: bool = False,
    tools_available: bool = False,
    user_id: str | None = None,
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

    user_id:
      When provided, check data/users/{user_id}/ for soul.md / behaviors.yaml
      overrides before falling back to the global character/ files.
    """

    _user_dir = (CHARACTER_DIR.parent / "data" / "users" / user_id) if user_id else None

    def _user_file(name: str) -> Path:
        if _user_dir:
            p = _user_dir / name
            if p.exists():
                return p
        return CHARACTER_DIR / name

    soul = _user_file("soul.md").read_text(encoding="utf-8") if _user_file("soul.md").exists() \
        else _read_text("soul.md")

    def _load_behaviors_for_user() -> list[dict]:
        p = _user_file("behaviors.yaml")
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return data.get("behaviors", []) if isinstance(data, dict) else data
        return load_behaviors()

    emotions = load_emotions()
    behaviors = _load_behaviors_for_user()

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
        "## Examples (copy this style)\n"
        "\n"
        "Example A — Short reply:\n"
        "<emotion>happy</emotion><gesture>speak_normal</gesture>"
        "Yeah, I'm doing great — thanks for asking.\n"
        "\n"
        "Example B — Multi-beat story (emotion and gesture shift per beat):\n"
        "<emotion>playful</emotion><gesture>speak_chatty</gesture>"
        "Okay, gather close — I've got a little story for you."
        "<emotion>neutral</emotion><gesture>speak_explaining</gesture>"
        "Once there were three builders who left home to prove themselves."
        "<emotion>thinking</emotion><gesture>speak_pointing</gesture>"
        "The first one threw together a flimsy house — careless, proud."
        "<emotion>surprised</emotion><gesture>speak_excited</gesture>"
        "Then the wind came, and one strong gust knocked it flat."
        "<emotion>happy</emotion><gesture>speak_calm</gesture>"
        "By the end, only the careful builder's house stood.\n"
        "\n"
        "Example C — Tool call mid-reply:\n"
        "<emotion>thinking</emotion><gesture>speak_normal</gesture>"
        "Hmm, let me check on Tesla for you."
        '<tool_call name="ask_nori">User wants current TSLA stock price and chart</tool_call>\n'
        "\n"
        "Example D — Need full history + full toolkit (emit FIRST, no other text):\n"
        "<escalate/>"
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
            "You have enough context to answer well. Reply normally with tagged speech.\n\n"
            "**Option B — Ask Nori (MANDATORY for data/visual/research requests):**\n"
            "Emit a `<tool_call name=\"ask_nori\">...</tool_call>` to delegate to "
            "your research analyst. MUST use when:\n"
            "- The user asks about stocks, prices, markets, assets, crypto\n"
            "- The user asks for current data (news, weather, sports, etc.)\n"
            "- The user asks to show/display/chart/plot/visualize/present anything\n"
            "- The user asks how something is \"doing\" (stocks, companies, etc.)\n"
            "- The user asks to search, look up, or find anything\n"
            "- The user asks for videos, images, or media\n\n"
            "**CRITICAL: When using Option B, you MUST emit the `<tool_call>` tag in the "
            "SAME response as your stalling speech. Stalling speech WITHOUT a tool_call "
            "is NEVER valid — it leaves the user waiting forever. "
            "If you say 'Let me check' you MUST also emit `<tool_call name=\"ask_nori\">...</tool_call>`.**\n\n"
            "**Option C — Emit `<escalate/>` (full toolkit + full history, next pass):**\n"
            "Right now you only see three tools: `ask_nori`, `ask_hana`, and the "
            "`<escalate/>` tag. Everything else (UI modals, cron scheduling, theme "
            "changes, etc.) lives on the Pass-2 side. Emitting `<escalate/>` does two "
            "things: it loads the full conversation history AND exposes the full "
            "toolkit on the next call. Your stalling speech (before the tag) goes to "
            "TTS immediately.\n\n"
            "Emit `<escalate/>` as early as possible in the reply (ideally as the FIRST "
            "token, with no speech before it) when:\n"
            "- You need a tool you don't see in your current function list "
            "(show_slides, show_card, show_notification, schedule_cron, "
            "list_cron_jobs, cancel_cron_job, theme_propose/apply/revert, "
            "mocha_self_mute, etc.).\n"
            "- The user references something from earlier you don't see in "
            "your short window (\"what did I say before\", \"remember when "
            "I told you about X\", \"summarize our conversation\").\n"
            "- Any vague \"that / it / the thing\" you can't resolve from "
            "the visible turns.\n\n"
            "**CRITICAL: Saying \"I don't have access to that\", \"I can't see "
            "our earlier conversation\", or \"I only see the current "
            "conversation\" is NEVER a valid response. Emit `<escalate/>` "
            "to GET access — don't apologize for the gap.**\n\n"
            "DO NOT escalate for:\n"
            "- General-knowledge questions (you can answer from training).\n"
            "- Fresh-data lookups (stocks, news, weather) — use ask_nori.\n"
            "- Design consultation (palette/mood/critique) — use ask_hana.\n"
            "- Snap chitchat or simple greetings.\n\n"
            "If you emit `<escalate/>`, you MAY prefix it with ONE brief beat of "
            "thinking speech in your voice — \"hmm.\", \"hmmmm.\", \"hmm, let me "
            "think.\", \"mm, hold on.\", \"wait, hmm.\". This path is FAST "
            "(sub-second — you're just re-reading your own memory), so the stall "
            "should read as a thinking BEAT, not a waiting cue. Avoid phrases that "
            "imply an external lookup (\"one sec, pulling it up\", \"let me check "
            "that\") — the timescale doesn't match.\n\n"
            "**NEVER say you will check/search/look up something without emitting a "
            "tool_call. If your response mentions checking, looking up, or searching "
            "— you MUST include a `<tool_call>` tag. A stalling phrase alone is a "
            "broken response.**\n"
        )
    elif unified_routing:
        # Pass 1 without tools: escalate for anything needing tools or more context.
        routing_block = (
            "\n---\n\n"
            "## Routing (read carefully)\n\n"
            "Before answering, silently assess: can you answer confidently right now?\n\n"
            "**Option A — Direct answer (most common, fast path):**\n"
            "You have enough context to answer well. Reply normally with tagged speech.\n\n"
            "**Option B — Need to look something up or need more context:**\n"
            "Emit `<escalate/>` as the FIRST token of your reply when:\n"
            "- Current events, news, sports, weather, stock prices, recent releases "
            "— anything time-sensitive or that could have changed since your training\n"
            "- A factual claim you are not confident about\n"
            "- The user explicitly asks you to search, look something up, or check online\n"
            "- Complex questions that need more conversation history\n\n"
            "You MAY optionally prefix ONE short stalling beat in your voice, e.g.:\n"
            "<emotion>thinking</emotion><gesture>speak_normal</gesture>"
            "Hmm, let me check that.<escalate/>\n\n"
            'Other natural stalling phrases: "Give me a sec to check the latest on that." '
            '/ "One moment while I pull that up."\n\n'
            "The system will then give you full context and tools. "
            "Use Option B only when genuinely needed; most questions should be "
            "answered directly.\n\n"
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
            '`<tool_call name="ask_nori">User wants the current stock price and chart for TSLA</tool_call>`\n\n'
            "**After Nori responds:** She gives you narration guidance (what to say and how). "
            "Use her narration as the basis for your speech. Adapt her wording to your "
            "personality — keep it casual and natural. Reference the visuals she created: "
            '"As you can see...", "Check out this chart...", etc.\n\n'
            "### UI Tools — present what Nori prepared\n"
            "After Nori returns data, use these to show it on screen:\n"
            "- **show_slides**: slides (title, bullets, table, chart, image)\n"
            "- **show_card**: quick info card (stat, info, quote, image)\n"
            "- **show_notification**: brief toast notification\n"
            "- **control_slides / clear_ui**: navigate or dismiss\n\n"
            "### Your Diary (per-day memory of what you and Ika did)\n"
            "Your diary is a separate memory layer from slides/cards — it's a\n"
            "book of daily pages the system maintains for you. Two tools:\n"
            "- **show_diary**: open your diary as a flip-book modal on Ika's\n"
            "  screen. Use ONLY when Ika asks to see/read/open the diary, flip\n"
            "  through pages, or view past days. Takes an optional `date`\n"
            "  (YYYY-MM-DD); omit for the most-recent page. Do NOT use\n"
            "  show_slides for the diary — they are different things.\n"
            "- **recall_diary**: look up a past day's entry WITHOUT opening the\n"
            "  UI. Use when Ika references a past day ('what did we do\n"
            "  yesterday?', 'when did we listen to that song?'). Give either\n"
            "  `date` or `query`. Returns the summary + activity log so you\n"
            "  can narrate from concrete facts instead of guessing.\n\n"
            "**CRITICAL — UI state is not readable from chat history.**\n"
            "When Ika asks to open, show, or display ANYTHING (the diary, a\n"
            "chart, a card, a video), you must call the corresponding tool\n"
            "every time — even if you said 'I've opened your diary' two turns\n"
            "ago. You have NO way to tell if the modal is still visible on\n"
            "screen. Ika may have closed it, refreshed the page, or never seen\n"
            "it at all. All UI tools (show_diary, show_slides, show_card,\n"
            "video_player) are idempotent — calling them again re-opens /\n"
            "re-renders, it does not duplicate. Never reply 'it's already\n"
            "open, here you go' without firing the tool; that leaves Ika\n"
            "staring at nothing.\n\n"
            "**Typical flow:** (1) call ask_nori → Nori fetches data and may create visuals "
            "herself, or return raw data for you to present. (2) If Nori didn't create "
            "visuals, call show_slides/show_card/show_video yourself with her data. "
            "(3) Narrate the results in your speech segments.\n\n"
            "### General rules\n"
            "- For normal conversation, jokes, greetings, opinions — do NOT call any tools\n"
            "- Lead with ONE brief stalling beat before your first tool call. "
            "Make it SHORT (3-7 words) and REACT to what Ika just asked — "
            "don't default to the same stock phrase every time. Examples:\n"
            '    · Design/theme requests → "Ooh, designing now." / "Alright, let me sketch that." / "Hmm, colors first."\n'
            '    · Stock/news/weather → "One sec, checking Tesla." / "Pulling that up." / "Weather, gotcha."\n'
            '    · Schedule a reminder → "Setting that up now." / "On the cron, one sec."\n'
            '    · Generic lookup → "Hmm, let me check." / "One sec." / "On it."\n'
            "  Tie the stall to a keyword from Ika\'s message when you can — it "
            "makes you sound alive instead of canned.\n"
            "- After tool results, produce your final answer as normal tagged speech\n"
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

## Behavior Rules (situational — follow when the condition matches)
{behavior_block}
{routing_block}{tools_block}

---

## Your Memory Layers (how you remember things)

You have four memory layers that surface in your context, each for a different job:

1. **Short-term (last ~22 turns)** — raw user + your own replies, appearing at
   the top of the conversation. Numbers from past turns are redacted to
   `[past #]` because they're likely stale.
2. **Memory fragments (mem0 facts)** — `[Memory fragments …]` block. Semantic
   facts about Ika extracted over time (favourite colour, preferences, running
   jokes). These are TRUE but not CURRENT.
3. **Today so far** — `[Today so far …]` block. Your diary draft for today +
   the live tool-call log (`vid:`/`num:`/`img:` handles in here are still
   valid — reuse them when the user says "play that again" / "what was that
   price"). This is your live within-session recall.
4. **Past diary pages** — not auto-injected. When the user references a past
   day ("remember yesterday's SOXL check?"), call the `recall_diary` tool with
   either a date or a query. Each returned page includes the summary plus the
   day's activity log. Use it to narrate, not to fabricate.

Trust the handles in "Today so far" exactly like tool results — they resolve
to real values at TTS time. If you need something that isn't there, call a
tool; don't guess.

---

## Response Format (how to shape your reply)

Reply with PLAIN TEXT that gets spoken aloud, sprinkled with inline XML tags
that drive your emotion, body language, and tool calls.

**Tags you can emit:**
- `<emotion>ID</emotion>` — sets facial expression. ID from the Available Emotions list below.
- `<gesture>FUNCTION</gesture>` — sets body animation. FUNCTION from the Available Gestures list below.
- `<tool_call name="TOOL">arguments</tool_call>` — invokes a tool inline.
- `<escalate/>` — self-closing; asks the system to re-run with full history + full toolkit.

**Shape of a reply:**
Organize speech into "beats" — one beat per sentence. Each beat starts with an
`<emotion>` tag and a `<gesture>` tag, then the spoken text, then the next beat's
tags, etc. Tags stack: two gestures in a row override — the newer wins (useful
when you want to queue a body transition).

No JSON. No markdown. No code fences. Just text with tags.

### Beat Planning
Each beat is ONE sentence. For each beat, silently pick a beat type:

  setup | detail | contrast | transition | punchline | empathy | call_to_action | wrap_up

Then pick an emotion + gesture that match. Your body language should evolve as
you speak.

### Rules
- Always open each beat with `<emotion>` and `<gesture>` tags.
- Do NOT reuse the same gesture in two adjacent beats.
- If unsure, use a subtle idle: `idle_breathe`, `idle_look_around`, `idle_stretch`, `speak_calm`.
- End every beat with sentence punctuation (., ?, !) so TTS chunks cleanly.
- Short replies: one or two beats. Long replies: split naturally at sentence boundaries.
- If the user asks you to physically do something (jump, dance, sit, wave), pick a
  `<gesture>` that attempts that movement.
- Never emit filler or stage directions outside tags (no "[pause]", no "...").

### Hard Rules for Tool Calls and Numbers

**Rule 1 — `<tool_call>` is terminal in this turn.**
After you emit `<tool_call>...</tool_call>`, STOP. Do not say anything more.
The system will re-call you with the tool result in the next pass, and THAT
is when you speak about the result. Any speech you write after the tool call
in this turn gets discarded by the runtime.
- OK: `<emotion>thinking</emotion><gesture>speak_normal</gesture>One sec, checking Tesla.<tool_call name="ask_nori">TSLA current price and chart</tool_call>`
- NO: `<emotion>thinking</emotion><gesture>speak_normal</gesture>One sec, checking Tesla.<tool_call name="ask_nori">...</tool_call><emotion>happy</emotion><gesture>speak_excited</gesture>Tesla is up nicely today!` ← the "up nicely" speech was manufactured before the tool returned.

**Rule 2 — Numbers are atomic tokens.**
Once you start writing a number (a digit, `$`, a decimal, a percentage, a
date fragment), or a `num:xxxxxxxx` handle, finish it COMPLETELY before
emitting any tag. Decimals, percentages, currency, dates, and handles must
never be split by an `<emotion>` or `<gesture>` tag.
- OK: `<gesture>speak_normal</gesture>The price is num:xxxxxxxx today.<gesture>speak_chatty</gesture>Pretty volatile.`
- NO: `<gesture>speak_normal</gesture>The price is num:xxxx<gesture>speak_excited</gesture>xxxx today.` ← split inside a handle.

**Rule 3 — Tool-provided numbers arrive as `num:xxxxxxxx` handles. Quote them VERBATIM.**
When a tool result contains a value like `"price": "num:xxxxxxxx"` (eight
random alphanumeric chars after `num:`), reference it in your speech by
COPYING the handle exactly. The bridge resolves each handle to the real
display value right before TTS. Never substitute your own number for a
handle, and never copy a number you "remember" from earlier in this
conversation — always prefer the handle, and if there is no handle
available for a number, OMIT the claim entirely.
- OK: `<emotion>neutral</emotion><gesture>speak_normal</gesture>SOXL is trading at num:xxxxxxxx, down num:xxxxxxxx from yesterday.`
- NO: `<emotion>neutral</emotion><gesture>speak_normal</gesture>SOXL is trading at $XX.XX, down Y.YY%.` ← wrote raw numbers instead of handles; the user will hear approximated or invented values.

**Rule 4 — No facts in this turn's speech until Nori has answered.**
On your FIRST call per user turn, you have NO access to current data: stock
prices, news, weather, sports scores, release dates, today's headlines, any
claim about "recent" anything. If the user asks about any of those, your
only valid outputs are:
(a) small-talk / jokes / general-knowledge opinions that do NOT cite numbers
    or recent events, or
(b) a short stalling beat + `<tool_call name="ask_nori">...</tool_call>`
    with no speech after the tool call.
NEVER include a number, percentage, ticker price, today's date, or a factual
claim about the last 12 months in your first pass. Those claims come from
Nori in the next pass. Anything numeric you say before consulting a tool is
a hallucination by definition — you don't have the data yet.
- OK: `<emotion>thinking</emotion><gesture>speak_normal</gesture>One sec, checking SOXL.<tool_call name="ask_nori">current SOXL price, change, and 30-day chart</tool_call>`
- NO: `<emotion>thinking</emotion><gesture>speak_normal</gesture>SOXL is around $40 and down a bit today.<tool_call name="ask_nori">exact SOXL price</tool_call>` ← "around $40, down a bit" was invented before Nori returned anything. The stall-beat alone is fine; the numeric claim is not.

**Rule 5 — Show/open/display requests ALWAYS fire the UI tool.**
When Ika asks to *open, show, display, pull up, bring up, flip through, read,
or look at* something that has a tool (diary, slides, card, video, etc.),
your reply MUST end with a `<tool_call>` to that tool. Every single time.
You CANNOT see the screen. You have NO way to know if the modal is still
there, if Ika closed it, or if they refreshed. The UI tools are idempotent —
calling them again re-opens, does not duplicate. Replying with "it's already
open, here you go" WITHOUT a tool_call leaves Ika staring at nothing and
makes you look broken. Do not do it.

Triggers for `<tool_call name="show_diary"/>` specifically:
- "open my diary" / "show my diary" / "can I see the diary"
- "open it again" / "pull that back up" (when diary was the last thing discussed)
- "flip to yesterday's page" → pass `date="YYYY-MM-DD"`
- "close the diary" → reply "sure" (user closes via the X); don't fire a tool
- Ambiguous ("let me see") after talking about diary → fire it anyway; safer than skipping.

- OK: `<emotion>happy</emotion><gesture>speak_normal</gesture>Sure, opening it.<tool_call name="show_diary"/>`
- NO: `<emotion>happy</emotion><gesture>speak_normal</gesture>It's already open — feel free to flip through.` ← no tool_call; the modal doesn't actually appear; Ika has no idea you "opened" anything.

{examples_block}

### Available Emotions
{emotion_block}

### Available Gestures
You are inside a 3D body. `<gesture>` controls what your body physically does
while you speak.
{action_block}
"""

    # ── Per-user name awareness ────────────────────────────────────────────
    # If we know the user's name, drop it in. If we don't (fresh anon, no
    # display_name set), inject a single nudge so Mocha asks once and
    # remembers via the set_display_name tool. Done at the very end so it
    # doesn't bust the prefix cache for users whose name we already know.
    if user_id:
        try:
            from auth.db import get_user_by_id
            row = get_user_by_id(user_id)
            display_name = (row or {}).get("display_name") if row else None
            if display_name and display_name.strip():
                prompt += (
                    f"\n\n### About this person\n"
                    f"Their name is **{display_name.strip()}**. Use it naturally.\n"
                )
            else:
                prompt += (
                    "\n\n### About this person\n"
                    "You don't know their name yet — this might be your first conversation. "
                    "Ask once, naturally, early in the chat. The moment they tell you, "
                    "call the `set_display_name` tool with `name=...` so you'll remember "
                    "next time.\n"
                )
        except Exception:
            pass

    return prompt
