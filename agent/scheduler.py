"""
Scheduled job definitions for the proactive agent.

Jobs are loaded from the ``agent_loop.jobs`` section of ``config.yaml`` at
startup and may also be added/removed at runtime (persisted to
``data/cron_jobs.json``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ScheduledJob:
    id: str
    cron: str                               # cron expression: "0 8 * * *"
    action: str                             # "prompt" | "command" | "reminder" | "nori_research"
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    enabled: bool = True
    created_at: Optional[str] = None        # ISO8601; set when created at runtime
    created_by: Optional[str] = None        # "config" | "mocha" | "nori" | "user"

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduledJob":
        return cls(
            id=data["id"],
            cron=data.get("cron", "0 8 * * *"),
            action=data.get("action", "prompt"),
            params=data.get("params", {}),
            description=data.get("description", ""),
            enabled=data.get("enabled", True),
            created_at=data.get("created_at"),
            created_by=data.get("created_by"),
        )


DEFAULT_JOBS: list[dict] = [
    {
        "id": "morning_greeting",
        "cron": "0 8 * * *",
        "action": "prompt",
        "params": {"text": "Good morning! Give me a brief, friendly morning greeting."},
        "description": "Morning greeting sent to all channels",
    },
]
