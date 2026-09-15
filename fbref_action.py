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
# H2H window: last 2 seasons (current + previous). Understat match rows carry
# a 'season' field ("2026", "2025", ...). Only these count toward H2H.
H2H_SEASONS = {str(int(SEASON)), str(int(SEASON) - 1)}

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


def scrape_h2h(max_players: int = 350) -> dict:
    """
    Build LAST-2-SEASONS per-opponent records for EPL players from Understat.

    For each current EPL player we fetch their full match history and total
    goals / assists / games / xG grouped by the OPPONENT team. This powers the
    "player vs next opponent" section.

    Returns {"surname|Squad": {opponent_canonical: {games, goals, assists, xg}}}
    keyed the same way as the shots data so the app matches identically.
    """
    from understatapi import UnderstatClient

    out = {}
    with UnderstatClient() as understat:
        players = understat.league(league="EPL").get_player_data(season=SEASON)
        # only players with meaningful minutes this season (keeps it quick)
        players = [p for p in players if _f(p.get("time")) >= 45][:max_players]

        for p in players:
            pid = p.get("id")
            name = (p.get("player_name") or "").strip()
            team_title = (p.get("team_title") or "").strip()
            if not pid or not name:
                continue
            try:
                matches = understat.player(player=str(pid)).get_match_data()
            except Exception:  # noqa: BLE001
                continue

            recs = {}
            for m in matches:
                # opponent is whichever side isn't the player's team in that match
                h, a = (m.get("h_team") or "").strip(), (m.get("a_team") or "").strip()
                # the player's team for that match isn't directly given per row,
                # so infer: opponent = the team that isn't the player's CURRENT
                # club title where possible; fall back to using both sides minus
                # the most-frequent (their club).
                opp_raw = None
                # roster/side hints aren't always present; use goals context:
                # Understat match rows include 'h_a' = 'h' or 'a' for the player.
                # last-2-seasons filter
                if str(m.get("season", "")) not in H2H_SEASONS:
                    continue
                side = m.get("h_a")
                if side == "h":
                    opp_raw = a
                elif side == "a":
                    opp_raw = h
                else:
                    continue
                opp = UNDERSTAT_TEAM.get(opp_raw, opp_raw)
                r = recs.setdefault(opp, {"games": 0, "goals": 0, "assists": 0, "xg": 0.0})
                r["games"] += 1
                r["goals"] += int(_f(m.get("goals")))
                r["assists"] += int(_f(m.get("assists")))
                r["xg"] += _f(m.get("xG"))
            if not recs:
                continue
            for opp in recs:
                recs[opp]["xg"] = round(recs[opp]["xg"], 2)
            squad = UNDERSTAT_TEAM.get(team_title, team_title)
            out[f"{name.split()[-1].lower()}|{squad}"] = recs
    return out


def scrape_team_stats() -> dict:
    """
    Team-level xG / xGA / goals from Understat league team data.

    Understat's get_team_data returns per-team, per-match history; we aggregate
    the season totals so the app's rankings (attack/defence) stay fresh without
    the app ever touching FBRef. Returns {canonical_team: {xg,xga,gf,ga,mp}}.
    """
    from understatapi import UnderstatClient
    out = {}
    with UnderstatClient() as understat:
        team_data = understat.league(league="EPL").get_team_data(season=SEASON)
    # team_data is keyed by team id -> {title, history:[per-match dicts]}
    for _tid, td in (team_data or {}).items():
        title = (td.get("title") or "").strip()
        name = UNDERSTAT_TEAM.get(title, title)
        hist = td.get("history", []) or []
        xg = sum(_f(h.get("xG")) for h in hist)
        xga = sum(_f(h.get("xGA")) for h in hist)
        gf = sum(int(_f(h.get("scored"))) for h in hist)
        ga = sum(int(_f(h.get("missed"))) for h in hist)
        out[name] = {"xg": round(xg, 2), "xga": round(xga, 2),
                     "gf": gf, "ga": ga, "mp": len(hist)}
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

    # --- team-level xG/xGA (keeps app rankings fresh; app never scrapes these)
    try:
        tstats = scrape_team_stats()
        print(f"Scraped team stats for {len(tstats)} teams")
        if len(tstats) >= 15:
            rt = requests.post(f"{app_url}/ingest/team-stats",
                               params={"token": token},
                               json={"team_stats": tstats}, timeout=60)
            print("POST /ingest/team-stats ->", rt.status_code, rt.text[:200])
    except Exception as exc:  # noqa: BLE001
        print(f"Team-stats scrape skipped: {exc}", file=sys.stderr)

    # --- all-time head-to-head records (best-effort; failure won't fail the job)
    try:
        h2h = scrape_h2h()
        print(f"Built H2H records for {len(h2h)} players")
        if len(h2h) >= 20:
            r2 = requests.post(f"{app_url}/ingest/h2h",
                               params={"token": token},
                               json={"players": h2h}, timeout=120)
            print("POST /ingest/h2h ->", r2.status_code, r2.text[:300])
    except Exception as exc:  # noqa: BLE001
        print(f"H2H build skipped: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
