"""
Tool schema definitions in Ollama function-calling format.

Each tool is a dict matching the Ollama ``tools`` parameter:
  { "type": "function", "function": { "name", "description", "parameters" } }

The bridge injects this list into the LLM request so the model can decide to
call tools before producing its final conversational answer.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
_tools_cfg = _cfg.get("tools", {})
_WORKING_DIR = _tools_cfg.get("working_dir", str(ROOT))

TOOL_DEFINITIONS: dict[str, dict] = {
    "bash_exec": {
        "type": "function",
        "function": {
            "name": "bash_exec",
            "description": (
                "Execute a bash command and return its stdout/stderr.  "
                "Use for system tasks: checking files, running scripts, git, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute.",
                    },
                    "working_dir": {
                        "type": "string",
                        "description": f"Working directory (default: {_WORKING_DIR}).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 30).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a file and return them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write (overwrite) content to a file, creating directories as needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    "git_status": {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Run `git status` in the working directory and return the output.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    "list_dir": {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path (default: working directory).",
                    },
                },
            },
        },
    },
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web using DuckDuckGo and return the top results. "
                "Use for: current events, news, sports scores, weather, stock prices, "
                "recent product releases, or any fact that may have changed since your "
                "training cutoff. NEVER simulate a search result — call this tool or "
                "admit you don't know."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 10).",
                    },
                },
                "required": ["query"],
            },
        },
    },
}


def get_allowed_tools(allowed: list[str] | None = None) -> list[dict]:
    """Return Ollama tool schemas filtered by the allowed list."""
    if allowed is None:
        allowed = _tools_cfg.get("allowed", list(TOOL_DEFINITIONS.keys()))
    return [TOOL_DEFINITIONS[name] for name in allowed if name in TOOL_DEFINITIONS]


TOOL_SCHEMAS = get_allowed_tools()
TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]


def reload_custom_tools() -> None:
    """Discover custom tools in ``tools/custom/`` and merge into the registry.

    Called by Shiro after writing a new tool file, or by the admin reload endpoint.
    """
    global TOOL_SCHEMAS, TOOL_NAMES
    from tools.custom import load_custom_tools
    custom_defs, custom_executors = load_custom_tools()
    TOOL_DEFINITIONS.update(custom_defs)
    # Push executors to the executor module
    from tools import executor as _exec
    _exec._CUSTOM_EXECUTORS.update(custom_executors)
    # Regenerate schemas — only include tools explicitly listed in config.
    # Custom tools not in the allowed list are still callable via execute_tool()
    # (e.g., Nori calls data/UI tools directly), just not in Mocha's LLM schema.
    allowed = _tools_cfg.get("allowed", list(TOOL_DEFINITIONS.keys()))
    TOOL_SCHEMAS = [TOOL_DEFINITIONS[n] for n in allowed if n in TOOL_DEFINITIONS]
    TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]
