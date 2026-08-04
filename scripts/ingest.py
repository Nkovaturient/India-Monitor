#!/usr/bin/env python3
"""
India Monitor — ingest.py  (Phase 3)

Runs twice a day from GitHub Actions. Pulls:
  1. Press Information Bureau (PIB) English RSS
  2. The 4 newspapers (Times of India, The Hindu, Hindustan Times, Indian
     Express) — added in Phase 3. Each is independent: if one blocks or
     changes its feed, the others and PIB still go through.
  3. Sensex / Nifty daily close, via Yahoo Finance (yfinance, no key needed)

Phase 3 hardening: let feedparser make the HTTP request itself, with
feedparser's default user-agent — several government/news sites block or
silently return empty/blocked responses to non-browser-looking requests.
This version fetches with realistic headers via `requests` first, and only
hands the raw bytes to feedparser afterward, so failures are visible
(status code, response snippet) instead of silently becoming "0 entries".

Every source's outcome — success or failure, item counts, error text —
gets written to `pipeline_health` so you can see what happened without
reading Action logs.
"""

import os
import re
import json
import time
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

# A real browser UA. Several .gov.in and newspaper sites block the default
# `python-requests/x.x` / `feedparser/x.x` UAs outright.
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

FEEDS = [
    {"name": "PIB · English releases", "url": "https://www.pib.gov.in/ViewRss.aspx?reg=1&lang=1"},
    {"name": "Times of India · Top Stories", "url": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"},
    {"name": "The Hindu · National", "url": "https://www.thehindu.com/news/national/feeder/default.rss"},
    {"name": "Hindustan Times · Top News", "url": "https://www.hindustantimes.com/rss/topnews/rssfeed.xml"},
    {"name": "Indian Express · India", "url": "https://indianexpress.com/section/india/feed/"},
]

# Cheap, deterministic pre-filter — intentionally an ALLOWLIST: education,
# governance/parliament, power & energy, and development/budget only.
# Everything else (sports, entertainment, crime blotter, celebrity news —
# newspapers' general feeds carry all of it) is dropped here, before it
# ever reaches the database or costs an LLM call in synthesize.py.
KEYWORDS = [
    "education", "university", "ugc", "aicte", "ncte", "school", "student",
    "vidya", "shiksha", "skilling", "skill development", "curriculum",
    "parliament", "lok sabha", "rajya sabha", "bill", "ordinance", "cabinet",
    "amendment", "constitution", "supreme court", "high court", "judiciary",
    "election commission", "rti", "governance",
    "power", "electricity", "discom", "tariff", "grid", "renewable",
    "solar", "energy", "coal", "transmission",
    "budget", "fiscal", "gdp", "infrastructure", "rural development",
    "poverty", "employment", "welfare", "scheme", "yojana", "subsidy",
    "finance ministry", "taxation", "income tax",
]
KEYWORD_RE = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)

# Topic tags for wire-feed tabs — scored by keyword hits; highest score wins.
TOPIC_RULES = [
    ("education", [
        "education", "university", "ugc", "aicte", "ncte", "school", "student",
        "vidya", "shiksha", "skilling", "skill development", "curriculum", "neet", "mbbs",
    ]),
    ("health", [
        "health", "hospital", "medical", "healthcare", "doctor", "pharma", "disease",
        "vaccine", "ayushman", "nha", "who", "patient", "clinical",
    ]),
    ("energy", [
        "power", "electricity", "discom", "tariff", "grid", "renewable", "solar",
        "energy", "coal", "transmission", "ntpc", "petroleum",
    ]),
    ("governance", [
        "parliament", "lok sabha", "rajya sabha", "bill", "ordinance", "cabinet",
        "amendment", "constitution", "supreme court", "high court", "judiciary",
        "election commission", "rti", "governance", "cbi", "court",
    ]),
    ("economy", [
        "budget", "fiscal", "gdp", "infrastructure", "rural development", "poverty",
        "employment", "welfare", "scheme", "yojana", "subsidy", "finance ministry",
        "taxation", "income tax", "wto", "trade", "market", "investment", "export",
    ]),
]


def log(msg):
    print(f"[{dt.datetime.utcnow().isoformat()}Z] {msg}", flush=True)


def is_relevant(title, description):
    text = f"{title} {description or ''}"
    return bool(KEYWORD_RE.search(text))


def classify_topic(title, description):
    text = f"{title} {description or ''}".lower()
    scores = {}
    for topic, keywords in TOPIC_RULES:
        for kw in keywords:
            if kw in text:
                scores[topic] = scores.get(topic, 0) + 1
    if not scores:
        return "general"
    return max(scores, key=scores.get)


def supabase_post(table, rows, on_conflict=None, prefer_extra=""):
    if not rows:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    prefer = "return=representation"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
        prefer = f"resolution=ignore-duplicates,{prefer}"
    if prefer_extra:
        prefer = f"{prefer_extra},{prefer}"
    headers = {**HEADERS, "Prefer": prefer}
    resp = requests.post(url, headers=headers, data=json.dumps(rows), timeout=30)
    if resp.status_code not in (200, 201):
        log(f"  ! write to {table} failed: {resp.status_code} {resp.text[:300]}")
        return 0
    try:
        return len(resp.json())
    except Exception:
        return 0


def record_health(source, ok, items_seen=0, items_kept=0, error=None):
    supabase_post("pipeline_health", [{
        "source": source, "ok": ok, "items_seen": items_seen,
        "items_kept": items_kept, "error": (error or "")[:500],
    }])


def fetch_feed_content(url, attempts=2):
    """Fetch raw bytes with real headers + retries. Returns bytes or raises."""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers=FETCH_HEADERS, timeout=20)
            if resp.status_code == 200 and resp.content:
                return resp.content
            last_err = f"HTTP {resp.status_code}, {len(resp.content)} bytes"
        except Exception as e:
            last_err = str(e)
        if attempt < attempts:
            time.sleep(2 * attempt)
    raise RuntimeError(last_err or "unknown fetch failure")


def ingest_feeds():
    total_new = 0
    for feed in FEEDS:
        name, url = feed["name"], feed["url"]
        log(f"Fetching {name} ...")
        try:
            content = fetch_feed_content(url)
        except Exception as e:
            log(f"  ! could not fetch {name}: {e}. Skipping this source.")
            record_health(name, ok=False, error=str(e))
            continue

        parsed = feedparser.parse(content)
        if not parsed.entries:
            reason = "0 entries parsed — feed may have changed structure or returned an error page"
            log(f"  ! {name}: {reason}")
            record_health(name, ok=False, error=reason)
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
                "source": name, "guid": guid, "title": title, "link": link,
                "description": (description or "")[:2000], "published_at": published_at,
                "topic": classify_topic(title, description),
            })

        log(f"  {len(parsed.entries)} entries seen, {len(rows)} passed the relevance filter.")
        inserted = supabase_post("raw_items", rows, on_conflict="guid")
        log(f"  {inserted} new rows written (duplicates by guid skipped).")
        record_health(name, ok=True, items_seen=len(parsed.entries), items_kept=len(rows))
        total_new += inserted
    return total_new


def ingest_markets():
    try:
        import yfinance as yf
    except ImportError:
        log("  ! yfinance not installed, skipping markets.")
        record_health("Markets · Yahoo Finance", ok=False, error="yfinance not installed")
        return 0
    try:
        sensex = yf.Ticker("^BSESN").history(period="5d")
        nifty = yf.Ticker("^NSEI").history(period="5d")
    except Exception as e:
        log(f"  ! yfinance fetch failed: {e}. Skipping markets this run.")
        record_health("Markets · Yahoo Finance", ok=False, error=str(e))
        return 0
    if sensex.empty or nifty.empty:
        log("  ! yfinance returned no rows. Skipping markets this run.")
        record_health("Markets · Yahoo Finance", ok=False, error="empty dataframe returned")
        return 0

    def latest_change(df):
        close = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2]) if len(df) > 1 else close
        pct = round((close - prev) / prev * 100, 2) if prev else 0.0
        return close, pct, df.index[-1].date().isoformat()

    s_close, s_pct, s_date = latest_change(sensex)
    n_close, n_pct, n_date = latest_change(nifty)
    row = [{
        "snapshot_date": s_date, "sensex_close": round(s_close, 2), "sensex_change_pct": s_pct,
        "nifty_close": round(n_close, 2), "nifty_change_pct": n_pct,
        "note": "Auto-fetched via Yahoo Finance (yfinance). No commentary generated — figures only.",
    }]
    inserted = supabase_post("market_snapshots", row, on_conflict="snapshot_date")
    log(f"  Markets: {inserted} new snapshot row written.")
    record_health("Markets · Yahoo Finance", ok=True, items_seen=1, items_kept=inserted)
    return inserted


def main():
    log("=== India Monitor ingest run starting (Phase 3) ===")
    new_news = ingest_feeds()
    new_market = ingest_markets()
    log(f"=== Done. {new_news} new wire items, {new_market} new market rows. "
        f"Check the `pipeline_health` table (or the site's Markets/News tabs) "
        f"if any source shows 0 — it logs exactly why. ===")


if __name__ == "__main__":
    main()
