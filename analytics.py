"""
analytics.py — Ranking + prediction engine for the FPL Dashboard.

Pure functions: given team stats + a fixture map, compute:
  * Clean-sheet target rankings
  * Goals-scored target rankings
  * Conceding target rankings
  * Per-fixture difficulty (easy / medium / hard) for each category
  * xG-model scoreline predictions for a gameweek

No I/O here — the app layer feeds in data and renders the output.
"""
from __future__ import annotations

from typing import Any

CANONICAL_TEAMS = [
    "Arsenal", "Aston Villa", "Bournemouth", "Brentford", "Brighton",
    "Chelsea", "Coventry", "Crystal Palace", "Everton", "Fulham",
    "Hull City", "Ipswich", "Leeds", "Liverpool", "Man City",
    "Man United", "Newcastle", "Nottm Forest", "Tottenham", "Sunderland",
]

# 3-letter codes for compact fixture chips
CODE = {
    "Arsenal": "ARS", "Aston Villa": "AVL", "Bournemouth": "BOU",
    "Brentford": "BRE", "Brighton": "BHA", "Chelsea": "CHE",
    "Coventry": "COV", "Crystal Palace": "CRY", "Everton": "EVE",
    "Fulham": "FUL", "Hull City": "HUL", "Ipswich": "IPS",
    "Leeds": "LEE", "Liverpool": "LIV", "Man City": "MCI",
    "Man United": "MUN", "Newcastle": "NEW", "Nottm Forest": "NFO",
    "Tottenham": "TOT", "Sunderland": "SUN",
}


def _games_played(stats: dict) -> int:
    return max(stats.get("mp", 3) or 3, 1)


def compute_rankings(team_stats: dict[str, dict]) -> dict[str, dict]:
    """
    Return {team: {cs_rk, gs_rk, gc_rk, xg, xga, gf, ga, ...}}.

    cs_rk  1 = best defence (back for clean sheet)
    gs_rk  1 = best attack (back to score)
    gc_rk  1 = worst defence (target to score against)
    """
    rows = []
    for team in CANONICAL_TEAMS:
        s = team_stats.get(team, {})
        rows.append({
            "team": team,
            "xg": float(s.get("xg", 0) or 0),
            "xga": float(s.get("xga", 0) or 0),
            "gf": int(s.get("gf", 0) or 0),
            "ga": int(s.get("ga", 0) or 0),
            "sh": int(s.get("sh", 0) or 0),
            "sot": int(s.get("sot", 0) or 0),
            "sh_ag": int(s.get("sh_ag", 0) or 0),
            "sot_ag": int(s.get("sot_ag", 0) or 0),
        })

    # Clean-sheet quality: low xGA, low GA, few SoT faced
    cs = sorted(rows, key=lambda r: (r["xga"] * 2 + r["ga"] * 1.5 + r["sot_ag"] * 0.2))
    for i, r in enumerate(cs):
        r["cs_rk"] = i + 1
    # Attack quality: high xG, high GF, high SoT
    gs = sorted(rows, key=lambda r: -(r["xg"] * 2 + r["gf"] * 1.5 + r["sot"] * 0.15))
    for i, r in enumerate(gs):
        r["gs_rk"] = i + 1
    # Conceding (worst defence first = best target)
    gc = sorted(rows, key=lambda r: -(r["xga"] * 2 + r["ga"] * 1.5 + r["sot_ag"] * 0.2))
    for i, r in enumerate(gc):
        r["gc_rk"] = i + 1

    return {r["team"]: r for r in rows}


def _difficulty(cat: str, opp_ranks: dict, venue: str) -> int:
    """Return 0 (easy), 1 (medium), 2 (hard) for a fixture in a category."""
    if cat == "cs":  # want weak-attacking opponent → high gs_rk
        v = opp_ranks["gs_rk"]
        if v >= 15 or (v >= 12 and venue == "H"):
            return 0
        if v <= 5 or (v <= 8 and venue == "A"):
            return 2
        return 1
    if cat == "gs":  # want leaky opponent → low gc_rk
        v = opp_ranks["gc_rk"]
        if v <= 6 or (v <= 9 and venue == "H"):
            return 0
        if v >= 15 or (v >= 12 and venue == "A"):
            return 2
        return 1
    # cat == "gc": this team likely to concede vs strong attacker → low gs_rk
    v = opp_ranks["gs_rk"]
    if v <= 5 or (v <= 8 and venue == "A"):
        return 0
    if v >= 15 or (v >= 12 and venue == "H"):
        return 2
    return 1


def build_target_tables(rankings: dict, fixtures: dict, gw_from: int,
                        gw_to: int) -> dict[str, list]:
    """
    Build the three ranked target tables across a gameweek window.

    Returns {cat: [ {team, rk, x, a, easy_n, easy_gws, em_n, em_gws,
                     fixtures:[{code, venue, d}]}, ... ]}.
    """
    out: dict[str, list] = {"cs": [], "gs": [], "gc": []}
    gws = list(range(gw_from, gw_to + 1))

    for team in CANONICAL_TEAMS:
        tr = rankings[team]
        team_fx = [f for f in fixtures.get(team, []) if f["gw"] in gws]
        per_cat = {}
        for cat in ("cs", "gs", "gc"):
            fx_list, easy, em = [], [], []
            for f in team_fx:
                opp = f["opponent"]
                if opp not in rankings:
                    continue
                d = _difficulty(cat, rankings[opp], f["venue"])
                fx_list.append({"code": CODE.get(opp, opp[:3].upper()),
                                "venue": f["venue"], "d": d, "gw": f["gw"]})
                if d == 0:
                    easy.append(f"GW{f['gw']}")
                if d <= 1:
                    em.append(f"GW{f['gw']}")
            per_cat[cat] = (fx_list, easy, em)

        for cat in ("cs", "gs", "gc"):
            fx_list, easy, em = per_cat[cat]
            rk = tr["gc_rk"] if cat == "gc" else tr[f"{cat}_rk"]
            x = tr["xg"] if cat == "gs" else tr["xga"]
            a = tr["gf"] if cat == "gs" else tr["ga"]
            out[cat].append({
                "team": team, "rk": rk, "x": round(x, 2), "a": a,
                "easy_n": len(easy), "easy_gws": easy,
                "em_n": len(em), "em_gws": em,
                "fixtures": fx_list,
            })

    # sort each category by (easy count desc, quality rank asc)
    for cat in out:
        out[cat].sort(key=lambda r: (-r["easy_n"], r["rk"]))
    return out


def predict_gameweek(rankings: dict, fixtures: dict, gw: int) -> list[dict]:
    """xG-model scoreline predictions for all fixtures in a gameweek."""
    seen = set()
    preds = []
    for team in CANONICAL_TEAMS:
        for f in fixtures.get(team, []):
            if f["gw"] != gw or f["venue"] != "H":
                continue
            home, away = team, f["opponent"]
            if (home, away) in seen or away not in rankings:
                continue
            seen.add((home, away))
            h, a = rankings[home], rankings[away]
            gp = 3.0
            h_att, h_def = h["xg"] / gp, h["xga"] / gp
            a_att, a_def = a["xg"] / gp, a["xga"] / gp
            h_xg = (h_att + a_def) / 2 * 1.12
            a_xg = (a_att + h_def) / 2 * 0.90
            hs, as_ = round(h_xg), round(a_xg)
            diff = h_xg - a_xg
            if hs > as_:
                verdict = f"{home} win"
            elif as_ > hs:
                verdict = f"{away} win"
            else:
                verdict = "Draw"
            conf = "Clear" if abs(diff) >= 0.7 else ("Lean" if abs(diff) >= 0.3 else "Tight")
            preds.append({
                "home": home, "away": away,
                "home_xg": round(h_xg, 2), "away_xg": round(a_xg, 2),
                "home_score": hs, "away_score": as_,
                "verdict": verdict, "confidence": conf,
            })
    return preds


def captain_picks(rankings: dict, fixtures: dict, squad: list[dict],
                  gw: int) -> list[dict]:
    """Score each squad player's captaincy appeal for a gameweek."""
    picks = []
    for p in squad:
        team = p["team"]
        fx = next((f for f in fixtures.get(team, []) if f["gw"] == gw), None)
        if not fx or team not in rankings:
            continue
        opp = fx["opponent"]
        if opp not in rankings:
            continue
        tr, orr = rankings[team], rankings[opp]
        gp = 3.0
        if p["position"] in ("MID", "FWD"):
            appeal = (tr["xg"] / gp) * 1.2 + (orr["xga"] / gp) + (0.3 if fx["venue"] == "H" else 0)
        else:
            appeal = (5 - orr["xg"] / gp) + (0.4 if fx["venue"] == "H" else 0) + (tr["xg"] / gp) * 0.5
        picks.append({
            "name": p["name"], "team": team, "position": p["position"],
            "opponent": CODE.get(opp, opp[:3]), "venue": fx["venue"],
            "appeal": round(appeal, 2),
        })
    picks.sort(key=lambda x: -x["appeal"])
    return picks


def scatter_data(rankings: dict) -> dict[str, list]:
    """
    Data for the two dashboard scatter charts.

    Returns:
      attack: [{team, x=xg, y=gf}]  (xG vs actual goals — finishing)
      defence: [{team, x=xga, y=ga}] (xGA vs actual conceded — keeper/luck)
    """
    attack, defence = [], []
    for team, r in rankings.items():
        attack.append({"team": team, "x": round(r["xg"], 2), "y": r["gf"]})
        defence.append({"team": team, "x": round(r["xga"], 2), "y": r["ga"]})
    return {"attack": attack, "defence": defence}


def form_trend_data(rankings: dict, fixtures: dict, gw_from: int,
                    gw_to: int) -> dict:
    """
    A per-team 'fixture ease trend' across the gameweek window, for each
    category. Each point is 3 - difficulty (so easy=3, medium=2, hard=1),
    giving an at-a-glance line of how a team's run rises and falls.

    Returns {cat: {"gws": [...], "series": [{team, data:[...]}]}}.
    """
    gws = list(range(gw_from, gw_to + 1))
    out = {}
    for cat in ("cs", "gs", "gc"):
        series = []
        for team in CANONICAL_TEAMS:
            data = []
            for gw in gws:
                fx = next((f for f in fixtures.get(team, []) if f["gw"] == gw), None)
                if not fx or fx["opponent"] not in rankings:
                    data.append(None)
                    continue
                d = _difficulty(cat, rankings[fx["opponent"]], fx["venue"])
                data.append(3 - d)  # 3 easy, 2 med, 1 hard
            series.append({"team": team, "data": data})
        out[cat] = {"gws": [f"GW{g}" for g in gws], "series": series}
    return out


def build_transfer_plan(rankings: dict, fixtures: dict, squad: list[dict],
                        gw_from: int, gw_to: int) -> list[dict]:
    """
    Suggest a gameweek-by-gameweek transfer plan across a window.

    Heuristic (transparent, not a solver):
      * For each GW, find the squad player whose team has the worst fixture
        that week AND a poor next-2 run, and suggest the best-ranked
        replacement in the same position whose team has a strong run.
      * Also surface the captain pick for the GW.
    Returns a list of {gw, captain, out, in, reason} rows.
    """
    gws = list(range(gw_from, gw_to + 1))
    plan = []

    # candidate replacement pool: teams that combine a strong easy-fixture run
    # AND genuine underlying quality (so we don't recommend a weak side just
    # because it has soft games). Rank = easy_n desc, then quality rank asc.
    tables = build_target_tables(rankings, fixtures, gw_from, gw_to)
    _atk = sorted((r for r in tables["gs"] if r["rk"] <= 8),
                  key=lambda r: (-r["easy_n"], r["rk"]))
    _def = sorted((r for r in tables["cs"] if r["rk"] <= 8),
                  key=lambda r: (-r["easy_n"], r["rk"]))
    best_attack = [r["team"] for r in _atk][:6]
    best_defence = [r["team"] for r in _def][:6]

    for gw in gws:
        caps = captain_picks(rankings, fixtures, squad, gw)
        # captain should be an attacking player (MID/FWD) — defenders rarely capped
        attackers = [c for c in caps if c["position"] in ("MID", "FWD")]
        captain = (attackers or caps)[0] if caps else None
        # worst-fixture squad player this GW
        worst = None
        for p in squad:
            fx = next((f for f in fixtures.get(p["team"], []) if f["gw"] == gw), None)
            if not fx or fx["opponent"] not in rankings:
                continue
            cat = "gs" if p["position"] in ("MID", "FWD") else "cs"
            d = _difficulty(cat, rankings[fx["opponent"]], fx["venue"])
            if d == 2:  # a hard fixture
                pool = best_attack if cat == "gs" else best_defence
                repl = next((t for t in pool if t != p["team"]), None)
                worst = {"out": p["name"], "out_team": p["team"],
                         "position": p["position"], "in_team": repl,
                         "reason": f"{p['team']} face a tough {cat.upper()} fixture in GW{gw}"}
                break
        plan.append({
            "gw": gw,
            "captain": (f"{captain['name']} ({captain['opponent']} {captain['venue']})"
                        if captain else "—"),
            "move": worst,
        })
    return plan


# ==========================================================================
# Player-level helpers (fed by scraper.build_player_prices -> snapshot["players"])
# ==========================================================================

_POS_ORDER = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}


def filter_sort_players(players: list, position: str = "ALL", team: str = "ALL",
                        sort: str = "points", search: str = "",
                        limit: int = 60) -> list:
    """Filter by position/team/search and sort by a numeric field."""
    rows = []
    for p in players:
        if position != "ALL" and p.get("position") != position:
            continue
        if team != "ALL" and p.get("team") != team:
            continue
        if search and search.lower() not in p.get("name", "").lower():
            continue
        rows.append(p)
    valid = {"points", "price", "form", "xg", "xa", "xgi", "defcon",
             "selected_by", "ppm", "ict", "goals", "assists",
             "defcon_pg", "xgi_pg", "ppg", "minutes", "bonus",
             "avg_min", "shots_90", "sot_90", "kp_90"}
    key = sort if sort in valid else "points"
    rows.sort(key=lambda r: r.get(key, 0) or 0, reverse=True)
    return rows[:limit]


def set_piece_takers(players: list) -> dict:
    """
    Return {team: {pens:[names], corners:[names], freekicks:[names]}}
    ordered by the FPL set-piece order (1 = first choice).
    """
    out = {}
    for p in players:
        t = p.get("team", "?")
        entry = out.setdefault(t, {"pens": [], "corners": [], "freekicks": []})
        if p.get("pen_order"):
            entry["pens"].append((p["pen_order"], p["name"]))
        if p.get("ck_order"):
            entry["corners"].append((p["ck_order"], p["name"]))
        if p.get("fk_order"):
            entry["freekicks"].append((p["fk_order"], p["name"]))
    for t, e in out.items():
        for k in e:
            e[k] = [n for _, n in sorted(e[k])][:3]
    return {t: e for t, e in sorted(out.items())
            if e["pens"] or e["corners"] or e["freekicks"]}


def price_changes(players: list) -> dict:
    """Return {'risers': [...], 'fallers': [...]} by season price change."""
    changed = [p for p in players if p.get("cost_change_start", 0)]
    risers = sorted((p for p in changed if p["cost_change_start"] > 0),
                    key=lambda p: -p["cost_change_start"])[:20]
    fallers = sorted((p for p in changed if p["cost_change_start"] < 0),
                     key=lambda p: p["cost_change_start"])[:20]
    return {"risers": risers, "fallers": fallers}


def availability_flags(players: list) -> list:
    """Players who are not fully available (injured / doubt / suspended)."""
    flagged = []
    for p in players:
        not_avail = p.get("status", "a") != "a"
        doubtful = p.get("chance") is not None and p.get("chance") < 100
        if (not_avail and p.get("minutes", 0) > 0) or doubtful:
            flagged.append(p)
    order = {"i": 0, "s": 1, "u": 2, "d": 3, "a": 4}
    flagged.sort(key=lambda p: (order.get(p.get("status", "a"), 5),
                                -(p.get("selected_by", 0))))
    return flagged[:40]


def value_finder(players: list, min_minutes: int = 90) -> dict:
    """Best points-per-million by position (min minutes filter)."""
    out = {}
    for pos in ("GK", "DEF", "MID", "FWD"):
        pool = [p for p in players
                if p.get("position") == pos and p.get("minutes", 0) >= min_minutes]
        pool.sort(key=lambda p: p.get("ppm", 0), reverse=True)
        out[pos] = pool[:8]
    return out


STATUS_LABEL = {"a": "Available", "i": "Injured", "s": "Suspended",
                "d": "Doubtful", "u": "Unavailable"}


# ==========================================================================
# Player transfer targets — per-gameweek and multi-GW overall
# ==========================================================================

def _player_target_score(p: dict) -> float:
    """
    Rank a player's appeal as a transfer target.
    Blends form, points-per-game, and position-appropriate underlying stats.
    """
    pos = p.get("position")
    base = p.get("form", 0) * 1.5 + p.get("ppg", 0) * 1.0
    if pos in ("MID", "FWD"):
        base += p.get("xgi_pg", 0) * 4.0        # attacking threat per game
        base += p.get("ppm", 0) * 0.5           # value
    else:  # GK / DEF
        base += p.get("defcon_pg", 0) * 0.3     # defensive contribution per game
        base += p.get("clean_sheets", 0) * 0.8
        base += p.get("xgi_pg", 0) * 2.0        # attacking returns are a bonus
    # nudge down players with little game time (rotation risk)
    if p.get("minutes", 0) < 90:
        base *= 0.5
    return round(base, 2)


def gw_player_targets(players: list, rankings: dict, fixtures: dict,
                      squad: list, gw: int, per_pos: int = 3) -> dict:
    """
    Best players to target for a single gameweek, by position.

    A player qualifies if their team has a favourable fixture that week
    (easy/medium for the relevant category — CS for GK/DEF, goals for MID/FWD).
    Each result is flagged 'owned' if the player is in the user's squad.
    """
    owned = {(s.get("name"), s.get("team")) for s in squad}
    # precompute each team's difficulty this GW for both categories
    team_fix = {}
    for team in CANONICAL_TEAMS:
        fx = next((f for f in fixtures.get(team, []) if f["gw"] == gw), None)
        if not fx or fx["opponent"] not in rankings:
            continue
        cs_d = _difficulty("cs", rankings[fx["opponent"]], fx["venue"])
        gs_d = _difficulty("gs", rankings[fx["opponent"]], fx["venue"])
        team_fix[team] = {"opp": CODE.get(fx["opponent"], fx["opponent"][:3]),
                          "venue": fx["venue"], "cs_d": cs_d, "gs_d": gs_d}

    out = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in players:
        tf = team_fix.get(p.get("team"))
        if not tf:
            continue
        pos = p.get("position")
        # relevant fixture difficulty: attackers need goals-fixture, defenders CS-fixture
        diff = tf["gs_d"] if pos in ("MID", "FWD") else tf["cs_d"]
        if diff == 2:  # tough fixture — skip
            continue
        out.setdefault(pos, []).append({
            "name": p["name"], "team": p["team"], "position": pos,
            "price": p.get("price", 0), "form": p.get("form", 0),
            "opp": tf["opp"], "venue": tf["venue"], "fix_d": diff,
            "score": _player_target_score(p),
            "owned": (p.get("name"), p.get("team")) in owned,
        })
    for pos in out:
        out[pos].sort(key=lambda r: -r["score"])
        out[pos] = out[pos][:per_pos]
    return out


def overall_targets(players: list, rankings: dict, fixtures: dict,
                    squad: list, gw_from: int, gw_to: int,
                    per_pos: int = 8) -> dict:
    """
    Best players to target across a multi-GW window (default next 5).

    Combines player quality (form + underlying) with how many easy/medium
    fixtures their team has over the window. Flags owned players.
    """
    owned = {(s.get("name"), s.get("team")) for s in squad}
    gws = list(range(gw_from, gw_to + 1))
    n = len(gws)

    # fixture ease per team over the window, per category (0=easy 1=med 2=hard)
    team_ease = {}
    for team in CANONICAL_TEAMS:
        cs_pts, gs_pts, chips = [], [], []
        for gw in gws:
            fx = next((f for f in fixtures.get(team, []) if f["gw"] == gw), None)
            if not fx or fx["opponent"] not in rankings:
                cs_pts.append(0); gs_pts.append(0); chips.append(None)
                continue
            cs_d = _difficulty("cs", rankings[fx["opponent"]], fx["venue"])
            gs_d = _difficulty("gs", rankings[fx["opponent"]], fx["venue"])
            cs_pts.append(2 - cs_d)   # easy=2, med=1, hard=0
            gs_pts.append(2 - gs_d)
            chips.append(f"{CODE.get(fx['opponent'], fx['opponent'][:3])}({fx['venue']})")
        team_ease[team] = {"cs": cs_pts, "gs": gs_pts, "chips": chips}

    out = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in players:
        te = team_ease.get(p.get("team"))
        if not te:
            continue
        pos = p.get("position")
        ease = te["gs"] if pos in ("MID", "FWD") else te["cs"]
        ease_sum = sum(ease)                 # 0..2n
        easy_n = sum(1 for e in ease if e == 2)
        # combined: player quality + fixture ease over the window
        combined = _player_target_score(p) + ease_sum * 0.8
        out.setdefault(pos, []).append({
            "name": p["name"], "team": p["team"], "position": pos,
            "price": p.get("price", 0), "form": p.get("form", 0),
            "points": p.get("points", 0), "ppg": p.get("ppg", 0),
            "easy_n": easy_n, "chips": te["chips"],
            "score": round(combined, 1),
            "owned": (p.get("name"), p.get("team")) in owned,
        })
    for pos in out:
        out[pos].sort(key=lambda r: -r["score"])
        out[pos] = out[pos][:per_pos]
    return {"positions": out, "gws": [f"GW{g}" for g in gws]}


# ==========================================================================
# Fixture ease % — a formula-based 0-100 score per team over a GW window
# ==========================================================================

def fixture_ease_percent(rankings: dict, fixtures: dict, gw_from: int,
                         gw_to: int) -> dict:
    """
    Score how EASY each team's fixtures are over a window, as a 0-100%.

    Formula (per fixture, then averaged over the window):

      For a CLEAN-SHEET / defensive view, ease depends on how weak the
      opponent's ATTACK is:   raw = opp_xg_per_game
      For a GOALS / attacking view, ease depends on how weak the opponent's
      DEFENCE is:             raw = opp_xga_per_game

      We invert and normalise raw against the league's min/max per-game value
      so 100% = facing the weakest attack/defence in the league, 0% = the
      strongest. A home game adds a small fixed bonus, away subtracts it.

        ease_fixture = clamp( 100 * (max_raw - raw) / (max_raw - min_raw)
                              + home_adj , 0, 100 )

      Team % = mean(ease_fixture over the window).

    Returns {cat: [{team, pct, chips:[{code,venue,pct}]}], ...} sorted desc.
    """
    gws = list(range(gw_from, gw_to + 1))
    GP = 3.0  # games played so far (per-game normaliser)
    HOME_ADJ = 6.0  # +/- percentage points for venue

    # league min/max of per-game xG (attack) and xGA (defence)
    xgs = [r["xg"] / GP for r in rankings.values()]
    xgas = [r["xga"] / GP for r in rankings.values()]
    xg_min, xg_max = min(xgs), max(xgs)
    xga_min, xga_max = min(xgas), max(xgas)

    def _norm(raw, lo, hi, venue):
        if hi - lo < 1e-9:
            base = 50.0
        else:
            base = 100.0 * (hi - raw) / (hi - lo)  # weaker opp -> higher %
        base += HOME_ADJ if venue == "H" else -HOME_ADJ
        return max(0.0, min(100.0, base))

    out = {}
    for cat in ("cs", "gs"):
        rows = []
        for team in CANONICAL_TEAMS:
            chips, pcts = [], []
            for gw in gws:
                fx = next((f for f in fixtures.get(team, []) if f["gw"] == gw), None)
                if not fx or fx["opponent"] not in rankings:
                    continue
                opp = rankings[fx["opponent"]]
                if cat == "cs":  # ease vs opponent attack
                    raw = opp["xg"] / GP
                    pct = _norm(raw, xg_min, xg_max, fx["venue"])
                else:            # ease vs opponent defence
                    raw = opp["xga"] / GP
                    pct = _norm(raw, xga_min, xga_max, fx["venue"])
                pcts.append(pct)
                chips.append({"code": CODE.get(fx["opponent"], fx["opponent"][:3]),
                              "venue": fx["venue"], "pct": round(pct)})
            avg = round(sum(pcts) / len(pcts)) if pcts else 0
            rows.append({"team": team, "pct": avg, "chips": chips})
        rows.sort(key=lambda r: -r["pct"])
        out[cat] = rows
    return out
