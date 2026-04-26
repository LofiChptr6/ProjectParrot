"""Custom tool: schedule_cron — add a recurring scheduled job."""

from __future__ import annotations

import logging

log = logging.getLogger("tools.schedule_cron")


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "schedule_cron",
        "description": (
            "Schedule a recurring task on a cron expression. Use for reminders, "
            "routines, or periodic research (e.g., 'weekday 9am market brief'). "
            "ONLY call this when Ika has explicitly confirmed the schedule — "
            "never schedule silently based on a guess."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cron": {
                    "type": "string",
                    "description": (
                        "5-field cron expression: 'minute hour day month day_of_week'. "
                        "Examples: '0 9 * * 1-5' (weekdays 9am), '30 18 * * *' (daily 6:30pm), "
                        "'0 10 * * 0' (Sunday 10am)."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["reminder", "nori_research", "prompt"],
                    "description": (
                        "reminder = deliver plain text. "
                        "nori_research = have Nori investigate and queue the finding. "
                        "prompt = send text as if Ika asked it."
                    ),
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Action-specific. reminder: {text: '...'}. "
                        "nori_research: {topic: '...'}. prompt: {text: '...'}."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Short human-readable label (for list_cron_jobs).",
                },
            },
            "required": ["cron", "action", "params"],
        },
    },
}


async def execute(arguments: dict) -> str:
    from bridge.server import get_agent_loop

    loop = get_agent_loop()
    if loop is None:
        return "Cron scheduler is not running (agent_loop disabled in config)."

    cron = arguments.get("cron", "").strip()
    action = arguments.get("action", "reminder")
    params = arguments.get("params") or {}
    description = arguments.get("description", "").strip() or f"{action} task"

    if action not in ("reminder", "nori_research", "prompt"):
        return f"Unsupported action '{action}'. Use reminder, nori_research, or prompt."

    from tools.executor import TOOL_USER_ID
    caller_user_id = TOOL_USER_ID.get()

    try:
        job_id = await loop.add_job({
            "cron": cron,
            "action": action,
            "params": params,
            "description": description,
            "created_by": "mocha",
            "user_id": caller_user_id,
        })
    except ValueError as exc:
        return f"Could not schedule: {exc}. Please double-check the cron expression."
    except Exception as exc:
        log.exception("schedule_cron failed")
        return f"Scheduler error: {exc}"

    # Look up next fire time for a nicer confirmation.
    next_run = ""
    for j in loop.list_jobs(user_id=caller_user_id):
        if j["id"] == job_id:
            next_run = j.get("next_run_iso") or ""
            break

    tail = f" Next run: {next_run}." if next_run else ""
    return f"Scheduled {job_id}: {description} ({cron}).{tail}"
