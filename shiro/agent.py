"""Shiro coaching agent — main coordinator.

Detects idle periods, queries PG for recent conversations, runs LLM
meta-analysis, sends reports via Telegram, and applies approved changes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger("shiro.agent")


class ShiroAgent:
    """Coaching agent that runs during idle periods."""

    def __init__(
        self,
        config: dict[str, Any],
        llm_config: dict[str, Any],
        call_log_dsn: str,
    ) -> None:
        self._config = config
        self._llm_config = llm_config
        self._dsn = call_log_dsn
        self._idle_threshold = config.get("idle_threshold_s", 300)
        self._cooldown = config.get("cooldown_s", 1800)
        self._lookback = config.get("lookback_minutes", 120)
        self._last_analysis: float = 0.0
        self._pg_pool: Any = None
        self._telegram: Any = None
        self._llm: Any = None
        self._task: asyncio.Task | None = None

        from shiro.memory import AnalysisMemory
        self._memory = AnalysisMemory(max_entries=config.get("memory_size", 20))

    async def start(self) -> None:
        from bridge.llm_client import LLMClient
        from shiro import pg_reader
        from shiro.telegram_bot import ShiroTelegramBot

        # Own LLM client (same endpoint, separate httpx client)
        self._llm = LLMClient(self._llm_config)

        # Read-only PG pool
        self._pg_pool = await pg_reader.create_pool(self._dsn)
        if not self._pg_pool:
            log.error("Shiro: PG pool creation failed — agent disabled")
            return

        # Telegram bot
        bot_token = self._config.get("telegram_bot_token", "")
        chat_id = self._config.get("admin_chat_id", 0)
        if bot_token:
            self._telegram = ShiroTelegramBot(bot_token, int(chat_id))
            self._telegram.set_agent(self)
            await self._telegram.start()
        else:
            log.warning("Shiro: No Telegram bot token configured — reports will be logged only")

        self._task = asyncio.create_task(self._loop())
        log.info("Shiro agent started (idle=%ds, cooldown=%ds, lookback=%dmin)",
                 self._idle_threshold, self._cooldown, self._lookback)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._telegram:
            await self._telegram.stop()
        if self._llm:
            await self._llm.close()
        if self._pg_pool:
            await self._pg_pool.close()
        log.info("Shiro agent stopped")

    async def _loop(self) -> None:
        import bridge.server as srv

        while True:
            await asyncio.sleep(10)

            elapsed = time.monotonic() - srv._last_interaction_time
            since_last = time.monotonic() - self._last_analysis

            if elapsed < self._idle_threshold:
                continue
            if since_last < self._cooldown:
                continue

            try:
                await self._run_analysis_cycle()
            except Exception as exc:
                log.error("Shiro analysis cycle failed: %s", exc, exc_info=True)
            finally:
                self._last_analysis = time.monotonic()

    async def _run_analysis_cycle(self) -> None:
        from shiro import pg_reader, analyzer
        from shiro.change_executor import execute_changes

        log.info("Shiro: starting analysis cycle")

        # Check if there's new data since last analysis
        last_shiro = await pg_reader.last_shiro_analysis(self._pg_pool)
        last_mocha = await pg_reader.last_mocha_activity(self._pg_pool)

        if last_shiro and last_mocha and last_mocha <= last_shiro:
            log.info("Shiro: no new conversations since last analysis — skipping")
            return

        # Fetch recent conversations
        conversations = await pg_reader.fetch_recent_conversations(
            self._pg_pool, since_minutes=self._lookback,
        )
        if not conversations:
            log.info("Shiro: no conversations found in lookback window — skipping")
            return

        log.info("Shiro: analyzing %d conversation group(s)", len(conversations))

        # Notify via Telegram that analysis is starting
        if self._telegram:
            await self._telegram.send_text(
                f"_Shiro is analyzing {len(conversations)} recent conversation(s)..._"
            )

        # Run LLM analysis
        analysis = await analyzer.analyze(
            self._llm,
            conversations,
            self._memory.recent(3),
            temperature=self._config.get("temperature", 0.6),
            max_tokens=self._config.get("max_tokens", 4096),
            enable_thinking=self._config.get("enable_thinking", True),
        )

        if not analysis:
            log.warning("Shiro: analysis returned nothing")
            if self._telegram:
                await self._telegram.send_text("_Analysis produced no results._")
            return

        # Record in memory
        self._memory.add({
            "summary": analysis.get("summary", ""),
            "proposed_changes": analysis.get("proposed_changes", []),
            "exchange_count": len(analysis.get("exchanges", [])),
        })

        proposed = analysis.get("proposed_changes", [])

        if not proposed:
            log.info("Shiro: analysis complete, no changes proposed")
            if self._telegram:
                summary = analysis.get("summary", "(no summary)")
                await self._telegram.send_text(
                    f"*Shiro Analysis Complete*\n\n{summary}\n\n_No changes proposed._"
                )
            return

        log.info("Shiro: %d change(s) proposed, sending to Telegram for approval", len(proposed))

        # Send report and collect approvals
        if self._telegram:
            approvals = await self._telegram.send_report(analysis)
        else:
            # No Telegram — log and skip
            log.info("Shiro proposed changes (no Telegram to approve): %s", proposed)
            return

        # Execute approved changes
        approved_changes = [
            change for i, change in enumerate(proposed)
            if approvals.get(i, False)
        ]

        if not approved_changes:
            log.info("Shiro: all changes rejected or timed out")
            if self._telegram:
                await self._telegram.send_text("_All changes rejected. No modifications made._")
            return

        log.info("Shiro: executing %d approved change(s)", len(approved_changes))
        results = execute_changes(approved_changes)

        # Report results
        if self._telegram:
            lines = ["*Changes applied:*"]
            for r in results:
                status = r["status"]
                icon = "\u2705" if status == "ok" else "\u274c"
                line = f"{icon} {r['type']}: {status}"
                if r.get("reason"):
                    line += f" ({r['reason']})"
                lines.append(line)
            await self._telegram.send_text("\n".join(lines))

        log.info("Shiro: analysis cycle complete — %d changes applied",
                 sum(1 for r in results if r["status"] == "ok"))
