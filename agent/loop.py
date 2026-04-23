"""
Proactive agent loop — runs scheduled tasks.

Uses APScheduler for cron-based scheduling.  Each job either sends a prompt
to the LLM, runs a shell command, delivers a reminder, or delegates a
research task to Nori (and enqueues the finding for delivery on next reconnect).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agent.scheduler import ScheduledJob, DEFAULT_JOBS
from channels.base import ChannelRegistry

log = logging.getLogger("agent.loop")

ROOT = Path(__file__).resolve().parent.parent


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_line(text: str, max_len: int = 160) -> str:
    text = (text or "").strip()
    first = text.splitlines()[0] if text else ""
    return first[:max_len]


class AgentLoop:
    def __init__(
        self,
        config: dict[str, Any],
        bridge_url: str,
        channel_registry: ChannelRegistry,
    ):
        self._config = config
        self._bridge_url = bridge_url.rstrip("/")
        self._registry = channel_registry
        self._http = httpx.AsyncClient(timeout=120.0)
        self._scheduler = AsyncIOScheduler()
        self._persist_path = ROOT / config.get("persist_file", "data/cron_jobs.json")
        self._persist_lock = asyncio.Lock()

        config_jobs = [ScheduledJob.from_dict(j) for j in config.get("jobs", DEFAULT_JOBS)]
        persisted_jobs = self._load_persisted()

        # Config wins on id collision; otherwise merge.
        config_ids = {j.id for j in config_jobs}
        self._jobs: list[ScheduledJob] = config_jobs + [j for j in persisted_jobs if j.id not in config_ids]

    # ------------------------------------------------------------------
    #  Persistence
    # ------------------------------------------------------------------
    def _load_persisted(self) -> list[ScheduledJob]:
        if not self._persist_path.exists():
            return []
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            return [ScheduledJob.from_dict(j) for j in data.get("jobs", [])]
        except Exception as exc:
            log.warning("Failed to load persisted jobs from %s: %s", self._persist_path, exc)
            return []

    async def _persist(self) -> None:
        """Write runtime-created jobs to disk. Config-seeded jobs are also written
        back so the file is self-sufficient."""
        async with self._persist_lock:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "jobs": [dataclasses.asdict(j) for j in self._jobs],
            }
            self._persist_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        for job in self._jobs:
            if not job.enabled:
                continue
            trigger = _parse_cron(job.cron)
            if trigger is None:
                log.warning("Invalid cron for job %s: %s", job.id, job.cron)
                continue

            self._scheduler.add_job(
                self._run_job,
                trigger=trigger,
                args=[job],
                id=job.id,
                replace_existing=True,
            )
            log.info("Scheduled job: %s (%s) — %s", job.id, job.cron, job.description)

        self._scheduler.start()
        log.info("Agent loop started with %d jobs", len(self._jobs))

    async def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        await self._http.aclose()

    # ------------------------------------------------------------------
    #  Runtime API — called by tools/custom/schedule_cron.py etc.
    # ------------------------------------------------------------------
    async def add_job(self, spec: dict) -> str:
        spec = dict(spec)
        spec.setdefault("id", f"cron_{uuid.uuid4().hex[:8]}")
        spec.setdefault("created_at", _now_iso())
        spec.setdefault("created_by", "mocha")
        job = ScheduledJob.from_dict(spec)
        trigger = _parse_cron(job.cron)
        if trigger is None:
            raise ValueError(f"Invalid cron: {job.cron}")
        self._scheduler.add_job(
            self._run_job,
            trigger=trigger,
            args=[job],
            id=job.id,
            replace_existing=True,
        )
        # Replace if id already exists in _jobs, else append.
        self._jobs = [j for j in self._jobs if j.id != job.id] + [job]
        await self._persist()
        log.info("Added runtime job: %s (%s) — %s", job.id, job.cron, job.description)
        return job.id

    def list_jobs(self) -> list[dict]:
        out: list[dict] = []
        for j in self._jobs:
            next_run: str | None = None
            try:
                sj = self._scheduler.get_job(j.id)
                if sj and sj.next_run_time:
                    next_run = sj.next_run_time.isoformat()
            except Exception:
                next_run = None
            out.append({
                "id": j.id,
                "cron": j.cron,
                "action": j.action,
                "params": j.params,
                "description": j.description,
                "enabled": j.enabled,
                "created_by": j.created_by,
                "next_run_iso": next_run,
            })
        return out

    async def remove_job(self, job_id: str) -> bool:
        try:
            self._scheduler.remove_job(job_id)
        except Exception as exc:
            log.info("Scheduler remove_job(%s) noop: %s", job_id, exc)
        before = len(self._jobs)
        self._jobs = [j for j in self._jobs if j.id != job_id]
        if len(self._jobs) == before:
            return False
        await self._persist()
        log.info("Removed job: %s", job_id)
        return True

    # ------------------------------------------------------------------
    #  Job dispatch
    # ------------------------------------------------------------------
    async def _run_job(self, job: ScheduledJob) -> None:
        log.info("Running scheduled job: %s (%s)", job.id, job.action)
        try:
            if job.action == "prompt":
                text = job.params.get("text", "Hello!")
                reply = await self._prompt(text)
                await self._route_text(reply, source=f"cron:{job.id}")
            elif job.action == "command":
                from tools.executor import execute_tool
                cmd = job.params.get("command", "echo hello")
                result = await execute_tool("bash_exec", {"command": cmd})
                await self._route_text(
                    f"**Scheduled: {job.description}**\n```\n{result}\n```",
                    source=f"cron:{job.id}",
                )
            elif job.action == "reminder":
                text = job.params.get("text", "Reminder!")
                await self._route_text(text, source=f"cron:{job.id}", kind="reminder",
                                       description=job.description)
            elif job.action == "nori_research":
                from nori.agent import process_request
                from bridge.notifications import enqueue
                topic = job.params.get("topic", "")
                result = await process_request(topic)
                await enqueue({
                    "kind": "research_finding",
                    "source": "nori_cron",
                    "summary": _first_line(result),
                    "detail": result,
                    "job_id": job.id,
                    "topic": topic,
                })
            else:
                log.warning("Unknown job action: %s", job.action)
        except Exception as exc:
            log.error("Job %s failed: %s", job.id, exc)

    async def _prompt(self, text: str) -> str:
        try:
            resp = await self._http.post(
                f"{self._bridge_url}/channel",
                json={"text": text, "user_id": "agent", "source": "agent_loop"},
            )
            if resp.status_code == 200:
                return resp.json().get("assistant_text", "")
        except Exception as exc:
            log.error("Agent prompt failed: %s", exc)
        return ""

    async def _route_text(self, text: str, source: str, kind: str = "message",
                          description: str = "") -> None:
        """Send through the channel router (web → Telegram → queue) if available;
        otherwise fall back to broadcasting on all registered channels."""
        if not text:
            return
        try:
            from bridge.channel_router import route_autonomous
            await route_autonomous({
                "text": text, "emotion": "neutral", "gesture": "",
                "autonomous": True, "source": source, "kind": kind,
                "description": description,
            })
            return
        except Exception as exc:
            log.warning("channel_router unavailable, falling back to broadcast: %s", exc)
        try:
            await self._registry.broadcast(text)
        except Exception as exc:
            log.error("Broadcast failed: %s", exc)


def _parse_cron(expr: str) -> CronTrigger | None:
    """Parse a 5-field cron expression into an APScheduler CronTrigger."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    try:
        return CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
        )
    except Exception:
        return None
