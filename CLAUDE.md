# ProjectMocha — Claude Code Instructions

Mocha is a real-time personal AI companion (3D VRM + voice + emotions +
gestures) whose job is running Ika's day: reminders, lookups, briefings, desk
read-outs, and knowing what's currently in motion in his life. Single agent —
**Mocha**. Warm, playful, direct; no affection scores, no relationship meters.

**STANDALONE since 2026-08-02** — split out of the opus trading tree. This repo
lives at `~/ProjectMocha` (own git repo, remote `LofiChptr6/ProjectParrot`).
The trading desk stays at `~/opus trading` and is reached read-only over MCP.

## Where it lives & how it runs

- **Services** (`./start.sh all`, or `systemctl --user start project-mocha`):
  bridge `:8090`, TTS `:8092`, web `:8080` (STT `:8091` disabled — text input).
- **GPU hold**: when `var/GPU_HOLD` exists (box GPU reserved, e.g. a training
  run), `start.sh` skips TTS and `project-mocha-vllm` is condition-blocked —
  Mocha runs **text-only** with all LLM turns on the remote deep lane. Delete
  the flag + restart to restore voice + local fast lane.
- **LLM lanes** (config.yaml `llm:`):
  - **Fast**: her own vLLM `Qwen/Qwen3-8B-FP8` at `127.0.0.1:8893/v1`
    (`project-mocha-vllm.service`, user unit, this box = HomePCBlackwell).
  - **Deep/thinking**: `deepseek-v3` at `https://llm.project-hello-mocha.com/v1`
    (sparks-cluster gateway behind Cloudflare tunnel `spark-llm`; bearer key
    from `.env` `SPARK_LLM_API_KEY` via `api_key_env`). 16k context — mind
    prompt budget on deep turns.
  - **Fallback**: `llm.fallback_to_deep: true` — when the fast lane is down
    (breaker open / health probe fails), fast turns, interpret, stall-rephrase,
    diary, autonomy, and mem0 extraction all ride the deep remote instead.
- **Desk access**: `tools/custom/_opus_proxy.py` spawns one-shot subprocesses
  in the desk's venv (`DESK_ROOT` env override; default
  `/home/tianyizhang/opus trading`). **Fully read-only** — allowlist gate
  `is_tool_allowed()` in `_opus_introspect.py`; `WRITE_ALLOWLIST` is empty
  (kg_raise_gap was deleted desk-side 2026-07-20). Mocha degrades gracefully
  if the desk is missing.
- **Front door**: web app `:8080`; public via cloudflared at
  `project-hello-mocha.com` / `mocha.project-hello-mocha.com`.
- **.env**: real file (no longer a symlink to `~/envs/.env`) — Mocha's own
  secrets + the spark gateway key. Never commit it.

## Orchestration — LangGraph (bridge/graph.py)

    router → interpret → build_messages → llm_pass → log_pass
        → {run_tools ⇄ llm_pass | escalate → llm_pass | verify} → finalize → END

- State: `bridge/graph_state.py` (`TurnState`).
- Routing: heuristic `_route_model` (keywords/cashtags/held tickers/length) →
  fast or deep; fast model can emit `<escalate/>`; any tool run forces deep
  synthesis. `llm_pass_node` also falls back fast→deep when the local lane is
  down (`state["fast_fell_back"]`).
- Tools are inline `<tool_call>` tags (not function-calling), executed in
  `run_tools_node`; `interpret_node` grounds two-entity questions via
  `kg_neighbors` (`_kg_consult` scans the 1-hop edges for the counterpart).
- Per-turn UI events flow through an `asyncio.Queue` relayed by
  `bridge/server.py:_run_inline_turn` — its event contract must not change.

## Memory layers (5)

1. **Short-term** — last 22 turns in RAM (`memory.short_term_limit`).
2. **mem0 facts** — Chroma + SQLite under `memory/`; retrieval via
   `server._query_memories`, which applies the **novelty gate**: a fragment
   surfaced in the last ~6h is spent (dropped unless results starve). This is
   the structural fix for "she recites my own history back at me".
3. **Diary** — `data/diary/YYYY-MM-DD.json` + Chroma index; drafted through
   the day, finalized at local midnight.
4. **Session scratchpad** — RAM deque of tool calls (handles stay live).
5. **Life context** — `memory/life_context.py` + `data/life_context.json`:
   ledger of ongoing THREADS in Ika's life (projects, deadlines, situations),
   merged nightly from the finalized diary page (deep lane), injected into
   both prompts as `[Ika's life right now …]`. Awareness = timing + follow-ups,
   never recitation.

## Anti-repetition (multi-layer, load-bearing)

- `character/soul.md` + `chat_style.md`: memory is for acting, not reciting;
  no reused phrasings; unprompted shares END ON A STATEMENT; "Want to
  guess…?"-style hooks are banned.
- `autonomy/engine.py`: 12-utterance ledger + `_is_near_dup` Jaccard filter +
  `_strip_template_closer` (deterministically removes trailing
  want-to-guess/bet/check hook sentences — see tests/test_autonomy_closers.py).
- vLLM `repetition_penalty: 1.08` on the fast lane only (deep must restate
  exact numbers).
- mem0 novelty gate (above).

## PostgreSQL

Every LLM call logs to `postgresql://mocha:5369@127.0.0.1:5432/mocha`, table
`llm_call_log` (same schema as before the split — `triggered_by`, `messages`
JSONB, `response_content`, latency/ttft/token columns). Useful queries live in
git history and reporting docs; the quick one:

```sql
SELECT created_at, triggered_by, LEFT(response_content, 200), latency_ms
FROM llm_call_log WHERE triggered_by NOT IN ('cache_warm','repair')
ORDER BY created_at DESC LIMIT 20;
```

## Key files

| Purpose | Path |
|---------|------|
| Personality | `character/soul.md` (re-read every call — edits are live) |
| Reply contract | `character/chat_style.md` |
| Prompt builders | `character/context.py` (`build_chat_prompt` fast / `build_system_prompt` deep) |
| Conversational graph | `bridge/graph.py` |
| LLM client (+ breaker, health, api_key_env) | `bridge/llm_client.py` |
| Fast→deep fallback helper | `bridge/server.py:active_fast_client` |
| Life-context ledger | `memory/life_context.py` |
| Desk proxy (read-only) | `tools/custom/_opus_proxy.py`, `_opus_introspect.py` |
| Autonomy engine + composers | `autonomy/engine.py` |
| Systemd units (repo copies) | `scripts/systemd/*.service` (installed at `~/.config/systemd/user/`) |
| Central config | `config.yaml` |

## Conventions

- `character/soul.md` / `behaviors.yaml` re-read on every LLM call — no restart.
- Custom tools in `tools/custom/` export `TOOL_DEF` + `async def execute(args) -> str`;
  hot-reload via `POST http://127.0.0.1:8090/admin/reload-tools`.
- Log LLM calls via `bridge.call_log.log_call()` with a `CallContext`.
- Never add a desk tool to the proxy allowlist unless it is read-only; the
  allowlist is the prime-directive boundary (tests pin it).
- Tests: `.venv/bin/python -m pytest tests/ bridge/test_*.py` (155+ pass, no
  live services needed except tests/test_graph_live.py).
