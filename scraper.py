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
    """
    Return rich per-player records from the FPL bootstrap.

    Includes points, price, form, ownership, expected stats (xG/xA/xGI/xGC),
    defensive contribution, set-piece orders, availability, and the latest
    price change — everything the new pages need, all from the FPL API.
    """
    id_to_name = {t["id"]: FPL_NAME_MAP.get(t.get("short_name", "").upper(), t["name"])
                  for t in bootstrap.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    def _f(v):  # safe float
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    players = []
    for p in bootstrap.get("elements", []):
        starts = p.get("starts", 0) or 0
        players.append({
            "id": p["id"],  # FPL element id — used to fetch detailed per-player stats
            "name": p["web_name"],
            "team": id_to_name.get(p["team"], "?"),
            "position": pos_map.get(p["element_type"], "?"),
            "price": p["now_cost"] / 10.0,
            "form": float(p.get("form") or 0),
            "points": p.get("total_points", 0),
            "selected_by": float(p.get("selected_by_percent") or 0),
            # expected stats (season totals from the API)
            "xg": _f(p.get("expected_goals")),
            "xa": _f(p.get("expected_assists")),
            "xgi": _f(p.get("expected_goal_involvements")),
            "xgc": _f(p.get("expected_goals_conceded")),
            # actual returns
            "goals": p.get("goals_scored", 0),
            "assists": p.get("assists", 0),
            "clean_sheets": p.get("clean_sheets", 0),
            "bonus": p.get("bonus", 0),
            "minutes": p.get("minutes", 0),
            "starts": starts,
            # defensive contribution (2025/26+ stat)
            "defcon": p.get("defensive_contribution", 0) or 0,
            # per-game normalised stats (per 90 minutes played).
            # ninetys = minutes / 90; guard against divide-by-zero.
            "defcon_pg": round((p.get("defensive_contribution", 0) or 0)
                               / max(p.get("minutes", 0) / 90.0, 1e-9), 2)
                         if p.get("minutes", 0) else 0.0,
            "xgi_pg": round(_f(p.get("expected_goal_involvements"))
                            / max(p.get("minutes", 0) / 90.0, 1e-9), 2)
                      if p.get("minutes", 0) else 0.0,
            # points per game the player actually appeared in
            "ppg": _f(p.get("points_per_game")),
            # total bonus already captured below as "bonus"; expose ninetys too
            "ninetys": round(p.get("minutes", 0) / 90.0, 1),
            # average minutes per appearance (games the player featured in).
            # FPL doesn't give "appearances" directly, so approximate via starts;
            # fall back to 90s when a player only ever subbed on.
            "avg_min": round(p.get("minutes", 0) / max(starts, 1), 0)
                       if starts else (p.get("minutes", 0) or 0),
            # shots / SoT / key passes per 90 — filled by enrich_player_detail()
            # (shots & SoT from FPL element-summary; key passes from FBRef).
            "shots_90": 0.0,
            "sot_90": 0.0,
            "kp_90": 0.0,
            # value + form
            "ppm": round(p.get("total_points", 0) / (p["now_cost"] / 10.0), 2)
                   if p.get("now_cost") else 0,
            "ict": _f(p.get("ict_index")),
            # set-piece order (1 = first-choice taker; None = not on them)
            "pen_order": p.get("penalties_order"),
            "ck_order": p.get("corners_and_indirect_freekicks_order"),
            "fk_order": p.get("direct_freekicks_order"),
            # price movement
            "cost_change_event": (p.get("cost_change_event") or 0) / 10.0,
            "cost_change_start": (p.get("cost_change_start") or 0) / 10.0,
            "transfers_in_event": p.get("transfers_in_event", 0),
            "transfers_out_event": p.get("transfers_out_event", 0),
            # availability
            "status": p.get("status", "a"),  # a=available i=injured s=susp d=doubt u=unavail
            "chance": p.get("chance_of_playing_next_round"),
            "news": (p.get("news") or "").strip(),
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


# --------------------------------------------------------------------------
# Per-player shooting / passing enrichment
#   shots + SoT  -> FPL element-summary endpoint (per player)
#   key passes   -> FBRef player passing table (single page)
# --------------------------------------------------------------------------
def enrich_shots_from_fpl(players: list[dict], max_players: int = 700,
                          pause: float = 0.03) -> None:
    """
    Add shots_90 / sot_90 to each player from the FPL element-summary endpoint.

    One lightweight call per player. Only players with minutes are fetched
    (skips the deep bench) to keep the weekly refresh quick. Mutates in place;
    failures for a single player are swallowed so the whole refresh never dies.
    """
    fetched = 0
    for p in players:
        if fetched >= max_players:
            break
        if p.get("minutes", 0) < 45:  # skip players who've barely featured
            continue
        try:
            data = _get(f"{FPL_BASE}/element-summary/{p['id']}/", timeout=15).json()
            history = data.get("history", [])
            shots = sum(h.get("shots", 0) or 0 for h in history)
            sot = sum(h.get("shots_on_target", 0) or 0 for h in history)
            n90 = max(p.get("minutes", 0) / 90.0, 1e-9)
            p["shots_90"] = round(shots / n90, 2)
            p["sot_90"] = round(sot / n90, 2)
            fetched += 1
            if pause:
                time.sleep(pause)
        except Exception:  # noqa: BLE001
            continue


def enrich_keypasses_from_fbref(players: list[dict]) -> None:
    """
    Add kp_90 (key passes per 90) to each player from FBRef's passing table.
    Matched on surname + team. Best-effort; mutates in place.
    """
    import io
    import pandas as pd

    url = "https://fbref.com/en/comps/9/passing/Premier-League-Stats"
    resp = _get(url, timeout=30)
    html = resp.text.replace("<!--", "").replace("-->", "")
    tables = pd.read_html(io.StringIO(html))
    passing = None
    for tbl in tables:
        cols = [str(c[-1]) if isinstance(c, tuple) else str(c) for c in tbl.columns]
        if "Player" in cols and ("KP" in cols):
            tbl.columns = cols
            passing = tbl
            break
    if passing is None:
        return

    # build {lastname_lower: kp_per90}
    kp_by_name: dict[str, float] = {}
    for _, row in passing.iterrows():
        name = str(row.get("Player", "")).strip()
        if not name or name == "Player":
            continue
        try:
            kp = float(row.get("KP", 0) or 0)
            mins = float(str(row.get("Min", "0")).replace(",", "") or 0)
        except (ValueError, TypeError):
            continue
        if mins <= 0:
            continue
        last = name.split()[-1].lower()
        kp_by_name[last] = round(kp / (mins / 90.0), 2)

    for p in players:
        last = p["name"].split()[-1].lower()
        if last in kp_by_name:
            p["kp_90"] = kp_by_name[last]


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

    # enrich players with shots/SoT (FPL) and key passes (FBRef)
    if bundle.get("players"):
        try:
            enrich_shots_from_fpl(bundle["players"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Shots enrichment: {exc}")
        try:
            enrich_keypasses_from_fbref(bundle["players"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Key-pass enrichment: {exc}")

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
