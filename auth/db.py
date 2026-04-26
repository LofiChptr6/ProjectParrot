"""Synchronous PostgreSQL helpers for the users table.

Uses psycopg2 (already a transitive dep via asyncpg-adjacent stack) for
one-shot auth operations. Auth calls are rare (login/signup) so sync is fine.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import psycopg2
import psycopg2.extras

DSN = "postgresql://mocha:5369@127.0.0.1:5432/mocha"

_ROOT = Path(__file__).resolve().parent.parent


def _conn():
    return psycopg2.connect(DSN)


def _seed_user_dir(user_id: str) -> None:
    """Copy default soul.md / behaviors.yaml into data/users/{user_id}/.

    Mirrors auth.migrations._seed_user_dir; inlined here so anon-user creation
    at runtime doesn't have to import the migration script.
    """
    user_dir = _ROOT / "data" / "users" / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("soul.md", "behaviors.yaml"):
        src = _ROOT / "character" / fname
        dst = user_dir / fname
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)


def get_user_by_email(email: str) -> dict | None:
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_user_by_email_or_username(identifier: str) -> dict | None:
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM users WHERE email = %s OR username = %s",
            (identifier, identifier),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def create_user(user_id: str, email: str, username: str, password_hash: str) -> dict:
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO users (user_id, email, username, password_hash)
               VALUES (%s, %s, %s, %s)
               RETURNING user_id, email, username, created_at""",
            (user_id, email, username, password_hash),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)


def get_user_by_id(user_id: str) -> dict | None:
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_user_setting(user_id: str, key: str):
    """Return a single JSON field from settings, or None."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT settings->%s FROM users WHERE user_id = %s", (key, user_id))
        row = cur.fetchone()
        if row is None:
            return None
        import json as _json
        val = row[0]
        return _json.loads(val) if isinstance(val, str) else val


def update_user_setting(user_id: str, key: str, value) -> None:
    import json as _json
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET settings = jsonb_set(settings, %s, %s::jsonb) WHERE user_id = %s",
            ([key], _json.dumps(value), user_id),
        )
        conn.commit()


def delete_user_setting(user_id: str, key: str) -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET settings = settings - %s WHERE user_id = %s",
            (key, user_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
#  Anonymous-user identity (browser token + IP)
# ---------------------------------------------------------------------------

def get_user_by_browser_token(browser_token: str) -> dict | None:
    """Resolve user_id from a browser_token (= localStorage parrot.session_id).

    Side-effect: bumps user_devices.last_seen if found. Returns the full user row
    (joined nothing — caller can re-fetch by id if it needs settings).
    """
    if not browser_token:
        return None
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """UPDATE user_devices SET last_seen = now()
               WHERE browser_token = %s
               RETURNING user_id""",
            (browser_token,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("SELECT * FROM users WHERE user_id = %s", (row["user_id"],))
        u = cur.fetchone()
        conn.commit()
        return dict(u) if u else None


def get_user_by_ip(ip: str) -> dict | None:
    """Resolve user_id from an IP address (most-recently-seen wins).

    Used as a fallback when no browser_token matches — e.g. user cleared
    localStorage but is hitting from the same network. Returns the full user row.
    """
    if not ip:
        return None
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT u.* FROM users u
               JOIN user_ips i ON i.user_id = u.user_id
               WHERE i.ip_address = %s
               ORDER BY i.last_seen DESC
               LIMIT 1""",
            (ip,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def link_browser_token(user_id: str, browser_token: str, user_agent: str | None = None) -> None:
    """Attach a browser_token to an existing user. Idempotent (UNIQUE on token)."""
    if not browser_token:
        return
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO user_devices (user_id, browser_token, user_agent)
               VALUES (%s, %s, %s)
               ON CONFLICT (browser_token) DO UPDATE
                 SET last_seen = now()""",
            (user_id, browser_token, user_agent),
        )
        conn.commit()


def touch_ip(user_id: str, ip: str) -> None:
    """Record (or bump last_seen for) an IP the user has visited from."""
    if not ip:
        return
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO user_ips (user_id, ip_address)
               VALUES (%s, %s)
               ON CONFLICT (user_id, ip_address) DO UPDATE
                 SET last_seen = now()""",
            (user_id, ip),
        )
        conn.commit()


def create_anon_user(browser_token: str, ip: str | None, user_agent: str | None = None) -> dict:
    """Create a new anonymous user with NULL email/username/password.

    Seeds data/users/{user_id}/ with default character files, links the browser
    token, and records the IP if given. Returns the full users row.
    """
    user_id = str(uuid.uuid4())
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO users (user_id, account_type)
               VALUES (%s, 'anon')
               RETURNING *""",
            (user_id,),
        )
        row = cur.fetchone()
        # Link device + IP in the same transaction.
        if browser_token:
            cur.execute(
                """INSERT INTO user_devices (user_id, browser_token, user_agent)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (browser_token) DO NOTHING""",
                (user_id, browser_token, user_agent),
            )
        if ip:
            cur.execute(
                """INSERT INTO user_ips (user_id, ip_address)
                   VALUES (%s, %s)
                   ON CONFLICT (user_id, ip_address) DO NOTHING""",
                (user_id, ip),
            )
        conn.commit()
    _seed_user_dir(user_id)
    return dict(row)


def upgrade_anon_to_registered(
    user_id: str, email: str, username: str, password_hash: str
) -> dict:
    """Attach credentials to an existing (anon) user. Same user_id; all per-user
    data (memory, diary, soul.md, etc.) keeps working without re-keying.

    Raises psycopg2.IntegrityError if email/username already taken.
    """
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """UPDATE users
               SET email = %s, username = %s, password_hash = %s,
                   account_type = 'registered'
               WHERE user_id = %s
               RETURNING user_id, email, username, account_type, created_at""",
            (email, username, password_hash, user_id),
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            raise ValueError(f"user_id {user_id} not found")
        return dict(row)


def set_display_name(user_id: str, name: str) -> None:
    """Persist the name Mocha learned for this user."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET display_name = %s WHERE user_id = %s",
            (name, user_id),
        )
        conn.commit()


def get_all_telegram_users() -> list[dict]:
    """Return all users who have a telegram_bot_token in their settings."""
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT user_id, username,
                      settings->>'telegram_bot_token' AS telegram_bot_token
               FROM users
               WHERE settings ? 'telegram_bot_token'
                 AND settings->>'telegram_bot_token' != ''""",
        )
        return [dict(r) for r in cur.fetchall()]
