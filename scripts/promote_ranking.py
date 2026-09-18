#!/usr/bin/env python3
"""Promote a rankings_candidates row into the public rankings table.

Usage:
  python scripts/promote_ranking.py --candidate-id 42
"""

import argparse
import datetime as dt
import sys

from common import log, supabase_get, supabase_patch, supabase_post


def main():
    parser = argparse.ArgumentParser(description="Promote a ranking candidate after human review.")
    parser.add_argument("--candidate-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = supabase_get("rankings_candidates", f"id=eq.{args.candidate_id}&select=*")
    if not rows:
        log(f"  ! no candidate with id={args.candidate_id}")
        return 1
    c = rows[0]
    if c.get("promoted"):
        log(f"  · candidate {args.candidate_id} already promoted")
        return 0

    payload = {
        "slug": c["slug"],
        "name": c.get("name"),
        "rank_label": c.get("rank_label"),
        "description": c.get("description"),
        "trend": c.get("trend") or "flat",
        "category": c.get("category"),
        "source_publisher": c.get("source_publisher"),
        "last_verified_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }
    log(f"  promote {c['slug']}: {c.get('rank_label')} ({c.get('source_url')})")
    if args.dry_run:
        return 0

    written = supabase_post("rankings", [payload], on_conflict="slug", merge=True)
    if not written:
        log("  ! rankings upsert failed")
        return 1
    supabase_patch("rankings_candidates", f"id=eq.{args.candidate_id}", {"promoted": True})
    log("  ok.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
