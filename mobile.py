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


def fpl_iq_score(player: dict, xpts: float = 0.0) -> dict:
    """Return {overall, form, fixtures, xpts, value, minutes, ownership}
    each on a 0-10 scale. Transparent — weights live right here."""
    form = float(player.get("form", 0) or 0)                 # ~0-10 already
    ppm = float(player.get("ppm", 0) or 0)                    # points per million
    mins = float(player.get("minutes", 0) or 0)
    avg_min = float(player.get("avg_min", 0) or 0)
    own = float(player.get("selected_by", 0) or 0)

    s_form = _norm(form, 0, 8) * 10
    s_fix = _norm(xpts, 0, 8) * 10            # fixture-adjusted projection
    s_xpts = _norm(xpts, 0, 9) * 10
    s_value = _norm(ppm, 0, 10) * 10
    s_mins = _norm(avg_min if avg_min else mins / 3.0, 0, 90) * 10
    s_own = (1 - _norm(own, 0, 50)) * 10      # lower ownership = higher differential appeal

    overall = round(
        0.28 * s_xpts + 0.22 * s_fix + 0.20 * s_form +
        0.15 * s_value + 0.15 * s_mins, 1)
    return {
        "overall": overall,
        "form": round(s_form, 1), "fixtures": round(s_fix, 1),
        "xpts": round(s_xpts, 1), "value": round(s_value, 1),
        "minutes": round(s_mins, 1), "ownership": round(s_own, 1),
    }


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
        iq = fpl_iq_score(pl, xp)
        out_rows.append({
            "name": pl.get("name"), "team": pl.get("team"),
            "position": pl.get("position"), "price": pl.get("price", 0),
            "form": pl.get("form", 0), "ownership": pl.get("selected_by", 0),
            "ppm": pl.get("ppm", 0), "points": pl.get("points", 0),
            "xpts": round(xp, 1), "iq": iq["overall"], "iq_parts": iq,
            "fixtures": _next_fixtures(pl.get("team"), fixtures, gw, 5),
        })
    return {
        "rows": out_rows, "has_live": has_live, "gw": gw,
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
            {"key": "captaincy", "title": "Captaincy", "desc": "Best armband picks, ranked by projected points.", "icon": "C"},
            {"key": "differentials", "title": "Differentials", "desc": "Low-owned, high-ceiling picks for your mini-leagues.", "icon": "D"},
            {"key": "radar", "title": "Team Radar", "desc": "Attack vs defence profile for every team.", "icon": "R"},
            {"key": "h2h", "title": "Head to Head", "desc": "Your players' record vs their next opponent.", "icon": "H"},
            {"key": "setpieces", "title": "Set Pieces", "desc": "Penalty, free-kick and corner takers.", "icon": "S"},
            {"key": "accuracy", "title": "Accuracy", "desc": "How our predictions have scored vs real results.", "icon": "A"},
        ],
    }


# ---------------------------------------------------------------------------
# ANALYTICS SUB-PAGES — one assembler per section, selected by key.
# ---------------------------------------------------------------------------
ANALYTICS_SECTIONS = {
    "captaincy": "Captaincy", "differentials": "Differentials",
    "radar": "Team Radar", "h2h": "Head to Head",
    "setpieces": "Set Pieces", "accuracy": "Accuracy",
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
