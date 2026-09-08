"""
scraper.py — Data ingestion for the FPL Dashboard.

Scrapes:
  1. Team attacking + defensive stats from FBRef (Squad + Opponent shooting).
  2. The official Fantasy Premier League API for player prices, fixtures,
     and team strength (no scraping fragility — FPL has a public JSON API).

Everything is cached to the SQLite DB (see models.py) so the app works even
if a source is temporarily unavailable.
"""
from __future__ import annotations

import datetime as _dt
import time
from typing import Any

import requests

FPL_BASE = "https://fantasy.premierleague.com/api"
FBREF_STATS = "https://fbref.com/en/comps/9/Premier-League-Stats"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Map FPL short names -> our canonical team names
FPL_NAME_MAP = {
    "ARS": "Arsenal", "AVL": "Aston Villa", "BOU": "Bournemouth",
    "BRE": "Brentford", "BHA": "Brighton", "CHE": "Chelsea",
    "COV": "Coventry", "CRY": "Crystal Palace", "EVE": "Everton",
    "FUL": "Fulham", "HUL": "Hull City", "IPS": "Ipswich",
    "LEE": "Leeds", "LIV": "Liverpool", "MCI": "Man City",
    "MUN": "Man United", "NEW": "Newcastle", "NFO": "Nottm Forest",
    "TOT": "Tottenham", "SUN": "Sunderland",
}


def _get(url: str, timeout: int = 20) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------------
# FPL API  (fixtures, prices, gameweeks) — stable public JSON, no scraping
# --------------------------------------------------------------------------
def fetch_fpl_bootstrap() -> dict[str, Any]:
    """Teams, players, prices, gameweek metadata."""
    return _get(f"{FPL_BASE}/bootstrap-static/").json()


def fetch_fpl_fixtures() -> list[dict[str, Any]]:
    """All fixtures with gameweek (event) and difficulty."""
    return _get(f"{FPL_BASE}/fixtures/").json()


def build_fixture_map(bootstrap: dict, fixtures: list[dict]) -> dict:
    """
    Return {team_name: [ {gw, opponent, venue, fdr}, ... ]} for future GWs.
    """
    id_to_name = {}
    for t in bootstrap.get("teams", []):
        # match on the 3-letter code where possible
        code = t.get("short_name", "").upper()
        id_to_name[t["id"]] = FPL_NAME_MAP.get(code, t["name"])

    out: dict[str, list] = {name: [] for name in id_to_name.values()}
    for fx in fixtures:
        gw = fx.get("event")
        if gw is None:
            continue
        h = id_to_name.get(fx["team_h"])
        a = id_to_name.get(fx["team_a"])
        if not h or not a:
            continue
        out[h].append({"gw": gw, "opponent": a, "venue": "H",
                       "fdr": fx.get("team_h_difficulty")})
        out[a].append({"gw": gw, "opponent": h, "venue": "A",
                       "fdr": fx.get("team_a_difficulty")})
    for name in out:
        out[name].sort(key=lambda x: x["gw"])
    return out


def build_player_prices(bootstrap: dict) -> list[dict]:
    """Return a list of {name, team, position, price, form, points}."""
    id_to_name = {t["id"]: FPL_NAME_MAP.get(t.get("short_name", "").upper(), t["name"])
                  for t in bootstrap.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    players = []
    for p in bootstrap.get("elements", []):
        players.append({
            "name": p["web_name"],
            "team": id_to_name.get(p["team"], "?"),
            "position": pos_map.get(p["element_type"], "?"),
            "price": p["now_cost"] / 10.0,
            "form": float(p.get("form") or 0),
            "points": p.get("total_points", 0),
            "selected_by": float(p.get("selected_by_percent") or 0),
        })
    return players


# --------------------------------------------------------------------------
# FBRef  (xG / xGA / shots)  — HTML tables, parsed with pandas
# --------------------------------------------------------------------------
def fetch_fbref_team_stats() -> dict[str, dict]:
    """
    Scrape FBRef squad standard + shooting + opponent tables.

    Returns {team_name: {gf, xg, ga, xga, sh, sot, sh_ag, sot_ag, mp}}.
    Uses pandas.read_html which handles FBRef's table markup well.
    """
    import io
    import pandas as pd

    resp = _get(FBREF_STATS, timeout=30)
    # FBRef wraps some tables in HTML comments; strip comment markers so
    # pandas can see every table.
    html = resp.text.replace("<!--", "").replace("-->", "")
    tables = pd.read_html(io.StringIO(html))

    stats: dict[str, dict] = {}

    def _norm(name: str) -> str:
        fixes = {
            "Manchester City": "Man City", "Manchester Utd": "Man United",
            "Newcastle Utd": "Newcastle", "Nott'ham Forest": "Nottm Forest",
            "Nottingham Forest": "Nottm Forest", "Tottenham": "Tottenham",
            "Brighton": "Brighton", "Crystal Palace": "Crystal Palace",
            "Leeds United": "Leeds", "Ipswich Town": "Ipswich",
            "Coventry City": "Coventry", "Hull City": "Hull City",
        }
        return fixes.get(name.strip(), name.strip())

    for tbl in tables:
        cols = [str(c[-1]) if isinstance(c, tuple) else str(c) for c in tbl.columns]
        tbl.columns = cols
        if "Squad" not in cols or "xG" not in cols:
            continue
        is_against = tbl["Squad"].astype(str).str.startswith("vs ").any()
        for _, row in tbl.iterrows():
            squad = str(row["Squad"]).replace("vs ", "").strip()
            if not squad or squad == "nan":
                continue
            name = _norm(squad)
            rec = stats.setdefault(name, {})
            try:
                if is_against:
                    rec["xga"] = float(row.get("xG", rec.get("xga", 0)))
                    rec["ga"] = int(float(row.get("Gls", rec.get("ga", 0))))
                    rec["sh_ag"] = int(float(row.get("Sh", rec.get("sh_ag", 0))))
                    rec["sot_ag"] = int(float(row.get("SoT", rec.get("sot_ag", 0))))
                else:
                    rec["xg"] = float(row.get("xG", rec.get("xg", 0)))
                    rec["gf"] = int(float(row.get("Gls", rec.get("gf", 0))))
                    rec["sh"] = int(float(row.get("Sh", rec.get("sh", 0))))
                    rec["sot"] = int(float(row.get("SoT", rec.get("sot", 0))))
            except (ValueError, TypeError):
                continue
    return stats


def scrape_all() -> dict[str, Any]:
    """Full weekly refresh. Returns a bundle the app persists to the DB."""
    bundle: dict[str, Any] = {"scraped_at": _dt.datetime.utcnow().isoformat()}
    errors = []

    try:
        bootstrap = fetch_fpl_bootstrap()
        fixtures = fetch_fpl_fixtures()
        bundle["fixtures"] = build_fixture_map(bootstrap, fixtures)
        bundle["players"] = build_player_prices(bootstrap)
        events = bootstrap.get("events", [])
        nxt = next((e["id"] for e in events if e.get("is_next")), None)
        cur = next((e["id"] for e in events if e.get("is_current")), None)
        bundle["current_gw"] = cur
        bundle["next_gw"] = nxt
    except Exception as exc:  # noqa: BLE001
        errors.append(f"FPL API: {exc}")

    try:
        bundle["team_stats"] = fetch_fbref_team_stats()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"FBRef: {exc}")

    bundle["errors"] = errors
    return bundle


if __name__ == "__main__":
    import json
    data = scrape_all()
    print(json.dumps({k: (v if k in ("errors", "current_gw", "next_gw",
                                      "scraped_at") else f"<{len(v)} items>")
                      for k, v in data.items()}, indent=2))
