"""Custom tool: set_display_name — save the user's preferred name.

Mocha calls this once after the user introduces themselves. Persisted to the
`users.display_name` column so the next system_prompt build picks it up and
Mocha doesn't ask again on reconnect.
"""

from __future__ import annotations

import asyncio


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "set_display_name",
        "description": (
            "Save what the user wants to be called. Call this exactly ONCE, "
            "the first time they introduce themselves (e.g. 'I'm Ika', 'call "
            "me Sam', 'the name's Tay'). Don't call it speculatively or from "
            "names mentioned in third-person ('my friend Ika said...'). "
            "After this is set, you'll see their name in the system prompt "
            "on every future turn — no need to ask again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name the user wants to be called (1–64 chars).",
                },
            },
            "required": ["name"],
        },
    },
}


async def execute(arguments: dict) -> str:
    name = (arguments.get("name") or "").strip()
    if not name:
        return "no name given — nothing saved"
    # Cap to a reasonable length; users don't have novel-length names.
    name = name[:64]

    from tools.executor import TOOL_USER_ID
    user_id = TOOL_USER_ID.get()
    if not user_id:
        # Non-web channels (Telegram/CLI legacy) don't have an authenticated
        # user_id; can't persist. Surface that to Mocha so she doesn't lie.
        return "couldn't save — no user context for this channel"

    try:
        from auth.db import set_display_name as _set
        await asyncio.to_thread(_set, user_id, name)
    except Exception as exc:
        return f"save failed: {exc}"

    return f"saved — calling them {name} from now on"
