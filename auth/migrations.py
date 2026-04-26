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

-- Anonymous user support: relax uniqueness constraints + add account_type/display_name.
-- email/username/password_hash become nullable so anon users (no credentials) can exist.
-- ALTER COLUMN ... DROP NOT NULL is idempotent in PG14+ (no-op if already nullable).
ALTER TABLE users ALTER COLUMN email         DROP NOT NULL;
ALTER TABLE users ALTER COLUMN username      DROP NOT NULL;
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS account_type TEXT NOT NULL DEFAULT 'registered';
ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT;

-- Browser-token (= parrot.session_id from localStorage) → user_id mapping.
-- Primary anon identity: stable per-browser, survives IP changes.
CREATE TABLE IF NOT EXISTS user_devices (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    browser_token TEXT UNIQUE NOT NULL,
    user_agent    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_devices_user ON user_devices(user_id);

-- IP → user_id mapping. Fallback identity when browser token is missing
-- (e.g. cleared localStorage); also records "user has visited from these networks".
CREATE TABLE IF NOT EXISTS user_ips (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    ip_address  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, ip_address)
);
CREATE INDEX IF NOT EXISTS idx_user_ips_ip ON user_ips(ip_address);
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
