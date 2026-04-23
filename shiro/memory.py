"""Simple in-memory ring buffer for Shiro's past analyses."""

from __future__ import annotations

import time
from collections import deque
from typing import Any


class AnalysisMemory:
    """Fixed-size ring buffer storing recent Shiro analysis results."""

    def __init__(self, max_entries: int = 20) -> None:
        self._buf: deque[dict[str, Any]] = deque(maxlen=max_entries)

    def add(self, entry: dict[str, Any]) -> None:
        entry.setdefault("timestamp", time.time())
        self._buf.append(entry)

    def recent(self, n: int = 5) -> list[dict[str, Any]]:
        items = list(self._buf)
        return items[-n:] if n < len(items) else items

    def __len__(self) -> int:
        return len(self._buf)
