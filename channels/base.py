"""
Abstract channel interface and global channel registry.

Every messaging surface (Telegram, Discord, CLI, ...) subclasses `Channel` and
registers itself with the singleton `ChannelRegistry`.  The bridge can then
push proactive messages (scheduled tasks, reminders) back through whatever
channels are active without knowing about platform details.
"""

from __future__ import annotations

import abc
import logging
from typing import Optional

log = logging.getLogger("channels")


class Channel(abc.ABC):
    """Base class for all messaging channels."""

    name: str = "unknown"

    @abc.abstractmethod
    async def start(self) -> None:
        """Begin listening for incoming messages (non-blocking)."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the channel."""

    @abc.abstractmethod
    async def send(self, text: str, user_id: Optional[str] = None) -> None:
        """Push a message out through this channel.

        *user_id* is optional — for broadcast channels (like CLI) it can be
        ignored; for DM-oriented channels (Telegram, Discord) it routes the
        reply to the right conversation.
        """

    def __repr__(self) -> str:
        return f"<Channel:{self.name}>"


class ChannelRegistry:
    """Singleton-ish registry that holds all active channels."""

    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}
        # Per-user Telegram bots, keyed by ProjectParrot app_user_id.
        self._user_bots: dict[str, Channel] = {}

    def register(self, channel: Channel) -> None:
        self._channels[channel.name] = channel
        log.info("Channel registered: %s", channel.name)

    def get(self, name: str) -> Optional[Channel]:
        return self._channels.get(name)

    def register_user_bot(self, app_user_id: str, channel: Channel) -> None:
        self._user_bots[app_user_id] = channel
        log.info("User bot registered: app_user_id=%s", app_user_id)

    def get_user_bot(self, app_user_id: str) -> Optional[Channel]:
        return self._user_bots.get(app_user_id)

    async def stop_user_bot(self, app_user_id: str) -> None:
        ch = self._user_bots.pop(app_user_id, None)
        if ch:
            try:
                await ch.stop()
                log.info("User bot stopped: app_user_id=%s", app_user_id)
            except Exception as exc:
                log.warning("Error stopping user bot %s: %s", app_user_id, exc)

    @property
    def all(self) -> list[Channel]:
        return list(self._channels.values()) + list(self._user_bots.values())

    async def broadcast(self, text: str) -> None:
        """Send a message through every registered channel."""
        for ch in self._channels.values():
            try:
                await ch.send(text)
            except Exception as exc:
                log.warning("Broadcast failed on %s: %s", ch.name, exc)

    async def start_all(self) -> None:
        for ch in list(self._channels.values()) + list(self._user_bots.values()):
            try:
                await ch.start()
                log.info("Channel started: %s", ch.name)
            except Exception as exc:
                log.error("Failed to start channel %s: %s", ch.name, exc)

    async def stop_all(self) -> None:
        for ch in list(self._channels.values()) + list(self._user_bots.values()):
            try:
                await ch.stop()
            except Exception:
                pass


registry = ChannelRegistry()
