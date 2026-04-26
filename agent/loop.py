"""
Proactive agent loop — runs scheduled tasks.

Uses APScheduler for cron-based scheduling. Each job is dispatched to the
right executor:

  - ``prompt`` / ``morning_greeting`` — run through **Nori**, not Mocha.
    Nori produces a concise report; the text is then routed to the user's
    channels (web + Telegram fanout, see ``cron_origin``).
  - ``command``                       — shell command via tools.executor.
  - ``reminder``                      — plain text reminder delivered as-is.
  - ``nori_research``                 — silent research; finding is enqueued
    for pickup on next web reconnect (no live voicing).

All cron outputs carry ``cron_origin=True`` so ``bridge.channel_router``
fans them out to Telegram in addition to the live platform — user might
not be at their desktop.
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


def _extract_narration(raw: str) -> str:
    """Nori sometimes returns a Mocha-style ``{"segments":[{"text":...}, ...]}``
    JSON blob meant to drive the voice pipeline directly. For cron reports we
    want a single plain string. If parsing succeeds, join segment texts; on
    any failure, return the raw string (it's already narration)."""
    if not raw:
        return ""
    stripped = raw.strip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return stripped
    try:
        data = json.loads(stripped)
        segs = data.get("segments") if isinstance(data, dict) else None
        if isinstance(segs, list) and segs:
            parts = [s.get("text", "").strip() for s in segs if isinstance(s, dict)]
            joined = " ".join(p for p in parts if p)
            return joined or stripped
    except Exception:
        pass
    return stripped


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
        self._scheduler = AsyncIOScheduler()
        self._persist_path = ROOT / config.get("persist_file", "data/cron_jobs.json")
        self._persist_lock = asyncio.Lock()
        # Last-fire timestamps — persisted across restarts so the UI cron modal
        # can show "last fired at ..." after the bridge is bounced.
        self._last_fires_path = ROOT / "data" / "cron_last_fires.json"
        self._last_fires_lock = asyncio.Lock()
        self._last_fires: dict[str, str] = self._load_last_fires()

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

    def list_jobs(self, user_id: str | None = None) -> list[dict]:
        """List jobs. If user_id is given, return only that user's jobs."""
        out: list[dict] = []
        for j in self._jobs:
            if user_id is not None and j.user_id != user_id:
                continue
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
                "user_id": j.user_id,
                "next_run_iso": next_run,
                "last_fire": self._last_fires.get(j.id),
            })
        return out

    async def remove_job(self, job_id: str, user_id: str | None = None) -> bool:
        """Remove a job. If user_id is given, only removes jobs owned by that user."""
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job is None:
            return False
        if user_id is not None and job.user_id != user_id:
            log.warning("remove_job: user %s tried to cancel job %s owned by %s",
                        user_id, job_id, job.user_id)
            return False
        try:
            self._scheduler.remove_job(job_id)
        except Exception as exc:
            log.info("Scheduler remove_job(%s) noop: %s", job_id, exc)
        self._jobs = [j for j in self._jobs if j.id != job_id]
        await self._persist()
        log.info("Removed job: %s", job_id)
        return True

    # ------------------------------------------------------------------
    #  Job dispatch
    # ------------------------------------------------------------------
    async def _run_job(self, job: ScheduledJob) -> None:
        log.info("Running scheduled job: %s (%s)", job.id, job.action)
        # Record the fire timestamp immediately so the UI shows a recent "last"
        # even if the job takes time. Best-effort JSON write; no lock contention
        # expected with default cron granularity (minute).
        self._last_fires[job.id] = _now_iso()
        try:
            await asyncio.to_thread(self._save_last_fires)
        except Exception:
            pass

        try:
            if job.action in ("prompt", "morning_greeting"):
                # Scheduled tasks go through Nori — Mocha doesn't chat with the
                # cron scheduler anymore. Nori produces a concise spoken report
                # that Mocha's voice pipeline reads back on the live channel
                # AND Telegram (via cron_origin fanout).
                from nori.agent import process_request
                prompt = job.params.get("text") or f"Scheduled task: {job.description}"
                hint = (
                    "This is a scheduled cron job, not a live user conversation. "
                    "Produce a concise spoken report (2-5 short sentences) "
                    "suitable for Mocha to voice back to the user. Respond with "
                    "plain text, NOT a JSON segments block."
                )
                raw = await process_request(f"{hint}\n\n{prompt}")
                spoken = _extract_narration(raw)
                await self._route_text(
                    spoken, source=f"cron:{job.id}", kind="cron_report",
                    description=job.description, cron_origin=True,
                    user_id=job.user_id,
                )
            elif job.action == "command":
                from tools.executor import execute_tool
                cmd = job.params.get("command", "echo hello")
                result = await execute_tool("bash_exec", {"command": cmd})
                await self._route_text(
                    f"**Scheduled: {job.description}**\n```\n{result}\n```",
                    source=f"cron:{job.id}", cron_origin=True,
                    user_id=job.user_id,
                )
            elif job.action == "reminder":
                text = job.params.get("text", "Reminder!")
                await self._route_text(
                    text, source=f"cron:{job.id}", kind="reminder",
                    description=job.description, cron_origin=True,
                    user_id=job.user_id,
                )
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
                    "cron_origin": True,
                })
            else:
                log.warning("Unknown job action: %s", job.action)
        except Exception as exc:
            log.error("Job %s failed: %s", job.id, exc)

    async def _route_text(
        self,
        text: str,
        source: str,
        kind: str = "message",
        description: str = "",
        cron_origin: bool = False,
        user_id: str | None = None,
    ) -> None:
        """Send through the channel router (web + Telegram fanout for cron,
        otherwise priority-based); fall back to broadcast on total failure."""
        if not text:
            return
        try:
            from bridge.channel_router import route_autonomous, route_autonomous_for_user
            if user_id:
                await route_autonomous_for_user(user_id, {
                    "text": text, "emotion": "neutral", "gesture": "",
                    "autonomous": True, "source": source, "kind": kind,
                    "description": description, "cron_origin": cron_origin,
                })
            else:
                await route_autonomous({
                    "text": text, "emotion": "neutral", "gesture": "",
                    "autonomous": True, "source": source, "kind": kind,
                    "description": description, "cron_origin": cron_origin,
                })
            return
        except Exception as exc:
            log.warning("channel_router unavailable, falling back to broadcast: %s", exc)
        try:
            await self._registry.broadcast(text)
        except Exception as exc:
            log.error("Broadcast failed: %s", exc)

    # ------------------------------------------------------------------
    #  Last-fire persistence (used by UI cron list)
    # ------------------------------------------------------------------
    def _load_last_fires(self) -> dict[str, str]:
        path = self._last_fires_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: str(v) for k, v in data.items() if isinstance(v, (str, int, float))}
        except Exception as exc:
            log.warning("Failed to load %s: %s", path, exc)
        return {}

    def _save_last_fires(self) -> None:
        path = self._last_fires_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._last_fires, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Failed to save %s: %s", path, exc)

    def get_last_fire(self, job_id: str) -> str | None:
        return self._last_fires.get(job_id)


_DOW_MAP = {"0": "sun", "1": "mon", "2": "tue", "3": "wed", "4": "thu", "5": "fri", "6": "sat", "7": "sun"}


def _translate_dow(field: str) -> str:
    """Convert Unix cron day-of-week (0/7=Sun, 1=Mon) to APScheduler names (mon..sun).

    APScheduler's CronTrigger uses 0=Monday for numeric DOW, which silently shifts
    every weekday by one when given Unix-style numbers. Rewrite numeric tokens to
    English names, which both conventions agree on.
    """
    def _tok(tok: str) -> str:
        # Handle step (*/2), ranges (1-5), lists are handled by outer split
        if "/" in tok:
            base, step = tok.split("/", 1)
            return f"{_tok(base)}/{step}"
        if "-" in tok:
            a, b = tok.split("-", 1)
            return f"{_tok(a)}-{_tok(b)}"
        if tok in _DOW_MAP:
            return _DOW_MAP[tok]
        return tok  # *, already-named (mon/tue/...), etc.

    return ",".join(_tok(t) for t in field.split(","))


def _parse_cron(expr: str) -> CronTrigger | None:
    """Parse a 5-field Unix-style cron expression into an APScheduler CronTrigger."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    try:
        return CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=_translate_dow(parts[4]),
        )
    except Exception:
        return None
