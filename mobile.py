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
        "outlook": [],
    }

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
