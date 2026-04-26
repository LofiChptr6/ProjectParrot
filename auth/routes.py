"""Auth routes — signup, login, me, plus anonymous-user bootstrap and
anon→registered upgrade for the welcome-first onboarding flow."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Optional

import bcrypt
import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import quota
from .db import (
    create_anon_user,
    create_user,
    get_user_by_browser_token,
    get_user_by_email,
    get_user_by_email_or_username,
    get_user_by_id,
    get_user_by_ip,
    get_user_by_username,
    link_browser_token,
    touch_ip,
    upgrade_anon_to_registered,
)
from .deps import get_current_user
from .models import CurrentUser, TokenResponse, UserCreate, UserLogin, jwt_encode

router = APIRouter(prefix="/api/auth", tags=["auth"])

ROOT = Path(__file__).resolve().parent.parent
_CHAR_DIR = ROOT / "character"
_USERS_DIR = ROOT / "data" / "users"


def _seed_user_dir(user_id: str) -> None:
    """Copy default Mocha character files into the new user's directory."""
    user_dir = _USERS_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("soul.md", "behaviors.yaml"):
        src = _CHAR_DIR / fname
        dst = user_dir / fname
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)


def _client_ip(request: Request) -> str | None:
    """Best-effort real-client IP behind Cloudflare / reverse proxies.

    Order: CF-Connecting-IP (set by Cloudflare tunnel) → X-Forwarded-For
    (first hop) → request.client.host (direct TCP peer). Returns None if
    nothing usable is found.
    """
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # First IP in the chain is the original client.
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


# ---------------------------------------------------------------------------
#  Anonymous bootstrap — fresh visitor → real user_id, no signup required
# ---------------------------------------------------------------------------

class AnonBootstrap(BaseModel):
    browser_token: str
    user_agent: Optional[str] = None


class AnonBootstrapResponse(TokenResponse):
    user_id: str
    account_type: str
    display_name: Optional[str] = None


@router.post("/anon", response_model=AnonBootstrapResponse)
async def anon_bootstrap(body: AnonBootstrap, request: Request):
    """Resolve (or create) an anonymous user for this browser/IP.

    Resolution order:
      1) browser_token already linked to a user → reuse
      2) IP already linked to a user → reuse, link this new browser_token to them
      3) neither → create a fresh anon user (account_type='anon')

    Always returns a JWT keyed to a real user_id, so the rest of the system
    (memory, diary, system prompt) works without caring whether the user is
    anon or registered.
    """
    token = (body.browser_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="browser_token is required")
    ip = _client_ip(request)

    user = get_user_by_browser_token(token)
    if user is None:
        # Fall back to IP lookup; if matched, attach this browser to that user.
        if ip:
            user = get_user_by_ip(ip)
            if user is not None:
                link_browser_token(user["user_id"], token, body.user_agent)
        if user is None:
            user = create_anon_user(token, ip, body.user_agent)

    # Always bump the IP record so user_ips.last_seen reflects this visit.
    if ip:
        touch_ip(user["user_id"], ip)

    jwt_token = jwt_encode(
        user["user_id"],
        user.get("email"),
        user.get("username"),
        user.get("account_type") or "anon",
    )
    return AnonBootstrapResponse(
        access_token=jwt_token,
        user_id=user["user_id"],
        account_type=user.get("account_type") or "anon",
        display_name=user.get("display_name"),
    )


# ---------------------------------------------------------------------------
#  Anon → Registered upgrade (Settings → Account → Sign Up)
# ---------------------------------------------------------------------------

class UpgradeRequest(BaseModel):
    email: str
    username: str
    password: str


class UpgradeResponse(TokenResponse):
    user_id: str
    account_type: str


@router.post("/upgrade", response_model=UpgradeResponse)
async def upgrade(body: UpgradeRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Attach credentials to the caller's existing (anon) user_id.

    Same user_id stays — all per-user data (memory, diary, soul.md, etc.)
    is keyed by user_id and continues working without re-keying.
    """
    if current_user.account_type != "anon":
        raise HTTPException(
            status_code=400,
            detail="Account is already registered. Use logout + signup to create a new one.",
        )
    email = body.email.strip().lower()
    username = body.username.strip()
    if not email or not username or not body.password:
        raise HTTPException(status_code=400, detail="email, username, and password are required")
    if get_user_by_email(email):
        raise HTTPException(status_code=409, detail="Email already registered")
    if get_user_by_username(username):
        raise HTTPException(status_code=409, detail="Username already taken")

    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    try:
        row = upgrade_anon_to_registered(current_user.user_id, email, username, pw_hash)
    except psycopg2.IntegrityError:
        raise HTTPException(status_code=409, detail="Email or username already taken")

    new_token = jwt_encode(row["user_id"], row["email"], row["username"], "registered")
    return UpgradeResponse(
        access_token=new_token,
        user_id=row["user_id"],
        account_type="registered",
    )


# ---------------------------------------------------------------------------
#  Existing endpoints
# ---------------------------------------------------------------------------

@router.post("/signup", response_model=TokenResponse)
async def signup(body: UserCreate):
    email = body.email.strip().lower()
    username = body.username.strip()
    if not email or not username or not body.password:
        raise HTTPException(status_code=400, detail="email, username, and password are required")
    if get_user_by_email(email):
        raise HTTPException(status_code=409, detail="Email already registered")
    if get_user_by_username(username):
        raise HTTPException(status_code=409, detail="Username already taken")

    user_id = str(uuid.uuid4())
    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    create_user(user_id, email, username, pw_hash)
    _seed_user_dir(user_id)

    token = jwt_encode(user_id, email, username, "registered")
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin):
    identifier = body.email_or_username.strip()
    user = get_user_by_email_or_username(identifier)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.get("password_hash"):
        # Anon users have NULL password_hash; they should never be reachable
        # via this path (no email/username), but guard just in case.
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not bcrypt.checkpw(body.password.encode(), user["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = jwt_encode(
        user["user_id"], user["email"], user["username"],
        user.get("account_type") or "registered",
    )
    return TokenResponse(access_token=token)


# ---------------------------------------------------------------------------
#  GET /me — extended with account_type, display_name, and quota usage
# ---------------------------------------------------------------------------

class MeResponse(BaseModel):
    user_id: str
    email: Optional[str] = None
    username: Optional[str] = None
    account_type: str
    display_name: Optional[str] = None
    tokens_used_today: int
    daily_token_cap: Optional[int] = None  # null = unlimited


@router.get("/me", response_model=MeResponse)
async def me(current_user: CurrentUser = Depends(get_current_user)):
    """Return the caller's profile + today's quota state.

    Re-fetches from the DB (rather than trusting the JWT) so display_name
    and account_type changes are seen immediately after upgrade. If the row
    was deleted out from under us, falls back to JWT claims.
    """
    row = get_user_by_id(current_user.user_id)
    if row is None:
        # JWT references a deleted user — surface it as 401 so the client
        # clears its localStorage and re-bootstraps.
        raise HTTPException(status_code=401, detail="User no longer exists")

    account_type = row.get("account_type") or "registered"
    used = await quota.tokens_used_today(current_user.user_id) if account_type == "anon" else 0
    cap = quota.cap_for(account_type)
    return MeResponse(
        user_id=row["user_id"],
        email=row.get("email"),
        username=row.get("username"),
        account_type=account_type,
        display_name=row.get("display_name"),
        tokens_used_today=used,
        daily_token_cap=cap,
    )
