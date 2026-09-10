"""
app.py — Flask all-in-one FPL Dashboard.

Pages:
  /                 Dashboard: 3 target tables (CS / goals / conceding) GW window
  /predictions      xG-model scoreline predictions for the next gameweek
  /my-team          Your FPL squad, captain picks, transfer hints
  /update           Trigger a live scrape (FBRef + FPL API) to refresh data

Data is stored in SQLite (fpl.db) and seeded from seed_data.json on first run.
Weekly refresh: click "Refresh Data" (calls scraper.scrape_all) — falls back
to the last cached snapshot if a source is unavailable.
"""
from __future__ import annotations

import json
import os

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   url_for)

import analytics
import models
import scraper

app = Flask(__name__)
try:
    BASE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    BASE = os.path.join(os.environ.get("WORKSPACE_DIR", "."), "artifacts", "fpl_dashboard")
SEED = os.path.join(BASE, "seed_data.json")

GW_FROM_DEFAULT = 4
GW_TO_DEFAULT = 11


def _ensure_data() -> dict:
    """Load current snapshot from DB, seeding from JSON on first run."""
    snap = models.load_snapshot()
    if snap is None:
        with open(SEED) as f:
            seed = json.load(f)
        models.save_snapshot(seed)
        models.save_squad(seed["squad"])
        snap = models.load_snapshot()
    return snap


@app.route("/")
def dashboard():
    snap = _ensure_data()
    gw_from = int(request.args.get("from", snap.get("next_gw") or GW_FROM_DEFAULT))
    # default to the full remaining season (through GW38); cap at 38
    gw_to = min(int(request.args.get("to", 38)), 38)
    rankings = analytics.compute_rankings(snap["team_stats"])
    tables = analytics.build_target_tables(rankings, snap["fixtures"], gw_from, gw_to)
    gws = list(range(gw_from, gw_to + 1))
    scatter = analytics.scatter_data(rankings)
    ease = analytics.fixture_ease_percent(rankings, snap["fixtures"], gw_from, gw_to)
    return render_template("dashboard.html", tables=tables, gws=gws,
                           gw_from=gw_from, gw_to=gw_to,
                           scatter=json.dumps(scatter),
                           ease=json.dumps(ease),
                           scraped_at=snap.get("scraped_at", "—"),
                           active="dashboard")


@app.route("/predictions")
def predictions():
    snap = _ensure_data()
    gw = int(request.args.get("gw", snap.get("next_gw") or GW_FROM_DEFAULT))
    rankings = analytics.compute_rankings(snap["team_stats"])
    preds = analytics.predict_gameweek(rankings, snap["fixtures"], gw)
    return render_template("predictions.html", preds=preds, gw=gw,
                           scraped_at=snap.get("scraped_at", "—"),
                           active="predictions")


@app.route("/my-team", methods=["GET", "POST"])
def my_team():
    snap = _ensure_data()
    players = _players(snap)
    if request.method == "POST":
        # squad submitted from the builder as JSON (list of picks)
        rows = []
        raw = request.form.get("squad_json", "").strip()
        if raw:
            try:
                picks = json.loads(raw)
                for p in picks:
                    if p.get("name") and p.get("team"):
                        rows.append({"name": p["name"], "team": p["team"],
                                     "position": p.get("position", ""),
                                     "price": float(p.get("price", 0) or 0)})
            except (ValueError, TypeError):
                rows = []
        models.save_squad(rows)  # allow saving an empty/partial squad
        return redirect(url_for("my_team"))

    squad = models.load_squad()
    gw = int(request.args.get("gw", snap.get("next_gw") or GW_FROM_DEFAULT))
    rankings = analytics.compute_rankings(snap["team_stats"])
    caps = analytics.captain_picks(rankings, snap["fixtures"], squad, gw)
    # transfer hints: squad players whose team has a poor upcoming run
    tables = analytics.build_target_tables(rankings, snap["fixtures"], gw, gw + 4)
    cs_rank = {r["team"]: r["easy_n"] for r in tables["cs"]}
    gs_rank = {r["team"]: r["easy_n"] for r in tables["gs"]}
    hints = []
    for p in squad:
        run = gs_rank.get(p["team"], 0) if p["position"] in ("MID", "FWD") else cs_rank.get(p["team"], 0)
        if run <= 1:
            hints.append({"name": p["name"], "team": p["team"],
                          "position": p["position"], "easy_next5": run})
    # squad economics + validity for the builder UI
    budget = round(sum(float(p.get("price", 0) or 0) for p in squad), 1)
    from collections import Counter
    pos_counts = Counter(p.get("position") for p in squad)
    club_counts = Counter(p.get("team") for p in squad)
    validity = {
        "budget": budget,
        "remaining": round(100.0 - budget, 1),
        "counts": {k: pos_counts.get(k, 0) for k in ("GK", "DEF", "MID", "FWD")},
        "over_club": [t for t, c in club_counts.items() if c > 3],
        "size": len(squad),
    }
    return render_template("my_team.html", squad=squad, caps=caps, hints=hints,
                           gw=gw, players=players, has_players=bool(players),
                           validity=validity,
                           scraped_at=snap.get("scraped_at", "—"),
                           active="my_team")


@app.route("/planner")
def planner():
    snap = _ensure_data()
    gw_from = int(request.args.get("from", snap.get("next_gw") or GW_FROM_DEFAULT))
    gw_to = int(request.args.get("to", gw_from + 7))
    squad = models.load_squad()
    players = _players(snap)
    rankings = analytics.compute_rankings(snap["team_stats"])
    # player-level targets per GW + an overall next-N-GW view
    next_gw = snap.get("next_gw") or GW_FROM_DEFAULT
    gw_targets = {gw: analytics.gw_player_targets(players, rankings, snap["fixtures"],
                                                  squad, gw)
                  for gw in range(gw_from, gw_to + 1)}
    overall = analytics.overall_targets(players, rankings, snap["fixtures"], squad,
                                        gw_from, min(gw_from + 4, gw_to))
    trends = analytics.form_trend_data(rankings, snap["fixtures"], gw_from, gw_to)
    return render_template("planner.html", gw_targets=gw_targets, overall=overall,
                           gw_from=gw_from, gw_to=gw_to, squad=squad,
                           has_players=bool(players),
                           trends=json.dumps(trends),
                           scraped_at=snap.get("scraped_at", "—"),
                           active="planner")


# --------------------------------------------------------------------------
# Player-data pages (all fed by the FPL API player list)
# --------------------------------------------------------------------------
def _players(snap: dict) -> list:
    return snap.get("players", []) or []


@app.route("/players")
def players_page():
    snap = _ensure_data()
    pl = _players(snap)
    position = request.args.get("position", "ALL")
    team = request.args.get("team", "ALL")
    sort = request.args.get("sort", "points")
    search = request.args.get("q", "").strip()
    rows = analytics.filter_sort_players(pl, position, team, sort, search)
    teams = sorted({p["team"] for p in pl}) if pl else []
    # Determine the shots source robustly. Prefer the stored flag, but also
    # auto-detect: real shots/90 are never above ~12, so if any player shows a
    # much larger value the data is the FPL Threat/Creativity proxy. This makes
    # the labels correct even if an older snapshot never stored the flag.
    shots_source = snap.get("shots_source")
    if shots_source not in ("fbref", "fpl_proxy"):
        shots_source = "fbref"
    max_sh = max((p.get("shots_90", 0) or 0) for p in pl) if pl else 0
    if max_sh > 12:  # implausible as real shots/90 → it's the proxy
        shots_source = "fpl_proxy"
    return render_template("players.html", rows=rows, teams=teams,
                           position=position, team=team, sort=sort, search=search,
                           has_data=bool(pl),
                           shots_source=shots_source,
                           scraped_at=snap.get("scraped_at", "—"),
                           active="players")


@app.route("/set-pieces")
def set_pieces_page():
    snap = _ensure_data()
    takers = analytics.set_piece_takers(_players(snap))
    return render_template("set_pieces.html", takers=takers,
                           has_data=bool(_players(snap)),
                           scraped_at=snap.get("scraped_at", "—"), active="set_pieces")


@app.route("/prices")
def prices_page():
    snap = _ensure_data()
    pc = analytics.price_changes(_players(snap))
    return render_template("prices.html", risers=pc["risers"], fallers=pc["fallers"],
                           has_data=bool(_players(snap)),
                           scraped_at=snap.get("scraped_at", "—"), active="prices")


@app.route("/availability")
def availability_page():
    snap = _ensure_data()
    flagged = analytics.availability_flags(_players(snap))
    return render_template("availability.html", flagged=flagged,
                           labels=analytics.STATUS_LABEL,
                           has_data=bool(_players(snap)),
                           scraped_at=snap.get("scraped_at", "—"), active="availability")


@app.route("/value")
def value_page():
    snap = _ensure_data()
    val = analytics.value_finder(_players(snap))
    return render_template("value.html", val=val,
                           has_data=bool(_players(snap)),
                           scraped_at=snap.get("scraped_at", "—"), active="value")


def _do_refresh() -> dict:
    """Scrape fresh data and merge it over the cached snapshot.

    Partial source failures never wipe good data. Returns the bundle
    (including any per-source errors) so callers can report status.
    """
    bundle = scraper.scrape_all()
    snap = _ensure_data()
    if bundle.get("team_stats"):
        snap["team_stats"].update(bundle["team_stats"])
    if bundle.get("fixtures"):
        snap["fixtures"] = bundle["fixtures"]
    if bundle.get("players"):
        snap["players"] = bundle["players"]
    if bundle.get("shots_source"):
        snap["shots_source"] = bundle["shots_source"]
    if bundle.get("current_gw"):
        snap["current_gw"] = bundle["current_gw"]
    if bundle.get("next_gw"):
        snap["next_gw"] = bundle["next_gw"]
    snap["scraped_at"] = bundle["scraped_at"]
    snap["errors"] = bundle.get("errors", [])
    models.save_snapshot(snap)
    return bundle


@app.route("/update", methods=["POST"])
def update():
    """Manual refresh from the 'Refresh Data' button in the UI."""
    _do_refresh()
    return redirect(url_for("dashboard"))


@app.route("/cron/refresh")
def cron_refresh():
    """
    Token-protected refresh endpoint for the weekly GitHub Action.

    Call with ?token=<CRON_TOKEN>. The token is read from the CRON_TOKEN
    env var (set it in Render + as a GitHub secret). Returns JSON status.
    """
    expected = os.environ.get("CRON_TOKEN", "")
    if not expected or request.args.get("token") != expected:
        abort(403)
    bundle = _do_refresh()
    return jsonify({
        "status": "ok",
        "scraped_at": bundle.get("scraped_at"),
        "errors": bundle.get("errors", []),
    })


@app.route("/ingest/shots", methods=["POST"])
def ingest_shots():
    """
    Receive real FBRef shot/SoT/key-pass data from the weekly GitHub Action.

    The Action scrapes FBRef from GitHub's runners (which FBRef blocks far
    less than a cloud host) and POSTs a JSON body:
        {"players": {"surname|Squad": {"shots_90":..,"sot_90":..,"kp_90":..}}}
    We match those onto our stored players by (surname, squad) with a
    surname-only fallback, then mark shots_source = 'fbref'.
    """
    expected = os.environ.get("CRON_TOKEN", "")
    if not expected or request.args.get("token") != expected:
        abort(403)
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("players") or {}
    if not incoming:
        return jsonify({"status": "error", "reason": "no players in payload"}), 400

    snap = _ensure_data()
    players = snap.get("players") or []
    if not players:
        return jsonify({"status": "error", "reason": "no player snapshot yet"}), 400

    # index incoming by surname (with squad) and surname-only fallback
    by_full, by_surname = {}, {}
    for key, vals in incoming.items():
        parts = key.split("|")
        surname = parts[0].strip().lower()
        squad = parts[1].strip() if len(parts) > 1 else ""
        by_full[(surname, squad)] = vals
        by_surname.setdefault(surname, vals)

    matched = 0
    for p in players:
        nm = p.get("name", "").split()
        if not nm:
            continue
        # FPL web_names can be like "B.Fernandes" or "J.Timber" — strip a
        # leading initial ("X.") so the surname matches FBRef.
        raw_last = nm[-1]
        if "." in raw_last:
            raw_last = raw_last.split(".")[-1]
        surname = raw_last.lower()
        vals = by_full.get((surname, p.get("team", ""))) or by_surname.get(surname)
        if not vals:
            continue
        for f in ("shots_90", "sot_90", "kp_90"):
            if f in vals:
                p[f] = vals[f]
        matched += 1

    snap["players"] = players
    snap["shots_source"] = "fbref"  # real data now present
    models.save_snapshot(snap)
    return jsonify({"status": "ok", "matched": matched, "received": len(incoming)})

@app.template_filter("dcls")
def difficulty_class(d: int) -> str:
    return {0: "fx-g", 1: "fx-y", 2: "fx-r"}.get(d, "fx-y")


if __name__ == "__main__":
    models.init_db()
    _ensure_data()
    app.run(debug=True, port=5001)
