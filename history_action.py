"""
history_action.py — run by the GitHub Action (NOT the web app).

Logs per-player, per-gameweek history from the FPL `element-summary` endpoint
into the app's gw_history table (via /ingest/gw-history). This is the accuracy
foundation: it enables recent-form blends (last-6 / last-10) and backtesting of
the xPts model against real outcomes.

Strategy (kind to the FPL API, ~600 players):
  * Read bootstrap to find the latest FINISHED gameweek and the element list.
  * On a normal daily run, log just the latest finished GW for every player
    (one element-summary call each — the history endpoint for a player returns
    ALL their GWs, so we filter to the target GW). Idempotent on the app side.
  * BACKFILL=1 logs every finished GW so far (first run / catch-up).

Env vars (provided by the workflow as secrets):
  APP_URL      e.g. https://fpl-dashboard-txgc.onrender.com
  CRON_TOKEN   shared secret, matches the app's CRON_TOKEN
  BACKFILL     "1" to log all finished GWs (default: latest finished GW only)

Usage: python history_action.py
"""
from __future__ import annotations

import os
import sys
import time

import requests

FPL_BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FPLIQ-history/1.0)"}


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _get(url: str, timeout: int = 30, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = r
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


def finished_gws(bootstrap: dict) -> list[int]:
    return [e["id"] for e in bootstrap.get("events", []) if e.get("finished")]


def main() -> int:
    app_url = os.environ.get("APP_URL", "").rstrip("/")
    token = os.environ.get("CRON_TOKEN", "")
    if not app_url or not token:
        print("Missing APP_URL / CRON_TOKEN", file=sys.stderr)
        return 0  # soft-fail, never block the pipeline

    try:
        bootstrap = _get(f"{FPL_BASE}/bootstrap-static/")
    except Exception as exc:  # noqa: BLE001
        print(f"bootstrap fetch failed: {exc}", file=sys.stderr)
        return 0

    done = finished_gws(bootstrap)
    if not done:
        print("No finished gameweeks yet — nothing to log.")
        return 0

    backfill = os.environ.get("BACKFILL", "0") == "1"
    target_gws = set(done) if backfill else {max(done)}
    print(f"Logging GW history for GWs: {sorted(target_gws)} "
          f"({'backfill' if backfill else 'latest only'})")

    elements = bootstrap.get("elements", [])
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    teams = {t["id"]: t.get("short_name", "") for t in bootstrap.get("teams", [])}

    rows, errors = [], 0
    for i, el in enumerate(elements):
        eid = el["id"]
        try:
            summ = _get(f"{FPL_BASE}/element-summary/{eid}/")
        except Exception:
            errors += 1
            continue
        for h in summ.get("history", []):
            gw = h.get("round")
            if gw not in target_gws:
                continue
            rows.append({
                "element": eid, "gw": gw,
                "name": el.get("web_name"),
                "team": teams.get(el.get("team"), ""),
                "position": pos_map.get(el.get("element_type"), "?"),
                "minutes": h.get("minutes", 0),
                "total_points": h.get("total_points", 0),
                "goals": h.get("goals_scored", 0),
                "assists": h.get("assists", 0),
                "clean_sheets": h.get("clean_sheets", 0),
                "goals_conceded": h.get("goals_conceded", 0),
                "saves": h.get("saves", 0),
                "bonus": h.get("bonus", 0),
                "bps": h.get("bps", 0),
                "yellow_cards": h.get("yellow_cards", 0),
                "red_cards": h.get("red_cards", 0),
                "xg": _f(h.get("expected_goals")),
                "xa": _f(h.get("expected_assists")),
                "xgi": _f(h.get("expected_goal_involvements")),
                "xgc": _f(h.get("expected_goals_conceded")),
                "defcon": h.get("defensive_contribution", 0) or 0,
                "was_home": h.get("was_home"),
                "opponent_team": h.get("opponent_team"),
                "value": h.get("value"),
                "selected": h.get("selected"),
            })
        # be gentle on the API
        if i % 50 == 0 and i:
            time.sleep(1)

    print(f"Collected {len(rows)} history rows ({errors} player fetch errors).")
    if not rows:
        return 0

    # POST in batches so we never send a giant payload.
    stored = 0
    for b in range(0, len(rows), 300):
        batch = rows[b:b + 300]
        try:
            r = requests.post(f"{app_url}/ingest/gw-history",
                              params={"token": token},
                              json={"history": batch}, timeout=60)
            print(f"POST /ingest/gw-history [{b}:{b+len(batch)}] ->",
                  r.status_code, r.text[:120])
            if r.status_code == 200:
                stored += len(batch)
        except Exception as exc:  # noqa: BLE001
            print(f"batch POST failed: {exc}", file=sys.stderr)
    print(f"Done — stored ~{stored} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
