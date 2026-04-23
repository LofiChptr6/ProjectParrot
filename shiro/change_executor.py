"""Shiro change executor — applies approved changes to soul.md, behaviors.yaml, and tools."""

from __future__ import annotations

import logging
import re
import textwrap
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("shiro.executor")

ROOT = Path(__file__).resolve().parent.parent
SOUL_PATH = ROOT / "character" / "soul.md"
BEHAVIORS_PATH = ROOT / "character" / "behaviors.yaml"
CUSTOM_TOOLS_DIR = ROOT / "tools" / "custom"


def execute_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply a list of approved changes.  Returns results with status per change."""
    results = []
    for change in changes:
        change_type = change.get("type", "")
        details = change.get("details", {})
        try:
            if change_type == "behavior_add":
                _add_behavior(details)
                results.append({"type": change_type, "status": "ok"})
            elif change_type == "behavior_modify":
                _modify_behavior(details)
                results.append({"type": change_type, "status": "ok"})
            elif change_type == "soul_edit":
                _edit_soul(details)
                results.append({"type": change_type, "status": "ok"})
            elif change_type == "new_tool":
                _create_tool(details)
                results.append({"type": change_type, "status": "ok"})
            else:
                results.append({"type": change_type, "status": "skipped",
                                "reason": f"unknown type: {change_type}"})
        except Exception as exc:
            log.error("Failed to execute %s change: %s", change_type, exc)
            results.append({"type": change_type, "status": "error", "reason": str(exc)})
    return results


# ---------------------------------------------------------------------------
#  behaviors.yaml
# ---------------------------------------------------------------------------

def _add_behavior(details: dict) -> None:
    """Append a new behavior entry to behaviors.yaml."""
    data = yaml.safe_load(BEHAVIORS_PATH.read_text()) or {}
    behaviors = data.get("behaviors", [])

    entry = {
        "condition": details["condition"],
        "emotion": details.get("emotion", "neutral"),
        "action": details.get("action", ""),
        "priority": details.get("priority", 5),
    }
    if details.get("speech"):
        entry["speech"] = details["speech"]

    behaviors.append(entry)
    data["behaviors"] = behaviors
    BEHAVIORS_PATH.write_text(yaml.safe_dump(data, default_flow_style=False, allow_unicode=True))
    log.info("Added behavior: %s", details["condition"][:60])


def _modify_behavior(details: dict) -> None:
    """Find a behavior by condition text and update specified fields."""
    match_cond = details.get("match_condition", "")
    new_fields = details.get("new_fields", {})
    if not match_cond or not new_fields:
        raise ValueError("behavior_modify requires match_condition and new_fields")

    data = yaml.safe_load(BEHAVIORS_PATH.read_text()) or {}
    behaviors = data.get("behaviors", [])

    found = False
    for b in behaviors:
        if b.get("condition", "").strip().lower() == match_cond.strip().lower():
            b.update(new_fields)
            found = True
            break

    if not found:
        raise ValueError(f"No behavior matching condition: {match_cond[:60]}")

    data["behaviors"] = behaviors
    BEHAVIORS_PATH.write_text(yaml.safe_dump(data, default_flow_style=False, allow_unicode=True))
    log.info("Modified behavior: %s", match_cond[:60])


# ---------------------------------------------------------------------------
#  soul.md
# ---------------------------------------------------------------------------

def _edit_soul(details: dict) -> None:
    """Append to or replace a section in soul.md."""
    section = details.get("section", "")
    action = details.get("action", "append")
    content = details.get("content", "")
    if not section or not content:
        raise ValueError("soul_edit requires section and content")

    text = SOUL_PATH.read_text()

    # Find the section heading
    pattern = re.compile(rf"^(## {re.escape(section)})\s*$", re.MULTILINE)
    match = pattern.search(text)

    if not match:
        # Section not found — append as new section at end
        text = text.rstrip() + f"\n\n## {section}\n{content}\n"
    elif action == "append":
        # Find end of section (next ## heading or EOF)
        next_heading = re.search(r"^## ", text[match.end():], re.MULTILINE)
        if next_heading:
            insert_pos = match.end() + next_heading.start()
        else:
            insert_pos = len(text)
        text = text[:insert_pos].rstrip() + f"\n{content}\n" + text[insert_pos:]
    elif action == "replace":
        next_heading = re.search(r"^## ", text[match.end():], re.MULTILINE)
        if next_heading:
            end_pos = match.end() + next_heading.start()
        else:
            end_pos = len(text)
        text = text[:match.end()] + f"\n{content}\n" + text[end_pos:]

    SOUL_PATH.write_text(text)
    log.info("Edited soul.md section '%s' (action=%s)", section, action)


# ---------------------------------------------------------------------------
#  Custom tools
# ---------------------------------------------------------------------------

def _create_tool(details: dict) -> None:
    """Write a new custom tool to tools/custom/ and trigger hot-reload."""
    name = details.get("name", "")
    description = details.get("description", "")
    parameters = details.get("parameters", {})
    hint = details.get("implementation_hint", "")

    if not name:
        raise ValueError("new_tool requires a name")

    # Sanitize name
    safe_name = re.sub(r"[^a-z0-9_]", "_", name.lower())

    # Build parameter schema
    props = {}
    required = []
    for param_name, param_desc in parameters.items():
        props[param_name] = {"type": "string", "description": str(param_desc)}
        required.append(param_name)

    tool_def = {
        "type": "function",
        "function": {
            "name": safe_name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }

    # Generate the Python file
    code = textwrap.dedent(f'''\
        """Custom tool: {safe_name} — created by Shiro."""

        TOOL_DEF = {repr(tool_def)}


        async def execute(arguments: dict) -> str:
            """Execute {safe_name}.

            Implementation hint: {hint}
            """
            # TODO: Implement based on hint above.
            # This is a placeholder — Shiro will refine the implementation
            # or it can be manually implemented.
            return f"Tool {safe_name} called with arguments: {{arguments}}"
    ''')

    CUSTOM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    tool_path = CUSTOM_TOOLS_DIR / f"{safe_name}.py"
    tool_path.write_text(code)
    log.info("Created custom tool: %s at %s", safe_name, tool_path)

    # Hot-reload
    try:
        from tools.registry import reload_custom_tools
        reload_custom_tools()
        log.info("Custom tools reloaded successfully")
    except Exception as exc:
        log.warning("Tool hot-reload failed (will load on next restart): %s", exc)
