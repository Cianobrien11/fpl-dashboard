# 🚀 Deploy & Automate — FPL Dashboard

Everything you need to push to GitHub, deploy on Render with **durable Postgres**, and set up the **weekly auto-refresh**.

---

## 1. Push to GitHub

Create an empty repo on GitHub first (suggested name: **fpl-dashboard**, same account as your Horse-Racing-Data repo), then from inside `fpl_dashboard/`:

```bash
git init
git add .
git commit -m "FPL Dashboard — all-in-one (targets, predictions, planner, my-team)"
git branch -M main
git remote add origin https://github.com/Cianobrien11/fpl-dashboard.git
git push -u origin main

```

> The included `.gitignore` keeps `fpl.db`, `__pycache__/`, and scratch files out of the repo.

---

## 2. Deploy on Render (with persistent Postgres)

The `render.yaml` blueprint provisions **both** the web service and a free Postgres database, and wires them together automatically.

1. Go to **render.com** → **New +** → **Blueprint**.
2. Connect the **fpl-dashboard** repo. Render detects `render.yaml`.
3. It creates:- **fpl-dashboard** (web service, `gunicorn app:app`)

- **fpl-db** (free Postgres) → its connection string is injected as `DATABASE_URL`, so your squad + scraped data now **survive redeploys**.

1. You'll be prompted for the **CRON_TOKEN** value (it's marked `sync: false`). Enter any long random string — e.g. run `openssl rand -hex 24` and paste it. Keep a copy; you'll add the same value to GitHub in step 3.
2. Click **Apply** → first build takes ~3 min. The app seeds itself from `seed_data.json` on first boot.

Your app will be live at `https://fpl-dashboard.onrender.com`. Click **↻ Refresh Data** once to pull live FBRef + FPL stats.

> **Local dev** still uses SQLite automatically — `models.py` only switches to Postgres when `DATABASE_URL` is present. No config needed to run locally.

---

## 3. Weekly auto-refresh (GitHub Action)

The workflow at `.github/workflows/weekly-refresh.yml` hits your app's token-protected `/cron/refresh` endpoint every **Friday 07:00 UTC** (ahead of the Saturday deadline), and can be run manually any time.

Add two **repository secrets** (GitHub repo → Settings → Secrets and variables → Actions → New repository secret):

| Secret | Value |
| --- | --- |
| `APP_URL` | `https://fpl-dashboard.onrender.com` (your Render URL, no trailing slash) |
| `CRON_TOKEN` | the **same** random string you set in Render step 2.4 |

That's it. Test it now: repo → **Actions** tab → **Weekly Data Refresh** → **Run workflow**. It should return `{"status":"ok", ...}`.

> **How it's secured:** `/cron/refresh` returns **403** unless the `?token=` query matches the `CRON_TOKEN` env var, so only your Action can trigger it.

---

## Endpoints reference

| Route | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Targets dashboard + charts |
| `/predictions` | GET | xG scoreline predictions |
| `/planner` | GET | Multi-GW transfer plan |
| `/my-team` | GET/POST | Squad + captain picks (POST saves squad) |
| `/update` | POST | Manual refresh (the ↻ button) |
| `/cron/refresh?token=[REDACTED_TOKEN] GET | Token-protected refresh for the weekly Action |  |

