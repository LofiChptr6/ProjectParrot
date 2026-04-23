"""
Discord channel — listens via discord.py v2+ and forwards messages to the
bridge's ``/channel`` endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
import httpx

from channels.base import Channel

log = logging.getLogger("channels.discord")


class DiscordChannel(Channel):
    name = "discord"

    def __init__(
        self,
        bot_token: str,
        bridge_url: str,
        allowed_guilds: list[int] | None = None,
        allowed_channels: list[int] | None = None,
    ):
        self._token = bot_token
        self._bridge_url = bridge_url.rstrip("/")
        self._allowed_guilds = set(allowed_guilds) if allowed_guilds else None
        self._allowed_channels = set(allowed_channels) if allowed_channels else None
        self._http = httpx.AsyncClient(timeout=120.0)

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._ready = asyncio.Event()

        @self._client.event
        async def on_ready() -> None:
            log.info("Discord bot logged in as %s", self._client.user)
            self._ready.set()

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            await self._handle_message(message)

    def _is_allowed(self, message: discord.Message) -> bool:
        if message.author == self._client.user or message.author.bot:
            return False
        if self._allowed_guilds and message.guild and message.guild.id not in self._allowed_guilds:
            return False
        if self._allowed_channels and message.channel.id not in self._allowed_channels:
            return False
        return True

    def _should_respond(self, message: discord.Message) -> bool:
        """Respond to DMs or messages that mention the bot."""
        if isinstance(message.channel, discord.DMChannel):
            return True
        if self._client.user and self._client.user.mentioned_in(message):
            return True
        return False

    async def _handle_message(self, message: discord.Message) -> None:
        if not self._is_allowed(message):
            return
        if not self._should_respond(message):
            return

        text = message.clean_content.strip()
        if not text:
            return

        user_id = str(message.author.id)
        log.info("Discord [%s]: %s", user_id, text[:80])

        try:
            async with message.channel.typing():
                resp = await self._http.post(
                    f"{self._bridge_url}/channel",
                    json={"text": text, "user_id": user_id, "source": "discord"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    reply = data.get("assistant_text", "")
                else:
                    reply = "Sorry, something went wrong."
                    log.error("Bridge returned %s: %s", resp.status_code, resp.text[:500])
        except Exception as exc:
            reply = "I can't reach my brain right now."
            log.error("Bridge request failed: %s", exc)

        if reply:
            for chunk in _split_discord(reply):
                await message.channel.send(chunk)

    async def start(self) -> None:
        asyncio.create_task(self._client.start(self._token))
        # Wait for the bot to be ready (up to 30s)
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=30)
        except asyncio.TimeoutError:
            log.warning("Discord bot didn't become ready within 30s — continuing anyway")

    async def stop(self) -> None:
        await self._client.close()
        await self._http.aclose()

    async def send(self, text: str, user_id: Optional[str] = None) -> None:
        """Proactively send a DM.  Requires a Discord user_id."""
        if not user_id:
            return
        try:
            user = await self._client.fetch_user(int(user_id))
            dm = await user.create_dm()
            for chunk in _split_discord(text):
                await dm.send(chunk)
        except Exception as exc:
            log.warning("Discord DM failed (user_id=%s): %s", user_id, exc)


def _split_discord(text: str, limit: int = 2000) -> list[str]:
    """Split a long message into <=2000-char chunks for Discord."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks
