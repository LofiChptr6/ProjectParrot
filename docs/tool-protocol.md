# Tool & Panel Protocol

This document defines the standard contract that any tool — whether built into this repo or delivered via an external MCP server — must follow to surface information through Mocha's UI.

---

## Overview

Mocha displays information through **panels**: draggable floating windows anchored to the chat. Panels are triggered by `ui_command` messages broadcast over the `/ws/live` WebSocket. Any tool, internal or external, that wants to show a panel must ultimately produce one of these messages.

There are two integration paths depending on where the tool lives:

| Path | When to use | How it works |
|------|-------------|--------------|
| **Internal tool** (in `tools/custom/`) | Tool lives in this repo | Call `_broadcast_clients()` directly inside `execute()` |
| **External tool** (MCP server, other repo) | Tool runs in a separate process | Return a structured JSON envelope; the bridge detects and broadcasts it |

---

## How Mocha presents slides

Understanding this before designing a payload matters.

Mocha's narration is split into **speech segments** — short sentence-chunks the LLM produces. The frontend advances slides proportionally as segments play:

```
segment 0/6 → slide 0
segment 2/6 → slide 1
segment 4/6 → slide 2
```

This means **slide count should match the number of narration beats**. A 3-slide deck works best when Mocha has roughly 3 things to say. She never manually clicks Next — slides advance as she speaks.

To make this precise, each slide carries its own `narration` field. The tool author writes what Mocha should say for that slide; the bridge assembles them into the ordered speech segment list. One narration beat = one slide turn.

```jsonc
// Each slide says what Mocha speaks while it is visible
{
  "type": "multi_chart",
  "narration": "NVDA is up 3% today on heavy volume, TSLA pulled back after the open.",
  "symbols": ["NVDA", "TSLA"]
}
```

If `narration` is omitted on a slide, Mocha may ad-lib based on context or skip that slide in her speech.

---

## Path 1 — Internal Tool

Internal tools call `_broadcast_clients` directly. The tool's string return value is what Mocha speaks for the overall summary; per-slide narration is embedded in the slide objects.

```python
"""Custom tool: my_tool."""
from bridge.server import _broadcast_clients

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "my_tool",
        "description": "...",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "..."},
            },
            "required": ["topic"],
        },
    },
}

async def execute(arguments: dict) -> str:
    topic = arguments["topic"]

    await _broadcast_clients({
        "type": "ui_command",
        "action": "create_presentation",
        "presentation": {
            "id": f"pres_{int(__import__('time').time() * 1000)}",
            "title": topic,
            "slides": [
                {
                    "type": "multi_chart",
                    "narration": "First slide, Mocha says this.",
                    "symbols": ["AAPL", "NVDA", "TSLA"],
                },
                {
                    "type": "chart",
                    "narration": "Second slide, Mocha says this.",
                    "chart_type": "line",
                    "labels": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                    "datasets": [{"label": "Portfolio", "data": [100, 102, 101, 104, 107]}],
                },
            ],
        },
    })

    return f"Here's the breakdown on {topic}."
```

Hot-reload without restart: `POST http://127.0.0.1:8000/admin/reload-tools`

---

## Path 2 — External Tool (MCP / other repo)

External tools return a JSON string with a top-level `__panel__` key. The bridge executor detects it and broadcasts the `ui_command` automatically.

**Return format:**
```json
{
  "__panel__": "create_presentation",
  "__payload__": { ... full presentation object ... }
}
```

- `__panel__`: panel action name (see Panel Types below)
- `__payload__`: the complete payload — same schema as the internal broadcast
- No separate `__narration__` needed: each slide carries its own `narration` field; the bridge assembles them into Mocha's speech in order

**Example — opus trading daily briefing:**
```json
{
  "__panel__": "create_presentation",
  "__payload__": {
    "id": "pres_portfolio_20260427",
    "title": "Daily Briefing — Apr 27",
    "slides": [
      {
        "type": "stat_row",
        "narration": "Your portfolio gained 2.3% today, led by NVDA which added $1,840.",
        "title": "Today's P&L",
        "stats": [
          { "label": "Total", "value": "+$4,210", "delta": "+2.3%" },
          { "label": "Largest winner", "value": "NVDA +$1,840" },
          { "label": "Largest loser", "value": "TSLA −$320" }
        ]
      },
      {
        "type": "multi_chart",
        "narration": "Here are your top three positions for today. NVDA broke out above resistance.",
        "title": "Top positions",
        "symbols": [
          { "symbol": "NVDA", "price": 875.32, "change": 3.1 },
          { "symbol": "TSLA", "price": 248.90, "change": -0.8 },
          { "symbol": "AAPL", "price": 212.50, "change": 1.2 }
        ],
        "default_period": "1d"
      },
      {
        "type": "chart",
        "narration": "Your equity curve stayed above the morning low and closed near the high of day.",
        "title": "Equity curve",
        "chart_type": "line",
        "labels": ["09:30", "10:00", "10:30", "11:00", "12:00", "13:00", "14:00", "15:00", "15:30"],
        "datasets": [{ "label": "Portfolio ($)", "data": [200000, 201200, 200800, 202400, 203100, 203800, 204100, 204500, 204210] }]
      }
    ]
  }
}
```

> **Bridge-side:** detection of `__panel__` lives in `tools/executor.py` → `execute_tool()` post-dispatch block. If a new panel type is added, wire it there.

---

## Slide Types

All slides live inside `presentation.slides[]`. Every slide may carry a `narration` string — what Mocha says while that slide is visible.

---

### `title` — Cover slide

```jsonc
{
  "type": "title",
  "narration": "Let me walk you through today's market snapshot.",
  "title": "Market Snapshot",
  "subtitle": "Apr 27, 2026"       // optional
}
```

---

### `stat_row` — Key metrics

Vertical list of labeled values, each with an optional delta badge.

```jsonc
{
  "type": "stat_row",
  "narration": "Revenue hit a record $25.1 billion, up 3.3% from last quarter.",
  "title": "Q1 Results",
  "stats": [
    { "label": "Revenue",  "value": "$25.1B", "delta": "+3.3%" },
    { "label": "Net income","value": "$6.9B",  "delta": "+8.1%" },
    { "label": "EPS",       "value": "$2.27",  "delta": "+7.6%" }
  ]
}
```

---

### `chart` — XY time-series / bar / pie

Rendered with Chart.js. Supply raw `labels` + `datasets` arrays — the tool author controls the data, not the bridge.

```jsonc
{
  "type": "chart",
  "narration": "The equity curve stayed above the morning low all day.",
  "title": "Equity curve",
  "chart_type": "line",            // line | bar | pie | doughnut
  "labels": ["09:30", "10:00", "10:30", "11:00"],
  "datasets": [
    {
      "label": "Portfolio ($)",
      "data": [200000, 201200, 200800, 202400]
    }
  ]
}
```

Multiple datasets on one chart are supported — add more objects to `datasets`.

---

### `candlestick` — Single OHLCV chart

Live candlestick chart fetched from the bridge's `/api/stock-chart` endpoint. Uses Lightweight Charts.

```jsonc
{
  "type": "candlestick",
  "narration": "NVDA broke above resistance at $860 around noon.",
  "title": "NVDA",
  "symbol": "NVDA",
  "default_period": "1d"          // 1d | 5d | 1mo | 3mo | 6mo | 1y
}
```

---

### `multi_chart` — Multiple candlesticks on one slide

Several mini candlestick charts stacked vertically with a shared timeframe bar. This is the right type when showing 2–4 symbols at once on a single slide.

```jsonc
{
  "type": "multi_chart",
  "narration": "NVDA leads the group. TSLA pulled back after the open. AAPL is quietly grinding higher.",
  "title": "Watchlist",
  "symbols": [
    { "symbol": "NVDA", "price": 875.32, "change": 3.1 },
    { "symbol": "TSLA", "price": 248.90, "change": -0.8 },
    { "symbol": "AAPL", "price": 212.50, "change": 1.2 }
  ],
  "default_period": "1d"
}
```

`symbols` can also be a plain string array if price/change are not available:
```json
{ "symbols": ["NVDA", "TSLA", "AAPL"] }
```

---

### `image` — Static image

```jsonc
{
  "type": "image",
  "narration": "Here's the annotated chart from this morning.",
  "title": "Morning setup",
  "url": "img:HANDLE"             // opaque handle resolved by bridge; or plain https:// URL
}
```

---

### `news_feed` — Article list

```jsonc
{
  "type": "news_feed",
  "narration": "Markets reacted to three headlines this morning.",
  "articles": [
    {
      "title": "Fed holds rates steady",
      "snippet": "The Federal Reserve held rates at 4.25% in a unanimous vote.",
      "source": "reuters.com",
      "date": "2 hours ago",
      "url": "url:HANDLE",
      "thumbnail": "img:HANDLE"   // optional
    }
  ]
}
```

---

### `markdown` — Freeform text

```jsonc
{
  "type": "markdown",
  "narration": "Here are the key risks I flagged for this position.",
  "title": "Risk summary",
  "content": "## Downside risks\n\n- Earnings miss possible\n- Sector rotation underway"
}
```

---

### `bullets` — Simple bullet list

```jsonc
{
  "type": "bullets",
  "narration": "Three things to watch today.",
  "title": "Watch list",
  "items": ["Fed speaker at 14:00", "NVDA earnings after close", "VIX above 20"]
}
```

---

## Other Panel Types

### `show_card` — Compact floating card

For a single metric or a small set of stats, without needing a full presentation.

```json
{
  "type": "ui_command",
  "action": "show_card",
  "card": {
    "id": "card_<timestamp>",
    "card_type": "stat",
    "title": "NVDA",
    "value": "$875.32",
    "fields": [
      { "label": "Change", "value": "+3.1%" },
      { "label": "Volume", "value": "52.3M" }
    ],
    "duration_sec": 0
  }
}
```

`card_type`: `stat` | `info` | `quote` | `image`

---

### `show_weather` — Weather panel

Immediate — bypasses the speech queue, appears before Mocha starts speaking.

```json
{
  "type": "ui_command",
  "action": "show_weather",
  "payload": {
    "location": "Tokyo, Japan",
    "current": { "temp_c": 18, "feels_c": 16, "humidity": 72, "wind_kph": 12, "wmo": 51, "label": "Light rain" },
    "hourly":   [{ "time": "15:00", "temp_c": 18, "wmo": 51 }],
    "forecast": [{ "date": "2026-04-28", "high_c": 22, "low_c": 18, "wmo": 51, "label": "Rain", "precip_mm": 2.5 }],
    "sunrise": "06:12",
    "sunset": "18:45",
    "bg_class": "rain"
  }
}
```

`bg_class`: `clear` | `cloudy` | `rain` | `snow` | `fog` | `thunder`

---

### `show_notification` — Toast

```json
{
  "type": "ui_command",
  "action": "show_notification",
  "message": "Sync complete.",
  "level": "info"
}
```

`level`: `info` | `success` | `warning` | `error`

---

### `show_diary` — Diary modal

Immediate.

```json
{
  "type": "ui_command",
  "action": "show_diary",
  "date": "2026-04-27"
}
```

---

## Routing & Timing

The frontend `UIOrchestrator` (`web/static/js/ui-orchestrator.js`) controls when panels appear:

- **Immediate** — `show_weather`, `show_diary`: panel opens on arrival, before speech starts.
- **Queued** — everything else: panel opens the moment Mocha begins speaking the first segment.

Slide advances happen automatically inside `onSegmentPlay()` in `presentation.js`. Segment index is mapped proportionally to slide index — Mocha does not send explicit "go to slide N" commands. This is why per-slide `narration` matters: it determines how many speech segments are created, which determines the pacing.

---

## Adding a New Slide Type

1. Define the schema here under a new `### <type>` heading with a `narration` field.
2. Add a `case '<type>': renderXxxSlide(slide)` in `presentation.js` → `renderSlide()`.
3. Implement `renderXxxSlide(slide)` — append DOM into `presSlide`.
4. If the slide hosts a chart library (Chart.js, Lightweight Charts), destroy the instance on slide change to avoid canvas leaks.

## Adding a New Panel Type (not a slide)

1. Define the schema here under a new `### show_<name>` heading.
2. Add HTML in `web/static/index.html` using the `.pres-header` pattern.
3. Register with PanelManager: `PanelManager.registerPanel('myPanel', el, defaultRect)`.
4. Add the action to `UIOrchestrator._deliverUiCommand()`. Decide: immediate or queued?
5. Add the renderer in your JS module.
6. Wire the tool in `tools/custom/` (Path 1) or document the `__panel__` envelope (Path 2).

---

## Quick reference for agents

| Data shape | Panel type | Slide type |
|-----------|-----------|-----------|
| Single metric | `show_card` | — |
| 2–5 metrics | `show_card` (with `fields`) | — |
| 2–4 symbols, candlesticks | `create_presentation` | `multi_chart` |
| 1 symbol deep-dive | `create_presentation` | `candlestick` |
| Raw XY time series | `create_presentation` | `chart` |
| Mix of metrics + charts | `create_presentation` | `stat_row` + `chart` slides |
| News / articles | `create_presentation` | `news_feed` |
| Freeform analysis | `create_presentation` | `markdown` or `bullets` |
| Weather | `show_weather` | — |
| One-liner update | `show_notification` | — |
