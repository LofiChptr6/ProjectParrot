"""Auth models and JWT helpers."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import jwt
import yaml
from fastapi import HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
_cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
_JWT_SECRET: str = _cfg.get("auth", {}).get("jwt_secret", "change-me-in-config")
_JWT_ALGORITHM = "HS256"
_JWT_EXPIRY_DAYS = 30


class UserCreate(BaseModel):
    email: str
    username: str
    password: str


class UserLogin(BaseModel):
    email_or_username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CurrentUser(BaseModel):
    user_id: str
    # email/username are NULL for anonymous users (account_type='anon').
    email: Optional[str] = None
    username: Optional[str] = None
    account_type: str = "registered"  # 'anon' | 'registered'


def jwt_encode(
    user_id: str,
    email: str | None,
    username: str | None,
    account_type: str = "registered",
) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "username": username,
        "account_type": account_type,
        "iat": int(time.time()),
        "exp": int(time.time()) + _JWT_EXPIRY_DAYS * 86400,
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


def jwt_decode(token: str) -> CurrentUser:
    try:
        payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        return CurrentUser(
            user_id=payload["sub"],
            email=payload.get("email"),
            username=payload.get("username"),
            # Default to 'registered' for legacy tokens issued before
            # account_type was added to the JWT claims.
            account_type=payload.get("account_type") or "registered",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
