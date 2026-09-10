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
    ]
    if USE_PG:
        conn = _pg_conn()
        try:
            with conn, conn.cursor() as cur:
                for stmt in ddl:
                    cur.execute(stmt)
        finally:
            conn.close()
    else:
        with _sqlite_conn() as c:
            for stmt in ddl:
                c.execute(stmt)


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
