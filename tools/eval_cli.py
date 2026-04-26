"""
CLI wrapper around POST /admin/eval. Feeds a prompt to Mocha's real pipeline
in isolation and pretty-prints the response.

Usage:
    python -m tools.eval_cli run "cawboy bebop ost"
    python -m tools.eval_cli run "who am I?" --mode quick
    python -m tools.eval_cli run "who am I?" \\
        --history-json '[{"role":"user","content":"I am Ika"}]'
    python -m tools.eval_cli run "hihi" --json        # raw JSON, for pipes

Exit code 0 on success, 1 if the response carries any errors.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


DEFAULT_URL = "http://127.0.0.1:8000/admin/eval"


def _pretty(resp: dict) -> None:
    status = resp.get("status", "?")
    print(f"=== Eval {resp.get('eval_id', '?')[:8]}  [{status}]  "
          f"conv={resp.get('conversation_id')}  "
          f"mode={resp.get('mode')}  "
          f"total={resp.get('total_latency_ms', 0):.0f}ms ===")
    print(f"prompt: {resp.get('prompt', '')!r}")

    segs = resp.get("segments") or []
    print(f"\n-- response ({len(segs)} segment{'s' if len(segs) != 1 else ''}) --")
    for s in segs:
        emo = s.get("emotion") or "-"
        act = s.get("action") or "-"
        print(f"  [{emo:<10}|{act:<12}] {s.get('text', '')}")
    full = resp.get("full_text") or ""
    if full and len(segs) > 1:
        print(f"  joined: {full}")

    tcs = resp.get("tool_calls") or []
    print(f"\n-- tool_calls ({len(tcs)}) --")
    for t in tcs:
        ok = "ok " if t.get("ok") else "ERR"
        args = json.dumps(t.get("arguments_raw") or {}, ensure_ascii=False)
        if len(args) > 180:
            args = args[:180] + "…"
        print(f"  [r{t.get('round', '?')}] {ok} {t.get('name')}"
              f"  ({t.get('latency_ms', 0):.0f}ms, {t.get('result_bytes', 0)}B)")
        print(f"         args: {args}")
        res = (t.get("result_preview") or "").replace("\n", " ")
        if len(res) > 180:
            res = res[:180] + "…"
        print(f"         result: {res}")

    handles = resp.get("handles_issued") or []
    if handles:
        print(f"\n-- handles_issued ({len(handles)}) --")
        for h in handles:
            print(f"  {h}")

    llm_calls = resp.get("llm_calls") or []
    print(f"\n-- llm_calls ({len(llm_calls)}) --")
    for c in llm_calls:
        tb = c.get("triggered_by") or "?"
        tr = c.get("tool_round")
        tr_s = f"r{tr}" if tr else "-"
        print(f"  [{tb:<16} {tr_s:<3}]  "
              f"finish={c.get('finish_reason')}  "
              f"tokens={c.get('prompt_tokens')}→{c.get('completion_tokens')}  "
              f"latency={c.get('latency_ms', 0):.0f}ms  "
              f"has_tool_call={c.get('has_tool_call')}"
              + (f"  error={c.get('error')}" if c.get("error") else ""))

    errs = resp.get("errors") or []
    if errs:
        print(f"\n-- errors ({len(errs)}) --")
        for e in errs:
            print(f"  {e}")


def cmd_run(args: argparse.Namespace) -> int:
    history = None
    if args.history_json:
        try:
            history = json.loads(args.history_json)
        except json.JSONDecodeError as exc:
            print(f"--history-json is not valid JSON: {exc}", file=sys.stderr)
            return 2

    body = {
        "text": args.text,
        "mode": args.mode,
        "include_memory": args.include_memory,
    }
    if history is not None:
        body["history"] = history

    try:
        with httpx.Client(timeout=args.timeout) as client:
            resp = client.post(args.url, json=body)
    except httpx.HTTPError as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 2

    if resp.status_code >= 400:
        print(f"HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        return 2

    data = resp.json()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        _pretty(data)

    return 1 if data.get("errors") else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eval_cli", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Run a prompt through /admin/eval")
    r.add_argument("text", help="The user prompt to send to Mocha")
    r.add_argument("--mode", choices=("tool_loop", "quick"), default="tool_loop")
    r.add_argument("--history-json", default=None,
                   help="Prime the context: JSON list of {role, content} messages")
    r.add_argument("--include-memory", action="store_true",
                   help="Include ChromaDB memory query (off by default for determinism)")
    r.add_argument("--url", default=DEFAULT_URL)
    r.add_argument("--timeout", type=float, default=180.0)
    r.add_argument("--json", action="store_true", help="Print raw JSON (for piping)")
    r.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
