# ProjectParrot — Claude Code Instructions

ProjectParrot is a real-time conversational AI system with three agents:
- **Mocha**: The main conversational agent (3D character with voice, emotions, animations)
- **Nori**: Mocha's research analyst sub-agent (fetches data, builds visuals, writes narration)
- **Shiro**: A coaching meta-agent that analyzes Mocha's conversations and proposes improvements

## Tool & Panel Protocol

When building a tool that surfaces data through Mocha's UI, follow **`docs/tool-protocol.md`**. It defines:
- The `ui_command` message envelope and all supported panel types (`create_presentation`, `show_card`, `show_weather`, `show_notification`, `show_diary`)
- Internal tool pattern (direct `_broadcast_clients` call)
- External / MCP tool pattern (`__panel__` JSON envelope — for tools outside this repo, e.g. opus trading)
- Step-by-step guide for adding a new panel type

## PostgreSQL Connection

Every LLM call is logged to PostgreSQL for offline analysis.

- **DSN**: `postgresql://mocha:5369@127.0.0.1:5432/mocha`
- **Table**: `llm_call_log`

### Table Schema

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Auto-increment PK |
| `call_id` | UUID | Unique call identifier |
| `created_at` | TIMESTAMPTZ | When the call was made |
| `triggered_by` | TEXT | Who triggered: `channel`, `chat_stream`, `voice`, `ws_unity`, `tool_loop`, `tool_round`, `repair`, `cache_warm`, `shiro` |
| `source` | TEXT | Channel source: `telegram`, `discord`, `cli`, `web`, `agent_loop` |
| `user_id` | TEXT | User identifier |
| `conversation_id` | TEXT | Groups all LLM calls in one user turn |
| `pass_number` | SMALLINT | 1=routing pass, 2=full-context pass (complexity routing) |
| `tool_round` | SMALLINT | Tool loop iteration (1-based) |
| `model` | TEXT | Model name (e.g., `Qwen/Qwen3-32B`) |
| `temperature` | REAL | Sampling temperature |
| `max_tokens` | INTEGER | Max output tokens |
| `stream` | BOOLEAN | Whether streaming was used |
| `enable_thinking` | BOOLEAN | Qwen3 thinking mode |
| `tools_provided` | BOOLEAN | Whether tool schemas were sent |
| `messages` | JSONB | **Full message array sent to LLM** (system prompt + memories + history + user message) |
| `message_count` | INTEGER | Number of messages in array |
| `response_content` | TEXT | **Full raw LLM response** |
| `response_tool_calls` | JSONB | Tool calls requested by LLM |
| `finish_reason` | TEXT | `stop`, `tool_calls`, `length` |
| `error` | TEXT | Error message if call failed |
| `latency_ms` | REAL | Total wall-clock time |
| `ttft_ms` | REAL | Time to first token (streaming only) |
| `prompt_tokens` | INTEGER | Input token count |
| `completion_tokens` | INTEGER | Output token count |
| `total_tokens` | INTEGER | Total token count |

## Useful SQL Queries

### Recent conversations (last 2 hours, human-readable)
```sql
SELECT created_at, triggered_by, source,
       messages->-1->>'content' AS user_message,
       LEFT(response_content, 300) AS response_preview,
       latency_ms, prompt_tokens, completion_tokens
FROM llm_call_log
WHERE triggered_by NOT IN ('shiro', 'cache_warm', 'warmup', 'repair')
  AND created_at > now() - interval '2 hours'
ORDER BY created_at DESC;
```

### Full conversation reconstruction (by conversation_id)
```sql
SELECT id, created_at, triggered_by, pass_number, tool_round,
       messages, response_content, response_tool_calls, finish_reason
FROM llm_call_log
WHERE conversation_id = 'CONVERSATION_ID_HERE'
ORDER BY id;
```

### Performance overview
```sql
SELECT triggered_by,
       COUNT(*) AS calls,
       ROUND(AVG(latency_ms)) AS avg_latency_ms,
       ROUND(AVG(prompt_tokens)) AS avg_prompt_tok,
       ROUND(AVG(completion_tokens)) AS avg_comp_tok
FROM llm_call_log
WHERE created_at > now() - interval '24 hours'
GROUP BY triggered_by
ORDER BY calls DESC;
```

### Find user corrections / dissatisfaction
```sql
SELECT a.conversation_id, a.response_content AS mocha_said,
       b.messages->-1->>'content' AS user_followup
FROM llm_call_log a
JOIN llm_call_log b ON b.created_at > a.created_at
  AND b.created_at < a.created_at + interval '5 minutes'
  AND b.triggered_by NOT IN ('shiro', 'cache_warm', 'repair')
WHERE a.triggered_by NOT IN ('shiro', 'cache_warm', 'repair')
  AND (b.messages->-1->>'content' ILIKE '%no,%'
    OR b.messages->-1->>'content' ILIKE '%wrong%'
    OR b.messages->-1->>'content' ILIKE '%not what%'
    OR b.messages->-1->>'content' ILIKE '%I meant%')
ORDER BY a.created_at DESC
LIMIT 20;
```

### Shiro's past analyses
```sql
SELECT created_at, LEFT(response_content, 500) AS analysis_preview
FROM llm_call_log
WHERE triggered_by = 'shiro'
ORDER BY created_at DESC
LIMIT 5;
```

## Key Files

| Purpose | Path |
|---------|------|
| Mocha's personality | `character/soul.md` |
| Behavior rules | `character/behaviors.yaml` |
| Emotion definitions | `character/emotions.yaml` |
| System prompt builder | `character/context.py` |
| Bridge server | `bridge/server.py` |
| LLM client | `bridge/llm_client.py` |
| PG call logger | `bridge/call_log.py` |
| Tool schemas | `tools/registry.py` |
| Tool execution | `tools/executor.py` |
| Custom tools dir | `tools/custom/` |
| Nori agent | `nori/agent.py` |
| Nori personality | `nori/soul.md` |
| Mocha→Nori bridge | `tools/custom/ask_nori.py` |
| Shiro agent | `shiro/agent.py` |
| Shiro analyzer | `shiro/analyzer.py` |
| Shiro PG reader | `shiro/pg_reader.py` |
| Animation functions | `character/animation_functions.csv` |
| VRM animation controller | `web/static/js/animation-controller.js` |
| Central config | `config.yaml` |

## Animation / Body Language

The character's body language is driven by a clip-based animation system. The LLM
picks a gesture by name (e.g. `speak_pointing`, `wave`, `dance_loop`); the web
app's animation controller plays the corresponding pre-converted FBX clip via
per-bone quaternion retargeting to VRM.

- **Clip roster:** `character/animation_functions.csv` — 76 functions, each
  flagged looping or oneshot, with Start/Loop/End phase clips for stateful
  gestures (dance_loop, sing, seiza, etc.).
- **Controller:** `web/static/js/animation-controller.js` — state machine
  (IDLE → STARTING → LOOPING → ENDING → IDLE for loopable; IDLE → PLAYING_ONCE
  → IDLE for oneshot). 250 ms crossfade between clips. Random idle fidget every
  8–15 s.
- **Default idle:** `idle_breathe` (looped).
- **Lip-sync:** handled by `stt/viseme_map.py` — phoneme timestamps from the
  STT `/align` endpoint map to 5-channel viseme weights (aa, ih, ou, ee, oh)
  and drive VRM mouth blendshapes client-side.

## Conventions

- `character/soul.md` and `character/behaviors.yaml` are re-read on every LLM call — changes take effect immediately, no restart needed.
- Tool definitions follow OpenAI function-calling schema format.
- Custom tools in `tools/custom/` must export `TOOL_DEF` (dict) and `async def execute(args: dict) -> str`.
- Hot-reload custom tools: call `POST http://127.0.0.1:8000/admin/reload-tools` or import and call `tools.registry.reload_custom_tools()`.
- Log all LLM calls with `CallContext(triggered_by="shiro")` via `bridge.call_log.log_call()`.

## Acting as Manual Shiro

When asked to analyze or improve Mocha's conversations:

1. **Query PG** for recent conversations using the SQL above
2. **Read** current `character/soul.md` and `character/behaviors.yaml`
3. **Analyze**: What did the user want? Did Mocha deliver? What patterns emerge?
4. **Propose** specific changes with clear rationale:
   - New behavior rules (append to `behaviors.yaml`)
   - Soul edits (modify sections in `soul.md`)
   - New tools (write to `tools/custom/<name>.py`)
5. **Always explain reasoning** before making any changes
6. **Never modify** `bridge/server.py` or `bridge/llm_client.py` without explicit permission
7. **Test changes** by checking that `character/context.py:build_system_prompt()` can still assemble the prompt

### Custom Tool Template

```python
"""Custom tool: example_tool — created by Shiro."""

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "example_tool",
        "description": "What this tool does",
        "parameters": {
            "type": "object",
            "properties": {
                "param1": {"type": "string", "description": "Parameter description"},
            },
            "required": ["param1"],
        },
    },
}


async def execute(arguments: dict) -> str:
    """Execute the tool and return a string result."""
    param1 = arguments.get("param1", "")
    # Implementation here
    return f"Result for {param1}"
```
