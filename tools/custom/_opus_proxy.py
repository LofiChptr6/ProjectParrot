"""Shared subprocess proxy for opus_trading MCP tools.

Each opus tool file (get_position_dossier.py, get_pnl_attribution.py, …)
declares its own TOOL_DEF and calls `call_opus(tool_name, arguments,
error_title)` from this module. The opus server has many internal tools
(place_order, kill switch, etc.) — only the read-only ones we explicitly
wrap here are exposed to Mocha.

Module name starts with `_` so the custom-tool loader skips it.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

OPUS_DIR = Path(__file__).resolve().parents[3]  # project_mocha/tools/custom -> opus trading root
assert (OPUS_DIR / "mcp_server.py").exists(), f"opus root not found at {OPUS_DIR}"
OPUS_PY = OPUS_DIR / ".venv" / "bin" / "python"
SUBPROCESS_TIMEOUT_SEC = 30.0
PROXY_CLIENT_ID = "42"

# Bootstrap reads JSON args from stdin so we don't have to f-string-inject
# them into Python source (avoids quoting bugs on free-text params like
# timestamps and symbol names).
_BOOTSTRAP = (
    "import asyncio, json, sys\n"
    "try:\n"
    "    from mcp_server import {tool}\n"
    "    args = json.loads(sys.stdin.read() or '{{}}')\n"
    "    out = asyncio.run({tool}(**args))\n"
    "    sys.stdout.write(out if isinstance(out, str) else json.dumps(out))\n"
    "except Exception as e:\n"
    "    sys.stdout.write(json.dumps({{'error': f'{{type(e).__name__}}: {{e}}'}}))\n"
)


def _error_envelope(error_title: str, message: str, **extras) -> str:
    """Single-slide panel describing why the tool failed — Mocha narrates this gracefully."""
    payload = {
        "__panel__": "create_presentation",
        "__payload__": {
            "id": f"pres_opus_error_{int(time.time() * 1_000_000)}",
            "title": f"{error_title} — Unavailable",
            "slides": [{
                "type": "markdown",
                "narration": f"I couldn't reach the trading desk for that. {message}",
                "title": f"{error_title} Unavailable",
                "content": f"**Reason:** {message}\n\n" + "\n".join(
                    f"- **{k}:** {v}" for k, v in extras.items()
                ) if extras else f"**Reason:** {message}",
            }],
        },
    }
    return json.dumps(payload)


async def call_opus(tool: str, arguments: dict, error_title: str) -> str:
    """Invoke an opus_trading MCP tool in a one-shot subprocess and return its envelope.

    Always returns a JSON string with a __panel__ envelope, even on failure.
    Argument shapes are validated against opus's own schema (discovered via
    introspection in _opus_introspect), so we don't translate here — the LLM
    sees opus's own parameter values and passes them through verbatim.
    """
    if not OPUS_PY.exists():
        return _error_envelope(error_title, f"opus_trading venv not found at {OPUS_PY}")

    arguments = arguments or {}

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "IBKR_CLIENT_ID": PROXY_CLIENT_ID,
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            str(OPUS_PY), "-c", _BOOTSTRAP.format(tool=tool),
            cwd=str(OPUS_DIR),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as e:
        return _error_envelope(error_title, f"failed to spawn opus subprocess: {e}")

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(arguments or {}).encode("utf-8")),
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return _error_envelope(
            error_title,
            f"{tool} timed out after {SUBPROCESS_TIMEOUT_SEC:.0f}s",
        )

    if proc.returncode != 0:
        tail = stderr.decode("utf-8", errors="replace").strip().splitlines()[-3:]
        return _error_envelope(
            error_title,
            f"opus subprocess exited with code {proc.returncode}",
            stderr_tail=" | ".join(tail) or "(empty)",
        )

    out = stdout.decode("utf-8", errors="replace").strip()
    if not out:
        return _error_envelope(error_title, "opus subprocess returned empty output")

    try:
        env_obj = json.loads(out)
    except json.JSONDecodeError as e:
        return _error_envelope(
            error_title,
            f"opus returned non-JSON output: {e}",
            preview=out[:200],
        )

    # Two valid shapes from opus_trading:
    #   1. Pre-wrapped __panel__ envelope (e.g. get_trading_briefing) — the
    #      executor's __panel__ detection broadcasts this directly to the
    #      panel WS. Pass through verbatim.
    #   2. Raw analytical data + analyst_note (most deep-dive tools) — Nori
    #      receives the JSON string, reads analyst_note for the framing, and
    #      decides what slides to build via show_slides.
    if env_obj.get("__panel__") == "create_presentation":
        return out

    if "error" in env_obj and len(env_obj) <= 2:
        # Plain {"error": "..."} from the bootstrap means the MCP call itself
        # raised — render an error panel so Mocha can speak it.
        return _error_envelope(error_title, str(env_obj["error"]))

    # Raw data: hand it to Nori as-is. Strip any leading/trailing whitespace
    # so the LLM doesn't see junk.
    return out
