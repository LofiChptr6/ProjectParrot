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

    def register(self, channel: Channel) -> None:
        self._channels[channel.name] = channel
        log.info("Channel registered: %s", channel.name)

    def get(self, name: str) -> Optional[Channel]:
        return self._channels.get(name)

    @property
    def all(self) -> list[Channel]:
        return list(self._channels.values())

    async def broadcast(self, text: str) -> None:
        """Send a message through every registered channel."""
        for ch in self._channels.values():
            try:
                await ch.send(text)
            except Exception as exc:
                log.warning("Broadcast failed on %s: %s", ch.name, exc)

    async def start_all(self) -> None:
        for ch in self._channels.values():
            try:
                await ch.start()
                log.info("Channel started: %s", ch.name)
            except Exception as exc:
                log.error("Failed to start channel %s: %s", ch.name, exc)

    async def stop_all(self) -> None:
        for ch in self._channels.values():
            try:
                await ch.stop()
            except Exception:
                pass


registry = ChannelRegistry()
