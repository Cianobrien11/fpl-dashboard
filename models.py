"""
models.py — persistence for the FPL Dashboard.

Two tables:
  snapshot(id, data)   single-row JSON blob of the current data bundle
  squad(id, data)      single-row JSON blob of the user's squad

Kept deliberately simple — the data is small and read-mostly, so a JSON
blob per logical object is easier to evolve than a wide relational schema.

Backend is chosen automatically:
  * If DATABASE_URL is set (Render Postgres), use Postgres via psycopg.
  * Otherwise fall back to a local SQLite file (fpl.db) for dev.
This means the same code persists across redeploys on Render's free tier
(Postgres is durable) while staying zero-config locally.
"""
from __future__ import annotations

import json
import os
import sqlite3

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

try:
    BASE = os.path.dirname(os.path.abspath(__file__))
except NameError:  # executed without a __file__ (e.g. sandbox exec)
    BASE = os.path.join(os.environ.get("WORKSPACE_DIR", "."), "artifacts", "fpl_dashboard")
DB_PATH = os.path.join(BASE, "fpl.db")


# --------------------------------------------------------------------------
# Connection helpers — Postgres (prod) or SQLite (dev)
# --------------------------------------------------------------------------
def _pg_conn():
    import psycopg  # imported lazily so SQLite-only dev needs no driver
    # Render sometimes provides the legacy "postgres://" scheme
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url)


def _sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# Postgres uses %s placeholders; SQLite uses ?
_PH = "%s" if USE_PG else "?"


def init_db() -> None:
    pk = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ddl = [
        "CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY, data TEXT)",
        "CREATE TABLE IF NOT EXISTS squad (id INTEGER PRIMARY KEY, data TEXT)",
        f"CREATE TABLE IF NOT EXISTS predictions_log (id {pk}, gw INTEGER, "
        "logged_at TEXT, data TEXT)",
        # --- account groundwork (ready for Phase B multi-user auth) ---
        # users: one row per account. password_hash stays NULL until auth ships.
        f"CREATE TABLE IF NOT EXISTS users (id {pk}, email TEXT UNIQUE, "
        "password_hash TEXT, created_at TEXT, verified INTEGER DEFAULT 0, "
        "verify_token TEXT, verify_expires TEXT)",
        # user_settings: per-user JSON blob (team id, favourite team, prefs).
        # user_id 0 is reserved for the current single-user / anonymous device.
        "CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, data TEXT)",
    ]
    # Idempotent migrations: add columns to a pre-existing users table.
    # CREATE TABLE IF NOT EXISTS won't alter an existing table, so a users
    # table created by an earlier (groundwork) deploy would miss the auth
    # columns. These ALTERs bring it up to date safely.
    pg_migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS verified INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS verify_token TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS verify_expires TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TEXT",
    ]
    # SQLite: ADD COLUMN IF NOT EXISTS isn't supported, so add only if missing.
    sqlite_cols = {"verified": "INTEGER DEFAULT 0", "verify_token": "TEXT",
                   "verify_expires": "TEXT", "password_hash": "TEXT",
                   "created_at": "TEXT"}

    if USE_PG:
        conn = _pg_conn()
        try:
            # Base tables in one transaction.
            with conn, conn.cursor() as cur:
                for stmt in ddl:
                    cur.execute(stmt)
            # Each migration in its OWN transaction so one failure can't roll
            # back the others (ADD COLUMN IF NOT EXISTS is safe to re-run).
            for stmt in pg_migrations:
                try:
                    with conn, conn.cursor() as cur:
                        cur.execute(stmt)
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        finally:
            conn.close()
    else:
        with _sqlite_conn() as c:
            for stmt in ddl:
                c.execute(stmt)
            # discover existing columns on users, add any missing
            try:
                existing = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
                for col, decl in sqlite_cols.items():
                    if col not in existing:
                        c.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
            except Exception:
                pass


def _upsert(table: str, payload: str) -> None:
    """Replace the single row (id=1) in the given table."""
    if USE_PG:
        conn = _pg_conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(f"DELETE FROM {table}")
                cur.execute(f"INSERT INTO {table} (id, data) VALUES (1, {_PH})", (payload,))
        finally:
            conn.close()
    else:
        with _sqlite_conn() as c:
            c.execute(f"DELETE FROM {table}")
            c.execute(f"INSERT INTO {table} (id, data) VALUES (1, ?)", (payload,))


def _fetch(table: str) -> str | None:
    if USE_PG:
        conn = _pg_conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(f"SELECT data FROM {table} WHERE id = 1")
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    else:
        with _sqlite_conn() as c:
            row = c.execute(f"SELECT data FROM {table} WHERE id = 1").fetchone()
        return row["data"] if row else None


def save_snapshot(bundle: dict) -> None:
    init_db()
    _upsert("snapshot", json.dumps(bundle))


def load_snapshot() -> dict | None:
    init_db()
    raw = _fetch("snapshot")
    if not raw:
        return None
    snap = json.loads(raw)
    # JSON turns int fixture gameweeks fine, but normalise fixture gw to int
    for team, fx in snap.get("fixtures", {}).items():
        for f in fx:
            f["gw"] = int(f["gw"])
    return snap


def save_squad(squad: list[dict]) -> None:
    init_db()
    _upsert("squad", json.dumps(squad))


def load_squad() -> list[dict]:
    init_db()
    raw = _fetch("squad")
    return json.loads(raw) if raw else []


def log_predictions(gw: int, preds: list) -> None:
    """Append a gameweek's scoreline predictions for later accuracy scoring.

    Idempotent per (gw): if a row for this gw already exists we skip, so
    repeated refreshes in the same gameweek don't create duplicates.
    """
    import datetime as _dt
    init_db()
    ph = _PH
    if USE_PG:
        conn = _pg_conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(f"SELECT 1 FROM predictions_log WHERE gw = {ph}", (gw,))
                if cur.fetchone():
                    return
                cur.execute(
                    f"INSERT INTO predictions_log (gw, logged_at, data) VALUES ({ph},{ph},{ph})",
                    (gw, _dt.datetime.utcnow().isoformat(), json.dumps(preds)))
        finally:
            conn.close()
    else:
        with _sqlite_conn() as c:
            if c.execute("SELECT 1 FROM predictions_log WHERE gw = ?", (gw,)).fetchone():
                return
            c.execute("INSERT INTO predictions_log (gw, logged_at, data) VALUES (?,?,?)",
                      (gw, _dt.datetime.utcnow().isoformat(), json.dumps(preds)))


def load_prediction_logs() -> list:
    """Return [{gw, logged_at, preds}] for all logged gameweeks, oldest first."""
    init_db()
    rows = []
    if USE_PG:
        conn = _pg_conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT gw, logged_at, data FROM predictions_log ORDER BY gw")
                for gw, logged_at, data in cur.fetchall():
                    rows.append({"gw": gw, "logged_at": logged_at, "preds": json.loads(data)})
        finally:
            conn.close()
    else:
        with _sqlite_conn() as c:
            for r in c.execute("SELECT gw, logged_at, data FROM predictions_log ORDER BY gw").fetchall():
                rows.append({"gw": r["gw"], "logged_at": r["logged_at"], "preds": json.loads(r["data"])})
    return rows


# ---------------------------------------------------------------------------
# Settings (account groundwork). For now a single row (user_id=0 = this device)
# so the Settings page persists server-side too; multi-user auth later keys by
# the real user id.
# ---------------------------------------------------------------------------
def save_settings(data: dict, user_id: int = 0) -> None:
    init_db()
    payload = json.dumps(data)
    ph = "%s" if USE_PG else "?"
    if USE_PG:
        conn = _pg_conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("DELETE FROM user_settings WHERE user_id = %s", (user_id,))
                cur.execute("INSERT INTO user_settings (user_id, data) VALUES (%s, %s)",
                            (user_id, payload))
        finally:
            conn.close()
    else:
        with _sqlite_conn() as c:
            c.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
            c.execute("INSERT INTO user_settings (user_id, data) VALUES (?, ?)",
                      (user_id, payload))


def load_settings(user_id: int = 0) -> dict:
    init_db()
    ph = "%s" if USE_PG else "?"
    if USE_PG:
        conn = _pg_conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT data FROM user_settings WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
        finally:
            conn.close()
    else:
        with _sqlite_conn() as c:
            cur = c.execute("SELECT data FROM user_settings WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return {}
    return {}


# ---------------------------------------------------------------------------
# Accounts / auth data layer. Passwords hashed via werkzeug (ships with Flask).
# Email verification: unverified users get a token; verifying flips verified=1.
# ---------------------------------------------------------------------------
import datetime as _dt
import secrets as _secrets

from werkzeug.security import check_password_hash, generate_password_hash


def _q(sql_pg: str, sql_sqlite: str, params=(), fetch=None):
    """Run a parametrised query against whichever backend is active.
    fetch: None (write), 'one', or 'all'. Returns rows for fetch modes."""
    init_db()
    if USE_PG:
        conn = _pg_conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(sql_pg, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
        finally:
            conn.close()
    else:
        with _sqlite_conn() as c:
            cur = c.execute(sql_sqlite, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
    return None


def get_user_by_email(email: str):
    """Return a user row dict or None."""
    email = (email or "").strip().lower()
    row = _q("SELECT id, email, password_hash, created_at, verified, verify_token, verify_expires "
             "FROM users WHERE email = %s",
             "SELECT id, email, password_hash, created_at, verified, verify_token, verify_expires "
             "FROM users WHERE email = ?",
             (email,), fetch="one")
    if not row:
        return None
    keys = ["id", "email", "password_hash", "created_at", "verified", "verify_token", "verify_expires"]
    return dict(zip(keys, row))


def create_user(email: str, password: str) -> dict:
    """Create an UNVERIFIED user with a fresh verification token.
    Returns {ok, error, token, user_id}. Does not send the email (app layer does)."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"ok": False, "error": "Please enter a valid email address."}
    if not password or len(password) < 8:
        return {"ok": False, "error": "Password must be at least 8 characters."}
    if get_user_by_email(email):
        return {"ok": False, "error": "An account with that email already exists."}
    pw_hash = generate_password_hash(password)
    token = _secrets.token_urlsafe(32)
    now = _dt.datetime.now(_dt.timezone.utc)
    expires = (now + _dt.timedelta(hours=24)).isoformat()
    _q("INSERT INTO users (email, password_hash, created_at, verified, verify_token, verify_expires) "
       "VALUES (%s, %s, %s, 0, %s, %s)",
       "INSERT INTO users (email, password_hash, created_at, verified, verify_token, verify_expires) "
       "VALUES (?, ?, ?, 0, ?, ?)",
       (email, pw_hash, now.isoformat(), token, expires))
    user = get_user_by_email(email)
    return {"ok": True, "error": None, "token": token,
            "user_id": user["id"] if user else None}


def verify_user_token(token: str) -> dict:
    """Activate the account matching this verification token (if not expired).
    Returns {ok, error, email}."""
    if not token:
        return {"ok": False, "error": "Missing verification token."}
    row = _q("SELECT id, email, verify_expires, verified FROM users WHERE verify_token = %s",
             "SELECT id, email, verify_expires, verified FROM users WHERE verify_token = ?",
             (token,), fetch="one")
    if not row:
        return {"ok": False, "error": "Invalid or already-used verification link."}
    uid, email, expires, verified = row
    if verified:
        return {"ok": True, "error": None, "email": email, "already": True}
    try:
        if expires and _dt.datetime.fromisoformat(expires) < _dt.datetime.now(_dt.timezone.utc):
            return {"ok": False, "error": "This verification link has expired. Please sign up again or resend."}
    except (ValueError, TypeError):
        pass
    _q("UPDATE users SET verified = 1, verify_token = NULL WHERE id = %s",
       "UPDATE users SET verified = 1, verify_token = NULL WHERE id = ?",
       (uid,))
    return {"ok": True, "error": None, "email": email}


def set_verify_token(email: str) -> str | None:
    """Issue a fresh verification token for an existing unverified user
    (for 'resend verification'). Returns the token or None."""
    user = get_user_by_email(email)
    if not user or user["verified"]:
        return None
    token = _secrets.token_urlsafe(32)
    expires = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=24)).isoformat()
    _q("UPDATE users SET verify_token = %s, verify_expires = %s WHERE id = %s",
       "UPDATE users SET verify_token = ?, verify_expires = ? WHERE id = ?",
       (token, expires, user["id"]))
    return token


def check_login(email: str, password: str) -> dict:
    """Validate credentials. Returns {ok, error, user_id, verified, email}."""
    user = get_user_by_email(email)
    if not user or not user.get("password_hash"):
        return {"ok": False, "error": "No account found with that email."}
    if not check_password_hash(user["password_hash"], password or ""):
        return {"ok": False, "error": "Incorrect password."}
    if not user["verified"]:
        return {"ok": False, "error": "Please verify your email first — check your inbox.",
                "unverified": True, "email": user["email"]}
    return {"ok": True, "error": None, "user_id": user["id"],
            "verified": True, "email": user["email"]}
