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

    def __init__(
        self,
        bot_token: str,
        bridge_url: str,
        app_user_id: str | None = None,
        allowed_users: list[str] | None = None,
    ):
        self._token = bot_token
        self._bridge_url = bridge_url.rstrip("/")
        self._app_user_id = app_user_id  # project mocha user_id this bot belongs to
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
                "Hey! I'm your Mocha assistant. Send me a message and I'll reply."
            )

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        user = update.effective_user
        if user and not self._is_allowed(user.id, user.username):
            log.warning("Telegram: blocked message from %s (%s)", user.id, user.username)
            return

        text = update.message.text
        tg_chat_id = str(user.id) if user else "unknown"
        log.info("Telegram [app_user=%s tg=%s]: %s", self._app_user_id, tg_chat_id, text[:80])

        payload: dict = {
            "text": text,
            "user_id": tg_chat_id,
            "source": "telegram",
        }
        if self._app_user_id:
            payload["app_user_id"] = self._app_user_id

        # If Ika is REPLYING to one of Mocha's messages, carry the replied-to
        # message_id (+ its text) so the bridge can resolve it back to the exact
        # news item Mocha shared (see autonomy/news_ledger.py) and talk about it.
        reply_to = update.message.reply_to_message
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to.message_id
            quoted = reply_to.text or reply_to.caption or ""
            if quoted:
                payload["quoted_text"] = quoted[:500]

        try:
            resp = await self._http.post(
                f"{self._bridge_url}/channel",
                json=payload,
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
        log.info("Telegram bot started (polling) app_user_id=%s", self._app_user_id)

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        await self._http.aclose()

    async def send(self, text: str, user_id: Optional[str] = None) -> Optional[int]:
        """Proactively send a message.  Requires a chat_id (user_id).

        Returns the sent message's Telegram message_id (so a proactive news share
        can be recorded against it and a later reply resolved back), or None."""
        if not self._app or not user_id:
            return None
        try:
            msg = await self._app.bot.send_message(chat_id=int(user_id), text=text)
            return getattr(msg, "message_id", None)
        except Exception as exc:
            log.warning("Telegram send failed (user_id=%s): %s", user_id, exc)
            return None
