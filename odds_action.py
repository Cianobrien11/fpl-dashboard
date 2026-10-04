"""
odds_action.py — run by the GitHub Action (NOT by the web app).

Fetches EPL bookmaker odds from The Odds API (the-odds-api.com, free tier),
derives per-team market signals, and POSTs them to the app's /ingest/odds
endpoint. Runs on GitHub's runners alongside the Understat scrape.

Markets used (all on the free tier):
  * h2h    — match result (home / draw / away)  -> win probabilities
  * totals — over/under 2.5 goals               -> match goal expectation

From these we derive, per team, for its next fixture:
  * win_prob        — de-vigged market win probability
  * cs_prob         — implied clean-sheet probability (see _clean_sheet_from_odds)
  * team_goals_exp  — share of the match's expected goals this team scores

Env vars (provided by the workflow as secrets):
  APP_URL          e.g. https://fpl-dashboard-txgc.onrender.com
  CRON_TOKEN       shared secret, matches the app's CRON_TOKEN
  ODDS_API_KEY     free key from the-odds-api.com

Usage: python odds_action.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import requests

ODDS_API = "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"

# The Odds API uses full club names; map to the app's canonical names.
ODDS_TEAM = {
    "Arsenal": "Arsenal", "Aston Villa": "Aston Villa",
    "AFC Bournemouth": "Bournemouth", "Bournemouth": "Bournemouth",
    "Brentford": "Brentford", "Brighton and Hove Albion": "Brighton",
    "Brighton & Hove Albion": "Brighton", "Brighton": "Brighton",
    "Chelsea": "Chelsea", "Crystal Palace": "Crystal Palace",
    "Everton": "Everton", "Fulham": "Fulham",
    "Ipswich Town": "Ipswich", "Ipswich": "Ipswich",
    "Leeds United": "Leeds", "Leeds": "Leeds",
    "Leicester City": "Leicester", "Liverpool": "Liverpool",
    "Manchester City": "Man City", "Manchester United": "Man United",
    "Newcastle United": "Newcastle", "Nottingham Forest": "Nottm Forest",
    "Southampton": "Southampton", "Tottenham Hotspur": "Tottenham",
    "Tottenham": "Tottenham", "West Ham United": "West Ham",
    "Wolverhampton Wanderers": "Wolves", "Sunderland": "Sunderland",
    "Hull City": "Hull City", "Coventry City": "Coventry",
}


def _canon(name: str) -> str:
    return ODDS_TEAM.get((name or "").strip(), (name or "").strip())


def _implied(dec_odds: float) -> float:
    """Implied probability from decimal odds (includes vig)."""
    try:
        return 1.0 / float(dec_odds) if dec_odds else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _clean_sheet_from_odds(win_p: float, draw_p: float, total_goals_exp: float,
                           is_favourite: bool) -> float:
    """Rough clean-sheet probability for a team from match-level markets.

    We don't have a direct CS market on the free tier, so approximate: a team
    is more likely to keep a clean sheet when (a) it's winning/strong (high
    win prob) and (b) the match has a LOW total-goals expectation. Calibrated
    to land in a sensible 0.08–0.60 band.
    """
    # Base from match goal expectation: fewer expected goals => higher CS.
    # total_goals_exp ~ 2.6 average. Map 1.5 -> ~0.45, 3.5 -> ~0.15.
    goal_term = max(0.08, min(0.60, 0.75 - 0.17 * total_goals_exp))
    # Strength tilt: stronger teams concede less.
    strength = win_p + 0.3 * draw_p
    tilt = (strength - 0.4) * 0.3  # +/- around average
    return max(0.05, min(0.65, goal_term + tilt))


def _total_goals_expectation(totals_market) -> float:
    """Approximate match total-goals expectation from the over/under line.

    The point (e.g. 2.5) with roughly even over/under prices implies the
    market's goal expectation sits near that line; skew nudges it. Falls back
    to 2.6 (EPL average) when the market is missing.
    """
    if not totals_market:
        return 2.6
    try:
        outcomes = totals_market.get("outcomes", [])
        over = next((o for o in outcomes if o.get("name") == "Over"), None)
        under = next((o for o in outcomes if o.get("name") == "Under"), None)
        if not over or not under:
            return 2.6
        line = float(over.get("point", 2.5))
        p_over = _implied(over.get("price"))
        p_under = _implied(under.get("price"))
        tot = p_over + p_under
        if tot <= 0:
            return line
        p_over_norm = p_over / tot
        # If over is favoured, expectation is above the line, and vice versa.
        return round(line + (p_over_norm - 0.5) * 1.2, 2)
    except Exception:
        return 2.6


def scrape_odds() -> dict:
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        print("ODDS_API_KEY not set — skipping odds.", file=sys.stderr)
        return {}
    params = {
        "apiKey": key,
        "regions": "uk",
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
    }
    resp = requests.get(ODDS_API, params=params, timeout=40)
    print("Odds API ->", resp.status_code,
          "| remaining:", resp.headers.get("x-requests-remaining"))
    resp.raise_for_status()
    games = resp.json() or []

    out = {}
    for g in games:
        home = _canon(g.get("home_team"))
        away = _canon(g.get("away_team"))
        books = g.get("bookmakers", [])
        if not books:
            continue
        # Use the first bookmaker with both markets (good enough for a signal).
        h2h = totals = None
        for b in books:
            mk = {m["key"]: m for m in b.get("markets", [])}
            if "h2h" in mk and h2h is None:
                h2h = mk["h2h"]
            if "totals" in mk and totals is None:
                totals = mk["totals"]
            if h2h and totals:
                break
        if not h2h:
            continue

        oc = {o["name"]: o.get("price") for o in h2h.get("outcomes", [])}
        p_home = _implied(oc.get(g.get("home_team")))
        p_away = _implied(oc.get(g.get("away_team")))
        p_draw = _implied(oc.get("Draw"))
        s = p_home + p_draw + p_away
        if s > 0:  # de-vig
            p_home, p_draw, p_away = p_home / s, p_draw / s, p_away / s

        total_exp = _total_goals_expectation(totals)
        # Split match goal expectation by relative attacking strength.
        denom = (p_home + p_away) or 1.0
        home_goals = round(total_exp * (p_home / denom), 2)
        away_goals = round(total_exp * (p_away / denom), 2)

        out[home] = {
            "opp": away, "venue": "H", "win_prob": round(p_home, 3),
            "cs_prob": round(_clean_sheet_from_odds(p_home, p_draw, total_exp, p_home > p_away), 3),
            "team_goals_exp": home_goals, "total_goals_exp": total_exp,
        }
        out[away] = {
            "opp": home, "venue": "A", "win_prob": round(p_away, 3),
            "cs_prob": round(_clean_sheet_from_odds(p_away, p_draw, total_exp, p_away > p_home), 3),
            "team_goals_exp": away_goals, "total_goals_exp": total_exp,
        }
    return out


def main() -> int:
    app_url = os.environ.get("APP_URL", "").rstrip("/")
    token = os.environ.get("CRON_TOKEN", "")
    if not app_url or not token:
        print("APP_URL / CRON_TOKEN not set.", file=sys.stderr)
        return 1
    try:
        odds = scrape_odds()
    except Exception as exc:  # noqa: BLE001
        print(f"Odds scrape failed: {exc}", file=sys.stderr)
        return 0  # soft-fail: never block the rest of the pipeline
    if not odds:
        print("No odds parsed — nothing to post.")
        return 0
    print(f"Parsed odds for {len(odds)} teams.")
    body = {"odds": odds, "updated": datetime.now(timezone.utc).isoformat()}
    r = requests.post(f"{app_url}/ingest/odds", params={"token": token},
                      json=body, timeout=40)
    print("POST /ingest/odds ->", r.status_code, r.text[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
