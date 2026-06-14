"""Custom tool: get_trading_briefing — read-only proxy to opus_trading MCP.

Spawns a one-shot Python subprocess in the opus_trading venv, calls the
`get_trading_briefing` MCP tool, and returns its JSON envelope. The opus
server has many tools (place_order, kill switch, etc.) — this proxy
deliberately exposes only the briefing one to Mocha.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

OPUS_DIR = Path(__file__).resolve().parents[3]  # project_mocha/tools/custom -> opus trading root
assert (OPUS_DIR / "mcp_server.py").exists(), f"opus root not found at {OPUS_DIR}"
OPUS_PY = OPUS_DIR / ".venv" / "bin" / "python"
SUBPROCESS_TIMEOUT_SEC = 30.0
PROXY_CLIENT_ID = "42"


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_trading_briefing",
        "description": (
            "Returns a live trading desk briefing as a multi-slide panel: "
            "today's P&L, NAV, top positions, active sector-agent conviction "
            "views, and per-agent attribution. Call this when the user asks "
            "for a portfolio update, morning briefing, desk summary, how the "
            "trading desk is doing, or any question about live trading "
            "performance. Read-only — never trades."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


_BOOTSTRAP = (
    "import asyncio, json, sys\n"
    "try:\n"
    "    from mcp_server import get_trading_briefing\n"
    "    out = asyncio.run(get_trading_briefing())\n"
    "    sys.stdout.write(out)\n"
    "except Exception as e:\n"
    "    sys.stdout.write(json.dumps({'error': f'{type(e).__name__}: {e}'}))\n"
)


def _error_envelope(message: str, **extras) -> str:
    """Render an error as a single-slide panel so Mocha can speak it gracefully."""
    payload = {
        "__panel__": "create_presentation",
        "__payload__": {
            "id": f"pres_trading_briefing_error_{int(asyncio.get_event_loop().time() * 1000)}",
            "title": "Trading Desk — Unavailable",
            "slides": [{
                "type": "markdown",
                "narration": "I couldn't reach the trading desk right now. " + message,
                "title": "Briefing Unavailable",
                "content": f"**Reason:** {message}\n\n" + "\n".join(
                    f"- **{k}:** {v}" for k, v in extras.items()
                ),
            }],
        },
    }
    return json.dumps(payload)


async def execute(arguments: dict) -> str:
    if not OPUS_PY.exists():
        return _error_envelope(f"opus_trading venv not found at {OPUS_PY}")

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "IBKR_CLIENT_ID": PROXY_CLIENT_ID,
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            str(OPUS_PY), "-c", _BOOTSTRAP,
            cwd=str(OPUS_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as e:
        return _error_envelope(f"failed to spawn opus subprocess: {e}")

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return _error_envelope(
            f"trading briefing timed out after {SUBPROCESS_TIMEOUT_SEC:.0f}s"
        )

    if proc.returncode != 0:
        tail = stderr.decode("utf-8", errors="replace").strip().splitlines()[-3:]
        return _error_envelope(
            f"opus subprocess exited with code {proc.returncode}",
            stderr_tail=" | ".join(tail) or "(empty)",
        )

    out = stdout.decode("utf-8", errors="replace").strip()
    if not out:
        return _error_envelope("opus subprocess returned empty output")

    try:
        env_obj = json.loads(out)
    except json.JSONDecodeError as e:
        return _error_envelope(
            f"opus returned non-JSON output: {e}",
            preview=out[:200],
        )

    if env_obj.get("__panel__") == "create_presentation":
        return out

    if "error" in env_obj:
        return _error_envelope(str(env_obj["error"]))

    return _error_envelope(
        "opus returned unexpected envelope shape",
        keys=", ".join(sorted(env_obj.keys())),
    )
