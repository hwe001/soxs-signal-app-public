#!/usr/bin/env python3
"""Insider-buying feed for the tiger_daily dashboard. Display only.

Reads the qualifying Form 4 dataset produced by the alpaca-trading-desk
insider collector. Source order (first that works wins):

1. Public rolling snapshot `insider_latest.json` in this repo on GitHub —
   published daily by the collector's workflow; no token needed, works on
   Streamlit Community Cloud.
2. Private collector repo via GitHub API — only if INSIDER_REPO_TOKEN is
   set (fine-grained PAT, Contents read-only).
3. Local sibling clone ../alpaca-trading-desk — offline fallback, only as
   fresh as the last `git pull`.

Whatever the source, the result carries explicit freshness metadata so
the dashboard can never silently show stale data. Context signal, not a
timing signal: qualifying insider buys play out over weeks. Nothing here
feeds order sizing or band logic.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
from typing import Any

import requests

PUBLIC_SNAPSHOT_URL = (
    "https://raw.githubusercontent.com/hwe001/soxs-signal-app-public/main/insider_latest.json"
)
PRIVATE_REPO = "hwe001/alpaca-trading-desk"
PRIVATE_API_BASE = f"https://api.github.com/repos/{PRIVATE_REPO}/contents/data/insider"
LOCAL_DIR = pathlib.Path(__file__).resolve().parent.parent / "alpaca-trading-desk" / "data" / "insider"
LOOKBACK_DAYS = 10
STALE_AFTER_DAYS = 4  # weekend + one holiday of tolerance before we call it stale

# Highlighted because a cluster buy in a semiconductor name is direct
# context for the short-SOXS sleeve. Coarse list, owner-editable.
SEMI_TICKERS = {
    "NVDA", "AMD", "AVGO", "TSM", "INTC", "MU", "QCOM", "TXN", "AMAT", "LRCX",
    "KLAC", "ASML", "MRVL", "ON", "NXPI", "ADI", "MCHP", "SWKS", "QRVO", "TER",
    "ENTG", "SNPS", "CDNS", "ARM", "SMCI", "WDC", "STX", "GFS", "MPWR", "FSLR",
}


def _wanted_dates(today: dt.date, lookback: int = LOOKBACK_DAYS) -> list[str]:
    return [(today - dt.timedelta(days=i)).isoformat() for i in range(lookback + 1)]


def _fetch_public_snapshot(today: dt.date) -> tuple[list[dict[str, Any]], str] | None:
    response = requests.get(PUBLIC_SNAPSHOT_URL, timeout=15)
    if response.status_code == 404:
        return None  # snapshot not published yet
    response.raise_for_status()
    payload = response.json()
    wanted = set(_wanted_dates(today))
    records = [
        r for r in payload.get("qualifying", [])
        if str(r.get("filed_at", ""))[:10] in wanted
    ]
    return records, payload.get("through", "")


def _fetch_private_api(today: dt.date) -> tuple[list[dict[str, Any]], str] | None:
    token = os.getenv("INSIDER_REPO_TOKEN")
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    listing = requests.get(PRIVATE_API_BASE, headers=headers, timeout=15)
    listing.raise_for_status()
    available = {item["name"] for item in listing.json() if item["name"].endswith(".json")}
    records: list[dict[str, Any]] = []
    latest = ""
    for date in _wanted_dates(today):
        name = f"{date}.json"
        if name not in available:
            continue
        raw = requests.get(
            f"{PRIVATE_API_BASE}/{name}",
            headers={**headers, "Accept": "application/vnd.github.raw+json"},
            timeout=15,
        )
        raw.raise_for_status()
        payload = raw.json() if isinstance(raw.json(), dict) else json.loads(raw.text)
        latest = max(latest, payload.get("date", ""))
        records.extend(payload.get("qualifying", []))
    return records, latest


def _fetch_local(today: dt.date) -> tuple[list[dict[str, Any]], str] | None:
    if not LOCAL_DIR.exists():
        return None
    records: list[dict[str, Any]] = []
    latest = ""
    for date in _wanted_dates(today):
        path = LOCAL_DIR / f"{date}.json"
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        latest = max(latest, payload.get("date", ""))
        records.extend(payload.get("qualifying", []))
    return records, latest


def rank_key(record: dict[str, Any]) -> tuple[int, float]:
    """Clusters first, then CEO/CFO buys, then the rest; big money first."""
    if record.get("cluster_id"):
        tier = 0
    elif record.get("is_ceo_cfo"):
        tier = 1
    else:
        tier = 2
    return (tier, -float(record.get("dollar_value", 0)))


def is_stale(latest_date: str, today: dt.date, tolerance_days: int = STALE_AFTER_DAYS) -> bool:
    if not latest_date:
        return True
    try:
        latest = dt.date.fromisoformat(latest_date)
    except ValueError:
        return True
    return (today - latest).days > tolerance_days


def load_insider_feed(today: dt.date | None = None) -> dict[str, Any]:
    """Fetch, rank, and annotate the last LOOKBACK_DAYS of qualifying buys.

    Returns {records, latest_date, source, stale, error}; `records` are
    sorted by rank_key and carry an `is_semi` flag for theme overlap.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    records: list[dict[str, Any]] = []
    latest = ""
    source = "none"
    errors: list[str] = []

    fetchers = (
        ("public snapshot", _fetch_public_snapshot),
        ("private repo API", _fetch_private_api),
        ("local clone (git pull for fresh data)", _fetch_local),
    )
    for name, fetcher in fetchers:
        try:
            got = fetcher(today)
        except requests.RequestException as exc:
            errors.append(f"{name}: {exc}")
            continue
        if got is not None:
            records, latest = got
            source = name
            break

    records = sorted(records, key=rank_key)
    for record in records:
        record["is_semi"] = record.get("ticker", "").upper() in SEMI_TICKERS

    return {
        "records": records,
        "latest_date": latest,
        "source": source,
        "stale": is_stale(latest, today),
        "error": "; ".join(errors) if errors and source == "none" else None,
    }
