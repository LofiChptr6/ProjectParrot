"""Custom tool: cancel_cron_job — remove a scheduled job by id."""

from __future__ import annotations


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "cancel_cron_job",
        "description": (
            "Cancel a previously scheduled recurring job. Use list_cron_jobs "
            "first if you don't know the id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The job id returned by schedule_cron (e.g., 'cron_ab12cd34').",
                },
            },
            "required": ["id"],
        },
    },
}


async def execute(arguments: dict) -> str:
    from bridge.server import get_agent_loop

    loop = get_agent_loop()
    if loop is None:
        return "Cron scheduler is not running."

    job_id = (arguments.get("id") or "").strip()
    if not job_id:
        return "Missing job id."

    removed = await loop.remove_job(job_id)
    if removed:
        return f"Cancelled {job_id}."
    return f"No job found with id {job_id}."
