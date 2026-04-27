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

## Path 1 — Internal Tool

Internal tools call `_broadcast_clients` directly. The tool's string return value is what Mocha speaks; the panel appears asynchronously on the user's screen.

```python
"""Custom tool: my_tool."""
from bridge.server import _broadcast_clients, _set_open_modal

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

    # Build the panel payload (see Panel Types below)
    payload = { ... }

    await _broadcast_clients({
        "type": "ui_command",
        "action": "create_presentation",   # or show_card, show_weather, etc.
        "presentation": payload,           # field name matches the action (see below)
    })

    # What Mocha will say out loud
    return f"Here's what I found on {topic}."
```

Hot-reload after adding: `POST http://127.0.0.1:8000/admin/reload-tools`

---

## Path 2 — External Tool (MCP / other repo)

External tools cannot import bridge internals. Instead, they return a JSON string with a top-level `__panel__` key. The bridge executor detects this key and broadcasts the appropriate `ui_command` automatically.

**Return format:**
```json
{
  "__panel__": "<panel_type>",
  "__narration__": "What Mocha should say about this result.",
  "__payload__": { ... panel-specific fields ... }
}
```

- `__panel__`: one of the panel types listed below  
- `__narration__`: plain text; the bridge forwards this as Mocha's spoken response  
- `__payload__`: the full panel payload object (same schema as the internal broadcast)

**Example — opus trading sending a chart:**
```json
{
  "__panel__": "create_presentation",
  "__narration__": "Here's your portfolio summary for today.",
  "__payload__": {
    "id": "pres_portfolio_20260427",
    "title": "Portfolio — Apr 27",
    "slides": [
      {
        "type": "stat_row",
        "title": "Today's P&L",
        "stats": [
          {"label": "Total", "value": "+$4,210", "delta": "+2.3%"},
          {"label": "Largest winner", "value": "NVDA +$1,840"}
        ]
      },
      {
        "type": "chart",
        "title": "Equity curve",
        "chart_type": "line",
        "labels": ["09:30", "10:00", "10:30", "..."],
        "datasets": [{"label": "Portfolio", "data": [100000, 101200, 102400]}]
      }
    ]
  }
}
```

> **Note:** The bridge-side detection of `__panel__` is the contract. If this bridge-side handling is not yet implemented for a given panel type, add it in `tools/executor.py` → `execute_tool()` post-dispatch block.

---

## Panel Types

### `create_presentation` — Slides panel

A floating panel with multiple slides. Navigation arrows appear automatically.

```json
{
  "type": "ui_command",
  "action": "create_presentation",
  "presentation": {
    "id": "pres_<timestamp>",
    "title": "Panel title shown in header",
    "slides": [ ... ],
    "auto_advance_sec": 0
  }
}
```

**Slide types:**

```jsonc
// Title slide
{ "type": "title", "title": "Main heading", "subtitle": "Optional subtext" }

// Stat row (key metrics, one per row)
{
  "type": "stat_row",
  "title": "Section label",
  "stats": [
    { "label": "Revenue", "value": "$25.1B", "delta": "+3.3%" }
  ]
}

// Chart (Chart.js)
{
  "type": "chart",
  "title": "Chart heading",
  "chart_type": "line",          // line | bar | pie | doughnut
  "labels": ["Q1", "Q2", "Q3"],
  "datasets": [
    { "label": "Series name", "data": [22.5, 23.6, 24.3] }
  ]
}

// News feed
{
  "type": "news_feed",
  "articles": [
    {
      "title": "Headline",
      "snippet": "First 2 sentences.",
      "source": "reuters.com",
      "date": "2 hours ago",
      "url": "url:HANDLE",        // opaque handle, resolved by bridge
      "thumbnail": "img:HANDLE"   // opaque handle, resolved by bridge
    }
  ]
}

// Markdown / freeform text
{ "type": "markdown", "title": "Section", "content": "## Heading\n\nBody text." }

// Image
{ "type": "image", "title": "Caption", "url": "img:HANDLE" }
```

---

### `show_card` — Compact info card

A small floating card, useful for a single metric or quote.

```json
{
  "type": "ui_command",
  "action": "show_card",
  "card": {
    "id": "card_<timestamp>",
    "card_type": "stat",          // stat | info | quote | image
    "title": "NVDA",
    "value": "$875.32",
    "fields": [
      { "label": "Change", "value": "+2.3%" },
      { "label": "Volume", "value": "42.1M" }
    ],
    "duration_sec": 0             // 0 = stays until dismissed
  }
}
```

---

### `show_weather` — Weather panel

Immediate (bypasses the speech queue — appears as soon as received).

```json
{
  "type": "ui_command",
  "action": "show_weather",
  "payload": {
    "location": "Tokyo, Japan",
    "current": {
      "temp_c": 18,
      "feels_c": 16,
      "humidity": 72,
      "wind_kph": 12,
      "wmo": 51,
      "label": "Light rain"
    },
    "hourly": [
      { "time": "15:00", "temp_c": 18, "wmo": 51 }
    ],
    "forecast": [
      { "date": "2026-04-28", "high_c": 22, "low_c": 18, "wmo": 51, "label": "Rain", "precip_mm": 2.5 }
    ],
    "sunrise": "06:12",
    "sunset": "18:45",
    "bg_class": "rain"
  }
}
```

`bg_class` drives the animated background: `clear` `cloudy` `rain` `snow` `fog` `thunder`

---

### `show_notification` — Toast

Ephemeral overlay, auto-dismisses.

```json
{
  "type": "ui_command",
  "action": "show_notification",
  "message": "Sync complete.",
  "level": "info"               // info | success | warning | error
}
```

---

### `show_diary` — Diary modal

Immediate. Opens Mocha's diary to a specific date.

```json
{
  "type": "ui_command",
  "action": "show_diary",
  "date": "2026-04-27"          // ISO date; omit for today
}
```

---

## Routing & Timing

The frontend UIOrchestrator (`web/static/js/ui-orchestrator.js`) controls when panels appear:

- **Immediate actions** — `show_weather`, `show_diary`: panel opens as soon as the message arrives, before Mocha starts speaking.
- **Queued actions** — everything else: panel opens at the moment Mocha begins speaking the segment that references it.

This prevents panels from appearing during Nori's research phase while the user is waiting.

---

## Adding a New Panel Type

1. **Define the payload schema** in this file under a new `### show_<name>` section.
2. **Add a new HTML panel** in `web/static/index.html` following the `.pres-header` pattern.
3. **Register it with PanelManager** in your JS module: `PanelManager.registerPanel('myPanel', el, defaultRect)`.
4. **Add the action to UIOrchestrator** (`_deliverUiCommand`) — decide immediate vs queued.
5. **Add the renderer** in your JS module.
6. **Add the internal tool** in `tools/custom/my_tool.py` using Path 1, or document the external schema for Path 2.
7. If it should be immediate, add the action name to the `IMMEDIATE_ACTIONS` set in `ui-orchestrator.js`.

---

## Quick reference for agents

When building a tool that reports to Mocha, pick the smallest panel type that fits:

| Data shape | Use |
|-----------|-----|
| Single number or metric | `show_card` (stat) |
| 2–5 metrics | `show_card` (stat, with fields) |
| Multiple sections or charts | `create_presentation` |
| Weather data | `show_weather` |
| One-liner status update | `show_notification` |
| Rich freeform content | `create_presentation` with `markdown` slides |
