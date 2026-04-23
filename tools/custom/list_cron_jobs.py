"""Custom tool: list_cron_jobs — report all active scheduled jobs."""

from __future__ import annotations


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "list_cron_jobs",
        "description": (
            "List all scheduled recurring jobs (reminders, research routines). "
            "Returns id, cron expression, action, description, and next run time."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    from bridge.server import get_agent_loop

    loop = get_agent_loop()
    if loop is None:
        return "Cron scheduler is not running."

    jobs = loop.list_jobs()
    if not jobs:
        return "No scheduled jobs."

    lines: list[str] = []
    for j in jobs:
        state = "on" if j["enabled"] else "off"
        next_run = j.get("next_run_iso") or "—"
        desc = j.get("description") or j["action"]
        created_by = j.get("created_by") or "config"
        lines.append(
            f"- {j['id']} [{state}] {j['cron']} → {desc} "
            f"(action={j['action']}, next={next_run}, by={created_by})"
        )
    return "\n".join(lines)
