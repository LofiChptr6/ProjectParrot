"""Run once to create the users table, indexes, and the ika_admin superuser."""

import json
import shutil
import uuid
from pathlib import Path

import bcrypt
import psycopg2

DSN = "postgresql://mocha:5369@127.0.0.1:5432/mocha"

ROOT = Path(__file__).resolve().parent.parent

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT UNIQUE NOT NULL DEFAULT gen_random_uuid()::TEXT,
    email         TEXT UNIQUE NOT NULL,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    settings      JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_llm_log_user ON llm_call_log(user_id);
"""

ADMIN_USERNAME = "ika_admin"
ADMIN_EMAIL    = "ika_admin@local"
ADMIN_PASSWORD = "9999"

# The existing telegram token migrates to ika_admin's settings.
ADMIN_TELEGRAM_TOKEN = "***REMOVED***"


def _seed_user_dir(user_id: str) -> None:
    user_dir = ROOT / "data" / "users" / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("soul.md", "behaviors.yaml"):
        src = ROOT / "character" / fname
        dst = user_dir / fname
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)


def _run() -> None:
    conn = psycopg2.connect(DSN)
    with conn, conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)

    # Create ika_admin if not already present.
    with conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE username = %s", (ADMIN_USERNAME,))
        row = cur.fetchone()
        if row:
            admin_user_id = row[0]
            print(f"ika_admin already exists (user_id={admin_user_id}), skipping creation.")
        else:
            admin_user_id = str(uuid.uuid4())
            pw_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
            settings = json.dumps({"telegram_bot_token": ADMIN_TELEGRAM_TOKEN})
            cur.execute(
                """INSERT INTO users (user_id, email, username, password_hash, settings)
                   VALUES (%s, %s, %s, %s, %s::jsonb)""",
                (admin_user_id, ADMIN_EMAIL, ADMIN_USERNAME, pw_hash, settings),
            )
            print(f"Created ika_admin (user_id={admin_user_id})")

    conn.close()
    _seed_user_dir(admin_user_id)
    print(f"Seeded data/users/{admin_user_id}/")
    print("Migration complete.")


if __name__ == "__main__":
    _run()
