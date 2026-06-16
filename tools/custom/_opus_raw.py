"""Shared raw-text proxy + helpers for opus_trading MCP tools that return prose.

The generic _opus_proxy.call_opus() runs json.loads() on every tool result, so
it rejects tools that return markdown (e.g. get_ticker_valuation's ~16 KB
Damodaran report). This module returns raw stdout instead, and provides the
valuation distiller shared by get_ticker_valuation.py and get_position_brief.py.

Module name starts with `_` so the custom-tool loader skips it; the tool files
import it with `from tools.custom._opus_raw import …`. All calls are READ-ONLY.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

OPUS_DIR = Path(__file__).resolve().parents[3]  # project_mocha/tools/custom -> opus trading root
assert (OPUS_DIR / "mcp_server.py").exists(), f"opus root not found at {OPUS_DIR}"
OPUS_PY = OPUS_DIR / ".venv" / "bin" / "python"
DEFAULT_TIMEOUT_SEC = 30.0
PROXY_CLIENT_ID = "42"
MAX_SUMMARY_CHARS = 1500

# Reads args from stdin as JSON so free-text params never get f-string injected
# into Python source. Only the tool NAME is formatted in (validated below).
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

_TOOL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\Z")


async def call_opus_raw(tool: str, arguments: dict | None = None,
                        timeout: float = DEFAULT_TIMEOUT_SEC) -> tuple[bool, str]:
    """Call an opus MCP tool in a one-shot subprocess; return ``(ok, text)``.

    On success ``text`` is the tool's raw stdout (may be markdown prose — we do
    NOT json.loads it). On failure ``ok`` is False and ``text`` is a short human
    error string. Read-only — only ever used to wrap read-only opus tools.
    """
    if not _TOOL_NAME_RE.match(tool or ""):
        return False, f"invalid tool name: {tool!r}"
    if not OPUS_PY.exists():
        return False, f"opus_trading venv not found at {OPUS_PY}"

    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "IBKR_CLIENT_ID": PROXY_CLIENT_ID}
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
        return False, f"failed to spawn opus subprocess: {e}"

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(arguments or {}).encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"{tool} timed out after {timeout:.0f}s"

    if proc.returncode != 0:
        tail = stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        return False, f"{tool} exited {proc.returncode}: {' | '.join(tail) or '(empty)'}"

    out = stdout.decode("utf-8", "replace").strip()
    if not out:
        return False, f"{tool} returned empty output"

    # The bootstrap emits {"error": "..."} on exception; real reports are prose
    # (or non-error JSON), so only a leading "{" with an "error" key is a failure.
    if out.startswith("{"):
        try:
            obj = json.loads(out)
            if isinstance(obj, dict) and "error" in obj and len(obj) <= 2:
                return False, str(obj["error"])
        except json.JSONDecodeError:
            pass
    return True, out


def round_decimals(text: str) -> str:
    """Round any 3+ decimal-place number to 2dp (the desk's 2dp house rule)."""
    def repl(m: re.Match) -> str:
        return f"{round(float(m.group(0)), 2):.2f}".rstrip("0").rstrip(".")
    return re.sub(r"\d+\.\d{3,}", repl, text)


def summarize_valuation(symbol: str, text: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """Distill a full ticker-valuation report down to a compact, speakable verdict."""
    low = text.lower()
    if "no damodaran-style valuation on file" in low or "no report" in low:
        return text.strip()

    model, asof = "", ""
    mm = re.search(r"model:\s*(\S+)", text)
    if mm:
        model = mm.group(1).strip()
    md = re.search(r"generated_at:\s*(\d{4}-\d{2}-\d{2})", text)
    if md:
        asof = md.group(1)

    body = ""
    mt = re.search(r"##\s*TL;DR[^\n]*\n(.*)", text, re.IGNORECASE | re.DOTALL)
    if mt:
        body = mt.group(1).strip()
    else:
        mv = re.search(r"##[^\n]*Verdict[^\n]*\n(.*?)(?=\n##\s|\Z)", text,
                       re.IGNORECASE | re.DOTALL)
        if mv:
            body = mv.group(1).strip()
    if not body:
        body = text[-900:].strip()

    body = round_decimals(body)
    if len(body) > max_chars:
        body = body[:max_chars].rsplit("\n", 1)[0].rstrip() + " …"

    prov_bits = [b for b in (model, f"as of {asof}" if asof else "") if b]
    prov = f" ({', '.join(prov_bits)})" if prov_bits else ""
    return f"Desk valuation for {symbol}{prov}:\n\n{body}"
