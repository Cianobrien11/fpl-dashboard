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



# ===========================================================================
# CENTRAL PROJECTION ENGINE — one source of truth for squad xPts.
# Every screen (Home, My Team, Team Rating, transfers) calls squad_projection()
# so projected totals + per-player xPts are IDENTICAL everywhere. Robust player
# matching (element id -> exact name -> accent-folded surname) means names like
# "Sangare"/"Sangaré" and "Joao Pedro"/"João Pedro" always resolve.
# ===========================================================================
import unicodedata as _ud


def _norm_name(s):
    """Lowercase + strip accents so 'Sangaré' == 'sangare', 'João' == 'joao'."""
    if not s:
        return ""
    s = _ud.normalize("NFKD", str(s))
    s = "".join(c for c in s if not _ud.combining(c))
    return s.strip().lower()


def _surname(s):
    """Last token of a normalised name (handles 'B.Fernandes' -> 'fernandes')."""
    n = _norm_name(s).replace(".", " ").replace("-", " ")
    parts = [t for t in n.split() if t]
    return parts[-1] if parts else ""


def _build_player_index(players):
    """Index live players for robust matching: by element id, exact norm-name,
    and surname+team (to disambiguate common surnames)."""
    by_el, by_name, by_surteam, by_sur = {}, {}, {}, {}
    for pl in players:
        el = pl.get("id") or pl.get("element")
        if el is not None:
            by_el[el] = pl
        nm = _norm_name(pl.get("name"))
        if nm:
            by_name.setdefault(nm, pl)
        sur = _surname(pl.get("name"))
        tm = _norm_name(pl.get("team"))
        if sur:
            by_surteam.setdefault((sur, tm), pl)
            by_sur.setdefault(sur, pl)  # last-resort, first match wins
    return by_el, by_name, by_surteam, by_sur


def _match_player(member, idx):
    """Resolve a squad member to a live player record. Returns (player|None)."""
    by_el, by_name, by_surteam, by_sur = idx
    el = member.get("element") or member.get("id")
    if el is not None and el in by_el:
        return by_el[el]
    nm = _norm_name(member.get("name"))
    if nm in by_name:
        return by_name[nm]
    sur, tm = _surname(member.get("name")), _norm_name(member.get("team"))
    if (sur, tm) in by_surteam:
        return by_surteam[(sur, tm)]
    if sur in by_sur:
        return by_sur[sur]
    return None


def squad_projection(snap, players, squad, gw):
    """THE central squad projection. Returns:
      {total, starting_total, players:[{name, team, position, xpts, matched,
       is_bench, is_captain, is_vice, multiplier, confidence}], gw}
    `total` = starting XI xPts with captain multiplier applied (if import flags
    present), else top-11 by xPts. Every screen uses this so numbers match.
    """
    players = players or []
    fixtures = snap.get("fixtures", {})
    rankings = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
    gw = int(gw or snap.get("next_gw") or snap.get("current_gw") or 1)

    # Run the ONE xPts model over all players for this GW -> lookup by (name,team)/id
    xp_rows = []
    try:
        xp_rows = analytics.expected_points(players, rankings, fixtures, gw)
    except Exception:
        xp_rows = []
    xp_by_el, xp_by_name, xp_by_surteam, xp_by_sur = {}, {}, {}, {}
    conf_by = {}
    for r in xp_rows:
        el = r.get("id")
        if el is not None:
            xp_by_el[el] = r
        nm = _norm_name(r.get("name"))
        xp_by_name.setdefault(nm, r)
        sur = _surname(r.get("name")); tm = _norm_name(r.get("team"))
        xp_by_surteam.setdefault((sur, tm), r)
        xp_by_sur.setdefault(sur, r)

    def _xp_for(member):
        el = member.get("element") or member.get("id")
        if el is not None and el in xp_by_el:
            return xp_by_el[el]
        nm = _norm_name(member.get("name"))
        if nm in xp_by_name:
            return xp_by_name[nm]
        sur, tm = _surname(member.get("name")), _norm_name(member.get("team"))
        if (sur, tm) in xp_by_surteam:
            return xp_by_surteam[(sur, tm)]
        if sur in xp_by_sur:
            return xp_by_sur[sur]
        return None

    has_import_flags = any(("is_bench" in m or "multiplier" in m) for m in squad)
    _pidx = _build_player_index(players)
    out_players = []
    for m in squad:
        r = _xp_for(m)
        xpts = round(r.get("xpts", 0), 1) if r else None
        # Explain a missing projection instead of silently showing nothing:
        #   unmatched  -> player not found in live FPL data (name/ID mismatch)
        #   no_fixture -> found, but the model has no fixture for their club this
        #                 GW (blank GW or club-name mapping mismatch)
        if r:
            reason = None
        else:
            live = _match_player(m, _pidx) if players else None
            if not live:
                reason = "unmatched"
            else:
                tm = live.get("team")
                has_fx = any(f.get("gw") == gw for f in fixtures.get(tm, []))
                reason = "no_fixture" if not has_fx else "no_ranking"
        # ID VALIDATION: when matched to a live player, trust the LIVE team &
        # position (the FPL API is the source of truth) so stale stored mappings
        # (e.g. a transferred player's old club) are auto-corrected.
        live_team = r.get("team") if r else None
        live_pos = r.get("position") if r else None
        out_players.append({
            "name": m.get("name"),
            "team": live_team or m.get("team"),
            "position": live_pos or m.get("position"),
            "team_corrected": bool(r and live_team and _norm_name(live_team) != _norm_name(m.get("team"))),
            "xpts": xpts, "matched": r is not None, "reason": reason,
            "confidence": r.get("confidence") if r else None,
            "is_bench": m.get("is_bench", False),
            "is_captain": m.get("is_captain", False),
            "is_vice": m.get("is_vice", False),
            "multiplier": m.get("multiplier", 1) or 1,
            "opp": r.get("opp") if r else None,
            "venue": r.get("venue") if r else None,
        })

    # Starting total: respect import flags (bench excluded, captain x mult);
    # else take the top 11 matched xPts.
    if has_import_flags:
        total = 0.0
        for pp in out_players:
            if pp["is_bench"] or pp["xpts"] is None:
                continue
            total += pp["xpts"] * (pp["multiplier"] or 1)
    else:
        xs = sorted((pp["xpts"] for pp in out_players if pp["xpts"] is not None), reverse=True)
        total = sum(xs[:11])

    return {"gw": gw, "total": round(total, 1),
            "players": out_players,
            "matched_n": sum(1 for pp in out_players if pp["matched"]),
            "squad_n": len(out_players)}

def _ease_band(ease: float) -> str:
    """Map a 0-10 ease score to a colour band for the UI.
    Higher ease = easier fixtures = green."""
    if ease >= 6.0:
        return "easy"
    if ease >= 3.5:
        return "mid"
    return "hard"


def recommend_transfer(snap, players, gw_from, gw_to):
    """Long-term (3-5 GW) transfer recommendation, starter-aware.

    Finds the squad player who is (a) a likely STARTER and (b) projects the
    FEWEST points over the window, and suggests the best available same-position
    replacement by total xPts GAIN across the window. Returns a dict:
      {out, out_team, in, in_team, position, gain, out_total, in_total, reason}
    or None if nothing sensible to suggest.
    """
    fixtures = snap.get("fixtures", {})
    rankings = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
    squad = snap.get("squad", []) or []
    if not players or not squad:
        return None
    try:
        rng = analytics.expected_points_range(players, rankings, fixtures, gw_from, gw_to)
    except Exception:
        return None
    by_name = {(r.get("name"), r.get("team")): r for r in rng}
    by_name_only = {}
    for r in rng:
        by_name_only.setdefault(r.get("name", "").lower(), r)

    # Build a player-record lookup for minutes/starts (starter detection).
    pdata = {}
    for pl in players:
        pdata[(pl.get("name"), pl.get("team"))] = pl
        pdata.setdefault(pl.get("name", "").lower(), pl)

    def _is_starter(member):
        rec = pdata.get((member.get("name"), member.get("team"))) or pdata.get(member.get("name", "").lower())
        if not rec:
            return True  # unknown -> don't exclude
        # a starter plays meaningful minutes: >=60 avg or >=2 starts
        return (rec.get("avg_min", 0) or 0) >= 55 or (rec.get("starts", 0) or 0) >= 2

    # Candidate "out": squad regulars ranked by LOWEST window xPts.
    outs = []
    for m in squad:
        if m.get("is_bench"):
            continue
        if not _is_starter(m):
            continue
        r = by_name.get((m.get("name"), m.get("team"))) or by_name_only.get(m.get("name", "").lower())
        if r:
            outs.append((r.get("total_xpts", 0), m, r))
    if not outs:
        return None
    outs.sort(key=lambda t: t[0])  # worst first
    squad_names = {m.get("name", "").lower() for m in squad}

    # Try the worst 5 outs; for each, find the best same-position upgrade that
    # passes VALIDATION. Rules (Fix #4):
    #   * both players must be projected for EVERY GW in the window (a partial
    #     total -- missed matches / blank / unmatched -- inflates the gain)
    #   * per-GW xPts must be plausible (0..15 per GW, avg <= 10)
    #   * gain > 25  -> rejected, try the next candidate (data artefact)
    #   * gain > 15  -> allowed but flagged "check" so the UI warns
    n_gw = gw_to - gw_from + 1
    GAIN_FLAG, GAIN_REJECT = 15.0, 25.0

    def _valid_proj(r):
        per = r.get("per_gw") or {}
        if len(per) < n_gw:
            return False
        vals = list(per.values())
        if any((v is None) or v < 0 or v > 15 for v in vals):
            return False
        return (sum(vals) / n_gw) <= 10.0

    for out_total, out_member, out_rng in outs[:5]:
        if not _valid_proj(out_rng):
            continue
        if out_total < 0.3 * n_gw:  # out-player barely projects -> artefact
            continue
        pos = out_member.get("position")
        budget = (out_member.get("price") or 99) + 2.0  # allow +£2m flexibility
        candidates = [r for r in rng
                      if r.get("position") == pos
                      and r.get("name", "").lower() not in squad_names
                      and 0 < (r.get("price") or 0) <= budget
                      and _valid_proj(r)]
        candidates.sort(key=lambda r: -r.get("total_xpts", 0))
        for best in candidates:
            gain = round(best.get("total_xpts", 0) - out_total, 1)
            if gain > GAIN_REJECT:
                continue  # implausible -> recalculate with next-best candidate
            if gain < 2.0:
                break  # sorted desc: nothing better left for this out-player
            flagged = gain > GAIN_FLAG
            return {
                "out": out_member.get("name"), "out_team": out_member.get("team"),
                "in": best.get("name"), "in_team": best.get("team"),
                "position": pos, "gain": gain, "flagged": flagged,
                "out_total": round(out_total, 1), "in_total": round(best.get("total_xpts", 0), 1),
                "out_avg": round(out_total / n_gw, 1),
                "in_avg": round(best.get("total_xpts", 0) / n_gw, 1),
                "n_gw": n_gw,
                "reason": (f"Over the next {n_gw} GWs, {best.get('name')} projects "
                           f"{round(best.get('total_xpts', 0), 1)} pts "
                           f"({round(best.get('total_xpts', 0) / n_gw, 1)}/GW) vs "
                           f"{out_member.get('name')}'s {round(out_total, 1)} "
                           f"({round(out_total / n_gw, 1)}/GW) — +{gain} pts."
                           + (" ⚠ Unusually large gain — double-check injuries/minutes." if flagged else "")),
            }
    return None


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
        "fixture_targets": [], # teams with the best upcoming fixture run
        "fixture_gws": [],     # the GW labels for the ticker columns
    }

    squad_names = {m.get("name", "").lower() for m in squad}
    odds = snap.get("odds") or {}

    # --- Captain & vice ---
    # When live player data exists, rank the user's OWN squad by real xPts
    # (the improved form-anchored + fixture + odds model) so captain/vice are
    # consistent with the rest of the app and both show xPts. Only fall back to
    # the appeal heuristic when there is no live player data (seed only).
    if has_live:
        try:
            # Rank the squad by the CENTRAL engine's per-player xPts (robust
            # matching, confidence included) so captain xPts matches My Team.
            _cap_proj = squad_projection(snap, players, squad, gw)
            ranked = [pp for pp in _cap_proj["players"]
                      if pp.get("xpts") is not None and not pp.get("is_bench")]
            ranked.sort(key=lambda pp: -(pp.get("xpts") or 0))
            if ranked:
                out["captain"] = dict(ranked[0])
                if len(ranked) > 1:
                    out["vice"] = dict(ranked[1])
        except Exception:
            pass
    if out["captain"] is None:
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
            # Captain/vice xPts already come from the central squad_projection
            # (same numbers My Team shows) — do NOT re-look them up by name here.
            if out["captain"]:
                out["captain"]["market"] = _market_chip_for(out["captain"], odds, fixtures, gw)
            if out["vice"]:
                out["vice"]["market"] = _market_chip_for(out["vice"], odds, fixtures, gw)

            # Best opportunities = top xPts players NOT already owned
            opps = [p for p in xp_sorted if p.get("name", "").lower() not in squad_names]
            for o in opps[:5]:
                o["market"] = _market_chip_for(o, odds, fixtures, gw)
            out["opportunities"] = opps[:5]

            # Projected score via the CENTRAL engine (same calc My Team uses),
            # so Home and My Team always agree. Robust matching included.
            _proj = squad_projection(snap, players, squad, gw)
            out["projected"] = _proj["total"] if _proj["players"] else None
        except Exception:
            pass

    # --- Recommended transfer: LONG-TERM (next 5 GWs), starter-aware ---
    # Not a one-week punt — ranks the squad's regulars by their 5-GW projected
    # points and suggests the best replacement by total xPts gain over the run.
    if has_live:
        try:
            rec = recommend_transfer(snap, players, gw, gw + 4)
            if rec:
                # Shape it like the template expects: {move:{out,in,reason}} + gain
                out["transfer"] = {"move": {"out": rec["out"], "in": rec["in"],
                                            "reason": rec["reason"]}}
                out["transfer_gain"] = rec["gain"]
                out["transfer_flagged"] = rec.get("flagged", False)
        except Exception:
            pass

    # --- Fixture Difficulty: teams with the best clean-sheet fixture run over
    # the next 5 GWs (mobile version of the desktop "Teams to Target"). ---
    try:
        gw_to = min(gw + 4, 38)
        tables = analytics.build_target_tables(rankings, fixtures, gw, gw_to)
        cs_rows = tables.get("cs", [])[:6]
        gws_lbl = [f"GW{g}" for g in range(gw, gw_to + 1)]
        targets = []
        for r in cs_rows:
            chips = [{"code": fxc["code"], "venue": fxc["venue"], "d": fxc["d"]}
                     for fxc in r.get("fixtures", [])]
            targets.append({"team": r["team"], "easy_n": r.get("easy_n", 0),
                            "avg_fdr": r.get("avg_fdr", 0), "chips": chips})
        out["fixture_targets"] = targets
        out["fixture_gws"] = gws_lbl
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


# Team kit colours (primary, secondary) for jersey-style cards on the pitch.
TEAM_KIT = {
    "Arsenal": ("#EF0107", "#ffffff"), "Aston Villa": ("#95BFE5", "#670E36"),
    "Bournemouth": ("#DA291C", "#000000"), "Brentford": ("#E30613", "#ffffff"),
    "Brighton": ("#0057B8", "#ffffff"), "Chelsea": ("#034694", "#ffffff"),
    "Coventry": ("#78D0F3", "#ffffff"), "Crystal Palace": ("#1B458F", "#C4122E"),
    "Everton": ("#003399", "#ffffff"), "Fulham": ("#ffffff", "#000000"),
    "Hull City": ("#F18A01", "#000000"), "Ipswich": ("#3A64A3", "#ffffff"),
    "Leeds": ("#FFCD00", "#1D428A"), "Liverpool": ("#C8102E", "#ffffff"),
    "Man City": ("#6CABDD", "#ffffff"), "Man United": ("#DA291C", "#ffffff"),
    "Newcastle": ("#241F20", "#ffffff"), "Nottm Forest": ("#DD0000", "#ffffff"),
    "Tottenham": ("#ffffff", "#132257"), "Sunderland": ("#EB172B", "#ffffff"),
}


def _player_fixture_chip(team, fixtures, gw, rankings, pos):
    """Fixture chip for a player's GW: {opp, venue, band}."""
    fx = next((f for f in fixtures.get(team, []) if f["gw"] == gw), None)
    if not fx or fx["opponent"] not in rankings:
        return None
    cat = "gs" if pos in ("MID", "FWD") else "cs"
    d = analytics._difficulty(cat, rankings[fx["opponent"]], fx["venue"])
    band = "easy" if d == 0 else ("mid" if d == 1 else "hard")
    return {"opp": analytics.CODE.get(fx["opponent"], fx["opponent"][:3]),
            "venue": fx["venue"], "band": band}


def team_payload(snap: dict, imported: dict | None = None,
                 players: list[dict] | None = None, gw_override: int | None = None) -> dict:
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
    next_gw = snap.get("next_gw") or snap.get("current_gw") or 1
    gw = int(gw_override) if gw_override else next_gw
    rankings = analytics.compute_rankings(team_stats, snap.get("team_strength"))

    out = {
        "ok": True, "error": None, "imported": False,
        "manager": None, "team_name": None, "overall_rank": None,
        "bank": None, "team_value": None, "gw": gw,
        "gk": [], "defs": [], "mids": [], "fwds": [], "bench": [],
        "captain": None, "vice": None, "projected": None,
        "next_gw": next_gw, "gws": list(range(next_gw, min(next_gw + 7, 39))),
        "analysis": None,
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
            "gw": gw if gw_override else imported.get("gw", gw),
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

    # Use the CENTRAL projection engine so My Team matches Home exactly, with
    # robust (accent-folded) player matching so no squad member is left blank.
    _proj = squad_projection(snap, players, squad, gw)
    # squad_projection returns players in the same order as `squad`, so pair
    # them by position — never re-match by name (that's what dropped players).
    _proj_rows = _proj["players"]

    starters, bench = [], []
    projected = 0.0
    have_proj = bool(_proj["players"] and _proj["matched_n"])

    odds = snap.get("odds") or {}
    _own_lookup = {(pl.get("name") or "").lower(): pl.get("selected_by")
                   for pl in players} if players else {}
    for _i, m in enumerate(squad):
        _pp = _proj_rows[_i] if _i < len(_proj_rows) else {}
        _raw = _pp.get("xpts")
        xpts = _raw or 0.0
        m = {**m, "xpts": None if _raw is None else round(_raw, 1),
             "xp_reason": _pp.get("reason"),
             "team": _pp.get("team") or m.get("team"),
             "position": _pp.get("position") or m.get("position"),
             "team_corrected": _pp.get("team_corrected", False)}
        m["market"] = _market_chip_for(m, odds, fixtures, gw)
        m["fix"] = _player_fixture_chip(m.get("team"), fixtures, gw, rankings, m.get("position", "MID"))
        # ownership from live player data if available
        own = _own_lookup.get((m.get("name") or "").lower()) if _own_lookup else None
        m["own"] = own
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

    # Central total is the single source of truth (captain mult + bench handled
    # inside squad_projection), guaranteeing parity with Home.
    out["projected"] = _proj["total"] if have_proj else None

    # --- In-page team analysis (shown under the pitch) ---
    if have_proj and starters:
        by_pos = {"GK": [], "DEF": [], "MID": [], "FWD": []}
        for mm in starters:
            by_pos.setdefault(mm.get("position", "MID"), []).append(mm)
        pos_totals = {k: round(sum((x.get("xpts") or 0) for x in v), 1)
                      for k, v in by_pos.items() if v}
        ranked_all = sorted([x for x in starters if x.get("xpts") is not None], key=lambda x: -x["xpts"])
        strongest = ranked_all[0] if ranked_all else None
        weakest = ranked_all[-1] if ranked_all else None
        # strongest LINE (by total)
        best_line = max(pos_totals, key=pos_totals.get) if pos_totals else None
        line_names = {"GK": "goalkeeper", "DEF": "defence", "MID": "midfield", "FWD": "attack"}
        rec = None
        try:
            rec = recommend_transfer(snap, players, gw, min(gw + 4, 38))
        except Exception:
            rec = None
        out["analysis"] = {
            "pos_totals": pos_totals,
            "best_line": best_line, "best_line_name": line_names.get(best_line, best_line),
            "strongest": strongest, "weakest": weakest,
            "transfer": rec, "gw": gw,
        }
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


def _market_chip_for(player, odds, fixtures, gw):
    """Return a market chip {label, value} for a player's next fixture, or None.
    GK/DEF -> clean-sheet %, others -> team expected goals. Only when the stored
    odds opponent matches the player's actual next opponent."""
    if not odds:
        return None
    team = player.get("team")
    tm = odds.get(team)
    nxt = next((f for f in fixtures.get(team, []) if f["gw"] == gw), None)
    if not (tm and nxt and tm.get("opp") == nxt.get("opponent")):
        return None
    pos = player.get("position")
    if pos in ("GK", "DEF") and tm.get("cs_prob") is not None:
        return {"label": "CS", "value": f"{round(tm['cs_prob']*100)}%"}
    if tm.get("team_goals_exp") is not None:
        return {"label": "mkt xG", "value": f"{tm['team_goals_exp']:.1f}"}
    return None


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
                    search="", limit=40, show_odds=True):
    players = players or snap.get("players", []) or []
    fixtures = snap.get("fixtures", {})
    rankings = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
    gw = snap.get("next_gw") or snap.get("current_gw") or 1

    has_live = len(players) > 0
    odds = (snap.get("odds") or {}) if show_odds else {}
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
    seen_n = set(); all_players = []
    for pl in sorted(players, key=lambda r: r.get("name", "")):
        nm = pl.get("name")
        if nm and nm.lower() not in seen_n and (pl.get("minutes", 0) or 0) > 0:
            seen_n.add(nm.lower())
            all_players.append({"name": nm, "team": pl.get("team", ""),
                                "pos": pl.get("position", "")})
    return {
        "rows": out_rows, "has_live": has_live, "gw": gw,
        "has_odds": bool(odds), "all_players": all_players,
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
    # Player name list for the transfer-simulator search dropdown (datalist).
    # Dedupe, keep those with meaningful minutes, sort alphabetically.
    seen = set()
    all_players = []
    for pl in sorted(players, key=lambda r: r.get("name", "")):
        nm = pl.get("name")
        if nm and nm.lower() not in seen and (pl.get("minutes", 0) or 0) > 0:
            seen.add(nm.lower())
            all_players.append({"name": nm, "team": pl.get("team", ""),
                                "pos": pl.get("position", "")})
    return {
        "has_live": has_live, "gw_from": gw_from, "gw_to": gw_to,
        "next_gw": next_gw, "gws": gws, "targets": targets, "sim": sim,
        "sim_a": sim_a, "sim_b": sim_b, "all_players": all_players,
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
            {"key": "accuracy", "title": "Model Accuracy", "desc": "How FPL IQ's predictions score vs real results (backtested).", "icon": "✓"},
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
            # Transfer-decision verdict (gain = B - A over the horizon).
            # Hits -4 if it would cost a point hit; here we show the raw gain
            # and a verdict band so it reads as a real decision, not a ranking.
            verdict = ("\u2705 Do it" if diff_total >= 2
                       else "\u2696\ufe0f Marginal" if diff_total >= 0
                       else "\u270b Hold")
            out.update({
                "ok": True, "kind": "transfer",
                "answer": (f"{better['name']} projects higher over "
                           f"GW{gw_from}\u2013{gw_to} \u2014 {verdict}."),
                "detail": (f"{b['name']} {b['total_xpts']} vs {a['name']} "
                           f"{a['total_xpts']} xPts over {gw_to - gw_from + 1} GWs "
                           f"({'+' if diff_total > 0 else ''}{diff_total}). A point hit "
                           f"(-4) would need a gain above ~4 to be worth it."),
                "comparison": {"a": a, "b": b, "per_gw": per, "diff_total": diff_total,
                               "verdict": verdict},
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


def matches_payload(snap, gw=None):
    """Matches tab: UPCOMING fixtures with model scoreline predictions, plus
    the most recent FINISHED results. Returns {gw, upcoming, finished, has_data}.
    """
    fixtures = snap.get("fixtures", {})
    rankings = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
    next_gw = snap.get("next_gw") or snap.get("current_gw") or 1
    gw = int(gw or next_gw)

    # Upcoming: predicted scorelines for the selected GW.
    upcoming = []
    try:
        for p in analytics.predict_gameweek(rankings, fixtures, gw):
            upcoming.append({
                "home": p["home"], "away": p["away"],
                "home_code": analytics.CODE.get(p["home"], p["home"][:3]),
                "away_code": analytics.CODE.get(p["away"], p["away"][:3]),
                "home_score": p["home_score"], "away_score": p["away_score"],
                "home_xg": p.get("home_xg"), "away_xg": p.get("away_xg"),
                "verdict": p.get("verdict"), "confidence": p.get("confidence"),
            })
    except Exception:
        pass

    # Finished: prefer the rich match_details (xG + top performers) from the
    # Action; fall back to the plain results scores if details aren't present.
    finished = []
    details = snap.get("match_details") or []
    if details:
        for d in details[:10]:
            finished.append({
                "home": d.get("home"), "away": d.get("away"),
                "home_code": analytics.CODE.get(d.get("home"), (d.get("home") or "")[:3]),
                "away_code": analytics.CODE.get(d.get("away"), (d.get("away") or "")[:3]),
                "hs": d.get("hs"), "as": d.get("as"),
                "hxg": d.get("hxg"), "axg": d.get("axg"),
                "top_home": d.get("top_home"), "top_away": d.get("top_away"),
                "rich": True,
            })
        return {"gw": gw, "next_gw": next_gw, "upcoming": upcoming,
                "finished": finished, "has_data": bool(snap.get("team_stats"))}
    results = snap.get("results", {}) or {}
    if results:
        try:
            # pick the highest finished gw <= current
            gws_done = sorted((int(k) for k in results.keys()), reverse=True)
            last_done = next((g for g in gws_done if g < gw), gws_done[0] if gws_done else None)
            if last_done is not None:
                for r in results.get(str(last_done), []):
                    hr = rankings.get(r.get("home"), {})
                    ar = rankings.get(r.get("away"), {})
                    finished.append({
                        "gw": last_done,
                        "home": r.get("home"), "away": r.get("away"),
                        "home_code": analytics.CODE.get(r.get("home"), (r.get("home") or "")[:3]),
                        "away_code": analytics.CODE.get(r.get("away"), (r.get("away") or "")[:3]),
                        "hs": r.get("hs"), "as": r.get("as"),
                    })
        except Exception:
            pass

    return {"gw": gw, "next_gw": next_gw, "upcoming": upcoming,
            "finished": finished, "has_data": bool(snap.get("team_stats"))}


def backtest_payload(snap, history_rows):
    """Run the model backtest across logged gameweeks for the Accuracy page."""
    rankings = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
    fixtures = snap.get("fixtures", {})
    if not history_rows:
        return {"ok": False, "note": "No gameweek history logged yet. The daily "
                "pipeline logs it automatically — the backtest activates once "
                "2+ gameweeks are stored."}
    res = analytics.backtest_all(history_rows, rankings, fixtures)
    if not res.get("ok"):
        return {"ok": False, "note": res.get("error", "Not enough data to backtest yet.")}
    # Plain-English interpretation of the headline metrics.
    mae = res.get("mae", 0); corr = res.get("correlation", 0); w2 = res.get("within_2_pct", 0)
    acc = ("very accurate" if mae <= 1.5 else "solid" if mae <= 2.2 else "rough — early-season noise")
    rank = ("ranks players reliably" if corr >= 0.5 else
            "ranks players reasonably" if corr >= 0.3 else "ranking is noisy so far")
    res["interpretation"] = (f"On average predictions land within {mae} pts of the real score "
                             f"({acc}); {w2}% within \u00b12. Correlation {corr} means the model "
                             f"{rank}. Accuracy sharpens as more gameweeks are logged.")
    return {"ok": True, **res}
