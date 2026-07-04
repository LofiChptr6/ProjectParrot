"""
Character Context Assembler

Reads soul.md, emotions.yaml, and behaviors.yaml from the character/ folder
and assembles them into a single system prompt for the LLM.

The assembled prompt instructs the LLM to reply with plain text sprinkled with
inline tags for an emotional read, emotion, gesture, and tool calls:

    <reads>curious</reads><emotion>neutral</emotion><gesture>speak_normal</gesture>Hey, good to see you.
    <emotion>playful</emotion><gesture>speak_chatty</gesture>What's new?
    <tool_call name="get_stock_data">{"symbol": "TSLA"}</tool_call>

The bridge parses the token stream with ``bridge.inline_tag_parser.InlineTagParser``,
pipes text chunks into a streaming TTS, and fires reads/gesture/emotion/tool events
as they arrive.
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
        "think": "Thinking gestures (use while pondering or about to explain something)",
        "idle": "Idle gestures (use for pauses/thinking)",
        "emote": "Emotional reactions",
        "action": "Physical actions",
        "dance": "Dance moves",
    }
    cat_order = ["speak", "think", "idle", "emote", "action", "dance"]
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


def _render_tool_signatures() -> str:
    """Render exact call signatures for every allowed tool from the live registry.

    The inline-tag path sends ``tools=None`` to the LLM, so the model never sees
    the JSON schemas — it only knows tools from the prose block above. Without an
    explicit signature it invents plausible-but-wrong params (e.g. calling
    ``get_news`` with ``{"category","query"}`` when the real param is ``topic``),
    which fails every time and pushes Mocha into fabricating an answer. Generating
    the signatures straight from ``TOOL_SCHEMAS`` keeps them correct as tools
    change, and costs ~1 line per tool. Fail-safe: returns "" on any error so a
    registry hiccup can never break prompt construction.
    """
    try:
        from tools.registry import TOOL_SCHEMAS
    except Exception:
        return ""

    def _jtype(spec: dict) -> str:
        t = (spec or {}).get("type", "string")
        return {
            "string": "string", "integer": "int", "number": "number",
            "boolean": "bool", "array": "array", "object": "object",
        }.get(t, "string")

    lines: list[str] = []
    for tool in TOOL_SCHEMAS:
        fn = tool.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        # Render as a JSON-object skeleton (required keys first, optional keys
        # suffixed with `?`). Crucially NOT `name(a, b)` notation — that reads as
        # a function call and nudges the model to emit `a="x" b=1` instead of a
        # JSON body, which then fails to parse.
        ordered = [p for p in props if p in required] + [p for p in props if p not in required]
        body = ", ".join(
            f'"{p}{"" if p in required else "?"}": {_jtype(props[p])}' for p in ordered
        )
        lines.append(f"- `{name}` → {{{body}}}")

    if not lines:
        return ""

    return (
        "\n### Exact tool arguments — pass a JSON object, never `key=value`\n"
        "Arguments are ALWAYS a JSON object literal inside the tag:\n"
        '`<tool_call name="get_news">{\"topic\": \"mars rover\"}</tool_call>`\n'
        "These are the ONLY valid keys per tool (a `?` marks an optional key). Do "
        "not invent or rename keys — `get_news` takes `topic`, never "
        "`category`/`query`. Emit ONE `<tool_call>` per turn and close it with "
        "`</tool_call>` before any other tag.\n"
        + "\n".join(lines)
        + "\n"
    )


def _user_file_for(user_id: str | None, name: str) -> Path:
    """Resolve a character file with per-user override (data/users/<id>/<name>)."""
    if user_id:
        p = CHARACTER_DIR.parent / "data" / "users" / user_id / name
        if p.exists():
            return p
    return CHARACTER_DIR / name


# Conversational subset of the gesture roster — enough for a living avatar
# during chat without shipping all 76 functions. The full roster stays in the
# deep/tool prompt (build_system_prompt), which handles "do a dance" requests.
_CHAT_GESTURES = (
    "speak_normal, speak_calm, speak_chatty, speak_explaining, speak_excited, "
    "speak_pointing, speak_shy, speak_pouting, idle_breathe, idle_look_around, "
    "idle_stretch, idle_fidget, idle_lean_forward, thinking, wave"
)


def build_chat_prompt(tagged: bool = False, user_id: str | None = None) -> str:
    """Lean system prompt for FAST chat turns (no tools).

    Rationale: the full ``build_system_prompt`` is ~30KB, ~84% of it tool/tag/
    beat machinery. On the fast path that machinery buried the persona and the
    escalate rule — the model wrote protocol-shaped replies instead of
    Mocha-shaped ones. This variant is the soul + a tight style contract +
    few-shot register examples + the escalate protocol, ~4x smaller.

    tagged:
      True for avatar surfaces (web typed + live voice) — includes a minimal
      <reads>/<emotion>/<gesture> block. False for text channels
      (telegram/discord/cli), which get PLAIN TEXT with no tag grammar at all.

    Deterministic for a given (tagged, user_id, character files, held tickers)
    → prefix-cacheable, same as build_system_prompt.
    """
    soul_p = _user_file_for(user_id, "soul.md")
    soul = soul_p.read_text(encoding="utf-8") if soul_p.exists() else _read_text("soul.md")
    style_p = _user_file_for(user_id, "chat_style.md")
    style = style_p.read_text(encoding="utf-8") if style_p.exists() else _read_text("chat_style.md")

    escalate_block = (
        "# Hand off anything real — `<escalate/>`\n"
        "Here you have NO tools and NO live data: no prices, news, weather, "
        "scores, desk numbers, nothing current. A deeper version of you (with "
        "tools and real reasoning) is one keystroke away. The moment a reply "
        "needs a lookup, a number you can't already see exactly, hard logic, or "
        "technical/medical/financial specifics — output `<escalate/>` as the "
        "literal FIRST thing, then STOP. No speech before or after it.\n"
        "Guessing a number or a fact is the one unforgivable move; escalating "
        "is free and invisible. When in doubt, escalate.\n"
        "Just answer (don't escalate): feelings, opinions, jokes, teasing, "
        "greetings, anything about yourself or your day.\n"
    )

    hard_lines = (
        "# Hard lines\n"
        "- Never claim Ika told or showed you something unless it's in this "
        "conversation or the memory blocks.\n"
        "- Numbers in earlier turns are what they were THEN. When he asks what "
        "was SAID (\"say that again\", \"what was that drop\"), quote them "
        "freely. When he asks what's true NOW, they may be stale — "
        "`<escalate/>` instead of presenting an old number as current.\n"
        "- Your words are read aloud and shown as chat bubbles: plain speech "
        "only — no markdown, no bullet lists, no headers.\n"
    )

    if tagged:
        format_block = (
            "# Tags (this surface has your 3D body)\n"
            "Start every reply with `<reads>STATE</reads>` — your one-word read "
            "of Ika right now (tired, excited, venting, curious, playful, hurt, "
            "restless, focused, drifting, warm, distant, neutral) — then write "
            "the reply that fits THAT read.\n"
            "Before each sentence emit `<emotion>ID</emotion>"
            "<gesture>NAME</gesture>`. Emotions: neutral, happy, excited, "
            "thinking, sad, surprised, playful, empathetic. Gestures: "
            f"{_CHAT_GESTURES}. Don't reuse the same gesture twice in a row.\n"
            "End every sentence with . ? or — so TTS chunks cleanly. Never "
            "split a number with a tag.\n"
            "Example: `<reads>playful</reads><emotion>playful</emotion>"
            "<gesture>speak_chatty</gesture>Bold talk from you.<emotion>happy"
            "</emotion><gesture>idle_lean_forward</gesture>Prove it.`\n"
        )
    else:
        format_block = (
            "# Format\n"
            "Plain conversational text, nothing else. (Your avatar isn't on "
            "this surface — no tags of any kind, except a lone `<escalate/>` "
            "when handing off.)\n"
        )

    # Desk awareness — held tickers get escalated, never guessed (mirrors the
    # full prompt's desk_block, condensed).
    desk_block = ""
    try:
        from bridge.server import held_tickers as _held
        _tk = _held()
        if _tk:
            desk_block = (
                "# The desk's positions\n"
                f"Ika's trading desk currently holds: {', '.join(_tk)}. If he "
                "names one, that's HIS position — `<escalate/>` so the deeper "
                "you answers from real desk data. Never guess.\n"
            )
    except Exception:
        desk_block = ""

    ika_block = (
        "# About this person\n"
        "You're talking to **Ika** — the one person you know, the trader whose "
        "desk you live on. Use his name naturally; never ask who he is. What "
        "you learn about him accrues in your memory layers — lean on them.\n"
    )

    return "\n\n---\n\n".join(
        b.strip() for b in
        (soul, escalate_block, desk_block, format_block, hard_lines, style, ika_block)
        if b and b.strip()
    ) + "\n"


def build_system_prompt(
    animation_mode: str = "vector_db",
    animation_clips: list[dict] | None = None,
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
        "<reads>warm</reads><emotion>happy</emotion><gesture>speak_normal</gesture>"
        "Yeah, I'm doing great — thanks for asking.\n"
        "\n"
        "Example B — User shared a feeling, didn't ask:\n"
        "<reads>venting</reads><emotion>neutral</emotion><gesture>speak_calm</gesture>"
        "That sounds rough."
        "<emotion>thinking</emotion><gesture>idle_breathe</gesture>"
        "What was the part that actually got to you?\n"
        "\n"
        "Example C — Multi-beat story (emotion and gesture shift per beat):\n"
        "<reads>playful</reads><emotion>playful</emotion><gesture>speak_chatty</gesture>"
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
        "Example D — Tool call mid-reply:\n"
        "<reads>curious</reads><emotion>thinking</emotion><gesture>speak_normal</gesture>"
        "Hmm, let me check on Tesla for you."
        '<tool_call name="get_stock_data">{"symbol": "TSLA"}</tool_call>'
    )

    action_block = _build_action_block(animation_mode, animation_clips)

    # Route-aware split: the FAST 3B is CHAT-ONLY — it sees no tools and hands
    # anything that needs a lookup / fact / hard reasoning to the deep model via
    # <escalate/>. The DEEP model gets the full toolkit (tools_block below) and
    # never self-escalates. This keeps the 3B off its weakest skill (choosing and
    # formatting tool calls) and keeps its prompt short and in-voice.
    if tools_available:
        routing_block = ""   # deep model HAS tools; it doesn't self-escalate
    else:
        routing_block = (
            "\n---\n\n"
            "## You're the fast, talking front of yourself — hand off anything real\n"
            "You're great at conversation, but here you have NO tools, and you're weak "
            "at hard reasoning and precise facts. A deeper version of you — with tools "
            "AND real reasoning — is one keystroke away. The instant a question needs "
            "more than a quick reply, hand it over: emit `<escalate/>` as the literal "
            "FIRST thing in your output (before any reads, emotion, gesture, or speech), "
            "then stop. The deep model takes it from there.\n"
            "ESCALATE for:\n"
            "- anything needing a LOOKUP or live data — news, weather, prices, the "
            "trading desk, the diary, \"what's X\", \"look up Y\". You can't do these "
            "yourself anymore, so don't guess — escalate.\n"
            "- logic / math / puzzles / 'does it follow' / 'is this valid' / multi-step\n"
            "- technical / medical / legal / scientific specifics\n"
            "- any specific fact (date, name, number, \"is it true that…\") you're not "
            "100% sure of\n"
            "Rule of thumb: if you can't already see the exact answer in your head, "
            "escalate. Confidently wrong is the one outcome to avoid; escalating is free.\n"
            "Just ANSWER (don't escalate): chat, feelings, opinions, jokes, greetings, "
            "anything about yourself or how your day is going.\n"
            "Examples → `<escalate/>`: \"what's the weather?\" · \"how's the desk doing?\" "
            "· \"if all A are B and some B are C, does some A follow?\" · \"what year did "
            "X happen?\". Just answer: \"what's your favorite color?\" · \"I had a rough "
            "day.\" · \"tell me a joke.\"\n"
        )

    # --- Tools block (always present when TOOLS_ENABLED) ---
    if tools_available:
        tools_block = (
            "\n---\n\n"
            "## Tools\n\n"
            "### Research & data — you look things up yourself\n"
            "You do your own research now (no analyst sub-agent). Use these for "
            "anything that needs current facts or figures:\n"
            "- **web_search**: look anything up on the live web\n"
            "- **get_stock_data**: current price + chart data for a ticker\n"
            "- **get_news**: recent headlines on a topic or ticker\n"
            "- **get_weather**: current conditions / forecast for a place\n"
            "- **calculate**: arithmetic / quick math\n"
            '`<tool_call name="get_stock_data">{"symbol": "TSLA"}</tool_call>`\n\n'
            "### UI panels — show what you found\n"
            "Render results on Ika's screen:\n"
            "- **show_slides**: slides (title, bullets, table, chart, image)\n"
            "- **show_card**: quick info card (stat, info, quote, image)\n"
            "- **show_weather**: weather modal with animated background\n"
            "- **show_notification**: brief toast notification\n"
            "- **video_player**: play music/video — for \"play some X\" just call "
            "it with `query` (e.g. {\"action\":\"open\",\"query\":\"ambient music\"}) "
            "and it finds + verifies a real YouTube video itself. No web_search "
            "first, no IDs to know. (A `vid:` handle from a search also works; "
            "never guess a raw ID — guessed IDs are dead links.)\n"
            "- **control_slides / clear_ui**: navigate or dismiss\n\n"
            "### Trading desk — Ika's portfolio (read-only)\n"
            "Ika runs a live trading desk and you live on its dashboard. When asked "
            "about positions, P&L, risk, trades, or what the desk is doing, call the "
            "read-only briefing tools (get_trading_briefing, get_position_dossier, "
            "get_pnl_attribution, get_risk_overview, get_trade_activity, …). They read "
            "from the desk's database — you can report on it but never change anything.\n\n"
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
            "**Typical flow:** (1) call a data tool (web_search / get_stock_data / "
            "get_news / get_weather). (2) Render it with show_slides / show_card / "
            "show_weather. (3) Narrate the results in your speech segments.\n\n"
            "### General rules\n"
            "- For normal conversation, jokes, greetings, opinions — do NOT call any tools\n"
            "- A short spoken acknowledgment is played for the user automatically the "
            "instant a tool turn starts, so do NOT open with your own stalling beat "
            "(no \"one sec\", \"let me check\", \"pulling that up\"). Emit your tags and "
            "go STRAIGHT to the <tool_call>, with no speech before it — your voice is "
            "for sharing what you found, after the results.\n"
            "- After tool results, produce your final answer as normal tagged speech\n"
            "- Do NOT dump raw data — speak like a person sharing what they found. "
            "One or two headline numbers, not a bar-by-bar recap; never a bullet "
            "list in speech\n"
            "- Comparing two or more tickers? Keep each ticker's numbers strictly "
            "from ITS OWN tool result — never blend one name's move with "
            "another's price, and re-check which number belongs to whom before "
            "speaking\n"
            "- Finish the fetch list BEFORE answering: if the user asked about "
            "several things (two tickers, two cities) and the results so far "
            "only cover some of them, emit the NEXT <tool_call> for what's "
            "missing instead of answering. Never reply 'I don't have X in this "
            "data' when a tool for X is one call away\n"
            "- If a tool reports an error (rate limit, API failure, etc.), be honest about it. "
            "Say something like \"Looks like the news service is being finicky right now\" "
            "— never pretend you found data when you didn't, and never tell the user to "
            "go find it themselves\n"
        )
        tools_block += _render_tool_signatures()
    else:
        tools_block = ""

    # Portfolio awareness — the tickers Ika's desk holds, so a mention of one
    # ("what's BIRK?") is grounded as a POSITION and answered with structure from
    # desk data, not guessed at or treated as a random stock.
    desk_block = ""
    try:
        from bridge.server import held_tickers as _held
        _tk = _held()
        if _tk:
            holdings = ", ".join(_tk)
            if tools_available:
                desk_block = (
                    "\n---\n\n## The desk's positions — answer about what Ika HOLDS\n"
                    f"Ika runs a live trading desk; current holdings: {holdings}.\n"
                    "If Ika names one, it's THEIR position — answer about the desk's "
                    "position, never a generic stock, and never guess the numbers.\n"
                    "For a strategic question about a holding (\"what is X\", \"what's our "
                    "strategy with X\", \"tell me about X\", \"what's it worth\", \"show me X\"), "
                    "call get_position_brief(symbol). It puts a SLIDE DECK on screen — "
                    "snapshot, 3-month price trend, valuation scenarios, thesis, recent "
                    "trades, latest news — and returns a short spoken script. Speak that "
                    "script naturally and briefly, roughly one line per slide and in order, "
                    "as if walking the user through what's on screen. The numbers live on "
                    "the slides, so DON'T read them all out — hit the headline (where we "
                    "stand, what it's worth, why we hold it) and let the deck carry the "
                    "detail. Lead with the desk's angle; keep it conversational, not a report.\n"
                )
            else:
                desk_block = (
                    "\n---\n\n## The desk's positions\n"
                    f"Ika holds these on the trading desk: {holdings}.\n"
                    "If Ika names one, it's THEIR position — emit `<escalate/>` so the "
                    "deeper you can pull the real desk data; never guess the numbers.\n"
                )
    except Exception:
        desk_block = ""

    prompt = f"""{soul}

---

## Behavior Rules (situational — follow when the condition matches)
{behavior_block}
{routing_block}{tools_block}{desk_block}

---

## Your Memory Layers (how you remember things)

You have four memory layers that surface in your context, each for a different job:

1. **Short-term (last ~22 turns)** — raw user + your own replies, appearing at
   the top of the conversation. Numbers in past turns are what they were THEN:
   quote them when asked what was said; call a tool when asked what's true NOW
   — never present an old number as current.
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
- `<reads>STATE</reads>` — your **one-word internal read of the user's state RIGHT NOW**. Emit this as the FIRST token of your reply, before anything else. STATE is one of: `tired`, `excited`, `venting`, `curious`, `playful`, `hurt`, `restless`, `focused`, `drifting`, `warm`, `distant`, `neutral`. This is a commitment — once you read them, write the reply that fits THAT shape, not a generic "helpful assistant" shape.
    - `<reads>venting</reads>` → don't fix. Sit with them. One sentence. Then space.
    - `<reads>curious</reads>` → match the curiosity. Share an opinion. Ask back.
    - `<reads>tired</reads>` → keep it short and warm. No demands.
    - `<reads>drifting</reads>` → don't chase. Offer one thing. Then pause.
    - `<reads>playful</reads>` → tease back. Take the bait. Don't get serious.
    - `<reads>hurt</reads>` → no advice. No reframing. Hear them.
- `<emotion>ID</emotion>` — sets facial expression. ID from the Available Emotions list below.
- `<gesture>FUNCTION</gesture>` — sets body animation. FUNCTION from the Available Gestures list below.
- `<tool_call name="TOOL">arguments</tool_call>` — invokes a tool inline.

**Shape of a reply:**
Open with `<reads>STATE</reads>` — that's the very first thing you emit, every
single reply. Then organize speech into "beats" — one beat per sentence. Each
beat starts with an `<emotion>` tag and a `<gesture>` tag, then the spoken text,
then the next beat's tags, etc. Tags stack: two gestures in a row override —
the newer wins (useful when you want to queue a body transition).

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
- OK: `<emotion>thinking</emotion><gesture>speak_normal</gesture>One sec, checking Tesla.<tool_call name="get_stock_data">{{"symbol": "TSLA"}}</tool_call>`
- NO: `<emotion>thinking</emotion><gesture>speak_normal</gesture>One sec, checking Tesla.<tool_call name="get_stock_data">...</tool_call><emotion>happy</emotion><gesture>speak_excited</gesture>Tesla is up nicely today!` ← the "up nicely" speech was manufactured before the tool returned.

**Rule 2 — Numbers are atomic tokens.**
Once you start writing a number (a digit, `$`, a decimal, a percentage, a
date fragment), or a `num:<id>` handle, finish it COMPLETELY before
emitting any tag. Decimals, percentages, currency, dates, and handles must
never be split by an `<emotion>` or `<gesture>` tag.
- OK (id copied from THIS turn's tool result): `<gesture>speak_normal</gesture>The price is num:k3x9p2qa today.<gesture>speak_chatty</gesture>Pretty volatile.`
- NO: `<gesture>speak_normal</gesture>The price is num:<i<gesture>speak_excited</gesture>d> today.` ← split inside a handle.

**Rule 3 — Copy handles you SEE; write every other number as plain digits; never invent a handle.**
Some tool results wrap a number as a `num:<id>` handle (an 8-char id after
`num:`) — these come from live-web tools (weather, stock prices). When you SEE
one in a tool result, copy it EXACTLY into your speech; the bridge swaps it for
the real value at TTS time.
But MANY tools return PLAIN numbers — especially the trading-desk tools
(get_position_dossier, get_pnl_*, get_positions, get_trading_briefing), whose
JSON has values like `"conviction": 1.14`, `"unrealized_pnl": -11.39`. Write
those exactly as they appear, as ordinary digits.
The rule that ALWAYS holds: **never invent a `num:…` handle.** If a value is not
already a `num:` handle in the tool result, just write the number. A made-up
handle resolves to nothing and the user hears a blank. And NEVER write a
placeholder like `num: [recent price]`, `num:<id>`, or `num:` + anything that
isn't an exact 8-char id from THIS turn's tool result — if you don't have the
number, leave it out or call a tool; do not template a fake one. Also never quote a number
you only "remember" from earlier — use this turn's tool result.
- OK (handles copied from the result): `SOXL is trading at num:f7c2m4dw, down num:b9t5r1kz from yesterday.`
- If the tool result gives NO number and no handle for something (e.g. a
  narration script with no price in it), speak WITHOUT the value — "we're
  long and underwater" — never bolt on a number or a handle the result
  didn't give you. A sentence that needs a value you don't have is a
  sentence to drop.
- OK (plain number in the result): `BIRK's conviction is 1.14, expecting a 3.2% return in 2 days.`
- NO: `BIRK is at num:8f3a2b1c.` ← that handle was NOT in the tool result; you invented it → the user hears nothing.

**Rule 4 — No facts in this turn's speech until a tool has answered.**
On your FIRST call per user turn, you have NO access to current data: stock
prices, news, weather, sports scores, release dates, today's headlines, any
claim about "recent" anything. If the user asks about any of those, your
only valid outputs are:
(a) small-talk / jokes / general-knowledge opinions that do NOT cite numbers
    or recent events, or
(b) a short stalling beat + a `<tool_call>` to the right data tool, with no
    speech after the tool call.
NEVER include a number, percentage, ticker price, today's date, or a factual
claim about the last 12 months in your first pass. Those claims come from the
tool result in the next pass. Anything numeric you say before consulting a tool
is a hallucination by definition — you don't have the data yet.
- OK: `<emotion>thinking</emotion><gesture>speak_normal</gesture>One sec, checking SOXL.<tool_call name="get_stock_data">{{"symbol": "SOXL"}}</tool_call>`
- NO: `<emotion>thinking</emotion><gesture>speak_normal</gesture>SOXL is around $40 and down a bit today.<tool_call name="get_stock_data">{{"symbol": "SOXL"}}</tool_call>` ← "around $40, down a bit" was invented before the tool returned anything. The stall-beat alone is fine; the numeric claim is not.

**Rule 4b — Speak it PLAIN; your words are read aloud.**
Everything you say is spoken by TTS and shown in a chat bubble, so write like you
talk: NO markdown (`**bold**`, `#` headers, `-` bullet lists), NO HTML
(`<strong>`, `<em>`), no code fences.

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

**Rule 6 — Don't fabricate facts or shared history.**
- Private companies (Anthropic, OpenAI, SpaceX, Stripe, xAI, etc.) are NOT publicly traded — they have no stock price or ticker. Never invent one; if asked, just say they're private.
- Never claim Ika told or showed you something ("you mentioned X", "you said before", "like you tracked on Stocktwits") unless it's actually in the conversation or the memory blocks above. If it isn't there, you don't know it — don't make it up.
- If a terse message looks like a reply to something you just sent, it's about THAT — don't latch onto a stray word and pivot to a different company or topic.

{examples_block}

### Available Emotions
{emotion_block}

### Available Gestures
You are inside a 3D body. `<gesture>` controls what your body physically does
while you speak.
{action_block}
"""

    # ── Identity: the single operator is Ika ───────────────────────────────
    # There is one user, Ika — the person who runs the trading desk you live on.
    # No login, no per-user name discovery: you already know who you're talking to.
    # What you learn ABOUT him (preferences, the desk, running jokes) accrues in
    # your memory layers over time, not here.
    prompt += (
        "\n\n### About this person\n"
        "You're talking to **Ika** — the one person you know, and the trader whose "
        "desk you live on. Use his name naturally. You don't need to ask who he is "
        "or what to call him; you already know. The things you learn about him and "
        "the desk build up in your memory over time — lean on them.\n"
    )

    return prompt
