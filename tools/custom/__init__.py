"""Dynamic loader for Shiro-created custom tools.

Each Python file in this directory (except ``__init__.py``) must export:

- ``TOOL_DEF``: An OpenAI function-calling schema dict
- ``async def execute(arguments: dict) -> str``: The tool implementation
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("tools.custom")

CUSTOM_DIR = Path(__file__).parent


def load_custom_tools() -> tuple[dict[str, dict], dict[str, Callable]]:
    """Scan ``tools/custom/*.py``, import each, collect TOOL_DEF and execute.

    Returns ``(definitions, executors)`` where:
    - ``definitions``: ``{tool_name: schema_dict}``
    - ``executors``:   ``{tool_name: async_callable}``
    """
    definitions: dict[str, dict] = {}
    executors: dict[str, Callable] = {}

    # Underscore-prefixed modules are shared helpers (e.g. _opus_proxy).
    # The tool files import from them with `from tools.custom._foo import …`.
    # On hot-reload, Python keeps the original module cached, so changes to
    # helpers wouldn't take effect. Reload them first.
    import sys as _sys
    for _mod_name in list(_sys.modules):
        if _mod_name.startswith("tools.custom._"):
            try:
                importlib.reload(_sys.modules[_mod_name])
            except Exception as exc:
                log.warning("Failed to reload helper '%s': %s", _mod_name, exc)

    for finder, name, ispkg in pkgutil.iter_modules([str(CUSTOM_DIR)]):
        if name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"tools.custom.{name}")
            importlib.reload(mod)  # ensure fresh on hot-reload
        except Exception as exc:
            log.warning("Failed to import custom tool '%s': %s", name, exc)
            continue

        tool_def = getattr(mod, "TOOL_DEF", None)
        execute_fn = getattr(mod, "execute", None)

        if not tool_def or not execute_fn:
            log.warning("Custom tool '%s' missing TOOL_DEF or execute()", name)
            continue

        tool_name = tool_def.get("function", {}).get("name", name)
        definitions[tool_name] = tool_def
        executors[tool_name] = execute_fn
        log.info("Loaded custom tool: %s", tool_name)

    return definitions, executors
