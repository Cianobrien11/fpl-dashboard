"""
validation.py — data-validation pipeline (Fix #5).

Every player record passes through validate_players() before ANY screen or
model sees it (wired in app._players). Bad records are repaired when the fix
is obvious (clamp) and dropped when they can't be trusted. A report of what
was fixed/dropped is kept so it can be inspected at /app/data-health.

Rules:
  price        > 0                        (drop otherwise)
  team         must be a known club       (drop otherwise)
  position     GK / DEF / MID / FWD       (drop otherwise)
  avg_min      0..90                      (clamp)
  selected_by  0..100                     (clamp)
  xpts         0..20   (per GW)           (clamp — see check_xpts_rows)
  gain         0 < gain < 20 per GW-window check is in recommend_transfer
"""
from __future__ import annotations

POSITIONS = {"GK", "DEF", "MID", "FWD"}
LAST_REPORT: dict = {"checked": 0, "dropped": [], "fixed": [], "valid_teams": 0}


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def known_teams(snap: dict) -> set:
    teams = set((snap.get("team_stats") or {}).keys())
    teams |= set((snap.get("fixtures") or {}).keys())
    teams |= set((snap.get("team_strength") or {}).keys())
    return {t for t in teams if t}


def validate_players(players: list, valid_teams: set | None = None) -> tuple[list, dict]:
    clean, dropped, fixed = [], [], []
    # Safety: if the team list itself looks wrong (naming mismatch would drop a
    # big share of players), skip the team check rather than blank the app.
    if valid_teams and players:
        unknown = sum(1 for p in players if p.get("team") not in valid_teams)
        if unknown > 0.2 * len(players):
            valid_teams = None
    for p in players or []:
        name = p.get("name") or f"id {p.get('id')}"
        price = _num(p.get("price"), 0)
        if not price or price <= 0:
            dropped.append((name, "price <= 0")); continue
        pos = p.get("position")
        if pos not in POSITIONS:
            dropped.append((name, f"bad position {pos!r}")); continue
        team = p.get("team")
        if not team or (valid_teams and team not in valid_teams):
            dropped.append((name, f"unknown team {team!r}")); continue
        q = dict(p)
        am = _num(q.get("avg_min"), 0)
        if am < 0 or am > 90:
            q["avg_min"] = max(0.0, min(90.0, am)); fixed.append((name, f"avg_min {am}->{q['avg_min']}"))
        own = _num(q.get("selected_by"), 0)
        if own < 0 or own > 100:
            q["selected_by"] = max(0.0, min(100.0, own)); fixed.append((name, f"own% {own}->{q['selected_by']}"))
        else:
            q["selected_by"] = own
        if _num(q.get("minutes"), 0) < 0:
            q["minutes"] = 0; fixed.append((name, "negative minutes"))
        clean.append(q)
    report = {"checked": len(players or []), "kept": len(clean),
              "dropped": dropped, "fixed": fixed,
              "valid_teams": len(valid_teams or ())}
    LAST_REPORT.clear(); LAST_REPORT.update(report)
    return clean, report


def check_xpts_rows(rows: list) -> list:
    """Clamp per-GW xPts rows to 0..20 (final guard on model output)."""
    for r in rows or []:
        x = _num(r.get("xpts"), 0)
        if x < 0 or x > 20:
            r["xpts"] = max(0.0, min(20.0, x))
    return rows
