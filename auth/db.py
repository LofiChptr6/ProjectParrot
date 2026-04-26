"""Synchronous PostgreSQL helpers for the users table.

Uses psycopg2 (already a transitive dep via asyncpg-adjacent stack) for
one-shot auth operations. Auth calls are rare (login/signup) so sync is fine.
"""

from __future__ import annotations

import psycopg2
import psycopg2.extras

DSN = "postgresql://mocha:5369@127.0.0.1:5432/mocha"


def _conn():
    return psycopg2.connect(DSN)


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
