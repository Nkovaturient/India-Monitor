#!/usr/bin/env python3
"""
India Monitor — ingest.py

Runs twice a day from GitHub Actions. Pulls from OFFICIAL sources only
(no newspaper scraping in this phase — see README):

  1. Press Information Bureau (PIB) English RSS feed
  2. Sensex / Nifty daily close, via Yahoo Finance (yfinance, no key needed)

Writes into Supabase using the service_role key (elevated — Actions-only,
never shipped to the browser). Every network call is wrapped so that one
source failing (blocked IP, timeout, schema change) does not kill the run —
we skip that source and keep going, and log clearly what was skipped.
"""

import os
import sys
import re
import json
import datetime as dt

import requests
import feedparser

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
}

# ----------------------------------------------------------------------
# Sources. PIB's RSS mechanism is ViewRss.aspx?reg=<region>&lang=<lang>.
# We only have reg=1 (English, general) confirmed working — add
# ministry-specific reg IDs here once you've confirmed them at
# https://www.pib.gov.in/RssMain.aspx, don't guess at undocumented ones.
# ----------------------------------------------------------------------
PIB_FEEDS = [
    {"name": "PIB · English releases", "url": "https://www.pib.gov.in/ViewRss.aspx?reg=1&lang=1"},
]

# Cheap, deterministic pre-filter — keeps the LLM call in synthesize.py
# small and cheap by only sending items that plausibly matter.
# This is intentionally an ALLOWLIST: education, governance/parliament,
# power & energy, and development/budget only. Everything else (sports,
# ceremonial greetings, entertainment, defence exercises, etc.) is dropped
# here, before it ever reaches the database.
KEYWORDS = [
    # education
    "education", "university", "ugc", "aicte", "ncte", "school", "student",
    "vidya", "shiksha", "skilling", "skill development", "curriculum",
    # governance / parliament / law
    "parliament", "lok sabha", "rajya sabha", "bill", "ordinance", "cabinet",
    "amendment", "constitution", "supreme court", "high court", "judiciary",
    "election commission", "rti", "governance",
    # power & energy
    "power", "electricity", "discom", "tariff", "grid", "renewable",
    "solar", "energy", "coal", "transmission",
    # development / budget / economy
    "budget", "fiscal", "gdp", "infrastructure", "rural development",
    "poverty", "employment", "welfare", "scheme", "yojana", "subsidy",
    "finance ministry", "taxation", "income tax",
]
KEYWORD_RE = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)


def log(msg):
    print(f"[{dt.datetime.utcnow().isoformat()}Z] {msg}", flush=True)


def is_relevant(title, description):
    text = f"{title} {description or ''}"
    return bool(KEYWORD_RE.search(text))


def supabase_upsert(table, rows, on_conflict):
    """Upsert rows into a Supabase table via PostgREST. Returns inserted count."""
    if not rows:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    headers = {**HEADERS, "Prefer": "resolution=ignore-duplicates,return=representation"}
    resp = requests.post(url, headers=headers, data=json.dumps(rows), timeout=30)
    if resp.status_code not in (200, 201):
        log(f"  ! upsert into {table} failed: {resp.status_code} {resp.text[:300]}")
        return 0
    try:
        return len(resp.json())
    except Exception:
        return 0


def ingest_pib():
    total_new = 0
    for feed in PIB_FEEDS:
        log(f"Fetching {feed['name']} ...")
        try:
            parsed = feedparser.parse(feed["url"])
        except Exception as e:
            log(f"  ! could not fetch {feed['name']}: {e}. Skipping this source.")
            continue

        if getattr(parsed, "bozo", 0) and not parsed.entries:
            log(f"  ! feed parse looked malformed and returned no entries. Skipping.")
            continue

        rows = []
        for entry in parsed.entries:
            title = getattr(entry, "title", "").strip()
            description = getattr(entry, "summary", "") or getattr(entry, "description", "")
            link = getattr(entry, "link", "")
            guid = getattr(entry, "id", None) or link
            if not title or not link:
                continue
            if not is_relevant(title, description):
                continue

            published_at = None
            if getattr(entry, "published_parsed", None):
                published_at = dt.datetime(*entry.published_parsed[:6], tzinfo=dt.timezone.utc).isoformat()

            rows.append({
                "source": feed["name"],
                "guid": guid,
                "title": title,
                "link": link,
                "description": (description or "")[:2000],
                "published_at": published_at,
            })

        log(f"  {len(parsed.entries)} entries seen, {len(rows)} passed the relevance filter.")
        inserted = supabase_upsert("raw_items", rows, on_conflict="guid")
        log(f"  {inserted} new rows written (duplicates by guid are silently skipped).")
        total_new += inserted
    return total_new


def ingest_markets():
    try:
        import yfinance as yf
    except ImportError:
        log("  ! yfinance not installed, skipping markets. Add it to requirements.txt.")
        return 0

    try:
        sensex = yf.Ticker("^BSESN").history(period="5d")
        nifty = yf.Ticker("^NSEI").history(period="5d")
    except Exception as e:
        log(f"  ! yfinance fetch failed: {e}. Skipping markets this run.")
        return 0

    if sensex.empty or nifty.empty:
        log("  ! yfinance returned no rows. Skipping markets this run.")
        return 0

    def latest_change(df):
        close = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2]) if len(df) > 1 else close
        pct = round((close - prev) / prev * 100, 2) if prev else 0.0
        date = df.index[-1].date().isoformat()
        return close, pct, date

    s_close, s_pct, s_date = latest_change(sensex)
    n_close, n_pct, n_date = latest_change(nifty)

    row = [{
        "snapshot_date": s_date,
        "sensex_close": round(s_close, 2),
        "sensex_change_pct": s_pct,
        "nifty_close": round(n_close, 2),
        "nifty_change_pct": n_pct,
        "note": "Auto-fetched via Yahoo Finance (yfinance). No commentary generated — figures only.",
    }]
    inserted = supabase_upsert("market_snapshots", row, on_conflict="snapshot_date")
    log(f"  Markets: {inserted} new snapshot row written.")
    return inserted


def main():
    log("=== India Monitor ingest run starting ===")
    new_news = ingest_pib()
    new_market = ingest_markets()
    log(f"=== Done. {new_news} new wire items, {new_market} new market rows. ===")


if __name__ == "__main__":
    main()
