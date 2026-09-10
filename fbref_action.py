"""
fbref_action.py — run by the GitHub Action (NOT by the web app).

Pulls REAL per-player shot / key-pass data from Understat via the
`understatapi` library (which decodes Understat's embedded JSON), computes
per-90 rates, and POSTs them to the app's secure /ingest/shots endpoint.

Understat is used because FBRef 403-blocks cloud/server IPs. GitHub's
runners can reach Understat's full page, and understatapi handles the
JSON extraction that a plain download misses.

Understat player fields used:
  player_name, team_title, shots, key_passes, time (minutes), games

Env vars (provided by the workflow as secrets):
  APP_URL      e.g. https://fpl-dashboard-txgc.onrender.com
  CRON_TOKEN   shared secret, matches the app's CRON_TOKEN

Usage: python fbref_action.py
"""
from __future__ import annotations

import os
import sys

import requests

SEASON = os.environ.get("UNDERSTAT_SEASON", "2026")  # 2026 = 2026/27 season

# Understat team_title -> our canonical team names
UNDERSTAT_TEAM = {
    "Arsenal": "Arsenal", "Aston Villa": "Aston Villa", "Bournemouth": "Bournemouth",
    "Brentford": "Brentford", "Brighton": "Brighton", "Chelsea": "Chelsea",
    "Coventry": "Coventry", "Crystal Palace": "Crystal Palace", "Everton": "Everton",
    "Fulham": "Fulham", "Hull": "Hull City", "Hull City": "Hull City",
    "Ipswich": "Ipswich", "Ipswich Town": "Ipswich", "Leeds": "Leeds",
    "Leeds United": "Leeds", "Liverpool": "Liverpool", "Manchester City": "Man City",
    "Manchester United": "Man United", "Newcastle United": "Newcastle",
    "Nottingham Forest": "Nottm Forest", "Tottenham": "Tottenham",
    "Sunderland": "Sunderland",
}


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def scrape() -> dict:
    """Return {"surname|Squad": {shots_90, sot_90, kp_90}} for the app to match."""
    from understatapi import UnderstatClient

    out: dict[str, dict] = {}
    with UnderstatClient() as understat:
        players = understat.league(league="EPL").get_player_data(season=SEASON)

    for p in players:
        name = (p.get("player_name") or "").strip()
        if not name:
            continue
        minutes = _f(p.get("time"))
        if minutes < 45:            # skip tiny samples
            continue
        n90 = minutes / 90.0
        shots = _f(p.get("shots"))
        kp = _f(p.get("key_passes"))
        # Understat has no SoT column; estimate from shots (league SoT rate ~0.35)
        # so the SoT/90 column stays meaningful. Shots & KP are the real figures.
        sot_est = shots * 0.35

        squad = UNDERSTAT_TEAM.get((p.get("team_title") or "").strip(),
                                   (p.get("team_title") or "").strip())
        surname = name.split()[-1].lower()
        out[f"{surname}|{squad}"] = {
            "shots_90": round(shots / n90, 2),
            "sot_90": round(sot_est / n90, 2),
            "kp_90": round(kp / n90, 2),
        }
    return out


def main() -> int:
    app_url = os.environ.get("APP_URL", "").rstrip("/")
    token = os.environ.get("CRON_TOKEN", "")
    if not app_url or not token:
        print("Missing APP_URL or CRON_TOKEN env vars", file=sys.stderr)
        return 1

    try:
        data = scrape()
    except Exception as exc:  # noqa: BLE001
        print(f"Understat scrape failed: {exc}", file=sys.stderr)
        return 1

    n = len(data)
    print(f"Scraped real shot/key-pass data for {n} players from Understat")
    if n < 20:
        print("Too few players parsed — aborting so we don't overwrite good data",
              file=sys.stderr)
        return 1

    resp = requests.post(f"{app_url}/ingest/shots",
                         params={"token": token},
                         json={"players": data}, timeout=60)
    print("POST /ingest/shots ->", resp.status_code, resp.text[:300])
    resp.raise_for_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
