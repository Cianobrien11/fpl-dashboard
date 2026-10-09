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

# Optional per-request GW-history cache. The app sets this once per request
# (set_history_cache) so every expected_points call picks up recent-form blends
# without threading the dict through every call site. Falls back cleanly to {}.
_HISTORY_CACHE: dict = {}


def set_history_cache(history_by_element: dict | None) -> None:
    """App layer calls this once per request with {element: [gw rows newest-first]}."""
    global _HISTORY_CACHE
    _HISTORY_CACHE = history_by_element or {}


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


def _rank_scale(values, reverse=False):
    """Return a dict mapping index->0..100 score by rank within `values`.
    reverse=True means higher raw value = higher score."""
    n = len(values)
    if n <= 1:
        return {0: 50.0}
    order = sorted(range(n), key=lambda i: values[i], reverse=reverse)
    out = {}
    for rank, idx in enumerate(order):
        out[idx] = round(100.0 * (n - 1 - rank) / (n - 1), 1)
    return out


def _recent_weight(history, key, n_recent=5, recent_w=2.0):
    """Weighted per-match average of `key` over a team's match history,
    weighting the most recent `n_recent` games `recent_w`x. Falls back to a
    simple mean when history is short. Returns None if no usable data."""
    if not history:
        return None
    vals = []
    for h in history:
        v = h.get(key)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None
    weights = []
    m = len(vals)
    for i in range(m):
        weights.append(recent_w if i >= m - n_recent else 1.0)
    num = sum(v * w for v, w in zip(vals, weights))
    den = sum(weights) or 1.0
    return num / den


def compute_rankings(team_stats: dict[str, dict],
                     strength: dict | None = None) -> dict[str, dict]:
    """
    Build a per-team rating set powering attack/defence, difficulty and xPts.

    For each team we derive PER-GAME rates (using real matches played) and a
    0-100 ATTACK and DEFENCE rating from a weighted composite of every factor
    the data pipeline supplies, degrading gracefully when a factor is absent:

      Attack  <- npxG/90 (or xG), shots/90, SoT/90, deep/90, goals/90
      Defence <- npxGA/90 (or xGA), shots faced/90, SoT against/90,
                 deep allowed/90, goals against/90   (lower = better)

    When per-match `history` is present we weight the most recent games more
    heavily (recent form). Season totals (xg/xga/gf/ga) are preserved so the
    existing xPts multiplier helpers keep working.

    Ranks returned:
      cs_rk  1 = best defence (back for clean sheet)
      gs_rk  1 = best attack  (back to score)
      gc_rk  1 = worst defence (best target to score against)
    """
    rows = []
    for team in CANONICAL_TEAMS:
        s = team_stats.get(team, {})
        st = (strength or {}).get(team, {})
        mp = max(int(s.get("mp", 0) or 0), 0)
        gp = mp if mp > 0 else 3  # avoid div-by-zero; seed assumes ~3 games
        hist = s.get("history") or []

        def pg(key_total, key_recent=None):
            """Per-game rate, form-weighted if history has the per-match key."""
            if hist and key_recent:
                rw = _recent_weight(hist, key_recent)
                if rw is not None:
                    return rw
            return (float(s.get(key_total, 0) or 0)) / gp

        # Attacking per-game factors (prefer non-penalty xG when present)
        npxg = s.get("npxg")
        xg_pg = pg("npxg", "npxG") if npxg is not None else pg("xg", "xG")
        gf_pg = pg("gf", "scored")
        sh_pg = pg("sh")
        sot_pg = pg("sot")
        deep_pg = pg("deep", "deep")

        # Defensive per-game factors (lower = better)
        npxga = s.get("npxga")
        xga_pg = pg("npxga", "npxGA") if npxga is not None else pg("xga", "xGA")
        ga_pg = pg("ga", "missed")
        shag_pg = pg("sh_ag")
        sotag_pg = pg("sot_ag")
        deepag_pg = pg("deep_allowed", "deep_allowed")
        ppda = float(s.get("ppda", 0) or 0)

        rows.append({
            "team": team,
            "ov_home": st.get("ov_home"), "ov_away": st.get("ov_away"),
            "form": st.get("form", 0),
            # season totals (kept for xPts helpers / display)
            "xg": float(s.get("xg", 0) or 0), "xga": float(s.get("xga", 0) or 0),
            "gf": int(s.get("gf", 0) or 0), "ga": int(s.get("ga", 0) or 0),
            "sh": int(s.get("sh", 0) or 0), "sot": int(s.get("sot", 0) or 0),
            "sh_ag": int(s.get("sh_ag", 0) or 0), "sot_ag": int(s.get("sot_ag", 0) or 0),
            "mp": mp,
            # per-game rates
            "xg_pg": round(xg_pg, 2), "xga_pg": round(xga_pg, 2),
            "gf_pg": round(gf_pg, 2), "ga_pg": round(ga_pg, 2),
            "sh_pg": round(sh_pg, 2), "sot_pg": round(sot_pg, 2),
            "sh_ag_pg": round(shag_pg, 2), "sot_ag_pg": round(sotag_pg, 2),
            "deep_pg": round(deep_pg, 2), "deep_ag_pg": round(deepag_pg, 2),
            "ppda": round(ppda, 2),
        })

    n = len(rows)
    # Composite ATTACK: weight each factor; factors with no data contribute 0
    # weight (so teams aren't penalised for a missing stat).
    att_factors = [("xg_pg", 0.40), ("sot_pg", 0.20), ("gf_pg", 0.18),
                   ("sh_pg", 0.12), ("deep_pg", 0.10)]
    def_factors = [("xga_pg", 0.40), ("sot_ag_pg", 0.20), ("ga_pg", 0.18),
                   ("sh_ag_pg", 0.12), ("deep_ag_pg", 0.10)]

    def composite(factors, reverse):
        # Returns list of 0-100 scores per row. reverse=True: higher raw=better.
        active = [(k, w) for k, w in factors if any(r[k] for r in rows)]
        if not active:
            return [50.0] * n
        tw = sum(w for _, w in active)
        scores = [0.0] * n
        for k, w in active:
            vals = [r[k] for r in rows]
            scaled = _rank_scale(vals, reverse=reverse)
            for i in range(n):
                scores[i] += (scaled[i] * w / tw)
        return [round(x, 1) for x in scores]

    attack = composite(att_factors, reverse=True)   # more xG/shots = stronger
    defence = composite(def_factors, reverse=False)  # less xGA/shots = stronger
    for i, r in enumerate(rows):
        r["attack"] = attack[i]
        r["defence"] = defence[i]

    # Ranks derived from the composite ratings (stronger, multi-factor)
    cs = sorted(range(n), key=lambda i: -rows[i]["defence"])   # best defence first
    for rank, i in enumerate(cs):
        rows[i]["cs_rk"] = rank + 1
    gs = sorted(range(n), key=lambda i: -rows[i]["attack"])    # best attack first
    for rank, i in enumerate(gs):
        rows[i]["gs_rk"] = rank + 1
    gc = sorted(range(n), key=lambda i: rows[i]["defence"])    # worst defence first
    for rank, i in enumerate(gc):
        rows[i]["gc_rk"] = rank + 1

    return {r["team"]: r for r in rows}



def _difficulty(cat: str, opp_ranks: dict, venue: str) -> int:
    """
    Return 0 (easy), 1 (medium), 2 (hard) for a fixture in a category,
    driven by the opponent's CONTINUOUS 0-100 attack/defence ratings plus a
    venue tilt and a current-form nudge.

      cs  (we want a clean sheet)  -> easier vs a WEAK attack (low opp attack)
      gs  (we want to score)       -> easier vs a LEAKY defence (low opp defence)
      gc  (we're likely to concede)-> easier target vs a STRONG attack

    Thresholds are on the 0-100 scale; venue shifts the effective rating by
    ~8 points (opponent is tougher at their home).
    """
    if cat == "cs":
        opp = opp_ranks.get("attack", 50.0)
        # opponent plays opposite venue: if WE are home, they're away (weaker)
        eff = opp - (8 if venue == "H" else -8)
        base = 0 if eff <= 38 else (2 if eff >= 68 else 1)
    elif cat == "gs":
        opp = opp_ranks.get("defence", 50.0)
        eff = opp - (8 if venue == "H" else -8)
        base = 0 if eff <= 38 else (2 if eff >= 68 else 1)
    else:  # gc
        opp = opp_ranks.get("attack", 50.0)
        eff = opp + (8 if venue == "A" else -8)  # their strong attack at their home = we concede
        base = 0 if eff >= 68 else (2 if eff <= 38 else 1)

    # Current-form nudge from FPL overall strength on borderline fixtures.
    oh, oa = opp_ranks.get("ov_home"), opp_ranks.get("ov_away")
    if base == 1 and oh is not None and oa is not None:
        opp_str = oa if venue == "H" else oh
        if opp_str <= 2:
            base = 0 if cat in ("cs", "gs") else 2
        elif opp_str >= 5:
            base = 2 if cat in ("cs", "gs") else 0
    return base



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
        gp = 3.0  # games played, for per-game context in tooltips
        per_cat = {}
        for cat in ("cs", "gs", "gc"):
            fx_list, easy, em, dsum = [], [], [], 0
            for f in team_fx:
                opp = f["opponent"]
                if opp not in rankings:
                    continue
                orr = rankings[opp]
                d = _difficulty(cat, orr, f["venue"])
                dsum += d
                # Context for the tooltip: what makes this fixture easy/hard.
                # CS/GC judged vs opponent ATTACK; GS judged vs opponent DEFENCE.
                if cat == "gs":
                    opp_stat, opp_lbl = round(_per_game(orr, "xga"), 2), "opp xGA/gm"
                    opp_rank = orr["gc_rk"]
                else:
                    opp_stat, opp_lbl = round(_per_game(orr, "xg"), 2), "opp xG/gm"
                    opp_rank = orr["gs_rk"]
                # 1-5 FDR-style rating (1=easiest, 5=hardest) from the 0/1/2 tier
                fdr = {0: 2, 1: 3, 2: 5}[d]
                fx_list.append({
                    "code": CODE.get(opp, opp[:3].upper()), "opp": opp,
                    "venue": f["venue"], "d": d, "gw": f["gw"], "fdr": fdr,
                    "opp_stat": opp_stat, "opp_lbl": opp_lbl, "opp_rank": opp_rank,
                })
                if d == 0:
                    easy.append(f"GW{f['gw']}")
                if d <= 1:
                    em.append(f"GW{f['gw']}")
            # average difficulty as a 1-5 score (lower = easier run)
            avg_fdr = round(sum(x["fdr"] for x in fx_list) / len(fx_list), 1) if fx_list else 0
            per_cat[cat] = (fx_list, easy, em, avg_fdr)

        for cat in ("cs", "gs", "gc"):
            fx_list, easy, em, avg_fdr = per_cat[cat]
            rk = tr["gc_rk"] if cat == "gc" else tr[f"{cat}_rk"]
            x = tr["xg"] if cat == "gs" else tr["xga"]
            a = tr["gf"] if cat == "gs" else tr["ga"]
            out[cat].append({
                "team": team, "rk": rk, "x": round(x, 2), "a": a,
                "easy_n": len(easy), "easy_gws": easy,
                "em_n": len(em), "em_gws": em, "avg_fdr": avg_fdr,
                "fixtures": fx_list,
            })

    # sort each category by (easy count desc, quality rank asc)
    for cat in out:
        out[cat].sort(key=lambda r: (-r["easy_n"], r["rk"]))
    return out


def _per_game(r: dict, key: str) -> float:
    """Season total -> per-game using the team's REAL matches played.
    (Previously hard-coded /3 games, which inflated xG ~2x by GW5-6.)"""
    mp = r.get("mp") or 0
    if mp <= 0:
        mp = 3
    return float(r.get(key, 0) or 0) / mp


def _poisson(k, lam):
    import math
    return math.exp(-lam) * lam ** k / math.factorial(k)


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
            # Attack x defence relative to league average (per REAL game),
            # with a modest home edge. Keeps team xG in a realistic 0.5-2.6 band.
            lg = _league_avg(rankings, "xg") or 1.4
            h_att, h_def = _per_game(h, "xg"), _per_game(h, "xga")
            a_att, a_def = _per_game(a, "xg"), _per_game(a, "xga")
            h_xg = (h_att / lg) * (a_def / lg) * lg * 1.10
            a_xg = (a_att / lg) * (h_def / lg) * lg * 0.92
            h_xg = max(0.3, min(3.0, h_xg)); a_xg = max(0.3, min(3.0, a_xg))
            # Win/draw/loss from independent Poisson goals (0-8).
            ph = [_poisson(k, h_xg) for k in range(9)]
            pa = [_poisson(k, a_xg) for k in range(9)]
            p_home = sum(ph[i] * pa[j] for i in range(9) for j in range(9) if i > j)
            p_draw = sum(ph[i] * pa[i] for i in range(9))
            p_away = max(0.0, 1 - p_home - p_draw)
            probs = {"home": p_home, "draw": p_draw, "away": p_away}
            best = max(probs, key=probs.get)
            verdict = {"home": f"{home} win", "away": f"{away} win", "draw": "Draw"}[best]
            # Most likely scoreline CONSISTENT with the verdict.
            cands = [(ph[i] * pa[j], i, j) for i in range(6) for j in range(6)
                     if (best == "home" and i > j) or (best == "away" and j > i)
                     or (best == "draw" and i == j)]
            _, hs, as_ = max(cands)
            top = probs[best]
            conf = "Clear" if top >= 0.55 else ("Lean" if top >= 0.42 else "Tight")
            diff = h_xg - a_xg
            preds.append({
                "home": home, "away": away,
                "home_xg": round(h_xg, 2), "away_xg": round(a_xg, 2),
                "home_score": hs, "away_score": as_,
                "verdict": verdict, "confidence": conf,
                "p_home": round(p_home * 100), "p_draw": round(p_draw * 100),
                "p_away": round(p_away * 100),
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


# ==========================================================================
# Phase 2 — player projections: expected points, price moves, set-piece boost
# ==========================================================================

# FPL points for a goal / clean sheet by position
_GOAL_PTS = {"GK": 6, "DEF": 6, "MID": 5, "FWD": 4}
_CS_PTS = {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}


# league-average xG / xGA per game (updated from the ranking pool at call time)
def _league_avg(rankings: dict, key: str) -> float:
    vals = [_per_game(r, key) for r in rankings.values() if r.get(key)]
    return (sum(vals) / len(vals)) if vals else 1.4


def _attack_multiplier(opp: dict, venue: str, league_xga: float) -> float:
    """
    Continuous attacking-fixture multiplier for a player facing `opp`.

    Scales by how the opponent's DEFENCE (xGA/game) compares to the league
    average — facing a mean defence gives ~1.0, a leaky one boosts >1, an
    elite one (e.g. Arsenal ~0.33/gm vs ~1.5 avg) cuts sharply toward ~0.4.
    Home/away tilts it +/-8%. Clamped to a sensible 0.35-1.8 band.
    """
    opp_xga_g = _per_game(opp, "xga")
    ratio = opp_xga_g / league_xga if league_xga else 1.0
    mult = ratio * (1.08 if venue == "H" else 0.92)
    return max(0.35, min(1.8, mult))


def _cs_multiplier(opp: dict, venue: str, league_xg: float) -> float:
    """Clean-sheet-fixture multiplier: scales by opponent ATTACK strength.
    Facing a weak attack raises CS chance; a strong one lowers it."""
    opp_xg_g = _per_game(opp, "xg")
    ratio = league_xg / opp_xg_g if opp_xg_g else 1.5
    mult = ratio * (1.08 if venue == "H" else 0.92)
    return max(0.35, min(1.8, mult))


def _fixture_ease_for(player_pos: str, opp_ranks: dict, venue: str) -> float:
    """Kept for callers that still want the coarse 3-tier ease (0.82-1.15)."""
    cat = "gs" if player_pos in ("MID", "FWD") else "cs"
    d = _difficulty(cat, opp_ranks, venue)
    return {0: 1.15, 1: 1.0, 2: 0.82}[d]


def _rate_from_history(rows, stat_key, n_recent):
    """Per-90 rate of `stat_key` over the most recent `n_recent` GWs with
    minutes. rows are per-GW dicts (newest first). Returns None if no usable
    minutes in the window so the caller can fall back."""
    if not rows:
        return None
    total_stat, total_mins = 0.0, 0.0
    used = 0
    for r in rows:
        if used >= n_recent:
            break
        mins = float(r.get("minutes", 0) or 0)
        if mins <= 0:
            continue
        total_stat += float(r.get(stat_key, 0) or 0)
        total_mins += mins
        used += 1
    if total_mins <= 0:
        return None
    return total_stat / (total_mins / 90.0)


def _blended_per90(player, key_season, hist_rows):
    """FPL IQ recent-form blend of a per-90 rate (xg/xa), per the model spec:
        40% season + 30% last-6 + 20% last-10 + 10% prev-season
    Uses REAL per-GW history (hist_rows, newest first) when available; weights
    re-normalise over whichever windows have data. Falls back to the season/
    form proxy (_weighted_per90) when there's no history yet.
    """
    mins = player.get("minutes", 0) or 0
    season_total = float(player.get(key_season, 0) or 0)
    season90 = (season_total / (mins / 90.0)) if mins >= 60 else None

    last6 = _rate_from_history(hist_rows, key_season, 6) if hist_rows else None
    last10 = _rate_from_history(hist_rows, key_season, 10) if hist_rows else None
    prev = float(player.get(key_season + "_prev", 0) or 0) or None  # not usually available

    parts = []
    if season90 is not None: parts.append((season90, 0.40))
    if last6 is not None:    parts.append((last6, 0.30))
    if last10 is not None:   parts.append((last10, 0.20))
    if prev is not None:     parts.append((prev, 0.10))

    if not parts:
        # No usable data at all — fall back to the proxy blend.
        return _weighted_per90(player, key_season)
    tw = sum(w for _, w in parts)
    return sum(v * w for v, w in parts) / tw


def _weighted_per90(player, key_season):
    """Blend a per-90 rate: 60% season total/90, 40% recent form proxy.
    FPL only gives season totals, so we approximate 'recent' via the ratio of
    form to ppg (hot players get a modest uplift). Keeps it grounded."""
    mins = player.get("minutes", 0) or 0
    if mins < 60:
        return 0.0
    season_total = float(player.get(key_season, 0) or 0)
    per90 = season_total / (mins / 90.0)
    # recent-form tilt: if form > ppg the player is trending up, nudge up to +25%
    ppg = float(player.get("ppg", 0) or 0)
    form = float(player.get("form", 0) or 0)
    if ppg > 0:
        tilt = max(0.75, min(1.25, 0.6 + 0.4 * (form / ppg)))
    else:
        tilt = 1.0
    return per90 * tilt


def _x_appearance(player):
    """Expected appearance points from P(60+) and P(1-59).
    Derives start/sub probabilities from avg minutes, starts ratio and
    availability status. Returns (points, p60, conf)."""
    avg_min = float(player.get("avg_min", 0) or 0)
    ninetys = float(player.get("ninetys", 0) or 0)
    starts = float(player.get("starts", 0) or 0)
    status = player.get("status", "a")
    chance = player.get("chance")

    # Base probability of starting from how many minutes they average + starts.
    # Fix #5: CONTINUOUS (was 5 buckets, so every 80+ min starter got the
    # identical 0.95 -> identical 87% confidence). 20' avg -> 0.10, 90' -> 0.92.
    p_start = 0.10 + 0.82 * max(0.0, min(1.0, (avg_min - 20) / 70.0))
    # share of appearances that were starts (ninetys ~ full games played)
    if ninetys >= 1:
        start_share = min(1.0, starts / max(ninetys, starts, 1))
        p_start *= 0.85 + 0.15 * start_share
    if starts >= 4:
        p_start = min(0.97, p_start + 0.04)
    # availability overrides
    if status in ("i", "s", "u"):
        p_start = 0.0
    elif status == "d":
        p_start *= 0.5
    if chance is not None:
        try:
            p_start *= max(0.0, min(1.0, float(chance) / 100.0))
        except (TypeError, ValueError):
            pass

    # P(60+) ~ most of p_start; P(1-59) ~ subs + early-subbed starters
    p60 = p_start * 0.92
    p_1_59 = p_start * 0.08 + (0.15 if (avg_min and avg_min < 65 and status == "a") else 0.0)
    p_1_59 = min(p_1_59, 1 - p60)
    pts = p60 * 2 + p_1_59 * 1
    return pts, p60, p_start


def _x_goals(player, opp, venue, league_xga, pos, hist_rows=None):
    """Expected goal points: blended xG/90 (season + recent-form from GW
    history) x fixture x position."""
    xg90 = _blended_per90(player, "xg", hist_rows)
    opp_factor = _attack_multiplier(opp, venue, league_xga)  # 0.35-1.8 continuous
    exp_goals = xg90 * opp_factor
    return exp_goals, exp_goals * _GOAL_PTS.get(pos, 4)


def _x_assists(player, opp, venue, league_xga, hist_rows=None):
    xa90 = _blended_per90(player, "xa", hist_rows)
    opp_factor = _attack_multiplier(opp, venue, league_xga)
    exp_assists = xa90 * opp_factor
    return exp_assists, exp_assists * 3


def _x_clean_sheet(cs_prob, pos):
    """CS points from probability. GK/DEF=4, MID=1, FWD=0."""
    return cs_prob * _CS_PTS.get(pos, 0)


def _x_defcon(player, pos, p60, hist_rows=None):
    """Expected DefCon points: P(hitting threshold) x 2.
    Threshold 10 (DEF) / 12 (MID,FWD).

    When per-GW history is available, use the EMPIRICAL hit rate — how often the
    player actually reached the threshold in their recent starts — which is far
    more accurate than a per-90 ratio. Falls back to the per-90 approximation
    when no history exists yet.
    """
    if pos not in ("DEF", "MID", "FWD"):
        return 0.0, 0.0
    threshold = 10 if pos == "DEF" else 12

    # --- Empirical path: real hit frequency from logged GWs (starts only) ---
    if hist_rows:
        starts = [r for r in hist_rows if float(r.get("minutes", 0) or 0) >= 60]
        starts = starts[:10]  # last ~10 starts
        if len(starts) >= 3:
            hits = sum(1 for r in starts if float(r.get("defcon", 0) or 0) >= threshold)
            p_hit = hits / len(starts)
            p_hit *= p60
            return p_hit, p_hit * 2.0

    # --- Fallback: per-90 ratio approximation ---
    dc90 = float(player.get("defcon_pg", 0) or 0)
    if dc90 <= 0:
        return 0.0, 0.0
    ratio = dc90 / threshold
    p_hit = max(0.0, min(0.95, (ratio - 0.6) / 0.6)) if ratio > 0.6 else 0.0
    p_hit *= p60
    return p_hit, p_hit * 2.0


def _x_saves(player, opp, venue, pos, p60):
    """GK save points: expected saves / 3. Estimate saves from opponent attack."""
    if pos != "GK":
        return 0.0, 0.0
    saves = float(player.get("saves", 0) or 0)
    ninetys = float(player.get("ninetys", 0) or 0)
    if ninetys <= 0:
        return 0.0, 0.0
    saves90 = saves / ninetys
    # opponent attack tilts expected shots faced
    opp_att = opp.get("attack", 50) / 50.0  # ~1.0 average, higher = more shots
    exp_saves = saves90 * (0.85 + 0.3 * (opp_att - 1)) * p60
    return exp_saves, exp_saves / 3.0


def _x_bonus(player, exp_goals, exp_assists, cs_prob, pos, p60, hist_rows=None):
    """Expected bonus points.

    When per-GW history is available, build the expectation from the player's
    ACTUAL bonus distribution — the real xBonus = P(1)*1 + P(2)*2 + P(3)*3 from
    how often they earned 1/2/3 bonus in recent starts — blended with a
    forward-looking signal (this fixture's projected involvement / CS). Falls
    back to the season-BPS proxy when no history exists.
    """
    # Forward-looking signal from THIS fixture's projection (always available).
    invo = exp_goals + exp_assists
    fwd = min(1.3, invo * 0.8) + (cs_prob * 0.3 if pos in ("GK", "DEF") else 0.0)

    # --- Empirical path: real bonus distribution from logged starts ---
    if hist_rows:
        starts = [r for r in hist_rows if float(r.get("minutes", 0) or 0) >= 60][:10]
        if len(starts) >= 3:
            n = len(starts)
            p1 = sum(1 for r in starts if int(r.get("bonus", 0) or 0) == 1) / n
            p2 = sum(1 for r in starts if int(r.get("bonus", 0) or 0) == 2) / n
            p3 = sum(1 for r in starts if int(r.get("bonus", 0) or 0) == 3) / n
            hist_xbonus = p1 * 1 + p2 * 2 + p3 * 3
            # Blend the player's established bonus habit (60%) with this
            # fixture's forward-looking lift (40%), scaled by minutes security.
            xbonus = (0.6 * hist_xbonus + 0.4 * min(2.0, fwd)) * p60
            return min(xbonus, 2.5)

    # --- Fallback: season-BPS proxy ---
    ninetys = float(player.get("ninetys", 0) or 0)
    bps = float(player.get("bps", 0) or 0)
    bps90 = (bps / ninetys) if ninetys > 0 else 0.0
    base = min(0.9, max(0.0, (bps90 - 14) / 16.0))
    xbonus = (base + fwd) * p60
    return min(xbonus, 2.2)


def _x_cards(player, p60):
    """Expected card deduction: P(yellow)*1 + P(red)*3 (negative)."""
    ninetys = float(player.get("ninetys", 0) or 0)
    if ninetys <= 0:
        return 0.0
    yc90 = (float(player.get("yellow_cards", 0) or 0)) / ninetys
    rc90 = (float(player.get("red_cards", 0) or 0)) / ninetys
    p_yellow = min(0.6, yc90)
    p_red = min(0.1, rc90)
    return -(p_yellow * 1 + p_red * 3) * p60


def expected_points(players: list, rankings: dict, fixtures: dict,
                    gw: int, history_by_element: dict | None = None) -> list:
    """
    Component-based xPts (FPL IQ v2):

      xPts = xAppearance + xGoals + xAssists + xCleanSheet
             + xDefCon + xSaves + xBonus - xCards

    Each component is an EXPECTED VALUE from the probability of the scoring
    event, not a blended heuristic — mirroring how serious FPL models work.
    Also returns a per-component breakdown and a confidence % (driven by
    minutes security + sample size) on each player.
    """
    league_xga = _league_avg(rankings, "xga")
    league_xg = _league_avg(rankings, "xg")
    out = []
    for p in players:
        team = p.get("team")
        fx = next((f for f in fixtures.get(team, []) if f["gw"] == gw), None)
        if not fx or team not in rankings or fx["opponent"] not in rankings:
            continue
        pos = p.get("position", "MID")
        opp = rankings[fx["opponent"]]
        venue = fx["venue"]

        # Per-GW history for this player (newest first) — powers recent-form
        # blends in the attacking components. Empty if not logged yet.
        hist_rows = None
        _hist = history_by_element if history_by_element is not None else _HISTORY_CACHE
        if _hist:
            hist_rows = _hist.get(p.get("id"))

        # Appearance + minutes security
        x_app, p60, p_start = _x_appearance(p)

        # Attacking (scaled by how likely they play 60+)
        egoals, x_goal_pts = _x_goals(p, opp, venue, league_xga, pos, hist_rows)
        eassists, x_assist_pts = _x_assists(p, opp, venue, league_xga, hist_rows)
        x_goal_pts *= p60
        x_assist_pts *= p60
        egoals *= p60
        eassists *= p60
        # set-piece uplift
        if p.get("pen_order") == 1:
            x_goal_pts += 0.5 * p60
        elif p.get("ck_order") == 1 or p.get("fk_order") == 1:
            x_assist_pts += 0.3 * p60

        # Clean sheet (uses the model+market CS probability helper's logic)
        cs_mult = _cs_multiplier(opp, venue, league_xg)
        cs_prob = max(0.03, min(0.70, 0.30 * cs_mult))
        x_cs_pts = _x_clean_sheet(cs_prob, pos) * p60

        # DefCon, Saves, Bonus, Cards
        _, x_defcon_pts = _x_defcon(p, pos, p60, hist_rows)
        _, x_saves_pts = _x_saves(p, opp, venue, pos, p60)
        x_bonus_pts = _x_bonus(p, egoals, eassists, cs_prob, pos, p60, hist_rows)
        x_card_pts = _x_cards(p, p60)

        model_xpts = (x_app + x_goal_pts + x_assist_pts + x_cs_pts
                      + x_defcon_pts + x_saves_pts + x_bonus_pts + x_card_pts)
        model_xpts = max(0.0, model_xpts)

        # --- ACTUAL-RETURNS ANCHOR ---------------------------------------
        # The component model is "opportunity" (xG/xA + fixture). On its own it
        # over-rates fringe players at weak clubs who get a soft fixture (e.g.
        # a rotation player projecting 8.0 despite ~1 pt/week in reality).
        # Anchor it to the player's REAL returns: season points-per-game (ppg)
        # blended with recent form, fixture-adjusted. Lean on the anchor more
        # as the player's sample (90s played) grows. This pulls low-return
        # players down toward reality while nailed performers stay high.
        ninetys = float(p.get("ninetys", 0) or 0)
        ppg = float(p.get("ppg", 0) or 0)
        form = float(p.get("form", 0) or 0)
        if ppg and form:
            base_return = 0.45 * ppg + 0.55 * form
        else:
            base_return = form or ppg
        # fixture tilt on the real-returns baseline (dampened so form leads)
        att_mult = _attack_multiplier(opp, venue, league_xga)
        cs_mult_a = _cs_multiplier(opp, venue, league_xg)
        fix_adj = att_mult if pos in ("MID", "FWD") else cs_mult_a
        # Fix #4: scale fully by P(60+) -- the old 0.4 floor let a 3-minute
        # sub keep 40% of a starter's projection.
        anchor_xpts = base_return * (1.0 + 0.4 * (fix_adj - 1.0)) * p60
        # Blend: tiny sample -> trust the opportunity model; real sample ->
        # lean on actual returns (caps a fringe player's ceiling).
        sample_conf = max(0.0, min(1.0, ninetys / 6.0))
        w_anchor = 0.30 + 0.45 * sample_conf        # 0.30 .. 0.75
        xpts = round(max(0.0, w_anchor * anchor_xpts + (1 - w_anchor) * model_xpts), 1)
        # Sanity clamp: a single-GW xPts above ~20 is physically implausible and
        # signals bad/stale input data. Cap to keep the UI trustworthy.
        xpts = max(0.0, min(xpts, 20.0))

        # Confidence: minutes certainty dominates; sample size + status.
        # Graded so it can't saturate at 100% for every regular starter:
        #   minutes certainty = raw P(start) (already capped at 0.97)
        #   sample certainty  = 90s played, full credit only at 8+ (not 5)
        #   capped at 95% -- football always has rotation/injury risk.
        mins_cert = max(0.0, min(1.0, p_start))
        sample_cert = min(1.0, ninetys / 8.0)
        conf = round(100 * (0.75 * mins_cert + 0.25 * sample_cert))
        conf = max(5, min(95, conf))

        rel_cat = "gs" if pos in ("MID", "FWD") else "cs"
        d = _difficulty(rel_cat, opp, venue)
        out.append({
            "name": p["name"], "team": team, "position": pos,
            "price": p.get("price", 0), "opp": CODE.get(fx["opponent"], fx["opponent"][:3]),
            "venue": venue, "xpts": xpts, "fix_d": d,
            "form": p.get("form", 0), "selected_by": p.get("selected_by", 0),
            "id": p.get("id"),
            "confidence": conf,
            "components": {
                "appearance": round(x_app, 2), "goals": round(x_goal_pts, 2),
                "assists": round(x_assist_pts, 2), "clean_sheet": round(x_cs_pts, 2),
                "defcon": round(x_defcon_pts, 2), "saves": round(x_saves_pts, 2),
                "bonus": round(x_bonus_pts, 2), "cards": round(x_card_pts, 2),
            },
        })
    out.sort(key=lambda x: -x["xpts"])
    return out



def price_predictions(players: list) -> dict:
    """
    Predict imminent price changes from this-gameweek transfer momentum.

    FPL price rises/falls are driven by net transfers relative to ownership.
    We approximate net transfer momentum and flag likely risers/fallers
    tonight. Uses transfers_in_event / transfers_out_event already scraped.

    Returns {'rising': [...], 'falling': [...]} sorted by momentum.
    """
    scored = []
    for p in players:
        tin = p.get("transfers_in_event", 0) or 0
        tout = p.get("transfers_out_event", 0) or 0
        net = tin - tout
        scored.append({
            "name": p["name"], "team": p.get("team"), "position": p.get("position"),
            "price": p.get("price", 0), "net": net,
            "in": tin, "out": tout,
            "selected_by": p.get("selected_by", 0),
        })
    rising = sorted((s for s in scored if s["net"] > 0), key=lambda s: -s["net"])[:15]
    falling = sorted((s for s in scored if s["net"] < 0), key=lambda s: s["net"])[:15]
    return {"rising": rising, "falling": falling}


# ==========================================================================
# Phase 3 — Captaincy, Differentials, Team radar, My-Team fixture ticker
# ==========================================================================

CAP_SD_K = {"FWD": 1.9, "MID": 1.8, "DEF": 1.3, "GK": 1.1}


def cap_sd(p):
    """Rough one-GW points spread: attackers haul, defenders rarely do."""
    return CAP_SD_K.get(p.get("position"), 1.6) * max(0.5, (p.get("xpts") or 0)) ** 0.5


def cap_score(p):
    """THE captain ranking score (used everywhere): mean + share of upside."""
    return (p.get("xpts") or 0) + 0.6 * cap_sd(p)


def cap_ceiling(p):
    """~90th-percentile score: a total he beats about 1 week in 10."""
    return round((p.get("xpts") or 0) + 1.28 * cap_sd(p), 1)


def captaincy_board(players: list, rankings: dict, fixtures: dict, gw: int,
                    limit: int = 20) -> list:
    """Best captain picks across ALL players for a gameweek, by xPts."""
    ranked = [r for r in expected_points(players, rankings, fixtures, gw)
              if (r.get("confidence") or 0) >= 40]   # Fix #4: must actually play
    # Fix #3: ONE ranking (mean + upside), same as Home's captain pick.
    for r in ranked:
        r["ceiling"] = cap_ceiling(r)
        r["cap_score"] = round(cap_score(r), 2)
    ranked.sort(key=lambda r: -r["cap_score"])
    return ranked[:limit]


def differentials(players: list, rankings: dict, fixtures: dict, gw: int,
                  max_own: float = 10.0, limit: int = 20) -> list:
    """
    Under-owned players (<= max_own %) in good form with a decent fixture.
    Ranked by xPts so you get low-owned, high-ceiling picks for mini-leagues.
    """
    xp = {(r["name"], r["team"]): r for r in expected_points(players, rankings, fixtures, gw)}
    out = []
    for p in players:
        if (p.get("selected_by", 0) or 0) > max_own:
            continue
        if (p.get("minutes", 0) or 0) < 90:
            continue
        r = xp.get((p["name"], p["team"]))
        if not r:
            continue
        out.append({**r, "form": p.get("form", 0), "points": p.get("points", 0)})
    out.sort(key=lambda r: -r["xpts"])
    return out[:limit]


def team_radar(rankings: dict, strength: dict | None = None) -> list:
    """
    Per-team 0-100 scores on Attack / Defence / Form / Set-pieces for a radar
    or bar visual. Attack from xG rank, Defence from xGA rank, Form from FPL
    strength+form, Set-pieces left as a placeholder the app can fill from
    player set-piece ownership if desired.
    """
    n = len(CANONICAL_TEAMS)
    out = []
    for team in CANONICAL_TEAMS:
        r = rankings.get(team, {})
        # gs_rk 1 = best attack -> invert to 0-100
        attack = round(100 * (n - r.get("gs_rk", n)) / (n - 1), 0)
        defence = round(100 * (n - r.get("cs_rk", n)) / (n - 1), 0)
        st = (strength or {}).get(team, {})
        form = st.get("form", 0)
        # FPL form is points over recent games; scale ~0-15 -> 0-100
        form_score = round(min(100, (form / 12.0) * 100), 0) if form else None
        out.append({
            "team": team, "attack": attack, "defence": defence,
            "form": form_score,
            "xg": r.get("xg", 0), "xga": r.get("xga", 0),
        })
    out.sort(key=lambda x: -(x["attack"] + x["defence"]))
    return out


def my_team_ticker(squad: list, rankings: dict, fixtures: dict,
                   gw_from: int, gw_to: int) -> dict:
    """
    Fixture-ease ticker for the user's 15. For each player, a per-GW ease
    (green/amber/red) using the position-appropriate category, plus a count
    of tough fixtures so you can spot who hits a rough patch and when.
    """
    gws = list(range(gw_from, gw_to + 1))
    rows = []
    for p in squad:
        team = p.get("team")
        pos = p.get("position", "MID")
        cat = "gs" if pos in ("MID", "FWD") else "cs"
        cells, tough = [], 0
        for gw in gws:
            fx = next((f for f in fixtures.get(team, []) if f["gw"] == gw), None)
            if not fx or fx["opponent"] not in rankings:
                cells.append({"txt": "-", "d": 1})
                continue
            d = _difficulty(cat, rankings[fx["opponent"]], fx["venue"])
            if d == 2:
                tough += 1
            cells.append({"txt": f"{CODE.get(fx['opponent'], fx['opponent'][:3])}({fx['venue']})", "d": d})
        rows.append({"name": p["name"], "team": team, "position": pos,
                     "cells": cells, "tough": tough})
    # order by position then most-tough first
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    rows.sort(key=lambda r: (order.get(r["position"], 4), -r["tough"]))
    return {"gws": [f"GW{g}" for g in gws], "rows": rows}


# ==========================================================================
# Phase 4 — prediction accuracy tracker
# ==========================================================================

def score_predictions(logs: list, results: dict) -> dict:
    """
    Score logged scoreline predictions against actual finished results.

    logs: [{gw, logged_at, preds:[{home,away,home_score,away_score,verdict}]}]
    results: {str(gw): [{home,away,hs,as}]}

    Returns per-GW and overall accuracy on:
      * outcome  (home win / draw / away win) — the headline metric
      * exact    (exact scoreline)
    """
    per_gw, tot_o, tot_e, tot_n = [], 0, 0, 0
    for entry in logs:
        gw = entry["gw"]
        actual = {(r["home"], r["away"]): r for r in results.get(str(gw), [])}
        if not actual:
            continue
        o = e = n = 0
        for p in entry.get("preds", []):
            key = (p["home"], p["away"])
            a = actual.get(key)
            if not a or a.get("hs") is None or a.get("as") is None:
                continue
            n += 1
            # predicted & actual outcome
            def _out(hs, as_):
                return "H" if hs > as_ else ("A" if as_ > hs else "D")
            pred_o = _out(p["home_score"], p["away_score"])
            act_o = _out(a["hs"], a["as"])
            if pred_o == act_o:
                o += 1
            if p["home_score"] == a["hs"] and p["away_score"] == a["as"]:
                e += 1
        if n:
            per_gw.append({"gw": gw, "n": n, "outcome": o, "exact": e,
                           "outcome_pct": round(100 * o / n), "exact_pct": round(100 * e / n)})
            tot_o += o; tot_e += e; tot_n += n
    overall = {
        "n": tot_n,
        "outcome_pct": round(100 * tot_o / tot_n) if tot_n else 0,
        "exact_pct": round(100 * tot_e / tot_n) if tot_n else 0,
    }
    return {"per_gw": per_gw, "overall": overall}


def expected_points_range(players: list, rankings: dict, fixtures: dict,
                          gw_from: int, gw_to: int) -> list:
    """
    Sum each player's projected xPts across a gameweek window.

    Runs expected_points for every GW in the range and totals per player, so
    you can target who will accumulate the most over the next N weeks (great
    for planning transfers ahead). Also returns the per-GW breakdown and the
    number of "green" (easy) fixtures in the window.

    Returns [{name, team, position, price, selected_by, total_xpts, per_gw:
              {gw: xpts}, easy_n, avg_xpts}], sorted by total_xpts desc.
    """
    gws = list(range(gw_from, gw_to + 1))
    agg = {}
    for gw in gws:
        for r in expected_points(players, rankings, fixtures, gw):
            key = (r["name"], r["team"])
            a = agg.setdefault(key, {
                "name": r["name"], "team": r["team"], "position": r["position"],
                "price": r["price"], "selected_by": r["selected_by"], "id": r.get("id"),
                "total_xpts": 0.0, "per_gw": {}, "easy_n": 0,
            })
            a["total_xpts"] += r["xpts"]
            a["per_gw"][gw] = r["xpts"]
            if r.get("fix_d") == 0:
                a["easy_n"] += 1
    out = []
    for a in agg.values():
        n = len(a["per_gw"]) or 1
        a["total_xpts"] = round(a["total_xpts"], 1)
        a["avg_xpts"] = round(a["total_xpts"] / n, 1)
        out.append(a)
    out.sort(key=lambda r: -r["total_xpts"])
    return out


def h2h_vs_next_opponent(players: list, h2h: dict, fixtures: dict,
                         rankings: dict, next_gw: int) -> list:
    """
    Forwards & mids who have scored or assisted vs their NEXT opponent
    (over the seasons the Action supplied — last 2).

    h2h: {"surname|squad": {opponent_team: {games, goals, assists, xg}}}
    Returns rows sorted by goals-vs-that-opponent desc, for the H2H page.
    Only includes players who have a prior record vs the upcoming opponent.
    """
    def _key(p):
        last = p.get("name", "").split()[-1]
        if "." in last:
            last = last.split(".")[-1]
        return f"{last.lower()}|{p.get('team','')}"

    out = []
    for p in players:
        # only attacking players — forwards & midfielders
        if p.get("position") not in ("MID", "FWD"):
            continue
        team = p.get("team")
        fx = next((f for f in fixtures.get(team, []) if f["gw"] == next_gw), None)
        if not fx:
            continue
        opp = fx["opponent"]
        rec = (h2h.get(_key(p)) or {}).get(opp)
        if not rec or rec.get("games", 0) == 0:
            continue
        # must have actually scored or assisted vs this opponent
        if (rec.get("goals", 0) + rec.get("assists", 0)) == 0:
            continue
        g = rec["games"]
        out.append({
            "name": p["name"], "team": team, "position": p.get("position"),
            "price": p.get("price", 0), "opponent": opp, "venue": fx["venue"],
            "games": g, "goals": rec.get("goals", 0), "assists": rec.get("assists", 0),
            "xg": rec.get("xg", 0),
            "gpg": round(rec.get("goals", 0) / g, 2),
            "gi": rec.get("goals", 0) + rec.get("assists", 0),
        })
    out.sort(key=lambda r: (-r["goals"], -r["gi"]))
    return out


def squad_fixture_history(squad: list, h2h: dict, fixtures: dict,
                          gw_from: int, gw_to: int) -> list:
    """
    For each squad player, list upcoming fixtures annotated with their record
    (last 2 seasons) vs each opponent — from the H2H data supplied by the Action.

    Returns [{name, team, position, fixtures:[{gw, opp, venue, played, goals,
              assists, gi}]}], attackers first.
    Each fixture shows the player's prior goals/assists vs that opponent
    ("played" = games in the 2-year window; 0 = no prior meeting).
    """
    def _key(p):
        last = p.get("name", "").split()[-1]
        if "." in last:
            last = last.split(".")[-1]
        return f"{last.lower()}|{p.get('team','')}"

    gws = list(range(gw_from, gw_to + 1))
    rows = []
    for p in squad:
        prec = h2h.get(_key(p)) or {}
        fixes = []
        for gw in gws:
            fx = next((f for f in fixtures.get(p.get("team"), []) if f["gw"] == gw), None)
            if not fx:
                continue
            opp = fx["opponent"]
            rec = prec.get(opp) or {}
            g, a = rec.get("goals", 0), rec.get("assists", 0)
            fixes.append({
                "gw": gw, "opp": CODE.get(opp, opp[:3]), "opp_full": opp,
                "venue": fx["venue"], "played": rec.get("games", 0),
                "goals": g, "assists": a, "gi": g + a,
                "xg": rec.get("xg", 0),
            })
        if fixes:
            rows.append({"name": p["name"], "team": p.get("team"),
                         "position": p.get("position"), "fixtures": fixes})
    order = {"FWD": 0, "MID": 1, "DEF": 2, "GK": 3}
    rows.sort(key=lambda r: order.get(r["position"], 4))
    return {"gws": [f"GW{g}" for g in gws], "rows": rows}


# ==========================================================================
# BACKTESTING — measure the xPts model against real outcomes using GW history.
# Reconstructs each player's state as-of-before a target GW from the logged
# per-GW history, predicts that GW, and scores vs the actual points scored.
# ==========================================================================
def _player_state_before(element, rows_sorted_asc, upto_gw, rankings, fixtures):
    """Build an approximate player dict (season-to-date up to upto_gw-1) from
    history rows (ascending by gw). Returns (player_dict, hist_newest_first)."""
    prior = [r for r in rows_sorted_asc if r.get("gw", 0) < upto_gw]
    if not prior:
        return None, None
    mins = sum(float(r.get("minutes", 0) or 0) for r in prior)
    if mins <= 0:
        return None, None
    starts = sum(1 for r in prior if float(r.get("minutes", 0) or 0) >= 60)
    pts = sum(float(r.get("total_points", 0) or 0) for r in prior)
    games = len(prior)
    last = prior[-1]
    player = {
        "id": element, "name": last.get("name"), "team": last.get("team"),
        "position": last.get("position", "MID"),
        "minutes": mins, "starts": starts, "ninetys": round(mins / 90.0, 1),
        "avg_min": round(mins / max(games, 1), 0),
        "ppg": round(pts / max(games, 1), 2),
        "form": round(sum(float(r.get("total_points", 0) or 0) for r in prior[-4:]) / min(4, games), 2),
        "status": "a",
        "xg": sum(float(r.get("xg", 0) or 0) for r in prior),
        "xa": sum(float(r.get("xa", 0) or 0) for r in prior),
        "xgi": sum(float(r.get("xgi", 0) or 0) for r in prior),
        "defcon": sum(float(r.get("defcon", 0) or 0) for r in prior),
        "defcon_pg": round(sum(float(r.get("defcon", 0) or 0) for r in prior) / (mins / 90.0), 2) if mins else 0,
        "bps": sum(float(r.get("bps", 0) or 0) for r in prior),
        "bonus": sum(int(r.get("bonus", 0) or 0) for r in prior),
        "saves": sum(float(r.get("saves", 0) or 0) for r in prior),
        "yellow_cards": sum(int(r.get("yellow_cards", 0) or 0) for r in prior),
        "red_cards": sum(int(r.get("red_cards", 0) or 0) for r in prior),
    }
    hist_newest_first = list(reversed(prior))
    return player, hist_newest_first


def backtest_gameweek(history_rows, rankings, fixtures, target_gw,
                      min_minutes=60):
    """Backtest the xPts model for one past gameweek.

    history_rows: all gw_history dicts. rankings/fixtures: current snapshot
    (used for opponent strength — an approximation, since we don't store
    point-in-time rankings). target_gw: the GW to predict & score.

    Returns a metrics dict: n, mae, rmse, within_1, within_2, correlation,
    top10_hit (overlap of predicted top-10 vs actual top-10), and a sample of
    the biggest misses.
    """
    # index history by element, ascending by gw
    by_el = {}
    for r in history_rows:
        by_el.setdefault(r.get("element"), []).append(r)
    for el in by_el:
        by_el[el].sort(key=lambda r: r.get("gw", 0))

    # actual points scored in target_gw, per element
    actual = {}
    for r in history_rows:
        if r.get("gw") == target_gw:
            actual[r.get("element")] = float(r.get("total_points", 0) or 0)
    if not actual:
        return {"ok": False, "error": f"No actual data for GW{target_gw}."}

    preds, acts, names = [], [], []
    for el, act in actual.items():
        rows = by_el.get(el, [])
        player, hist = _player_state_before(el, rows, target_gw, rankings, fixtures)
        if not player:
            continue
        # need a fixture for this player's team in target_gw
        res = expected_points([player], rankings, fixtures, target_gw,
                              history_by_element={el: hist})
        if not res:
            continue
        preds.append(res[0]["xpts"]); acts.append(act); names.append(player.get("name"))

    n = len(preds)
    if n < 5:
        return {"ok": False, "error": f"Too few comparable players for GW{target_gw} (got {n})."}

    errs = [p - a for p, a in zip(preds, acts)]
    abs_errs = [abs(e) for e in errs]
    mae = sum(abs_errs) / n
    rmse = (sum(e * e for e in errs) / n) ** 0.5
    within1 = 100 * sum(1 for e in abs_errs if e <= 1) / n
    within2 = 100 * sum(1 for e in abs_errs if e <= 2) / n
    # correlation
    mp, ma = sum(preds) / n, sum(acts) / n
    cov = sum((p - mp) * (a - ma) for p, a in zip(preds, acts))
    sp = (sum((p - mp) ** 2 for p in preds)) ** 0.5
    sa = (sum((a - ma) ** 2 for a in acts)) ** 0.5
    corr = (cov / (sp * sa)) if sp and sa else 0.0
    # top-10 overlap
    order_pred = sorted(range(n), key=lambda i: -preds[i])[:10]
    order_act = sorted(range(n), key=lambda i: -acts[i])[:10]
    top10 = len(set(order_pred) & set(order_act))
    # biggest misses
    idx_sorted = sorted(range(n), key=lambda i: -abs_errs[i])[:5]
    misses = [{"name": names[i], "pred": round(preds[i], 1),
               "actual": round(acts[i], 1), "err": round(errs[i], 1)} for i in idx_sorted]

    # Captaincy (Fix #13): the model's #1 pick vs reality.
    #   hit      = the pick "returned" (>= 6 actual pts, i.e. 12+ as captain)
    #   in_top10 = the pick finished in the actual top-10 scorers
    #   regret   = best actual score in the pool minus the pick's score
    cap_i = order_pred[0]
    cap_actual = acts[cap_i]
    best_actual = max(acts)
    captain = {"name": names[cap_i], "pred": round(preds[cap_i], 1),
               "actual": round(cap_actual, 1), "hit": cap_actual >= 6,
               "in_top10": cap_i in set(order_act),
               "best_name": names[order_act[0]], "best_actual": round(best_actual, 1),
               "regret": round(best_actual - cap_actual, 1)}
    # Baseline: how would "just pick by form" have done? (proves model adds value)
    return {
        "ok": True, "gw": target_gw, "n": n, "captain": captain,
        "top10_pct": top10 * 10,
        "mae": round(mae, 2), "rmse": round(rmse, 2),
        "within_1_pct": round(within1), "within_2_pct": round(within2),
        "correlation": round(corr, 3), "top10_hit": top10,
        "biggest_misses": misses,
    }


def backtest_all(history_rows, rankings, fixtures):
    """Run the backtest across every gameweek that has both prior history and
    actuals. Returns per-GW metrics + an aggregate."""
    gws = sorted({r.get("gw") for r in history_rows if r.get("gw")})
    per_gw = []
    for g in gws:
        if g <= min(gws):  # need at least one prior GW
            continue
        m = backtest_gameweek(history_rows, rankings, fixtures, g)
        if m.get("ok"):
            per_gw.append(m)
    if not per_gw:
        return {"ok": False, "error": "Not enough history to backtest yet (need 2+ gameweeks)."}
    tot_n = sum(m["n"] for m in per_gw)
    agg = {
        "ok": True,
        "gws_tested": [m["gw"] for m in per_gw],
        "n": tot_n,
        "mae": round(sum(m["mae"] * m["n"] for m in per_gw) / tot_n, 2),
        "rmse": round(sum(m["rmse"] * m["n"] for m in per_gw) / tot_n, 2),
        "within_1_pct": round(sum(m["within_1_pct"] * m["n"] for m in per_gw) / tot_n),
        "within_2_pct": round(sum(m["within_2_pct"] * m["n"] for m in per_gw) / tot_n),
        "correlation": round(sum(m["correlation"] * m["n"] for m in per_gw) / tot_n, 3),
        "per_gw": per_gw,
    }
    caps = [m["captain"] for m in per_gw if m.get("captain")]
    if caps:
        agg["captain_hit_pct"] = round(100 * sum(1 for c in caps if c["hit"]) / len(caps))
        agg["captain_top10_pct"] = round(100 * sum(1 for c in caps if c["in_top10"]) / len(caps))
        agg["captain_avg_pts"] = round(sum(c["actual"] for c in caps) / len(caps), 1)
        agg["captain_avg_regret"] = round(sum(c["regret"] for c in caps) / len(caps), 1)
    agg["top10_pct"] = round(sum(m.get("top10_hit", 0) for m in per_gw) * 10 / len(per_gw))
    return agg


# ==========================================================================
# PHASE B — horizon & decision layer: xPPG, multi-GW projections, Team Rating,
# transfer value. Turns the xPts engine into a decision tool.
# ==========================================================================
# Configurable weights for the weighted-GW Team Rating "Expected Points" axis.
TEAM_RATING_GW_WEIGHTS = [0.40, 0.25, 0.175, 0.10, 0.075]


def player_horizon(players, rankings, fixtures, gw_from, gw_to):
    """Per-player projection over a horizon. Returns list of
    {name, team, position, price, total_xpts, xppg, per_gw, n_gw} sorted by total."""
    rng = expected_points_range(players, rankings, fixtures, gw_from, gw_to)
    n_gw = gw_to - gw_from + 1
    out = []
    for r in rng:
        total = r.get("total_xpts", 0)
        played = len(r.get("per_gw", {})) or n_gw
        out.append({
            "name": r.get("name"), "team": r.get("team"),
            "position": r.get("position"), "price": r.get("price", 0),
            "selected_by": r.get("selected_by", 0),
            "total_xpts": total, "xppg": round(total / max(played, 1), 1),
            "per_gw": r.get("per_gw", {}), "n_gw": n_gw,
        })
    return out


def _norm100(v, lo, hi):
    if hi <= lo:
        return 50.0
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100))


def team_rating(squad, players, rankings, fixtures, gw,
                weights=None, horizon=5, central_total=None):
    """FPL IQ Team Rating (0-100) across six axes, per the model spec:
      Rating = 45% ExpectedPoints + 15% Fixtures + 10% Minutes
             + 10% Value + 10% Structure + 10% Captaincy
    ExpectedPoints uses a WEIGHTED multi-GW view (nearer GWs weigh more).
    Returns {overall, axes:{expected_points, fixtures, minutes, value,
             structure, captaincy}, projected_gw}.
    """
    weights = weights or TEAM_RATING_GW_WEIGHTS
    by_name, by_id = {}, {}
    for pl in players:
        by_name.setdefault(pl.get("name", "").lower(), pl)
        if pl.get("id") is not None:
            by_id[pl["id"]] = pl

    # match squad members to live player records -- FPL id first (#2)
    members = []
    for m in squad:
        rec = by_id.get(m.get("element") or m.get("id")) or by_name.get(m.get("name", "").lower())
        if rec:
            members.append({**rec, **m})  # squad flags override
    if not members:
        return {"ok": False, "error": "No squad players matched live data."}

    # --- Expected Points axis: weighted multi-GW xPts for the starting XI ---
    gw_scores = []
    for i in range(horizon):
        g = gw + i
        xp = {(r["name"], r["team"]): r["xpts"]
              for r in expected_points(members, rankings, fixtures, g)}
        vals = sorted((xp.get((m.get("name"), m.get("team")), 0) for m in members), reverse=True)
        gw_scores.append(sum(vals[:11]))  # starting XI
    # Fix #3: this GW's score MUST equal the central squad projection (actual
    # XI + captain) so the rating, Home and My Team all quote one number.
    if central_total is not None and gw_scores:
        gw_scores[0] = float(central_total)
    wsum = sum(weights[:len(gw_scores)]) or 1
    weighted_xpts = sum(s * w for s, w in zip(gw_scores, weights)) / wsum
    projected_gw = round(gw_scores[0], 1) if gw_scores else 0.0
    # a strong XI gameweek is ~55-75 pts; scale to 0-100
    s_expected = _norm100(weighted_xpts, 35, 75)

    # --- Fixtures axis: squad's average fixture ease over the horizon ---
    eases = []
    for m in members:
        team = m.get("team"); pos = m.get("position", "MID")
        cat = "gs" if pos in ("MID", "FWD") else "cs"
        for i in range(horizon):
            fx = next((f for f in fixtures.get(team, []) if f["gw"] == gw + i), None)
            if fx and fx["opponent"] in rankings:
                d = _difficulty(cat, rankings[fx["opponent"]], fx["venue"])
                eases.append(2 - d)  # 0 hard..2 easy
    s_fixtures = _norm100(sum(eases) / len(eases), 0.4, 1.6) if eases else 50.0

    # --- Minutes axis: squad minutes security ---
    secs = [min(1.0, (m.get("avg_min", 0) or 0) / 85.0) for m in members]
    s_minutes = _norm100(sum(secs) / len(secs), 0.45, 0.95) if secs else 50.0

    # --- Value axis: points-per-million of the squad ---
    ppm = [float(m.get("ppm", 0) or 0) for m in members if m.get("ppm")]
    s_value = _norm100(sum(ppm) / len(ppm), 3.0, 8.0) if ppm else 50.0

    # --- Structure axis: do they have a valid, balanced XI? ---
    pos_counts = {"GK": 0, "DEF": 0, "MID": 0, "FWD": 0}
    for m in members:
        pos_counts[m.get("position", "MID")] = pos_counts.get(m.get("position", "MID"), 0) + 1
    have_formation = (pos_counts["GK"] >= 1 and pos_counts["DEF"] >= 3
                      and pos_counts["MID"] >= 2 and pos_counts["FWD"] >= 1)
    premium = sum(1 for m in members if (m.get("price", 0) or 0) >= 9.5)
    s_structure = (70 if have_formation else 40) + min(30, premium * 10)
    s_structure = min(100, s_structure)

    # --- Captaincy axis: strength of best captain option this GW ---
    caps = captaincy_board(members, rankings, fixtures, gw, limit=1)
    cap_xp = caps[0]["xpts"] if caps else 0
    s_captaincy = _norm100(cap_xp, 3.0, 8.0)

    axes = {
        "expected_points": round(s_expected), "fixtures": round(s_fixtures),
        "minutes": round(s_minutes), "value": round(s_value),
        "structure": round(s_structure), "captaincy": round(s_captaincy),
    }
    overall = round(0.45 * s_expected + 0.15 * s_fixtures + 0.10 * s_minutes
                    + 0.10 * s_value + 0.10 * s_structure + 0.10 * s_captaincy)

    # Plain-English strength / weakness summary from the axes.
    _labels = {"expected_points": "scoring potential", "fixtures": "fixture run",
               "minutes": "minutes security", "value": "squad value",
               "structure": "squad structure", "captaincy": "captaincy options"}
    best_k = max(axes, key=lambda k: axes[k])
    worst_k = min(axes, key=lambda k: axes[k])
    tier = ("elite" if overall >= 80 else "strong" if overall >= 65
            else "solid" if overall >= 50 else "work needed")
    summary = (f"{tier.capitalize()} squad. Strongest: {_labels[best_k]} "
               f"({axes[best_k]}). Weakest: {_labels[worst_k]} ({axes[worst_k]}) "
               f"\u2014 the clearest area to improve.")

    # Benchmark framing: compare the squad's projected GW score to a model
    # benchmark (a balanced average FPL XI ~ this figure). Makes "53/100"
    # meaningful: "below-average GW team, projects X vs benchmark Y".
    GW_BENCHMARK = 52.0  # typical balanced XI projected points (tunable)
    diff_vs_bench = round(projected_gw - GW_BENCHMARK, 1)
    if projected_gw >= GW_BENCHMARK + 8:
        bench_label = "Elite — well above an average GW team"
    elif projected_gw >= GW_BENCHMARK + 2:
        bench_label = "Above-average GW team"
    elif projected_gw >= GW_BENCHMARK - 2:
        bench_label = "About average for this gameweek"
    elif projected_gw >= GW_BENCHMARK - 8:
        bench_label = "Below-average GW team"
    else:
        bench_label = "Well below an average GW team"
    benchmark = {"value": GW_BENCHMARK, "diff": diff_vs_bench, "label": bench_label}

    headline = f"{overall}/100 \u2014 {bench_label} (GW{gw})"
    return {"ok": True, "overall": overall, "axes": axes, "headline": headline,
            "gw": gw, "weighted_xpts": round(weighted_xpts, 1),
            "projected_gw": projected_gw, "horizon": horizon,
            "summary": summary, "best": best_k, "worst": worst_k,
            "benchmark": benchmark}


def transfer_value(squad, players, rankings, fixtures, gw,
                   out_name, in_name, horizon=5, transfer_cost=0):
    """Does swapping out_name -> in_name improve MY team's xPts over the horizon?
    Returns {ok, out, in, out_xpts, in_xpts, gain, net_gain, verdict}."""
    def _horizon_xpts(name):
        rng = expected_points_range(players, rankings, fixtures, gw, gw + horizon - 1)
        for r in rng:
            if r.get("name", "").lower() == (name or "").lower():
                return round(r.get("total_xpts", 0), 1)
        return None
    out_xp = _horizon_xpts(out_name)
    in_xp = _horizon_xpts(in_name)
    if out_xp is None or in_xp is None:
        return {"ok": False, "error": "Could not match one or both players."}
    gain = round(in_xp - out_xp, 1)
    net = round(gain - (transfer_cost or 0), 1)
    verdict = ("Do it" if net >= 2 else "Marginal" if net >= 0 else "Hold")
    return {"ok": True, "out": out_name, "in": in_name,
            "out_xpts": out_xp, "in_xpts": in_xp, "gain": gain,
            "net_gain": net, "transfer_cost": transfer_cost, "verdict": verdict,
            "horizon": horizon}
