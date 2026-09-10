"""
fbref_action.py — run by the weekly GitHub Action (NOT by the web app).

Scrapes the FBRef player shooting + passing pages from GitHub's runners
(which FBRef blocks far less than a cloud host like Render), extracts real
per-90 shot / SoT / key-pass rates, and POSTs them to the app's secure
/ingest/shots endpoint.

Env vars (provided by the workflow as secrets):
  APP_URL      e.g. https://fpl-dashboard-txgc.onrender.com
  CRON_TOKEN   shared secret, matches the app's CRON_TOKEN

Usage: python fbref_action.py
"""
from __future__ import annotations

import io
import os
import sys
import time

import pandas as pd
import requests

SHOOTING = "https://fbref.com/en/comps/9/shooting/Premier-League-Stats"
PASSING = "https://fbref.com/en/comps/9/passing/Premier-League-Stats"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Referer": "https://fbref.com/en/comps/9/Premier-League-Stats",
}


def _get(url: str, retries: int = 4) -> str:
    last = None
    for attempt in range(retries):
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code in (403, 429) and attempt < retries - 1:
            last = r
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.text
    if last is not None:
        last.raise_for_status()
    return r.text


def _flatten(tbl):
    cols = []
    for c in tbl.columns:
        if isinstance(c, tuple):
            parts = [str(x) for x in c if x and not str(x).startswith("Unnamed")]
            cols.append(parts[-1] if parts else str(c[-1]))
        else:
            cols.append(str(c))
    tbl.columns = cols
    return tbl


def _table(html: str, need: set):
    best = None
    for tbl in pd.read_html(io.StringIO(html)):
        t = _flatten(tbl.copy())
        if need.issubset(set(t.columns)):
            if best is None or len(t) > len(best):
                best = t
    return best


def scrape() -> dict:
    """Return {"NAME|SQUAD": {shots_90, sot_90, kp_90}} keyed for matching."""
    out: dict[str, dict] = {}

    html = _get(SHOOTING)
    sh = _table(html, {"Player", "Sh", "SoT"})
    if sh is not None:
        for _, row in sh.iterrows():
            name = str(row.get("Player", "")).strip()
            if not name or name == "Player":
                continue
            try:
                n90 = float(str(row.get("90s", "0")).replace(",", "") or 0)
                if n90 <= 0:
                    continue
                shots = float(str(row.get("Sh", 0)).replace(",", "") or 0)
                sot = float(str(row.get("SoT", 0)).replace(",", "") or 0)
            except (ValueError, TypeError):
                continue
            key = f"{name.split()[-1].lower()}|{str(row.get('Squad','')).strip()}"
            out.setdefault(key, {})
            out[key]["shots_90"] = round(shots / n90, 2)
            out[key]["sot_90"] = round(sot / n90, 2)

    time.sleep(4)  # be polite between FBRef pages

    html = _get(PASSING)
    kp = _table(html, {"Player", "KP"})
    if kp is not None:
        for _, row in kp.iterrows():
            name = str(row.get("Player", "")).strip()
            if not name or name == "Player":
                continue
            try:
                n90 = float(str(row.get("90s", "0")).replace(",", "") or 0)
                if n90 <= 0:
                    continue
                kpv = float(str(row.get("KP", 0)).replace(",", "") or 0)
            except (ValueError, TypeError):
                continue
            key = f"{name.split()[-1].lower()}|{str(row.get('Squad','')).strip()}"
            out.setdefault(key, {})
            out[key]["kp_90"] = round(kpv / n90, 2)

    return out


def main() -> int:
    app_url = os.environ.get("APP_URL", "").rstrip("/")
    token = os.environ.get("CRON_TOKEN", "")
    if not app_url or not token:
        print("Missing APP_URL or CRON_TOKEN env vars", file=sys.stderr)
        return 1

    data = scrape()
    n = len(data)
    print(f"Scraped shot/pass data for {n} players from FBRef")
    if n < 20:
        print("Too few players parsed — aborting so we don't overwrite good data",
              file=sys.stderr)
        return 1

    resp = requests.post(f"{app_url}/ingest/shots",
                         params={"token": token},
                         json={"players": data}, timeout=60)
    print("POST /ingest/shots ->", resp.status_code, resp.text[:300])
    resp.raise_for_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
