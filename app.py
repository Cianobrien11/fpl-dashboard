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

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)

import functools

import analytics
import billing
import mailer
import mobile
import models
import scraper
import validation

app = Flask(__name__)
# Secret key for signed session cookies. Set SECRET_KEY in Render env for
# production; falls back to a dev default locally.
app.secret_key = os.environ.get("SECRET_KEY", "dev-fpliq-change-me-in-prod")
try:
    BASE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    BASE = os.path.join(os.environ.get("WORKSPACE_DIR", "."), "artifacts", "fpl_dashboard")
SEED = os.path.join(BASE, "seed_data.json")


def _freshness(iso):
    """Fix #14: turn the snapshot's UTC ISO stamp into a friendly, London-time
    label + age + staleness level (fresh <6h, aging <24h, stale >=24h)."""
    import datetime as _dt
    if not iso:
        return {"label": "never", "age": "no data yet", "level": "stale"}
    try:
        t = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            local = t.astimezone(ZoneInfo("Europe/London"))
        except Exception:
            local = t
        hrs = (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() / 3600
        if hrs < 1:
            age = f"{max(1, int(hrs * 60))} min ago"
        elif hrs < 48:
            age = f"{int(hrs)}h ago"
        else:
            age = f"{int(hrs // 24)} days ago"
        level = "fresh" if hrs < 6 else "aging" if hrs < 24 else "stale"
        return {"label": local.strftime("%a %d %b, %H:%M"), "age": age, "level": level}
    except Exception:
        return {"label": str(iso)[:16], "age": "", "level": "aging"}


@app.context_processor
def inject_settings():
    """Make saved settings AND the logged-in user available to every template
    (as `app_settings` and `current_user`), so theme/odds and account state
    apply app-wide without each route passing them."""
    try:
        s = models.load_settings(_uid())
    except Exception:
        s = {}
    try:
        cu = current_user()
    except Exception:
        cu = None
    try:
        pro = user_is_pro()
    except Exception:
        pro = True
    # Data-freshness: expose when the snapshot was last refreshed so every
    # screen can show an "Updated ..." indicator (predictions go stale fast).
    try:
        _snap = models.load_snapshot() or {}
        updated = _snap.get("scraped_at")
    except Exception:
        _snap, updated = {}, None
    return {"app_settings": s or {}, "current_user": cu,
            "is_pro": pro, "billing_on": billing.billing_enabled(),
            "pro_price": billing.pro_price(), "data_updated": updated,
            "freshness": _freshness(updated),
            "odds_freshness": _freshness(_snap.get("odds_updated")) if _snap.get("odds_updated") else None}


# Build the GW-history index once per request and hand it to the analytics
# engine so every xPts call uses real recent-form blends. Cached on the app
# object with the max logged GW as a cheap freshness key to avoid re-reading
# the whole table on every request.
_HISTORY_IDX = {"key": None, "data": {}}


@app.before_request
def _load_history_cache():
    try:
        cov = models.gw_history_coverage()
        key = (cov.get("rows"), cov.get("max_gw"))
        if key != _HISTORY_IDX["key"]:
            rows = models.load_gw_history()  # newest gw first (ORDER BY gw DESC)
            idx = {}
            for r in rows:
                el = r.get("element")
                if el is None:
                    continue
                idx.setdefault(el, []).append(r)
            _HISTORY_IDX["key"] = key
            _HISTORY_IDX["data"] = idx
        analytics.set_history_cache(_HISTORY_IDX["data"])
    except Exception:
        analytics.set_history_cache({})


def current_user():
    """Return the logged-in user's {id, email} from the session, or None."""
    uid = session.get("uid")
    if not uid:
        return None
    return {"id": uid, "email": session.get("uemail")}


def _uid() -> int:
    """Logged-in user's id, or 0 for the shared/anonymous device row."""
    try:
        return int(session.get("uid") or 0)
    except (TypeError, ValueError):
        return 0


def user_is_pro() -> bool:
    """True if the current user has Pro (always True while billing is off)."""
    return billing.is_pro(_uid())


def pro_required(view):
    """Gate a view behind Pro. When billing is OFF this is a no-op (everyone
    passes). When ON, non-Pro users are sent to the upgrade page."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if billing.billing_enabled() and not user_is_pro():
            return redirect(url_for("m_upgrade"))
        return view(*args, **kwargs)
    return wrapper


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


@app.route("/service-worker.js")
def service_worker():
    """Serve the service worker from the root scope so it can control all pages.
    (A SW only controls URLs at or below its own path, so it cannot live
    under /static/.)"""
    resp = send_from_directory(os.path.join(BASE, "static"), "service-worker.js")
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/offline")
def offline():
    return send_from_directory(os.path.join(BASE, "static"), "offline.html")


@app.route("/")
def dashboard():
    snap = _ensure_data()
    gw_from = int(request.args.get("from", snap.get("next_gw") or GW_FROM_DEFAULT))
    # default to the full remaining season (through GW38); cap at 38
    gw_to = min(int(request.args.get("to", 38)), 38)
    rankings = analytics.compute_rankings(snap["team_stats"], snap.get("team_strength"))
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
    rankings = analytics.compute_rankings(snap["team_stats"], snap.get("team_strength"))
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

    squad = _live_squad(models.load_squad(), players)
    gw = int(request.args.get("gw", snap.get("next_gw") or GW_FROM_DEFAULT))
    rankings = analytics.compute_rankings(snap["team_stats"], snap.get("team_strength"))
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
    # fixture ticker for the user's 15 over the next 6 GWs
    ticker = analytics.my_team_ticker(squad, rankings, snap["fixtures"],
                                      gw, min(gw + 5, 38)) if squad else None
    # upcoming fixtures + 2-year record vs each opponent (from H2H data)
    fixture_history = analytics.squad_fixture_history(
        squad, snap.get("h2h") or {}, snap["fixtures"], gw, min(gw + 5, 38)) if squad else None
    return render_template("my_team.html", squad=squad, caps=caps, hints=hints,
                           gw=gw, players=players, has_players=bool(players),
                           validity=validity, ticker=ticker,
                           fixture_history=fixture_history,
                           has_h2h=bool(snap.get("h2h")),
                           scraped_at=snap.get("scraped_at", "—"),
                           active="my_team")


@app.route("/planner")
def planner():
    snap = _ensure_data()
    gw_from = int(request.args.get("from", snap.get("next_gw") or GW_FROM_DEFAULT))
    gw_to = int(request.args.get("to", gw_from + 7))
    players = _players(snap)
    squad = _live_squad(models.load_squad(), players)
    rankings = analytics.compute_rankings(snap["team_stats"], snap.get("team_strength"))
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
    """Single choke point: every page gets VALIDATED player records (Fix #5)."""
    raw = snap.get("players", []) or []
    key = (id(raw), len(raw))
    if snap.get("_valid_key") != key:
        clean, _ = validation.validate_players(raw, validation.known_teams(snap))
        snap["_valid_players"], snap["_valid_key"] = clean, key
    return snap["_valid_players"]


_SQUAD_REPORT: dict = {}


def _live_squad(squad, players):
    """Every screen's squad goes through the FPL-ID resolver (Fix #15)."""
    try:
        res, rep = mobile.resolve_squad(squad, players)
        _SQUAD_REPORT.clear(); _SQUAD_REPORT.update(rep)
        return res
    except Exception:
        return squad


@app.route("/app/data-health")
def m_data_health():
    """Show what the validation pipeline fixed / dropped."""
    snap = _ensure_data()
    _players(snap)
    return jsonify({"players": validation.LAST_REPORT, "squad_ids": _SQUAD_REPORT})


# ---------------------------------------------------------------------------
# MOBILE APP (FPL IQ) — a separate, mobile-first frontend served under /app.
# Reuses the same analytics engine; the desktop dashboard is unchanged.
# ---------------------------------------------------------------------------
@app.route("/app")
def m_home():
    snap = _ensure_data()
    players = _players(snap)
    # Use the SAME squad My Team uses (the user's saved/imported squad), not the
    # shared seed squad — otherwise Home and My Team project different teams.
    uid = _uid()
    if uid:
        try:
            user_squad = models.load_squad(uid)
            if user_squad:
                snap = {**snap, "squad": user_squad}
        except Exception:
            pass
    snap = {**snap, "squad": _live_squad(snap.get("squad", []) or [], players)}
    data = mobile.home_payload(snap, players)
    return render_template("m_home.html", tab="home", **data)


@app.route("/app/players")
def m_players():
    snap = _ensure_data()
    players = _players(snap)
    _s = {}
    try:
        _s = models.load_settings(_uid()) or {}
    except Exception:
        _s = {}
    show_odds = _s.get("show_odds", True)
    data = mobile.players_payload(
        snap, players,
        position=request.args.get("position", "ALL"),
        sort=request.args.get("sort", "points"),
        search=request.args.get("q", "").strip(),
        limit=40, show_odds=show_odds)
    return render_template("m_players.html", tab="players", **data)


@app.route("/app/planner")
@pro_required
def m_planner():
    snap = _ensure_data()
    players = _players(snap)
    gw_from = request.args.get("from")
    gw_to = request.args.get("to")
    if not gw_from or not gw_to:
        try:
            _s = models.load_settings(_uid()) or {}
            gw_from = gw_from or (_s.get("gw_from") or None)
            gw_to = gw_to or (_s.get("gw_to") or None)
        except Exception:
            pass
    data = mobile.planner_payload(
        snap, players, gw_from=gw_from, gw_to=gw_to,
        sim_a=request.args.get("a", "").strip(),
        sim_b=request.args.get("b", "").strip())
    grid = mobile.planner_grid(snap, players, gw_from=data["gw_from"], gw_to=data["gw_to"])
    return render_template("m_planner.html", tab="planner", gw=snap.get("next_gw"),
                           grid=grid, **data)


@app.route("/app/team", methods=["GET", "POST"])
def m_team():
    snap = _ensure_data()
    players = _players(snap)
    imported = None
    team_id = request.values.get("team_id", "").strip()
    # Auto-load the saved Team ID on a plain GET (so My Team fills itself in).
    auto = False
    if not team_id and request.method == "GET":
        try:
            team_id = str((models.load_settings(_uid()) or {}).get("team_id", "") or "").strip()
            auto = bool(team_id)
        except Exception:
            team_id = ""
    if (request.method == "POST" or auto) and team_id:
        try:
            imported = scraper.import_fpl_team(int(team_id))
        except ValueError:
            imported = {"ok": False, "error": "Team ID must be a number."}
        if imported and imported.get("ok"):
            try:
                slim = [{"name": m.get("name"), "team": m.get("team"),
                         "position": m.get("position"), "price": m.get("price", 0),
                         "element": m.get("element"),
                         "is_captain": m.get("is_captain", False),
                         "is_vice": m.get("is_vice", False),
                         "is_bench": m.get("is_bench", False),
                         "multiplier": m.get("multiplier", 1)}
                        for m in imported.get("squad", [])]
                models.save_squad(slim, _uid())
            except Exception:
                pass

    # When logged in, show THIS user's saved squad (not the shared seed squad).
    uid = _uid()
    if uid and not imported:
        try:
            user_squad = models.load_squad(uid)
            if user_squad:
                snap = {**snap, "squad": user_squad}
        except Exception:
            pass
    snap = {**snap, "squad": _live_squad(snap.get("squad", []) or [], players)}
    if imported and imported.get("ok"):
        imported = {**imported, "squad": _live_squad(imported.get("squad", []), players)}
    gw_sel = request.args.get("gw")
    data = mobile.team_payload(snap, imported=imported, players=players,
                               gw_override=gw_sel)
    # Phase B: FPL IQ Team Rating (/100) for the current squad.
    rating = None
    try:
        squad_for_rating = snap.get("squad", []) or []
        if squad_for_rating and players:
            rk = analytics.compute_rankings(snap.get("team_stats", {}), snap.get("team_strength"))
            # Fix #3: match squad to live players with the SAME robust matcher
            # (id -> accent-folded name -> surname+team) so nobody is dropped,
            # and feed the central projection total in.
            _idx = mobile._build_player_index(players)
            matched_sq = []
            for _m in squad_for_rating:
                _live = mobile._match_player(_m, _idx)
                matched_sq.append({**_m, "name": _live.get("name"), "team": _live.get("team"),
                                   "position": _live.get("position")} if _live else _m)
            _gw_r = data.get("gw") or snap.get("next_gw") or 1
            _central = (data.get("projection") or {}).get("total") if isinstance(data.get("projection"), dict) else None
            if _central is None:
                _central = mobile.squad_projection(snap, players, squad_for_rating, _gw_r)["total"]
            tr = analytics.team_rating(matched_sq, players, rk,
                                       snap.get("fixtures", {}), int(_gw_r),
                                       central_total=_central)
            if tr.get("ok"):
                rating = tr
    except Exception:
        rating = None
    return render_template("m_team.html", tab="team", team_id=team_id,
                           rating=rating, **data)


@app.route("/app/analytics")
def m_analytics():
    snap = _ensure_data()
    players = _players(snap)
    data = mobile.analytics_payload(snap, players)
    return render_template("m_analytics.html", tab="analytics", **data)


@app.route("/app/analytics/<section>")
@pro_required
def m_analytics_sub(section):
    snap = _ensure_data()
    players = _players(snap)
    logs = []
    if section == "accuracy":
        try:
            logs = models.load_prediction_logs()
        except Exception:
            logs = []
    if section not in mobile.ANALYTICS_SECTIONS:
        abort(404)
    data = mobile.analytics_sub_payload(section, snap, players=players, logs=logs)
    return render_template("m_analytics_sub.html", tab="analytics", **data)


@app.route("/app/ask")
@pro_required
def m_ask():
    snap = _ensure_data()
    players = _players(snap)
    q = request.args.get("q", "").strip()
    result = mobile.ask_fpl_iq(q, snap, players=players) if q else None
    return render_template("m_ask.html", tab="analytics", q=q, result=result,
                           gw=snap.get("next_gw"))


@app.route("/app/settings", methods=["GET", "POST"])
def m_settings():
    snap = _ensure_data()
    if request.method == "POST":
        # Persist settings server-side too (groundwork for per-user accounts).
        settings = {
            "team_id": request.form.get("team_id", "").strip(),
            "fav_team": request.form.get("fav_team", "").strip(),
            "gw_from": request.form.get("gw_from", "").strip(),
            "gw_to": request.form.get("gw_to", "").strip(),
            "show_odds": request.form.get("show_odds") == "on",
            "theme": request.form.get("theme", "dark").strip(),
        }
        try:
            models.save_settings(settings, _uid())
        except Exception:
            pass
        return redirect(url_for("m_settings", saved=1))
    try:
        settings = models.load_settings(_uid())
    except Exception:
        settings = {}
    return render_template("m_settings.html", tab="settings",
                           gw=snap.get("next_gw"), settings=settings,
                           teams=analytics.CANONICAL_TEAMS,
                           next_gw=snap.get("next_gw") or 1,
                           saved=request.args.get("saved") == "1")


@app.route("/app/account")
def m_account():
    snap = _ensure_data()
    return render_template("m_account.html", tab="settings",
                           gw=snap.get("next_gw"))


@app.route("/app/matches")
def m_matches():
    snap = _ensure_data()
    data = mobile.matches_payload(snap, gw=request.args.get("gw"))
    return render_template("m_matches.html", tab="matches", **data)


@app.route("/app/accuracy")
def m_accuracy():
    snap = _ensure_data()
    try:
        history = models.load_gw_history()
    except Exception:
        history = []
    data = mobile.backtest_payload(snap, history)
    return render_template("m_accuracy.html", tab="analytics",
                           gw=snap.get("next_gw"), **data)


# ---------------------------------------------------------------------------
# AUTH — signup / verify / login / logout. Sessions store the logged-in user.
# ---------------------------------------------------------------------------


@app.route("/app/signup", methods=["POST"])
def signup():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    res = models.create_user(email, password)
    if not res["ok"]:
        return render_template("m_account.html", tab="settings",
                               gw=_ensure_data().get("next_gw"),
                               error=res["error"], mode="signup", email=email)
    # Email verification is gated behind a toggle. Default OFF for now so
    # friends can sign up and use the app instantly (no inbox round-trip).
    # Set REQUIRE_EMAIL_VERIFICATION=1 in Render env once Resend + domain are
    # ready to re-enable verify-by-email before public launch.
    require_verify = os.environ.get("REQUIRE_EMAIL_VERIFICATION", "0") == "1"
    if require_verify:
        verify_url = url_for("verify_email", token=res["token"], _external=True)
        mailer.send_verification_email(email, verify_url)
        return render_template("m_account.html", tab="settings",
                               gw=_ensure_data().get("next_gw"),
                               pending=email)
    # No verification required: activate immediately and log them straight in.
    try:
        models.mark_verified(email)
    except Exception:
        pass
    session["uid"] = res.get("user_id")
    session["uemail"] = email
    return redirect(url_for("m_settings"))


@app.route("/app/verify/<token>")
def verify_email(token):
    res = models.verify_user_token(token)
    return render_template("m_verify.html", tab="settings",
                           gw=_ensure_data().get("next_gw"),
                           ok=res["ok"], error=res.get("error"),
                           already=res.get("already", False))


@app.route("/app/login", methods=["POST"])
def login():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    res = models.check_login(email, password)
    if not res["ok"]:
        return render_template("m_account.html", tab="settings",
                               gw=_ensure_data().get("next_gw"),
                               error=res["error"], mode="signin", email=email,
                               unverified=res.get("unverified", False))
    session["uid"] = res["user_id"]
    session["uemail"] = res["email"]
    return redirect(url_for("m_settings"))


@app.route("/app/logout")
def logout():
    session.clear()
    return redirect(url_for("m_account"))


@app.route("/app/resend", methods=["POST"])
def resend_verification():
    email = request.form.get("email", "").strip().lower()
    token = models.set_verify_token(email)
    if token:
        verify_url = url_for("verify_email", token=token, _external=True)
        mailer.send_verification_email(email, verify_url)
    return render_template("m_account.html", tab="settings",
                           gw=_ensure_data().get("next_gw"), pending=email)


# ---------------------------------------------------------------------------
# BILLING — Stripe subscriptions (dormant until BILLING_ENABLED=1 + keys set).
# ---------------------------------------------------------------------------
@app.route("/app/upgrade")
def m_upgrade():
    snap = _ensure_data()
    return render_template("m_upgrade.html", tab="settings",
                           gw=snap.get("next_gw"))


@app.route("/app/subscribe", methods=["POST"])
def subscribe():
    if not current_user():
        return redirect(url_for("m_account"))
    uid = _uid()
    email = session.get("uemail", "")
    res = billing.create_checkout_session(
        uid, email,
        success_url=url_for("m_settings", upgraded=1, _external=True),
        cancel_url=url_for("m_upgrade", _external=True))
    if res["ok"]:
        return redirect(res["url"])
    return render_template("m_upgrade.html", tab="settings",
                           gw=_ensure_data().get("next_gw"), error=res["error"])


@app.route("/app/billing-portal")
def billing_portal():
    if not current_user():
        return redirect(url_for("m_account"))
    res = billing.create_portal_session(_uid(), url_for("m_settings", _external=True))
    if res["ok"]:
        return redirect(res["url"])
    return redirect(url_for("m_settings"))


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    res = billing.handle_webhook(request.get_data(), request.headers.get("Stripe-Signature", ""))
    if res["ok"]:
        return jsonify({"received": True})
    return jsonify({"error": res["error"]}), 400


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
    pred = analytics.price_predictions(_players(snap))
    return render_template("prices.html", risers=pc["risers"], fallers=pc["fallers"],
                           pred_rising=pred["rising"], pred_falling=pred["falling"],
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


@app.route("/xpts")
def xpts_page():
    snap = _ensure_data()
    pl = _players(snap)
    next_gw = snap.get("next_gw") or GW_FROM_DEFAULT
    gw = int(request.args.get("gw", next_gw))
    position = request.args.get("position", "ALL")
    # multi-GW mode: ?to=<gw> aggregates xPts across gw..to
    gw_to = request.args.get("to")
    rankings = analytics.compute_rankings(snap["team_stats"], snap.get("team_strength"))
    if gw_to:
        gw_to = min(int(gw_to), 38)
        rows = analytics.expected_points_range(pl, rankings, snap["fixtures"], gw, gw_to)
        if position != "ALL":
            rows = [r for r in rows if r["position"] == position]
        gws = list(range(gw, gw_to + 1))
        return render_template("xpts.html", rows=rows[:50], gw=gw, gw_to=gw_to,
                               gws=gws, position=position, mode="range",
                               has_data=bool(pl), scraped_at=snap.get("scraped_at", "—"),
                               active="xpts")
    rows = analytics.expected_points(pl, rankings, snap["fixtures"], gw)
    if position != "ALL":
        rows = [r for r in rows if r["position"] == position]
    return render_template("xpts.html", rows=rows[:50], gw=gw, gw_to=None,
                           gws=None, position=position, mode="single",
                           has_data=bool(pl), scraped_at=snap.get("scraped_at", "—"),
                           active="xpts")


@app.route("/captaincy")
def captaincy_page():
    snap = _ensure_data()
    pl = _players(snap)
    gw = int(request.args.get("gw", snap.get("next_gw") or GW_FROM_DEFAULT))
    rankings = analytics.compute_rankings(snap["team_stats"], snap.get("team_strength"))
    rows = analytics.captaincy_board(pl, rankings, snap["fixtures"], gw)
    return render_template("captaincy.html", rows=rows, gw=gw, has_data=bool(pl),
                           scraped_at=snap.get("scraped_at", "—"), active="captaincy")


@app.route("/differentials")
def differentials_page():
    snap = _ensure_data()
    pl = _players(snap)
    gw = int(request.args.get("gw", snap.get("next_gw") or GW_FROM_DEFAULT))
    max_own = float(request.args.get("own", 10))
    rankings = analytics.compute_rankings(snap["team_stats"], snap.get("team_strength"))
    rows = analytics.differentials(pl, rankings, snap["fixtures"], gw, max_own)
    return render_template("differentials.html", rows=rows, gw=gw, max_own=max_own,
                           has_data=bool(pl), scraped_at=snap.get("scraped_at", "—"),
                           active="differentials")


@app.route("/radar")
def radar_page():
    snap = _ensure_data()
    rankings = analytics.compute_rankings(snap["team_stats"], snap.get("team_strength"))
    rows = analytics.team_radar(rankings, snap.get("team_strength"))
    return render_template("radar.html", rows=json.dumps(rows),
                           has_data=bool(snap.get("team_stats")),
                           scraped_at=snap.get("scraped_at", "—"), active="radar")


@app.route("/accuracy")
def accuracy_page():
    snap = _ensure_data()
    logs = models.load_prediction_logs()
    results = snap.get("results", {})
    report = analytics.score_predictions(logs, results)
    return render_template("accuracy.html", report=report, n_logs=len(logs),
                           scraped_at=snap.get("scraped_at", "—"), active="accuracy")


@app.route("/h2h")
def h2h_page():
    snap = _ensure_data()
    pl = _players(snap)
    h2h = snap.get("h2h") or {}
    gw = int(request.args.get("gw", snap.get("next_gw") or GW_FROM_DEFAULT))
    rankings = analytics.compute_rankings(snap["team_stats"], snap.get("team_strength"))
    rows = analytics.h2h_vs_next_opponent(pl, h2h, snap["fixtures"], rankings, gw)
    return render_template("h2h.html", rows=rows, gw=gw,
                           has_h2h=bool(h2h), has_data=bool(pl),
                           scraped_at=snap.get("scraped_at", "—"), active="h2h")


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
    if bundle.get("team_strength"):
        snap["team_strength"] = bundle["team_strength"]
    if bundle.get("shots_source"):
        snap["shots_source"] = bundle["shots_source"]
    if bundle.get("results"):
        snap["results"] = bundle["results"]
    # log this gameweek's predictions so we can score accuracy later
    try:
        nxt = bundle.get("next_gw") or snap.get("next_gw")
        if nxt:
            rk = analytics.compute_rankings(snap["team_stats"], snap.get("team_strength"))
            preds = analytics.predict_gameweek(rk, snap["fixtures"], nxt)
            if preds:
                models.log_predictions(nxt, preds)
    except Exception:  # noqa: BLE001
        pass
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


@app.route("/ingest/h2h", methods=["POST"])
def ingest_h2h():
    """
    Receive all-time player-vs-opponent records from the GitHub Action.

    Body: {"players": {"surname|Squad": {opponent: {games, goals, assists, xg}}}}
    Stored as-is in the snapshot under "h2h"; resolved per-player at render time
    against each player's NEXT opponent.
    """
    expected = os.environ.get("CRON_TOKEN", "")
    if not expected or request.args.get("token") != expected:
        abort(403)
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("players") or {}
    if len(incoming) < 20:
        return jsonify({"status": "error", "reason": "too few players"}), 400
    snap = _ensure_data()
    snap["h2h"] = incoming
    models.save_snapshot(snap)
    return jsonify({"status": "ok", "received": len(incoming)})


@app.route("/ingest/team-stats", methods=["POST"])
def ingest_team_stats():
    """
    Receive team-level xG/xGA/goals from the GitHub Action (Understat source).

    Body: {"team_stats": {team_name: {xg, xga, npxg, npxga, gf, ga, mp,
                                      deep, deep_allowed, ppda, history[]}}}
    Merges over the stored team_stats so the app's rankings stay fresh without
    the app ever scraping FBRef (which crashes the free-tier worker). The
    richer fields (npxG, deep completions, PPDA, per-match history) power the
    multi-factor, form-weighted attack/defence model in analytics.py.
    """
    expected = os.environ.get("CRON_TOKEN", "")
    if not expected or request.args.get("token") != expected:
        abort(403)
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("team_stats") or {}
    if len(incoming) < 15:
        return jsonify({"status": "error", "reason": "too few teams"}), 400
    snap = _ensure_data()
    snap.setdefault("team_stats", {}).update(incoming)
    models.save_snapshot(snap)
    return jsonify({"status": "ok", "received": len(incoming)})


@app.route("/ingest/odds", methods=["POST"])
def ingest_odds():
    """
    Receive bookmaker market data from the GitHub Action (The Odds API source).

    Body: {"odds": {team_name: {cs_prob, win_prob, team_goals_exp, opp,
                                venue, gw, updated}}}
      cs_prob        market-implied clean-sheet probability (0-1)
      win_prob       market-implied win probability (0-1)
      team_goals_exp market-implied expected goals this team scores
    Stored under snap['odds']; blended into the model (clean-sheet prob,
    fixture difficulty) alongside the xG-based estimates. Market consensus is a
    sharp, pre-computed signal (prices in team news / lineups faster than any
    model), so it anchors our own numbers.
    """
    expected = os.environ.get("CRON_TOKEN", "")
    if not expected or request.args.get("token") != expected:
        abort(403)
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("odds") or {}
    if not incoming:
        return jsonify({"status": "error", "reason": "no odds"}), 400
    snap = _ensure_data()
    snap["odds"] = incoming
    snap["odds_updated"] = payload.get("updated")
    models.save_snapshot(snap)
    return jsonify({"status": "ok", "received": len(incoming)})


@app.route("/ingest/match-details", methods=["POST"])
def ingest_match_details():
    """Receive per-match detail (scores, xG, top performers) from the Action."""
    expected = os.environ.get("CRON_TOKEN", "")
    if not expected or request.args.get("token") != expected:
        abort(403)
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("matches") or []
    if not incoming:
        return jsonify({"status": "error", "reason": "no matches"}), 400
    snap = _ensure_data()
    snap["match_details"] = incoming
    models.save_snapshot(snap)
    return jsonify({"status": "ok", "received": len(incoming)})
@app.template_filter("dcls")
def difficulty_class(d: int) -> str:
    return {0: "fx-g", 1: "fx-y", 2: "fx-r"}.get(d, "fx-y")


if __name__ == "__main__":
    models.init_db()
    _ensure_data()
    app.run(debug=True, port=5001)

@app.route("/ingest/gw-history", methods=["POST"])
def ingest_gw_history():
    """Receive per-player per-GW history rows from the Action (FPL
    element-summary). Idempotent per (element, gw) — safe to re-send."""
    expected = os.environ.get("CRON_TOKEN", "")
    if not expected or request.args.get("token") != expected:
        abort(403)
    payload = request.get_json(silent=True) or {}
    rows = payload.get("history") or []
    if not rows:
        return jsonify({"status": "error", "reason": "no history"}), 400
    try:
        n = models.save_gw_history(rows)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "reason": str(exc)}), 500
    return jsonify({"status": "ok", "stored": n})


