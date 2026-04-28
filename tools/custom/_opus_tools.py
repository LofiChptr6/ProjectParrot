"""Auto-register opus_trading deep-dive tools from MCP introspection.

Replaces 10 hand-maintained TOOL_DEF files (get_position_dossier.py,
get_agent_overview.py, …) with a single dynamic registry. Schemas
follow whatever opus actually exposes — if opus renames a parameter
or adds a new field, we pick it up on the next bridge restart with
zero code changes here.

`get_trading_briefing` stays as its own hand-written file because
it returns a __panel__ envelope (auto-broadcast path), not raw data.

Module name starts with ``_`` so the loader skips the regular
"one TOOL_DEF per file" scan; ``__init__.py`` invokes ``register()``
explicitly.
"""

from __future__ import annotations

import logging

from tools.custom._opus_introspect import discover_opus_tools
from tools.custom._opus_proxy import call_opus

log = logging.getLogger("tools.custom._opus_tools")

# Tools introspection finds but that have bespoke proxy files in this
# directory. Skip them here so the hand-written file's TOOL_DEF wins.
_HANDWRITTEN_OVERRIDES: set[str] = {
    "get_trading_briefing",  # returns __panel__ envelope
}


def _make_executor(tool_name: str):
    """Build an async execute() that proxies to the named opus tool."""
    error_title = tool_name.replace("get_", "").replace("_", " ").title()

    async def _execute(arguments: dict) -> str:
        return await call_opus(tool_name, arguments, error_title)

    return _execute


def register() -> tuple[dict[str, dict], dict[str, callable]]:
    """Discover opus tools and return (definitions, executors) for the loader.

    Called by tools.custom.__init__.load_custom_tools() after the regular
    one-file-per-tool scan, so handwritten files take precedence on name
    collision.
    """
    definitions: dict[str, dict] = {}
    executors: dict[str, callable] = {}

    discovered = discover_opus_tools()
    for tool in discovered:
        name = tool.get("name")
        if not name or name in _HANDWRITTEN_OVERRIDES:
            continue
        description = tool.get("description") or f"Opus MCP tool {name}."
        parameters = tool.get("parameters") or {"type": "object", "properties": {}}
        # Ensure the OpenAI tool-call schema shape is well-formed.
        if "type" not in parameters:
            parameters["type"] = "object"
        if "properties" not in parameters:
            parameters["properties"] = {}

        definitions[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }
        executors[name] = _make_executor(name)

    log.info("Registered %d opus tools via MCP introspection", len(definitions))
    return definitions, executors
