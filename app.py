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
    trends = analytics.form_trend_data(rankings, snap["fixtures"], gw_from, gw_to)
    return render_template("dashboard.html", tables=tables, gws=gws,
                           gw_from=gw_from, gw_to=gw_to,
                           scatter=json.dumps(scatter),
                           trends=json.dumps(trends),
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
    if request.method == "POST":
        # squad edited via the form: list of name|team|position|price rows
        rows = []
        for line in request.form.get("squad", "").strip().splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) == 4:
                rows.append({"name": parts[0], "team": parts[1],
                             "position": parts[2], "price": float(parts[3])})
        if rows:
            models.save_squad(rows)
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
    return render_template("my_team.html", squad=squad, caps=caps, hints=hints,
                           gw=gw, scraped_at=snap.get("scraped_at", "—"),
                           active="my_team")


@app.route("/planner")
def planner():
    snap = _ensure_data()
    gw_from = int(request.args.get("from", snap.get("next_gw") or GW_FROM_DEFAULT))
    gw_to = int(request.args.get("to", gw_from + 7))
    squad = models.load_squad()
    rankings = analytics.compute_rankings(snap["team_stats"])
    plan = analytics.build_transfer_plan(rankings, snap["fixtures"], squad,
                                          gw_from, gw_to)
    trends = analytics.form_trend_data(rankings, snap["fixtures"], gw_from, gw_to)
    return render_template("planner.html", plan=plan, gw_from=gw_from,
                           gw_to=gw_to, squad=squad,
                           trends=json.dumps(trends),
                           scraped_at=snap.get("scraped_at", "—"),
                           active="planner")


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
