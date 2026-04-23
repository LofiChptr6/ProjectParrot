"""
CLI channel — interactive REPL powered by rich.

Runs in-process alongside the bridge.  Reads from stdin via
``asyncio.get_event_loop().run_in_executor`` so it never blocks the event
loop.  Sends each line to the bridge ``/channel`` endpoint and prints the
response.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from channels.base import Channel

log = logging.getLogger("channels.cli")
console = Console()


class CLIChannel(Channel):
    name = "cli"

    def __init__(self, bridge_url: str):
        self._bridge_url = bridge_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=120.0)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._repl())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        await self._http.aclose()

    async def send(self, text: str, user_id: Optional[str] = None) -> None:
        console.print(Panel(Markdown(text), title="[bold cyan]Parrot[/]", border_style="cyan"))

    async def _repl(self) -> None:
        await asyncio.sleep(2)
        console.print()
        console.print(
            Panel(
                Text("Parrot CLI — type a message and press Enter.  Ctrl+C to quit.", style="bold"),
                border_style="green",
            )
        )

        loop = asyncio.get_event_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(None, lambda: input("you> "))
            except (EOFError, KeyboardInterrupt):
                console.print("[dim]Bye![/dim]")
                break

            line = line.strip()
            if not line:
                continue
            if line.lower() in ("exit", "quit", "/quit", "/exit"):
                console.print("[dim]Bye![/dim]")
                break

            try:
                resp = await self._http.post(
                    f"{self._bridge_url}/channel",
                    json={"text": line, "user_id": "cli", "source": "cli"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    reply = data.get("assistant_text", "(no response)")
                    segments = data.get("segments", [])

                    if segments:
                        for seg in segments:
                            emotion = seg.get("emotion", "")
                            text_s = seg.get("text", "")
                            badge = f"[yellow][{emotion}][/yellow] " if emotion else ""
                            console.print(f"  {badge}{text_s}")
                    else:
                        console.print(Panel(Markdown(reply), title="[bold cyan]Parrot[/]", border_style="cyan"))
                else:
                    console.print(f"[red]Bridge error ({resp.status_code})[/red]")
            except Exception as exc:
                console.print(f"[red]Connection error: {exc}[/red]")
