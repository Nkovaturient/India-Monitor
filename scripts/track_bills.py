#!/usr/bin/env python3
"""
India Monitor — track_bills.py

Scrapes PRS Bill Track (server-rendered HTML) and, for each seeded bill
with a prs_slug, appends a bill_status_history row when the status changes
and patches bills.stage / stage_label / last_checked_at / detail_url.

Never rewrites history. Use --dry-run to print diffs without writing.
"""

import argparse
import datetime as dt
import re
import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from common import fetch_url, log, record_health, supabase_get, supabase_patch, supabase_post

PRS_INDEX = "https://prsindia.org/billtrack"
PRS_BASE = "https://prsindia.org"

STAGE_MAP = [
    (re.compile(r"pass(ed)?|assent|enacted|became law", re.I), "pass", "Passed"),
    (re.compile(r"joint (parliamentary )?committee|\bjpc\b|standing committee|in committee", re.I), "jpc", "In Committee"),
    (re.compile(r"draft|consult", re.I), "wait", "Draft · consulting"),
    (re.compile(r"introduced|pending|listed|lok sabha|rajya sabha", re.I), "new", "Introduced"),
]


def now_iso():
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def map_stage(status_text):
    text = (status_text or "").strip()
    if not text:
        return None
    for rx, stage, default_label in STAGE_MAP:
        if rx.search(text):
            label = text if len(text) <= 48 else default_label
            return stage, label
    return "new", text[:48]


def slug_from_href(href):
    if not href:
        return None
    path = href.split("?")[0].rstrip("/")
    if "/billtrack/" not in path:
        return None
    slug = path.split("/billtrack/")[-1].strip("/")
    if not slug or slug in ("", "prs-products"):
        return None
    if slug.startswith("prs-"):
        return None
    return slug


def parse_prs_index(html):
    soup = BeautifulSoup(html, "lxml")
    found = {}
    for a in soup.select('a[href*="/billtrack/"]'):
        href = a.get("href") or ""
        slug = slug_from_href(href)
        if not slug:
            continue
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 8:
            continue
        parent = a.find_parent(["article", "li", "tr", "div"]) or a.parent
        blob = parent.get_text(" ", strip=True) if parent else title
        status = None
        for label in ("Status", "Stage", "Introduced", "In Committee", "Passed", "Draft"):
            m = re.search(rf"{label}\s*[:\-–]?\s*([A-Za-z][A-Za-z /()]{{2,40}})", blob)
            if m:
                status = m.group(1).strip()
                break
        if not status:
            for cand in ("In Committee", "Passed", "Introduced", "Draft", "Pending"):
                if re.search(rf"\b{re.escape(cand)}\b", blob, re.I):
                    status = cand
                    break
        url = urljoin(PRS_BASE, href)
        prev = found.get(slug)
        if prev and prev.get("status") and not status:
            continue
        found[slug] = {"slug": slug, "title": title, "status": status, "url": url}
    return found


def latest_history_label(bill_id):
    rows = supabase_get(
        "bill_status_history",
        f"bill_id=eq.{bill_id}&select=status_label,changed_at&order=changed_at.desc&limit=1",
    )
    return (rows[0].get("status_label") if rows else None)


def main():
    parser = argparse.ArgumentParser(description="Refresh bill statuses from PRS.")
    parser.add_argument("--dry-run", action="store_true", help="Print diffs; do not write.")
    args = parser.parse_args()

    log("=== Bill tracker (PRS) starting ===")
    try:
        html = fetch_url(PRS_INDEX).decode("utf-8", errors="replace")
    except Exception as e:
        log(f"  ! could not fetch PRS index: {e}")
        if not args.dry_run:
            record_health("Bills · PRS", ok=False, error=str(e))
        return 1

    listings = parse_prs_index(html)
    log(f"  parsed {len(listings)} bill listings from PRS.")

    bills = []
    try:
        query = "prs_slug=not.is.null&select=*" if not args.dry_run else "select=*"
        bills = supabase_get("bills", query)
    except Exception as e:
        if not args.dry_run:
            raise
        log(f"  (dry-run) no Supabase bills ({e}) — showing PRS sample only.")
        for item in list(listings.values())[:8]:
            log(f"    {item['slug']}: {item['status'] or '—'} · {item['title'][:70]}")
        return 0

    if args.dry_run and not bills:
        log("  (dry-run) no Supabase bills — showing PRS sample only.")
        for item in list(listings.values())[:8]:
            log(f"    {item['slug']}: {item['status'] or '—'} · {item['title'][:70]}")
        return 0

    seen = 0
    changed = 0
    for bill in bills:
        slug = bill.get("prs_slug")
        if not slug:
            continue
        seen += 1
        listing = listings.get(slug)
        if not listing:
            log(f"  · {bill.get('name')}: not on PRS index (slug={slug})")
            if not args.dry_run:
                supabase_patch("bills", f"id=eq.{bill['id']}", {
                    "last_checked_at": now_iso(),
                })
            continue
        mapped = map_stage(listing.get("status") or "")
        if not mapped:
            log(f"  · {bill.get('name')}: no status text on PRS card")
            continue
        stage, label = mapped
        prev_label = bill.get("stage_label")
        if not args.dry_run:
            hist = latest_history_label(bill["id"])
            if hist:
                prev_label = hist
        if prev_label and prev_label.strip().lower() == label.strip().lower() and bill.get("stage") == stage:
            log(f"  · {bill.get('name')}: unchanged ({label})")
            if not args.dry_run:
                supabase_patch("bills", f"id=eq.{bill['id']}", {
                    "last_checked_at": now_iso(),
                    "detail_url": listing["url"],
                })
            continue
        log(f"  ~ {bill.get('name')}: {prev_label or '—'} → {label}")
        changed += 1
        if args.dry_run:
            continue
        supabase_post("bill_status_history", [{
            "bill_id": bill["id"],
            "status": stage,
            "status_label": label,
            "source": "PRS Legislative Research",
        }])
        supabase_patch("bills", f"id=eq.{bill['id']}", {
            "stage": stage,
            "stage_label": label,
            "detail_url": listing["url"],
            "last_checked_at": now_iso(),
        })

    log(f"=== Done. {seen} tracked, {changed} status changes. ===")
    if seen == 0:
        log("  no bills with prs_slug set — run supabase/seed_bill_prs_slugs.sql in the SQL editor.")
    if not args.dry_run:
        record_health("Bills · PRS", ok=True, items_seen=seen, items_kept=changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
