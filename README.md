# ⚽ FPL Dashboard

An all-in-one Fantasy Premier League dashboard: target rankings, xG-model
match predictions, and your own squad tracker — with weekly auto-scraping.

## Features

| Page | What it does |
|------|--------------|
| **Targets** (`/`) | Three ranked 1–20 tables — Clean Sheets, Goals Scored, Conceding — with a colour-coded fixture grid across any gameweek window. Toggle Easy-only vs Easy+Medium. Plus **xG/xGA scatter charts** (finishing vs luck) and **fixture-ease trend lines**. |
| **Predictions** (`/predictions`) | xG-model scoreline predictions for any gameweek (each team's attacking xG vs the opponent's defensive xGA, home-adjusted). |
| **Planner** (`/planner`) | Gameweek-by-gameweek transfer guide: flags your weakest fixture each week, suggests a stronger team to move toward, and gives the captain pick. Includes the fixture-ease trend chart. |
| **My Team** (`/my-team`) | Your squad, captaincy rankings for the week, a transfer watch-list, and an editable squad box. |
| **Refresh Data** | Scrapes FBRef (xG/xGA/shots) + the official FPL API (fixtures, prices, gameweeks) and recalculates everything. |

## How the data works

- **FPL API** (`https://fantasy.premierleague.com/api`) — fixtures, prices, current/next gameweek. Stable public JSON.
- **FBRef** — squad + opponent shooting tables (xG, xGA, shots, SoT), parsed with pandas.
- Everything is cached in **SQLite** (`fpl.db`). If a source is down, the app keeps serving the last good snapshot.
- Ships with **seed data** (GW3 stats + GW4–11 fixtures) so it works out of the box before the first scrape.

## Run locally

```bash
pip install -r requirements.txt
python app.py
# open http://localhost:5001
```

Click **↻ Refresh Data** once to pull live stats and fixtures.

## Deploy (Render)

This repo includes `Procfile` and `render.yaml`. On [Render](https://render.com):

1. New → Web Service → connect this repo.
2. It auto-detects `render.yaml` (build: `pip install -r requirements.txt`, start: `gunicorn app:app`).
3. Deploy. The app seeds itself on first boot.

## Project layout

```
fpl_dashboard/
├── app.py            # Flask routes
├── analytics.py      # ranking + prediction engine (pure functions)
├── scraper.py        # FBRef + FPL API ingestion
├── models.py         # SQLite persistence
├── seed_data.json    # GW3 stats + GW4-11 fixtures (initial data)
├── templates/        # base, dashboard, predictions, my_team
├── requirements.txt
├── Procfile
└── render.yaml
```

## Updating each week

Just click **↻ Refresh Data**. It re-scrapes FBRef + FPL, merges over the
cached snapshot (partial failures never wipe good data), and every table,
prediction, and captain pick recalculates automatically.

## Notes

- The xG model is intentionally simple and transparent (blend of attack xG and
  opponent defensive xGA + home advantage). Tune the weights in
  `analytics.py` if you want.
- Ranking weightings also live in `analytics.py` (`compute_rankings`).
