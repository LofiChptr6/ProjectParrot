"""FastAPI dependencies for JWT auth."""

from __future__ import annotations

from fastapi import Header, HTTPException, Query

from .models import CurrentUser, jwt_decode


async def get_current_user(authorization: str = Header(default="")) -> CurrentUser:
    """Extract and validate JWT from Authorization: Bearer <token> header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    return jwt_decode(token)


async def get_current_user_ws(token: str = Query(default="")) -> CurrentUser:
    """Extract and validate JWT from WebSocket ?token= query param."""
    if not token:
        raise HTTPException(status_code=401, detail="Missing token query param")
    return jwt_decode(token)
