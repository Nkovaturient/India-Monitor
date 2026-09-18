#!/usr/bin/env python3
"""
India Monitor — rankings_refresh.py

Calendar-aware check: only indexes whose release window includes today (or
the previous-month catch-up) are fetched. Proposed numeric changes land in
rankings_candidates with promoted=false. Never writes to `rankings`.
"""

import argparse
import datetime as dt
import sys
from urllib.parse import quote

from common import log, record_health, supabase_get, supabase_post
from rankings_fetchers import run_fetcher
from rankings_registry import INDEXES


def in_check_window(entry, today):
    months = entry.get("check_months") or []
    day = entry.get("check_day")
    if today.month in months:
        if day is None:
            return True
        try:
            target = dt.date(today.year, today.month, min(int(day), 28))
        except ValueError:
            return True
        return abs((today - target).days) <= 7
    prev_month = 12 if today.month == 1 else today.month - 1
    prev_year = today.year - 1 if today.month == 1 else today.year
    if prev_month in months and today.day <= 8:
        if day is None:
            return True
        try:
            target = dt.date(prev_year, prev_month, min(int(day), 28))
        except ValueError:
            return True
        return today >= target
    return False


def current_rankings():
    rows = supabase_get("rankings", "select=slug,name,rank_label,description,trend,category,source_publisher")
    return {r["slug"]: r for r in rows}


def already_open_candidate(slug, rank_label):
    rows = supabase_get(
        "rankings_candidates",
        f"slug=eq.{slug}&promoted=eq.false&rank_label=eq.{quote(rank_label, safe='')}&select=id&limit=1",
    )
    return bool(rows)


def write_candidate(entry, current, rank_label, description, stub=False):
    row = {
        "slug": entry["slug"],
        "name": entry["name"],
        "rank_label": rank_label,
        "description": description,
        "trend": (current or {}).get("trend") or "flat",
        "category": entry["category"],
        "source_publisher": entry["source_publisher"],
        "source_url": entry["source_url"],
        "promoted": False,
    }
    return supabase_post("rankings_candidates", [row])


def stub_description(current):
    current_label = (current or {}).get("rank_label") or "unknown"
    return (
        f"Check window open — manual verify. Live public page could not be parsed "
        f"confidently. Current published rank on the site: {current_label}."
    )


def process_entry(entry, current_map, dry_run):
    slug = entry["slug"]
    current = current_map.get(slug)
    health_src = f"Rankings · {slug}"
    parsed = None
    if entry.get("fetcher"):
        parsed = run_fetcher(entry)

    if parsed and parsed.get("rank_label"):
        label = parsed["rank_label"].strip()
        current_label = (current or {}).get("rank_label", "").strip()
        if label == current_label:
            log(f"  · {slug}: unchanged ({label})")
            if not dry_run:
                record_health(health_src, ok=True, items_seen=1, items_kept=0)
            return 0
        desc = (current or {}).get("description") or ""
        note = parsed.get("note") or "Proposed from public ranking page."
        description = f"{desc} ({note})" if desc else note
        log(f"  ~ {slug}: {current_label or '—'} → {label}")
        if dry_run:
            return 1
        if already_open_candidate(slug, label):
            log(f"  · {slug}: candidate already open")
            record_health(health_src, ok=True, items_seen=1, items_kept=0)
            return 0
        write_candidate(entry, current, label, description)
        record_health(health_src, ok=True, items_seen=1, items_kept=1)
        return 1

    # Stub — no false numeric claim
    label = (current or {}).get("rank_label") or "unverified"
    log(f"  · {slug}: stub candidate (manual verify)")
    if dry_run:
        return 1
    if already_open_candidate(slug, label):
        record_health(health_src, ok=True, items_seen=1, items_kept=0)
        return 0
    write_candidate(entry, current, label, stub_description(current), stub=True)
    record_health(health_src, ok=True, items_seen=1, items_kept=1)
    return 1


def main():
    parser = argparse.ArgumentParser(description="Check ranking indexes in their release windows.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--all", action="store_true", help="Ignore calendar window (still never auto-promote).")
    parser.add_argument("--slug", help="Only this ranking slug.")
    args = parser.parse_args()

    today = dt.date.today()
    due = []
    for entry in INDEXES:
        if args.slug and entry["slug"] != args.slug:
            continue
        if args.all or in_check_window(entry, today):
            due.append(entry)

    log(f"=== Rankings refresh {today.isoformat()} — {len(due)} index(es) in window ===")
    if not due:
        log("  nothing due today.")
        return 0

    current_map = {}
    if not args.dry_run:
        current_map = current_rankings()
    else:
        try:
            current_map = current_rankings()
        except Exception:
            log("  (dry-run) rankings table not reachable; comparing against empty set.")

    kept = 0
    for entry in due:
        try:
            kept += process_entry(entry, current_map, args.dry_run)
        except Exception as e:
            log(f"  ! {entry['slug']}: {e}")
            if not args.dry_run:
                record_health(f"Rankings · {entry['slug']}", ok=False, error=str(e))
    log(f"=== Done. {kept} candidate write(s). ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
