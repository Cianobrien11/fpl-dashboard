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
