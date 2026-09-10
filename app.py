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
    gw_to = int(request.args.get("to", gw_from + 7))
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
    return render_template("players.html", rows=rows, teams=teams,
                           position=position, team=team, sort=sort, search=search,
                           has_data=bool(pl),
                           shots_source=snap.get("shots_source", "fbref"),
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


@app.template_filter("dcls")
def difficulty_class(d: int) -> str:
    return {0: "fx-g", 1: "fx-y", 2: "fx-r"}.get(d, "fx-y")


if __name__ == "__main__":
    models.init_db()
    _ensure_data()
    app.run(debug=True, port=5001)
