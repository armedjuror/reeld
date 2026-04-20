import os
import json
import psycopg2
import psycopg2.extras
from datetime import datetime

from dotenv import load_dotenv

# ─── Connection ───────────────────────────────────────────────────────────────
# Set DATABASE_URL env var, e.g.:
# postgresql://user:password@localhost:5432/ReelD

load_dotenv()

def get_conn():
    url = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/reeld")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    return conn

# ─── Init ─────────────────────────────────────────────────────────────────────

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Users
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id          SERIAL PRIMARY KEY,
                    google_id   TEXT UNIQUE NOT NULL,
                    email       TEXT UNIQUE NOT NULL,
                    name        TEXT,
                    avatar_url  TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Presets (user-scoped, DB-backed)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS presets (
                    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE NOT NULL,
                    name        TEXT NOT NULL,
                    data        JSONB NOT NULL DEFAULT '{}',
                    share_token UUID UNIQUE DEFAULT gen_random_uuid(),
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Reels — the core entity
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reels (
                    id            SERIAL PRIMARY KEY,
                    user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    name          TEXT NOT NULL,
                    preset_id     TEXT,
                    preset_name   TEXT,
                    audio_path    TEXT,
                    intro_path    TEXT,
                    outro_path    TEXT,
                    segments      JSONB DEFAULT '[]',
                    last_output   TEXT,
                    status        TEXT DEFAULT 'draft',
                    created_at    TIMESTAMPTZ DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Migrate existing reels table if user_id column is missing
            cur.execute("""
                ALTER TABLE reels ADD COLUMN IF NOT EXISTS
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE
            """)
            cur.execute("""
                ALTER TABLE reels ADD COLUMN IF NOT EXISTS
                    last_charged_seconds REAL DEFAULT 0
            """)
            # User billing fields
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS
                    type TEXT DEFAULT 'free'
            """)
            # Credits passbook
            cur.execute("""
                CREATE TABLE IF NOT EXISTS credit_transactions (
                    id                  SERIAL PRIMARY KEY,
                    user_id             INTEGER REFERENCES users(id) ON DELETE CASCADE NOT NULL,
                    type                TEXT NOT NULL,
                    amount              INTEGER NOT NULL,
                    description         TEXT,
                    razorpay_payment_id TEXT,
                    razorpay_order_id   TEXT,
                    reel_id             INTEGER REFERENCES reels(id) ON DELETE SET NULL,
                    created_at          TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Generations log — each export event
            cur.execute("""
                CREATE TABLE IF NOT EXISTS generations (
                    id            SERIAL PRIMARY KEY,
                    reel_id       INTEGER REFERENCES reels(id) ON DELETE CASCADE,
                    preset_name   TEXT,
                    segment_count INTEGER,
                    duration_sec  REAL,
                    output_path   TEXT,
                    status        TEXT DEFAULT 'completed',
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        conn.commit()

# ─── Users ────────────────────────────────────────────────────────────────────

def upsert_user(google_id: str, email: str, name: str, avatar_url: str) -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO users (google_id, email, name, avatar_url)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (google_id) DO UPDATE
                    SET email = EXCLUDED.email, name = EXCLUDED.name,
                        avatar_url = EXCLUDED.avatar_url
                RETURNING *
            """, (google_id, email, name, avatar_url))
            row = dict(cur.fetchone())
            conn.commit()
            return row

def get_user_by_id(user_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None

# ─── Presets ──────────────────────────────────────────────────────────────────

def _flat_preset(row: dict) -> dict:
    data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
    return {**data, "id": str(row["id"]), "share_token": str(row["share_token"])}

def list_presets(user_id: int) -> list:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM presets WHERE user_id = %s ORDER BY updated_at DESC",
                (user_id,)
            )
            return [_flat_preset(dict(r)) for r in cur.fetchall()]

def get_preset(preset_id: str, user_id: int = None) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if user_id is not None:
                cur.execute(
                    "SELECT * FROM presets WHERE id = %s AND user_id = %s",
                    (preset_id, user_id)
                )
            else:
                cur.execute("SELECT * FROM presets WHERE id = %s", (preset_id,))
            row = cur.fetchone()
            return _flat_preset(dict(row)) if row else None

def create_preset(user_id: int, data: dict) -> dict:
    name = data.get("name", "Untitled")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO presets (user_id, name, data)
                VALUES (%s, %s, %s) RETURNING *
            """, (user_id, name, json.dumps(data)))
            row = dict(cur.fetchone())
            conn.commit()
            return _flat_preset(row)

def update_preset(preset_id: str, user_id: int, data: dict) -> dict | None:
    name = data.get("name", "Untitled")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                UPDATE presets SET name = %s, data = %s, updated_at = NOW()
                WHERE id = %s AND user_id = %s RETURNING *
            """, (name, json.dumps(data), preset_id, user_id))
            row = cur.fetchone()
            conn.commit()
            return _flat_preset(dict(row)) if row else None

def delete_preset(preset_id: str, user_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM presets WHERE id = %s AND user_id = %s",
                (preset_id, user_id)
            )
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted

def get_preset_by_share_token(token: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM presets WHERE share_token = %s", (token,))
            row = cur.fetchone()
            return _flat_preset(dict(row)) if row else None

# ─── Reels ────────────────────────────────────────────────────────────────────

def create_reel(name: str, preset_id: str, preset_name: str,
                audio_path: str, segments: list, user_id: int = None,
                intro_path: str = None, outro_path: str = None) -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO reels (user_id, name, preset_id, preset_name, audio_path,
                                   intro_path, outro_path, segments, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'draft')
                RETURNING *
            """, (user_id, name, preset_id, preset_name, audio_path,
                  intro_path, outro_path, json.dumps(segments)))
            row = dict(cur.fetchone())
            conn.commit()
            row["segments"] = _parse_json(row["segments"])
            return row

def get_reel(reel_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM reels WHERE id = %s", (reel_id,))
            row = cur.fetchone()
            if not row:
                return None
            row = dict(row)
            row["segments"] = _parse_json(row["segments"])
            return row

def list_reels(user_id: int = None) -> list:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if user_id is not None:
                cur.execute(
                    "SELECT * FROM reels WHERE user_id = %s ORDER BY updated_at DESC",
                    (user_id,)
                )
            else:
                cur.execute("SELECT * FROM reels ORDER BY updated_at DESC")
            rows = cur.fetchall()
            result = []
            for row in rows:
                row = dict(row)
                row["segments"] = _parse_json(row["segments"])
                result.append(row)
            return result

def update_reel(reel_id: int, **fields) -> dict | None:
    """
    Update any subset of reel fields.
    Supported: name, preset_id, preset_name, audio_path, intro_path,
               outro_path, segments, last_output, status
    """
    if not fields:
        return get_reel(reel_id)

    allowed = {"name", "preset_id", "preset_name", "audio_path",
               "intro_path", "outro_path", "segments", "last_output", "status"}
    fields = {k: v for k, v in fields.items() if k in allowed}

    if "segments" in fields:
        fields["segments"] = json.dumps(fields["segments"])

    set_clause = ", ".join(f"{k} = %s" for k in fields)
    set_clause += ", updated_at = NOW()"
    values = list(fields.values()) + [reel_id]

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE reels SET {set_clause} WHERE id = %s RETURNING *",
                values
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                return None
            row = dict(row)
            row["segments"] = _parse_json(row["segments"])
            return row

def delete_reel(reel_id: int) -> dict | None:
    """Delete reel and return it so caller can clean up files."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("DELETE FROM reels WHERE id = %s RETURNING *", (reel_id,))
            row = cur.fetchone()
            conn.commit()
            if not row:
                return None
            row = dict(row)
            row["segments"] = _parse_json(row["segments"])
            return row

# ─── Generations ──────────────────────────────────────────────────────────────

def log_generation(reel_id: int, preset_name: str, segment_count: int,
                   duration_sec: float, output_path: str,
                   status: str = "completed") -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO generations (reel_id, preset_name, segment_count,
                                         duration_sec, output_path, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (reel_id, preset_name, segment_count,
                  round(duration_sec, 2), output_path, status))
            gen_id = cur.fetchone()[0]
            conn.commit()
            return gen_id

def get_generations_for_reel(reel_id: int) -> list:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM generations WHERE reel_id = %s
                ORDER BY created_at DESC
            """, (reel_id,))
            return [dict(r) for r in cur.fetchall()]

# ─── Credits ──────────────────────────────────────────────────────────────────

CREDIT_PRICE_PAISE = 50   # 0.50 rupees per credit (current offer), in paise
CREDIT_PRICE_LABEL = "₹0.50"

def get_credit_balance(user_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(amount), 0)::int FROM credit_transactions WHERE user_id = %s",
                (user_id,)
            )
            return cur.fetchone()[0]

def add_credits(user_id: int, amount: int, description: str,
                razorpay_payment_id: str = None, razorpay_order_id: str = None) -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO credit_transactions
                    (user_id, type, amount, description, razorpay_payment_id, razorpay_order_id)
                VALUES (%s, 'recharge', %s, %s, %s, %s) RETURNING *
            """, (user_id, amount, description, razorpay_payment_id, razorpay_order_id))
            row = dict(cur.fetchone())
            conn.commit()
            row["created_at"] = str(row["created_at"])
            return row

def deduct_credits(user_id: int, amount: int, description: str, reel_id: int = None) -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO credit_transactions
                    (user_id, type, amount, description, reel_id)
                VALUES (%s, 'export_deduction', %s, %s, %s) RETURNING *
            """, (user_id, -amount, description, reel_id))
            row = dict(cur.fetchone())
            conn.commit()
            row["created_at"] = str(row["created_at"])
            return row

def get_credit_transactions(user_id: int) -> list:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM credit_transactions
                WHERE user_id = %s ORDER BY created_at DESC
            """, (user_id,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["created_at"] = str(r["created_at"])
            return rows

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_json(val):
    if isinstance(val, str):
        return json.loads(val)
    return val if val is not None else []
