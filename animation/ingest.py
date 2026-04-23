"""
Animation Ingest — Parse clip names from unityactions.txt and generate
human-readable descriptions for vector embedding.
"""

import re
from pathlib import Path

CHARACTER_DIR = Path(__file__).resolve().parent.parent / "character"

# Semantic keywords added per category to improve embedding quality
_CATEGORY_KEYWORDS = {
    "idle": "idle standing pose expression gesture",
    "speak": "speaking talking conversation voice",
    "combat": "combat fighting attack battle",
    "walk": "walking locomotion movement",
    "run": "running locomotion fast movement",
    "jump": "jumping airborne leap",
    "sit": "sitting seated chair rest",
    "sleep": "sleeping lying resting",
    "fly": "flying airborne floating",
    "swim": "swimming water aquatic",
    "skip": "skipping hopping playful locomotion",
    "crawl": "crawling ground baby",
    "dash": "dashing quick burst movement",
    "turn": "turning rotation direction",
    "death": "death defeated fallen",
    "lantern": "lantern carrying light sneaking",
    "wall": "wall leaning hiding stealth",
    "stop": "stopping halting sudden",
}

_PHASE_TAGS = {"start", "loop", "end", "entry", "exit", "pivot", "stop"}

_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _split_camel(s: str) -> str:
    return _CAMEL_RE.sub(" ", s)


def _classify(tokens: list[str]) -> str:
    low = [t.lower() for t in tokens]
    for cat in _CATEGORY_KEYWORDS:
        if any(cat in t for t in low):
            return cat
    return "misc"


def _is_conversational(category: str, tokens: list[str]) -> bool:
    return category in {"idle", "speak", "sit"}


def describe_clip(clip_name: str) -> dict:
    """Turn a clip name like 'KA_Idle16_WaveHands' into a searchable record."""
    raw = clip_name.lstrip("@").strip()
    parts = raw.split("_")

    if parts and parts[0] == "KA":
        parts = parts[1:]

    category = _classify(parts)
    phase = None
    clean = []
    for p in parts:
        if p.lower() in _PHASE_TAGS:
            phase = p.lower()
        else:
            clean.append(p)

    human_parts = [_split_camel(p) for p in clean]
    description = " ".join(human_parts).strip()

    extra = _CATEGORY_KEYWORDS.get(category, "")
    full_description = f"{description}. {extra}".strip()

    return {
        "clip": raw,
        "description": full_description,
        "category": category,
        "phase": phase,
        "conversational": _is_conversational(category, parts),
    }


def parse_actions_file(path: str | Path | None = None) -> list[dict]:
    """Read unityactions.txt and return a list of clip records."""
    if path is None:
        path = CHARACTER_DIR / "unityactions.txt"
    path = Path(path)
    if not path.exists():
        return []

    clips = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or not line.startswith("@"):
            continue
        name = line.lstrip("@").strip()
        if name in seen:
            continue
        seen.add(name)
        clips.append(describe_clip(line))
    return clips
