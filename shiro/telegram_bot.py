"""Shiro's own Telegram bot — sends analysis reports and collects approvals."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from shiro.agent import ShiroAgent

log = logging.getLogger("shiro.telegram")

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (
        Application, CallbackQueryHandler, CommandHandler,
        ContextTypes, MessageHandler, filters,
    )
except ImportError:
    Application = None  # type: ignore[assignment,misc]


class ShiroTelegramBot:
    """Telegram bot for Shiro that sends reports and collects approve/reject decisions.

    If ``admin_chat_id`` is 0, the bot operates in open mode: the first person
    to send any message becomes the admin.

    Supported commands:
      /analyze  — manually trigger an analysis cycle right now
      /status   — show idle time, last analysis time, cooldown state
      /help     — list commands
    """

    def __init__(self, bot_token: str, admin_chat_id: int) -> None:
        self._token = bot_token
        self._admin_chat_id = admin_chat_id  # 0 = not yet registered
        self._app: Any = None
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._agent: ShiroAgent | None = None  # set after agent starts

    def set_agent(self, agent: ShiroAgent) -> None:
        """Give the bot a reference to the agent so commands can trigger it."""
        self._agent = agent

    async def start(self) -> None:
        if Application is None:
            log.warning("python-telegram-bot not installed — Shiro Telegram disabled")
            return
        self._app = Application.builder().token(self._token).build()

        # Commands
        self._app.add_handler(CommandHandler("analyze", self._cmd_analyze))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("start", self._cmd_help))

        # Inline keyboard callbacks (approve/reject)
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        # Catch-all: auto-register admin + friendly fallback
        self._app.add_handler(MessageHandler(filters.ALL, self._handle_message))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        if self._admin_chat_id:
            log.info("Shiro Telegram bot started (admin_chat_id=%s)", self._admin_chat_id)
        else:
            log.info("Shiro Telegram bot started in open mode — send any message to register as admin")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    # ------------------------------------------------------------------ #
    # Public send helpers                                                  #
    # ------------------------------------------------------------------ #

    async def send_text(self, text: str) -> None:
        """Send a plain text message to the admin."""
        if not self._app:
            return
        if not self._admin_chat_id:
            log.warning("Shiro: no admin chat_id yet — message not sent (send any message to the bot first)")
            return
        try:
            for chunk in _chunk_text(text, 4000):
                await self._app.bot.send_message(
                    self._admin_chat_id, chunk, parse_mode="Markdown",
                )
        except Exception as exc:
            log.warning("Shiro Telegram send failed: %s", exc)

    async def send_report(
        self, analysis: dict[str, Any], *, timeout: float = 3600,
    ) -> dict[int, bool]:
        """Send analysis summary and proposed changes with approve/reject buttons."""
        if not self._app:
            return {}

        summary = analysis.get("summary", "(no summary)")
        profile = analysis.get("user_profile_update")
        header = f"*Shiro Analysis*\n\n{summary}"
        if profile:
            header += f"\n\n*User profile update:* {profile}"
        await self.send_text(header)

        exchanges = analysis.get("exchanges", [])
        if exchanges:
            lines = ["*Exchange review:*"]
            for ex in exchanges[:10]:
                quality = ex.get("mocha_response_quality", "?")
                icon = {"good": "\u2705", "okay": "\u26a0\ufe0f", "poor": "\u274c"}.get(quality, "\u2753")
                user_said = (ex.get("user_said") or "")[:80]
                issue = ex.get("issue")
                line = f"{icon} _{user_said}_"
                if issue:
                    line += f"\n   Issue: {issue}"
                lines.append(line)
            await self.send_text("\n".join(lines))

        proposed = analysis.get("proposed_changes", [])
        if not proposed:
            await self.send_text("_No changes proposed._")
            return {}

        results: dict[int, bool] = {}
        for i, change in enumerate(proposed):
            change_id = f"shiro_{int(time.time())}_{i}"
            change_type = change.get("type", "unknown")
            description = change.get("description", "(no description)")
            text = f"*Proposed change ({change_type})*\n{description}"

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("\u2705 Approve", callback_data=f"approve:{change_id}"),
                    InlineKeyboardButton("\u274c Reject", callback_data=f"reject:{change_id}"),
                ]
            ])

            try:
                await self._app.bot.send_message(
                    self._admin_chat_id, text,
                    reply_markup=keyboard, parse_mode="Markdown",
                )
            except Exception as exc:
                log.warning("Failed to send change proposal: %s", exc)
                results[i] = False
                continue

            future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
            self._pending[change_id] = future

            try:
                approved = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                approved = False
                log.info("Change %s timed out — auto-rejected", change_id)

            results[i] = approved

        return results

    # ------------------------------------------------------------------ #
    # Command handlers                                                     #
    # ------------------------------------------------------------------ #

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._ensure_registered(update)
        await update.message.reply_text(
            "*Shiro commands:*\n\n"
            "/analyze — trigger an analysis cycle right now\n"
            "/status  — show current idle/cooldown state\n"
            "/help    — show this message",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._ensure_registered(update)
        if not self._agent:
            await update.message.reply_text("_Agent not connected yet._", parse_mode="Markdown")
            return

        import time as _time
        import bridge.server as srv

        elapsed_idle = _time.monotonic() - srv._last_interaction_time
        since_analysis = _time.monotonic() - self._agent._last_analysis
        cooldown = self._agent._cooldown
        threshold = self._agent._idle_threshold

        idle_str = _fmt_seconds(elapsed_idle)
        since_str = _fmt_seconds(since_analysis) if self._agent._last_analysis > 0 else "never"
        cooldown_remaining = max(0, cooldown - since_analysis)

        lines = [
            "*Shiro Status*",
            f"Mocha idle for: `{idle_str}` (threshold: {threshold}s)",
            f"Last analysis: `{since_str}` ago",
        ]
        if cooldown_remaining > 0:
            lines.append(f"Cooldown remaining: `{_fmt_seconds(cooldown_remaining)}`")
        else:
            lines.append("Cooldown: _ready_")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._ensure_registered(update)
        if not self._agent:
            await update.message.reply_text("_Agent not connected yet._", parse_mode="Markdown")
            return

        await update.message.reply_text("_Starting analysis cycle..._", parse_mode="Markdown")
        log.info("Shiro: manual analysis triggered via Telegram")

        # Run in background so we don't block the handler
        async def _run() -> None:
            try:
                await self._agent._run_analysis_cycle()
            except Exception as exc:
                log.error("Manual analysis cycle failed: %s", exc, exc_info=True)
                await self.send_text(f"\u274c Analysis failed: {exc}")

        asyncio.create_task(_run())

    # ------------------------------------------------------------------ #
    # Catch-all message handler (auto-register)                           #
    # ------------------------------------------------------------------ #

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        registered = await self._ensure_registered(update)
        if not registered:
            # Already registered, give a hint
            await update.message.reply_text(
                "Use /help to see available commands.",
            )

    # ------------------------------------------------------------------ #
    # Inline keyboard callback (approve / reject)                         #
    # ------------------------------------------------------------------ #

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        await query.answer()

        parts = query.data.split(":", 1)
        if len(parts) != 2:
            return
        action, change_id = parts
        approved = action == "approve"

        if change_id in self._pending:
            self._pending[change_id].set_result(approved)
            del self._pending[change_id]

        status = "\u2705 Approved" if approved else "\u274c Rejected"
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(status)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    async def _ensure_registered(self, update: Update) -> bool:
        """Register admin from update if not yet set. Returns True if newly registered."""
        if self._admin_chat_id or not update.effective_chat:
            return False
        self._admin_chat_id = update.effective_chat.id
        log.info("Shiro: registered admin chat_id=%s", self._admin_chat_id)
        try:
            await update.effective_chat.send_message(
                f"\u2705 *Shiro here.* You're now registered as admin (`chat_id: {self._admin_chat_id}`).\n\n"
                "I'll send analysis reports and change proposals here.\n\n"
                "Use /help to see what I can do.",
                parse_mode="Markdown",
            )
        except Exception as exc:
            log.warning("Shiro: failed to send registration confirmation: %s", exc)
        return True


def _fmt_seconds(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _chunk_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        idx = text.rfind("\n", 0, max_len)
        if idx < max_len // 2:
            idx = max_len
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks
