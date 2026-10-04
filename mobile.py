"""
mobile.py — assembles payloads for the mobile-first app (FPL IQ).

The mobile app is a SEPARATE frontend from the desktop dashboard. It reuses the
same analytics engine (analytics.py) but presents a focused, 5-tab experience:
Home / Players / Planner / My Team / Analytics.

These are pure assembler functions: the app layer passes in the current snapshot
(team_stats, fixtures, squad, players, …) and gets back ready-to-render dicts.
Everything degrades gracefully when live player data isn't present yet (the seed
snapshot has team_stats + fixtures + squad but no players until first refresh).
"""
from __future__ import annotations

from typing import Any

import analytics


def _ease_band(ease: float) -> str:
    """Map a 0-10 ease score to a colour band for the UI.
    Higher ease = easier fixtures = green."""
    if ease >= 6.0:
        return "easy"
    if ease >= 3.5:
        return "mid"
    return "hard"


def home_payload(snap: dict, players: list[dict] | None = None) -> dict:
    """Build the Home-tab payload from the current snapshot.

    Returns a dict with keys:
      gw, projected, captain, vice, transfer, top_players, outlook, has_live
    Any section that can't be computed yet is returned as None/empty so the
    template can hide it.
    """
    players = players or []
    has_live = len(players) > 0

    team_stats = snap.get("team_stats", {})
    fixtures = snap.get("fixtures", {})
    squad = snap.get("squad", []) or []
    gw = snap.get("next_gw") or snap.get("current_gw") or 1

    rankings = analytics.compute_rankings(team_stats, snap.get("team_strength"))

    out: dict[str, Any] = {
        "gw": gw,
        "has_live": has_live,
        "projected": None,
        "captain": None,
        "vice": None,
        "transfer": None,
        "top_players": [],
        "opportunities": [],   # best xPts players NOT already in the squad
        "transfer_gain": None, # +xPts the recommended transfer adds (if known)
        "outlook": [],
    }

    squad_names = {m.get("name", "").lower() for m in squad}

    # --- Captain & vice (works from seed; appeal-ranked squad members) ---
    try:
        caps = analytics.captain_picks(rankings, fixtures, squad, gw)
        if caps:
            out["captain"] = caps[0]
            if len(caps) > 1:
                out["vice"] = caps[1]
    except Exception:
        pass

    # --- Projected GW score + top players (needs live player xPts) ---
    if has_live:
        try:
            xp = analytics.expected_points(players, rankings, fixtures, gw)
            # xp: list of player dicts with an 'xpts' field (sorted desc)
            xp_sorted = sorted(xp, key=lambda p: p.get("xpts", 0), reverse=True)
            out["top_players"] = xp_sorted[:6]

            # Attach real xPts to captain / vice if we recommended them
            xp_name = {p.get("name", "").lower(): p.get("xpts", 0) for p in xp}
            if out["captain"]:
                out["captain"]["xpts"] = round(xp_name.get(out["captain"]["name"].lower(), 0), 1)
            if out["vice"]:
                out["vice"]["xpts"] = round(xp_name.get(out["vice"]["name"].lower(), 0), 1)

            # Best opportunities = top xPts players NOT already owned
            opps = [p for p in xp_sorted if p.get("name", "").lower() not in squad_names]
            out["opportunities"] = opps[:5]

            # Projected score = sum of xPts for the user's starting XI if we can
            # match squad names to the xp list; else sum top-11 of their squad.
            by_name = {p.get("name", "").lower(): p for p in xp}
            squad_xp = []
            for member in squad:
                nm = member.get("name", "").lower()
                hit = by_name.get(nm)
                if hit:
                    squad_xp.append(hit.get("xpts", 0))
            if squad_xp:
                squad_xp.sort(reverse=True)
                out["projected"] = round(sum(squad_xp[:11]), 1)
        except Exception:
            pass

    # --- Recommended transfer (needs live players for a real suggestion) ---
    if has_live:
        try:
            plan = analytics.build_transfer_plan(rankings, fixtures, squad, gw, gw + 5)
            # plan may be a dict with 'moves' or a list; handle both
            moves = None
            if isinstance(plan, dict):
                moves = plan.get("moves") or plan.get("suggestions")
            elif isinstance(plan, list):
                moves = plan
            if moves:
                out["transfer"] = moves[0]
                mv = moves[0]
                gain = None
                if isinstance(mv, dict):
                    gain = mv.get("gain") or mv.get("xpts_gain") or mv.get("delta")
                    if gain is None and isinstance(mv.get("move"), dict):
                        gain = mv["move"].get("gain")
                out["transfer_gain"] = round(gain, 1) if isinstance(gain, (int, float)) else None
        except Exception:
            pass

    # --- Gameweek outlook: next 6 GWs average fixture ease for the squad ---
    try:
        ticker = analytics.my_team_ticker(squad, rankings, fixtures, gw, gw + 5)
        gws = ticker.get("gws", [])
        rows = ticker.get("rows", [])
        # average difficulty per GW column (lower d = easier). Convert to a 0-10
        # "ease" bar height where easy fixtures score high.
        if gws and rows:
            n_cols = len(gws)
            sums = [0.0] * n_cols
            counts = [0] * n_cols
            for r in rows:
                for i, cell in enumerate(r.get("cells", [])[:n_cols]):
                    sums[i] += cell.get("d", 0)
                    counts[i] += 1
            outlook = []
            for i, label in enumerate(gws):
                avg_d = (sums[i] / counts[i]) if counts[i] else 0
                # d is 0 (hard) .. 2 (easy) in the ticker; map to 0-10 ease
                ease = round((avg_d / 2.0) * 10, 1)
                outlook.append({"gw": label, "ease": ease, "band": _ease_band(ease)})
            out["outlook"] = outlook
    except Exception:
        pass

    return out


def team_payload(snap: dict, imported: dict | None = None,
                 players: list[dict] | None = None) -> dict:
    """Build the My Team tab payload.

    If `imported` (the result of scraper.import_fpl_team) is present and ok,
    use that rich squad with captain/bench flags. Otherwise fall back to the
    stored squad from the snapshot (name/team/position only).

    Returns:
      {
        ok, error, imported (bool),
        manager, team_name, overall_rank, bank, team_value, gw,
        gk, defs, mids, fwds,    # starting XI split by position (for the pitch)
        bench,                   # list
        captain, vice,           # dicts or None
        projected                # float or None
      }
    """
    players = players or snap.get("players", []) or []
    fixtures = snap.get("fixtures", {})
    team_stats = snap.get("team_stats", {})
    gw = snap.get("next_gw") or snap.get("current_gw") or 1
    rankings = analytics.compute_rankings(team_stats, snap.get("team_strength"))

    out = {
        "ok": True, "error": None, "imported": False,
        "manager": None, "team_name": None, "overall_rank": None,
        "bank": None, "team_value": None, "gw": gw,
        "gk": [], "defs": [], "mids": [], "fwds": [], "bench": [],
        "captain": None, "vice": None, "projected": None,
    }

    if imported and imported.get("ok"):
        squad = imported.get("squad", [])
        out.update({
            "imported": True,
            "manager": imported.get("manager"),
            "team_name": imported.get("team_name"),
            "overall_rank": imported.get("overall_rank"),
            "bank": imported.get("bank"),
            "team_value": imported.get("team_value"),
            "gw": imported.get("gw", gw),
        })
        gw = out["gw"]
    elif imported and not imported.get("ok"):
        out["ok"] = False
        out["error"] = imported.get("error")
        return out
    else:
        squad = snap.get("squad", []) or []

    if not squad:
        return out

    # Project points for every squad member we can (needs live player xpts).
    xp_by_el, xp_by_name = {}, {}
    try:
        xp = analytics.expected_points(players, rankings, fixtures, gw)
        for p_ in xp:
            if p_.get("id") is not None:
                xp_by_el[p_["id"]] = p_.get("xpts", 0)
            xp_by_name[p_.get("name", "").lower()] = p_.get("xpts", 0)
    except Exception:
        pass

    def _xp(member):
        el = member.get("element") or member.get("id")
        if el in xp_by_el:
            return xp_by_el[el]
        return xp_by_name.get(member.get("name", "").lower(), 0)

    starters, bench = [], []
    projected = 0.0
    have_proj = bool(xp_by_el or xp_by_name)

    for m in squad:
        xpts = _xp(m)
        m = {**m, "xpts": round(xpts, 1)}
        is_bench = m.get("is_bench", False)
        if out["imported"]:
            if is_bench:
                bench.append(m)
            else:
                starters.append(m)
                mult = m.get("multiplier", 1) or 1
                projected += xpts * mult
        else:
            starters.append(m)
            projected += xpts
        if m.get("is_captain"):
            out["captain"] = m
        if m.get("is_vice"):
            out["vice"] = m

    # Split starters by position for the pitch view
    pos_order = {"GK": "gk", "DEF": "defs", "MID": "mids", "FWD": "fwds"}
    for m in starters:
        key = pos_order.get(m.get("position", ""), "mids")
        out[key].append(m)
    out["bench"] = bench

    # Fallback captain pick from analytics if the import had none
    if out["captain"] is None:
        try:
            caps = analytics.captain_picks(rankings, fixtures, squad, gw)
            if caps:
                out["captain"] = caps[0]
                if len(caps) > 1:
                    out["vice"] = caps[1]
        except Exception:
            pass

    out["projected"] = round(projected, 1) if have_proj else None
    return out


# ---------------------------------------------------------------------------
# FPL IQ SCORE — a transparent 0-10 composite (your plan's "AMR-style" score).
# Blends: form, fixtures (xPts as the fixture-adjusted proxy), value (ppm),
# minutes security and (inverse) ownership differential appeal.
# ---------------------------------------------------------------------------
def _norm(v, lo, hi):
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def fpl_iq_score(player: dict, xpts: float = 0.0, fix_score: float | None = None) -> dict:
    """Return {overall, form, fixtures, xpts, value, minutes, ownership}
    each on a 0-10 scale. Transparent — weights live right here.

    `fix_score` is a REAL 0-10 fixture-favourability score for THIS player's
    next opponent (computed in players_payload from the opponent's attack/
    defence ratings + venue, position-appropriate). When it's None we fall
    back to the xPts proxy, but the caller should always supply it.

    Weighting is POSITION-AWARE: a goalkeeper/defender's rating leans on the
    fixture (clean-sheet chance vs the opponent's attack), while a mid/forward
    leans more on xPts/form. This is why a keeper facing a top attack (e.g.
    Alisson vs Man City) is now correctly marked down on fixtures.
    """
    pos = player.get("position", "MID")
    form = float(player.get("form", 0) or 0)
    ppm = float(player.get("ppm", 0) or 0)
    mins = float(player.get("minutes", 0) or 0)
    avg_min = float(player.get("avg_min", 0) or 0)
    own = float(player.get("selected_by", 0) or 0)

    s_form = _norm(form, 0, 8) * 10
    s_fix = fix_score if fix_score is not None else _norm(xpts, 0, 8) * 10
    s_xpts = _norm(xpts, 0, 9) * 10
    s_value = _norm(ppm, 0, 10) * 10
    s_mins = _norm(avg_min if avg_min else mins / 3.0, 0, 90) * 10
    s_own = (1 - _norm(own, 0, 50)) * 10

    # Position-aware weights. GK/DEF are clean-sheet assets -> fixture matters
    # most; MID/FWD are returns assets -> xPts/form matter most.
    if pos in ("GK", "DEF"):
        w = {"xpts": 0.20, "fix": 0.38, "form": 0.14, "value": 0.12, "mins": 0.16}
    else:
        w = {"xpts": 0.30, "fix": 0.22, "form": 0.21, "value": 0.13, "mins": 0.14}

    overall = round(
        w["xpts"] * s_xpts + w["fix"] * s_fix + w["form"] * s_form +
        w["value"] * s_value + w["mins"] * s_mins, 1)
    return {
        "overall": overall,
        "form": round(s_form, 1), "fixtures": round(s_fix, 1),
        "xpts": round(s_xpts, 1), "value": round(s_value, 1),
        "minutes": round(s_mins, 1), "ownership": round(s_own, 1),
    }


def _clean_sheet_prob(opp, venue, market_cs=None):
    """Estimate clean-sheet probability (0-1) for a team facing `opp`.

    Model estimate is driven by the opponent's attacking threat (0-100 attack
    rating) + venue tilt. When a MARKET clean-sheet probability is supplied
    (derived from bookmaker odds), we BLEND it in — the market is a sharp,
    pre-computed consensus, so it anchors the model estimate (60% market /
    40% model when both exist).
    """
    att = opp.get("attack", 50)
    prob = 0.60 - (att / 100.0) * 0.54
    prob += 0.05 if venue == "H" else -0.05
    prob = max(0.04, min(0.65, prob))
    if market_cs is not None:
        try:
            m = float(market_cs)
            prob = 0.6 * m + 0.4 * prob
        except (TypeError, ValueError):
            pass
    return max(0.04, min(0.70, prob))


def _one_fixture_score(player, opp, venue, market=None):
    """0-10 favourability of a SINGLE fixture for this player's position.

    `market` (optional) is this team's odds record for the fixture:
    {cs_prob, team_goals_exp, win_prob, ...}. When present it anchors the
    model-derived score with the bookmaker signal.
    """
    pos = player.get("position", "MID")
    market_cs = market.get("cs_prob") if market else None
    if pos in ("GK", "DEF"):
        base = 100 - opp.get("attack", 50)
        base += 6 if venue == "H" else -6
        fav = max(0.0, min(100.0, base)) / 10.0
        cs = _clean_sheet_prob(opp, venue, market_cs) / 0.70 * 10.0
        return 0.5 * fav + 0.5 * cs
    else:
        base = 100 - opp.get("defence", 50)
        base += 6 if venue == "H" else -6
        model_s = max(0.0, min(100.0, base)) / 10.0
        if market and market.get("team_goals_exp") is not None:
            # More market-expected goals = better attacking fixture.
            # ~0.6 goals -> ~2/10, ~2.4 goals -> ~9/10.
            g = float(market["team_goals_exp"])
            market_s = max(0.0, min(10.0, (g - 0.3) / 2.2 * 10.0))
            return 0.5 * model_s + 0.5 * market_s
        return model_s


def _fixture_score(player, rankings, fixtures, gw, window=4, odds=None):
    """Real 0-10 fixture-favourability averaged over the next `window` games.

    Reflects a RUN of fixtures, not just the immediate one, weighting nearer
    gameweeks more heavily (the next GW matters most, later ones taper). For
    GK/DEF this now also folds in an explicit clean-sheet-probability term
    (opponent attack strength -> CS odds); for MID/FWD it uses opponent defence
    leakiness. Venue tilts each fixture. 10 = dream run, 0 = brutal run.
    """
    team = player.get("team")
    upcoming = sorted([f for f in fixtures.get(team, []) if f["gw"] >= gw],
                      key=lambda f: f["gw"])[:window]
    if not upcoming:
        return 5.0
    # Weights: nearest GW heaviest, tapering (e.g. 1.0, 0.7, 0.5, 0.35...)
    team = player.get("team")
    team_odds = (odds or {}).get(team)
    scores, weights = [], []
    for idx, fx in enumerate(upcoming):
        if fx["opponent"] not in rankings:
            continue
        # Market odds only cover the IMMEDIATE fixture (idx 0) and only if the
        # stored opponent matches this fixture's opponent.
        mk = None
        if idx == 0 and team_odds and team_odds.get("opp") == fx["opponent"]:
            mk = team_odds
        s = _one_fixture_score(player, rankings[fx["opponent"]], fx["venue"], mk)
        w = 0.7 ** idx
        scores.append(s * w)
        weights.append(w)
    if not weights:
        return 5.0
    return round(sum(scores) / sum(weights), 1)


def _next_fixtures(team, fixtures, gw, n=5):
    out = []
    for f in fixtures.get(team, []):
        if f["gw"] >= gw:
            out.append({"opp": f["opponent"], "venue": f["venue"],
                        "fdr": f.get("fdr", 3), "gw": f["gw"]})
        if len(out) >= n:
            break
    return out


def players_payload(snap, players=None, position="ALL", sort="points",
                    search="", limit=40):
    players = players or snap.get("players", []) or []
    fixtures = snap.get("fixtures", {})
    rankings = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
    gw = snap.get("next_gw") or snap.get("current_gw") or 1

    has_live = len(players) > 0
    odds = snap.get("odds") or {}
    xp_by_name = {}
    if has_live:
        try:
            for r in analytics.expected_points(players, rankings, fixtures, gw):
                xp_by_name[(r.get("name"), r.get("team"))] = r.get("xpts", 0)
        except Exception:
            pass

    rows = analytics.filter_sort_players(players, position=position, sort=sort,
                                         search=search, limit=limit)
    out_rows = []
    for pl in rows:
        xp = xp_by_name.get((pl.get("name"), pl.get("team")), 0)
        fix_s = _fixture_score(pl, rankings, fixtures, gw, odds=odds)
        iq = fpl_iq_score(pl, xp, fix_score=fix_s)
        # Market chip: show the bookmaker signal for this player's next fixture.
        # GK/DEF -> clean-sheet %, MID/FWD -> team expected goals. Only when the
        # stored odds opponent matches this player's actual next opponent.
        mk_chip = None
        tm_odds = odds.get(pl.get("team"))
        nxt = next((f for f in fixtures.get(pl.get("team"), []) if f["gw"] == gw), None)
        if tm_odds and nxt and tm_odds.get("opp") == nxt.get("opponent"):
            if pl.get("position") in ("GK", "DEF") and tm_odds.get("cs_prob") is not None:
                mk_chip = {"label": "CS", "value": f"{round(tm_odds['cs_prob']*100)}%"}
            elif tm_odds.get("team_goals_exp") is not None:
                mk_chip = {"label": "xG", "value": f"{tm_odds['team_goals_exp']:.1f}"}
        out_rows.append({
            "name": pl.get("name"), "team": pl.get("team"),
            "position": pl.get("position"), "price": pl.get("price", 0),
            "form": pl.get("form", 0), "ownership": pl.get("selected_by", 0),
            "ppm": pl.get("ppm", 0), "points": pl.get("points", 0),
            "xpts": round(xp, 1), "iq": iq["overall"], "iq_parts": iq,
            "fixtures": _next_fixtures(pl.get("team"), fixtures, gw, 5),
            "market": mk_chip,
        })
    return {
        "rows": out_rows, "has_live": has_live, "gw": gw,
        "has_odds": bool(odds),
        "position": position, "sort": sort, "search": search,
        "positions": ["ALL", "GK", "DEF", "MID", "FWD"],
        "sorts": [("points", "Points"), ("xpts", "xPts"), ("form", "Form"),
                  ("ppm", "Value"), ("selected_by", "Owned"), ("price", "Price")],
    }


def planner_payload(snap, players=None, gw_from=None, gw_to=None,
                    sim_a="", sim_b=""):
    players = players or snap.get("players", []) or []
    fixtures = snap.get("fixtures", {})
    rankings = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
    next_gw = snap.get("next_gw") or snap.get("current_gw") or 1
    gw_from = int(gw_from or next_gw)
    gw_to = int(gw_to or min(gw_from + 5, 38))
    has_live = len(players) > 0

    targets = []
    if has_live:
        try:
            rng = analytics.expected_points_range(players, rankings, fixtures, gw_from, gw_to)
            targets = rng[:15]
        except Exception:
            pass

    # Transfer simulator: compare two named players over the GW window
    sim = None
    if has_live and (sim_a or sim_b):
        def _agg(name):
            name = (name or "").strip().lower()
            if not name:
                return None
            try:
                rng = analytics.expected_points_range(players, rankings, fixtures, gw_from, gw_to)
            except Exception:
                return None
            for r in rng:
                if r.get("name", "").lower() == name:
                    return r
            return None
        a = _agg(sim_a); b = _agg(sim_b)
        if a or b:
            diff = round((b["total_xpts"] if b else 0) - (a["total_xpts"] if a else 0), 1)
            sim = {"a": a, "b": b, "diff": diff}

    gws = list(range(gw_from, gw_to + 1))
    return {
        "has_live": has_live, "gw_from": gw_from, "gw_to": gw_to,
        "next_gw": next_gw, "gws": gws, "targets": targets, "sim": sim,
        "sim_a": sim_a, "sim_b": sim_b,
    }


def analytics_payload(snap, players=None):
    """Landing hub for the Analytics tab — small previews + links."""
    players = players or snap.get("players", []) or []
    fixtures = snap.get("fixtures", {})
    rankings = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
    gw = snap.get("next_gw") or snap.get("current_gw") or 1
    has_live = len(players) > 0

    captaincy, diffs = [], []
    if has_live:
        try:
            captaincy = analytics.captaincy_board(players, rankings, fixtures, gw, limit=3)
        except Exception:
            pass
        try:
            diffs = analytics.differentials(players, rankings, fixtures, gw, max_own=10.0, limit=3)
        except Exception:
            pass

    return {
        "has_live": has_live, "gw": gw,
        "captaincy": captaincy, "differentials": diffs,
        "sections": [
            {"key": "captainconf", "title": "Captain Confidence", "desc": "Top armband picks: model xPts vs market goal odds.", "icon": "C"},
            {"key": "cleansheet", "title": "Clean Sheet Board", "desc": "Teams ranked by model + market clean-sheet odds.", "icon": "CS"},
            {"key": "movers", "title": "Market Movers", "desc": "Who the transfer market is backing or dropping.", "icon": "M"},
            {"key": "swings", "title": "Fixture Swings", "desc": "Whose fixtures turn easier or harder over 6 GWs.", "icon": "FS"},
            {"key": "form", "title": "Form & Momentum", "desc": "Players hot or cold vs their season baseline.", "icon": "F"},
            {"key": "value", "title": "Value Picks", "desc": "Best points-per-million by position.", "icon": "V"},
            {"key": "differentials", "title": "Differentials", "desc": "Low-owned, high-ceiling picks for your mini-leagues.", "icon": "D"},
            {"key": "setpieces", "title": "Set Pieces", "desc": "Penalty, free-kick and corner takers.", "icon": "S"},
        ],
    }


# ---------------------------------------------------------------------------
# ANALYTICS SUB-PAGES — one assembler per section, selected by key.
# ---------------------------------------------------------------------------
def _fixture_swings(rankings, fixtures, gw, window=6):
    """Teams whose upcoming fixtures swing easiest/hardest over `window` GWs.

    Scores each team's run from the perspective of ATTACKERS (opponent defence
    leakiness) and DEFENDERS (opponent attack weakness), averaged & weighted to
    nearer GWs. Returns {easiest:[...], hardest:[...]} with a 0-10 run score.
    """
    rows = []
    for team in rankings:
        upcoming = sorted([f for f in fixtures.get(team, []) if f["gw"] >= gw],
                          key=lambda f: f["gw"])[:window]
        if not upcoming:
            continue
        att_scores, def_scores, weights, chips = [], [], [], []
        for idx, fx in enumerate(upcoming):
            opp = rankings.get(fx["opponent"])
            if not opp:
                continue
            w = 0.85 ** idx
            # attacking ease = opponent defence leakiness
            a = (100 - opp.get("defence", 50)) + (6 if fx["venue"] == "H" else -6)
            # defensive ease = opponent attack weakness
            d = (100 - opp.get("attack", 50)) + (6 if fx["venue"] == "H" else -6)
            att_scores.append(max(0, min(100, a)) / 10.0 * w)
            def_scores.append(max(0, min(100, d)) / 10.0 * w)
            weights.append(w)
            chips.append({"opp": fx["opponent"], "venue": fx["venue"]})
        if not weights:
            continue
        tw = sum(weights)
        att = round(sum(att_scores) / tw, 1)
        dfn = round(sum(def_scores) / tw, 1)
        rows.append({"team": team, "attack_ease": att, "defence_ease": dfn,
                     "overall": round((att + dfn) / 2, 1), "chips": chips})
    easiest = sorted(rows, key=lambda r: -r["overall"])[:8]
    hardest = sorted(rows, key=lambda r: r["overall"])[:8]
    return {"easiest": easiest, "hardest": hardest}


def _clean_sheet_board(rankings, fixtures, odds, gw):
    """Rank teams by combined model+market clean-sheet probability for next GW."""
    rows = []
    for team in rankings:
        fx = next((f for f in fixtures.get(team, []) if f["gw"] == gw), None)
        if not fx or fx["opponent"] not in rankings:
            continue
        opp = rankings[fx["opponent"]]
        venue = fx["venue"]
        tm_odds = odds.get(team)
        market_cs = tm_odds.get("cs_prob") if (tm_odds and tm_odds.get("opp") == fx["opponent"]) else None
        prob = _clean_sheet_prob(opp, venue, market_cs)
        rows.append({"team": team, "opp": fx["opponent"], "venue": venue,
                     "cs_prob": round(prob, 3), "cs_pct": round(prob * 100),
                     "has_market": market_cs is not None})
    rows.sort(key=lambda r: -r["cs_prob"])
    return rows


ANALYTICS_SECTIONS = {
    "captaincy": "Captaincy", "differentials": "Differentials",
    "radar": "Team Radar", "setpieces": "Set Pieces",
    "movers": "Market Movers", "form": "Form & Momentum",
    "swings": "Fixture Swings", "value": "Value Picks",
    "cleansheet": "Clean Sheet Board", "captainconf": "Captain Confidence",
    # Hidden for now (still reachable by direct URL, removed from the hub):
    "h2h": "Head to Head", "accuracy": "Accuracy",
}


def analytics_sub_payload(section, snap, players=None, logs=None):
    """Build the payload for a single analytics sub-page.

    Returns {section, title, has_live, gw, kind, data, note}.
    `kind` tells the template which block to render.
    """
    players = players or snap.get("players", []) or []
    fixtures = snap.get("fixtures", {})
    rankings = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
    gw = snap.get("next_gw") or snap.get("current_gw") or 1
    has_live = len(players) > 0

    out = {"section": section, "title": ANALYTICS_SECTIONS.get(section, "Analytics"),
           "has_live": has_live, "gw": gw, "kind": section, "data": None, "note": None}

    try:
        if section == "captaincy":
            out["data"] = analytics.captaincy_board(players, rankings, fixtures, gw, limit=15) if has_live else []
        elif section == "differentials":
            out["data"] = analytics.differentials(players, rankings, fixtures, gw, max_own=10.0, limit=15) if has_live else []
        elif section == "radar":
            # Radar needs team stats only (works from seed)
            out["data"] = analytics.team_radar(rankings, snap.get("team_strength"))
            out["has_live"] = bool(snap.get("team_stats"))
        elif section == "h2h":
            h2h = snap.get("h2h") or {}
            out["data"] = analytics.h2h_vs_next_opponent(players, h2h, fixtures, rankings, gw) if (players and h2h) else []
            out["note"] = None if h2h else "Head-to-head history loads from the daily data pipeline (Understat). It will populate after the next refresh."
        elif section == "setpieces":
            out["data"] = analytics.set_piece_takers(players) if has_live else {}
        elif section == "accuracy":
            results = snap.get("results", {})
            out["data"] = analytics.score_predictions(logs or [], results)
            out["note"] = None if (logs and results) else "Accuracy builds up over time as we log each gameweek's predictions and compare them to real results."
        elif section == "movers":
            # Market movers: price risers/fallers from transfer momentum.
            out["data"] = analytics.price_predictions(players) if has_live else {"rising": [], "falling": []}
        elif section == "form":
            # Hot/cold players by recent form vs season ppg.
            rows = []
            for pl in players:
                form = float(pl.get("form", 0) or 0)
                ppg = float(pl.get("ppg", 0) or 0)
                if (pl.get("minutes", 0) or 0) < 180:
                    continue
                rows.append({"name": pl.get("name"), "team": pl.get("team"),
                             "position": pl.get("position"), "form": round(form, 1),
                             "ppg": round(ppg, 1), "delta": round(form - ppg, 1),
                             "selected_by": pl.get("selected_by", 0)})
            hot = sorted([r for r in rows if r["delta"] > 0], key=lambda r: -r["delta"])[:10]
            cold = sorted([r for r in rows if r["delta"] < 0], key=lambda r: r["delta"])[:10]
            out["data"] = {"hot": hot, "cold": cold}
            out["has_live"] = has_live
        elif section == "swings":
            # Teams whose fixtures turn sharply easier/harder over next 6 GWs.
            out["data"] = _fixture_swings(rankings, fixtures, gw, window=6)
            out["has_live"] = bool(snap.get("team_stats"))
        elif section == "value":
            out["data"] = analytics.value_finder(players, min_minutes=180) if has_live else {}
        elif section == "cleansheet":
            out["data"] = _clean_sheet_board(rankings, fixtures, snap.get("odds") or {}, gw)
            out["has_live"] = bool(snap.get("team_stats"))
        elif section == "captainconf":
            board = analytics.captaincy_board(players, rankings, fixtures, gw, limit=8) if has_live else []
            odds = snap.get("odds") or {}
            enriched = []
            for c in board:
                tm = odds.get(c.get("team"))
                goals_exp = tm.get("team_goals_exp") if tm else None
                enriched.append({**c, "market_goals": goals_exp,
                                 "own": c.get("selected_by", 0)})
            # differential captain = best xpts among <15% owned
            diff_cap = next((c for c in enriched if (c.get("own", 0) or 0) < 15), None)
            out["data"] = {"board": enriched, "diff_cap": diff_cap}
            out["has_live"] = has_live
        else:
            out["note"] = "Unknown section."
    except Exception as e:
        out["note"] = f"Could not load {section}: {e}"
        out["data"] = [] if section not in ("setpieces", "accuracy") else ({} if section == "setpieces" else {"per_gw": [], "overall": {}})
    return out


def planner_grid(snap, players=None, gw_from=None, gw_to=None):
    """Position x gameweek difficulty grid for the user's squad.

    Returns {gws, rows:[{pos, cells:[{band, d}]}], team_row:[{xpts}|None],
             has_live}. Each position cell averages that position's players'
    fixture difficulty for the GW (0 easy .. 2 hard). The team row sums the
    squad's per-GW xPts when live data is present.
    """
    players = players or snap.get("players", []) or []
    fixtures = snap.get("fixtures", {})
    rankings = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
    squad = snap.get("squad", []) or []
    next_gw = snap.get("next_gw") or snap.get("current_gw") or 1
    gw_from = int(gw_from or next_gw)
    gw_to = int(gw_to or min(gw_from + 5, 38))
    has_live = len(players) > 0

    ticker = analytics.my_team_ticker(squad, rankings, fixtures, gw_from, gw_to)
    gws = ticker.get("gws", [])
    trows = ticker.get("rows", [])
    n = len(gws)

    def band_from_d(d):
        # d: 0 easy, 1 mid, 2 hard  -> green/amber/red
        if d <= 0.66:
            return "easy"
        if d <= 1.33:
            return "mid"
        return "hard"

    rows = []
    for pos in ("GK", "DEF", "MID", "FWD"):
        pos_rows = [r for r in trows if r.get("position") == pos]
        cells = []
        for i in range(n):
            ds = [r["cells"][i]["d"] for r in pos_rows if i < len(r["cells"]) and r["cells"][i]["txt"] != "-"]
            if ds:
                avg = sum(ds) / len(ds)
                cells.append({"d": round(avg, 2), "band": band_from_d(avg)})
            else:
                cells.append({"d": None, "band": "mid"})
        rows.append({"pos": pos, "cells": cells})

    # Team projected row: sum squad xPts per GW (needs live players)
    team_row = [None] * n
    if has_live:
        by_name = {pl.get("name", "").lower(): pl for pl in players}
        for gi, gwlabel in enumerate(gws):
            gwnum = int(gwlabel.replace("GW", ""))
            try:
                xp = analytics.expected_points(players, rankings, fixtures, gwnum)
                xp_name = {r.get("name", "").lower(): r.get("xpts", 0) for r in xp}
                vals = []
                for m in squad:
                    v = xp_name.get(m.get("name", "").lower())
                    if v is not None:
                        vals.append(v)
                if vals:
                    vals.sort(reverse=True)
                    team_row[gi] = round(sum(vals[:11]), 1)
            except Exception:
                pass

    return {"gws": gws, "rows": rows, "team_row": team_row,
            "has_live": has_live, "gw_from": gw_from, "gw_to": gw_to,
            "next_gw": next_gw}


# ---------------------------------------------------------------------------
# ASK FPL IQ — natural-language questions answered from the model's structured
# data (NOT a generic LLM). Supports transfer comparisons, captaincy, and
# "best <position> under £Nm" style queries.
# ---------------------------------------------------------------------------
import re as _re


def _find_player(name_frag, pool):
    """Best-effort match a name fragment to a player in the xPts pool."""
    name_frag = (name_frag or "").strip().lower()
    if not name_frag:
        return None
    # exact, then startswith, then contains
    for r in pool:
        if r["name"].lower() == name_frag:
            return r
    for r in pool:
        if r["name"].lower().startswith(name_frag):
            return r
    for r in pool:
        if name_frag in r["name"].lower():
            return r
    return None


def ask_fpl_iq(question, snap, players=None, gw_from=None, gw_to=None):
    """Answer an FPL question from the model. Returns:
      {ok, kind, question, answer, detail, comparison, players, note}
    kind in {transfer, captain, best, unknown}. `comparison` carries the
    per-GW A/B breakdown for transfer questions.
    """
    players = players or snap.get("players", []) or []
    fixtures = snap.get("fixtures", {})
    rankings = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
    next_gw = snap.get("next_gw") or snap.get("current_gw") or 1
    gw_from = int(gw_from or next_gw)
    gw_to = int(gw_to or min(gw_from + 5, 38))
    q = (question or "").strip()
    out = {"ok": False, "kind": "unknown", "question": q, "answer": None,
           "detail": None, "comparison": None, "players": [], "note": None,
           "gw_from": gw_from, "gw_to": gw_to}

    if not q:
        out["note"] = "Ask me something like \u201cShould I transfer Mbeumo for Saka?\u201d"
        return out
    if not players:
        out["note"] = "I need live player data first \u2014 tap \u201cRefresh Data\u201d on the dashboard."
        return out

    rng = analytics.expected_points_range(players, rankings, fixtures, gw_from, gw_to)
    ql = q.lower()

    # --- Transfer comparison: "X for Y", "X vs Y", "X or Y", "X to Y" ---
    m = _re.search(r"([a-z\u00c0-\u017f .'-]+?)\s+(?:for|vs\.?|or|to|->|\u2192)\s+([a-z\u00c0-\u017f .'-]+)", ql)
    if m and ("transfer" in ql or "swap" in ql or " for " in ql or " vs" in ql or " or " in ql or "->" in ql or "\u2192" in ql):
        a_name = m.group(1).replace("should i transfer", "").replace("transfer", "").replace("swap", "").strip()
        b_name = m.group(2).strip().rstrip("?.! ")
        a = _find_player(a_name, rng)
        b = _find_player(b_name, rng)
        if a and b:
            per = []
            diff_total = 0.0
            for gw in range(gw_from, gw_to + 1):
                av = a["per_gw"].get(gw, 0.0)
                bv = b["per_gw"].get(gw, 0.0)
                per.append({"gw": gw, "a": round(av, 1), "b": round(bv, 1),
                            "diff": round(bv - av, 1)})
            diff_total = round(b["total_xpts"] - a["total_xpts"], 1)
            better = b if diff_total > 0 else a
            out.update({
                "ok": True, "kind": "transfer",
                "answer": (f"{'Yes' if diff_total > 0 else 'No'} \u2014 "
                           f"{better['name']} projects higher over GW{gw_from}\u2013{gw_to}."),
                "detail": (f"{b['name']} is projected {abs(diff_total)} pts "
                           f"{'more' if diff_total > 0 else 'fewer'} than {a['name']} "
                           f"across these {gw_to - gw_from + 1} gameweeks."),
                "comparison": {"a": a, "b": b, "per_gw": per, "diff_total": diff_total},
            })
            return out
        out["note"] = ("I couldn\u2019t match both players. Try full surnames, "
                       "e.g. \u201cMbeumo for Saka\u201d.")
        return out

    # --- Captain: "who should I captain" ---
    if "captain" in ql or "armband" in ql:
        board = analytics.captaincy_board(players, rankings, fixtures, gw_from, limit=5)
        if board:
            top = board[0]
            out.update({
                "ok": True, "kind": "captain",
                "answer": f"Captain {top['name']} ({top['team']}) in GW{gw_from}.",
                "detail": f"Highest projected points this gameweek at {round(top['xpts'],1)} xPts.",
                "players": board,
            })
            return out

    # --- Best <position> under £Nm ---
    pos = None
    for key, label in [("goalkeep", "GK"), ("keeper", "GK"), ("defend", "DEF"),
                       ("midfield", "MID"), ("forward", "FWD"), ("striker", "FWD"),
                       (" gk", "GK"), (" def", "DEF"), (" mid", "MID"), (" fwd", "FWD")]:
        if key in ql:
            pos = label
            break
    price_m = _re.search(r"(?:under|below|<|max)\s*\u00a3?\s*(\d+(?:\.\d+)?)", ql)
    maxp = float(price_m.group(1)) if price_m else None
    if pos or maxp or "best" in ql or "who should i" in ql:
        pool = rng
        if pos:
            pool = [r for r in pool if r.get("position") == pos]
        if maxp:
            pool = [r for r in pool if (r.get("price", 99) or 99) <= maxp]
        if (pos or maxp) and not pool:
            out["note"] = "No players match that filter in the current data \u2014 try a higher price or different position."
            return out
        pool = pool[:6]
        if pool:
            top = pool[0]
            desc = []
            if pos: desc.append({"GK": "goalkeeper", "DEF": "defender",
                                 "MID": "midfielder", "FWD": "forward"}[pos])
            else: desc.append("player")
            if maxp: desc.append(f"under \u00a3{maxp}m")
            out.update({
                "ok": True, "kind": "best",
                "answer": f"{top['name']} ({top['team']}) is the top {' '.join(desc)} "
                          f"for GW{gw_from}\u2013{gw_to}.",
                "detail": f"Projected {top['total_xpts']} pts over the window "
                          f"(\u00a3{top.get('price','?')}m).",
                "players": pool,
            })
            return out

    out["note"] = ("Try: \u201cShould I transfer X for Y?\u201d, \u201cWho should I "
                   "captain?\u201d, or \u201cbest midfielder under \u00a38m\u201d.")
    return out
