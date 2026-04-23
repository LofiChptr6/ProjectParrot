"""
Telegram channel — receives messages via python-telegram-bot v20+ and
forwards them to the bridge's ``/channel`` endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from channels.base import Channel

log = logging.getLogger("channels.telegram")


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, bot_token: str, bridge_url: str, allowed_users: list[str] | None = None):
        self._token = bot_token
        self._bridge_url = bridge_url.rstrip("/")
        self._allowed_users = set(allowed_users) if allowed_users else None
        self._app: Optional[Application] = None
        self._http = httpx.AsyncClient(timeout=120.0)

    def _is_allowed(self, user_id: int, username: str | None) -> bool:
        if self._allowed_users is None:
            return True
        return str(user_id) in self._allowed_users or (username and username in self._allowed_users)

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat:
            await update.effective_chat.send_message(
                "Hey! I'm your Parrot assistant. Send me a message and I'll reply."
            )

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        user = update.effective_user
        if user and not self._is_allowed(user.id, user.username):
            log.warning("Telegram: blocked message from %s (%s)", user.id, user.username)
            return

        text = update.message.text
        user_id = str(user.id) if user else "unknown"
        log.info("Telegram [%s]: %s", user_id, text[:80])

        try:
            resp = await self._http.post(
                f"{self._bridge_url}/channel",
                json={"text": text, "user_id": user_id, "source": "telegram"},
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data.get("assistant_text", "")
            else:
                reply = "Sorry, something went wrong on my end."
                log.error("Bridge returned %s: %s", resp.status_code, resp.text[:500])
        except Exception as exc:
            reply = "I can't reach my brain right now. Try again in a moment."
            log.error("Bridge request failed: %s", exc)

        if update.effective_chat and reply:
            await update.effective_chat.send_message(reply)

    async def start(self) -> None:
        self._app = Application.builder().token(self._token).build()
        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started (polling)")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        await self._http.aclose()

    async def send(self, text: str, user_id: Optional[str] = None) -> None:
        """Proactively send a message.  Requires a chat_id (user_id)."""
        if not self._app or not user_id:
            return
        try:
            await self._app.bot.send_message(chat_id=int(user_id), text=text)
        except Exception as exc:
            log.warning("Telegram send failed (user_id=%s): %s", user_id, exc)
