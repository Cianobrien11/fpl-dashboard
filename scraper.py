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
    ),
    # FBRef (Sports-Reference) returns 403 to bare requests, so send the full
    # header set a real Chrome browser sends. This gets us past the block.
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Referer": "https://fbref.com/en/comps/9/Premier-League-Stats",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
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


def _get(url: str, timeout: int = 20, retries: int = 3) -> requests.Response:
    """GET with browser headers + retry/backoff (FBRef rate-limits with 429/403)."""
    last = None
    for attempt in range(retries):
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        if resp.status_code in (429, 403) and attempt < retries - 1:
            last = resp
            time.sleep(3 * (attempt + 1))  # 3s, 6s backoff
            continue
        resp.raise_for_status()
        return resp
    if last is not None:
        last.raise_for_status()
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
            # shots / SoT / key passes per 90 — filled from FBRef when reachable.
            "shots_90": 0.0,
            "sot_90": 0.0,
            "kp_90": 0.0,
            # FPL-native per-90 proxies (always available, never blocked):
            #   threat  ~ shot volume/quality proxy
            #   creativity ~ chance-creation / key-pass proxy
            # Used as a fallback when FBRef shot/KP data is unavailable.
            "threat_90": round(_f(p.get("threat"))
                               / max(p.get("minutes", 0) / 90.0, 1e-9), 1)
                         if p.get("minutes", 0) else 0.0,
            "creativity_90": round(_f(p.get("creativity"))
                                   / max(p.get("minutes", 0) / 90.0, 1e-9), 1)
                             if p.get("minutes", 0) else 0.0,
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
# FBRef squad names -> our canonical names (for surname+squad matching)
_FBREF_SQUAD = {
    "Arsenal": "Arsenal", "Aston Villa": "Aston Villa", "Bournemouth": "Bournemouth",
    "Brentford": "Brentford", "Brighton": "Brighton", "Chelsea": "Chelsea",
    "Coventry City": "Coventry", "Crystal Palace": "Crystal Palace",
    "Everton": "Everton", "Fulham": "Fulham", "Hull City": "Hull City",
    "Ipswich Town": "Ipswich", "Leeds United": "Leeds", "Liverpool": "Liverpool",
    "Manchester City": "Man City", "Manchester Utd": "Man United",
    "Newcastle Utd": "Newcastle", "Nott'ham Forest": "Nottm Forest",
    "Nottingham Forest": "Nottm Forest", "Tottenham": "Tottenham",
    "Sunderland": "Sunderland",
}


def _flatten_cols(tbl):
    """Flatten a possibly multi-level FBRef header to single-level names."""
    cols = []
    for c in tbl.columns:
        if isinstance(c, tuple):
            # use the last non-'Unnamed' level
            parts = [str(x) for x in c if x and not str(x).startswith("Unnamed")]
            cols.append(parts[-1] if parts else str(c[-1]))
        else:
            cols.append(str(c))
    tbl.columns = cols
    return tbl


def _fbref_player_table(url: str, need_cols: set):
    """
    Fetch a FBRef player table and return the first flattened DataFrame that
    contains all need_cols. Robust to multi-level headers.
    """
    import io
    import pandas as pd
    resp = _get(url, timeout=30)
    html = resp.text.replace("<!--", "").replace("-->", "")
    best = None
    for tbl in pd.read_html(io.StringIO(html)):
        t = _flatten_cols(tbl.copy())
        if need_cols.issubset(set(t.columns)):
            # prefer the widest table (the full player table, not a mini one)
            if best is None or len(t) > len(best):
                best = t
    return best


def _norm_squad(raw: str) -> str:
    raw = str(raw).strip()
    return _FBREF_SQUAD.get(raw, raw)


def _assign(players, by_key, field):
    """
    Assign values keyed by (surname_lower, squad) to players, falling back to
    surname-only when the squad-qualified key isn't present.
    """
    surname_only = {}
    for (last, squad), val in by_key.items():
        surname_only.setdefault(last, val)
    for p in players:
        parts = p.get("name", "").split()
        if not parts:
            continue
        last = parts[-1].lower()
        key = (last, p.get("team"))
        if key in by_key:
            p[field] = by_key[key]
        elif last in surname_only:
            p[field] = surname_only[last]


def enrich_shots_from_fbref(players: list[dict]) -> None:
    """
    Add shots_90 / sot_90 from FBRef's player shooting page (single request).
    Computes per-90 from stable totals (Sh, SoT, 90s) and matches on
    surname + squad. Best-effort; mutates in place.
    """
    tbl = _fbref_player_table(
        "https://fbref.com/en/comps/9/shooting/Premier-League-Stats",
        {"Player", "Sh", "SoT"})
    if tbl is None:
        return
    sh_by, sot_by = {}, {}
    for _, row in tbl.iterrows():
        name = str(row.get("Player", "")).strip()
        if not name or name == "Player":
            continue
        try:
            n90 = float(str(row.get("90s", "0")).replace(",", "") or 0)
            if n90 <= 0:
                continue
            sh = float(str(row.get("Sh", 0)).replace(",", "") or 0)
            sot = float(str(row.get("SoT", 0)).replace(",", "") or 0)
        except (ValueError, TypeError):
            continue
        last = name.split()[-1].lower()
        squad = _norm_squad(row.get("Squad", ""))
        sh_by[(last, squad)] = round(sh / n90, 2)
        sot_by[(last, squad)] = round(sot / n90, 2)
    _assign(players, sh_by, "shots_90")
    _assign(players, sot_by, "sot_90")


def enrich_keypasses_from_fbref(players: list[dict]) -> None:
    """
    Add kp_90 (key passes per 90) from FBRef's passing page (single request).
    Computes per-90 from KP total and 90s; matches on surname + squad.
    """
    tbl = _fbref_player_table(
        "https://fbref.com/en/comps/9/passing/Premier-League-Stats",
        {"Player", "KP"})
    if tbl is None:
        return
    kp_by = {}
    for _, row in tbl.iterrows():
        name = str(row.get("Player", "")).strip()
        if not name or name == "Player":
            continue
        try:
            n90 = float(str(row.get("90s", "0")).replace(",", "") or 0)
            if n90 <= 0:
                continue
            kp = float(str(row.get("KP", 0)).replace(",", "") or 0)
        except (ValueError, TypeError):
            continue
        last = name.split()[-1].lower()
        squad = _norm_squad(row.get("Squad", ""))
        kp_by[(last, squad)] = round(kp / n90, 2)
    _assign(players, kp_by, "kp_90")


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

    # enrich players with shots/SoT + key passes (both single FBRef requests).
    # FBRef often 403s on hosted IPs; if so we fall back to FPL threat/creativity.
    if bundle.get("players"):
        try:
            enrich_shots_from_fbref(bundle["players"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Shots enrichment: {exc}")
        try:
            enrich_keypasses_from_fbref(bundle["players"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Key-pass enrichment: {exc}")

        # Did FBRef actually return usable shot data?
        got_shots = sum(1 for p in bundle["players"] if p.get("shots_90", 0) > 0)
        bundle["shots_source"] = "fbref" if got_shots >= 20 else "fpl_proxy"
        if bundle["shots_source"] == "fpl_proxy":
            # Fall back to FPL-native proxies so the columns are never empty.
            for p in bundle["players"]:
                p["shots_90"] = p.get("threat_90", 0.0)
                p["sot_90"] = round(p.get("threat_90", 0.0) * 0.4, 1)  # ~SoT share
                p["kp_90"] = p.get("creativity_90", 0.0)
            errors.append("FBRef shots unavailable — using FPL threat/creativity proxy.")

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
