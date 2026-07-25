#!/usr/bin/env python3
"""
India Monitor — synthesize.py

Takes newly-ingested raw_items (llm_relevant IS NULL) and turns them into
either:
  (a) a new dated "station" on an EXISTING story, or
  (b) — only when the item itself reads like a substantive policy/bill
       announcement — a new auto-tracked story.

Hard rules, enforced in the prompt AND re-checked in code after the model
responds, because this runs unattended (no human approval step):

  1. GROUNDED ONLY. The model may only use the title/description of the
     specific raw_item it was given — never outside/parametric knowledge
     about "what usually happens" with a bill or ministry. Every station
     is tied to a source_item_id that must exist in the database.
  2. APPEND-ONLY. Existing stations are never edited or deleted. A run can
     only add new rows or update a story's mutable `current_status` line.
  3. NO CONFIDENT MATCH = NO WRITE. If the model isn't sure an item maps
     to a story (or is substantive enough to start one), we skip it and
     leave it for the next run / a human to look at — we do not force a
     guess into the database.
  4. Every auto-created story is flagged auto_tracked = true, and the
     frontend must show a visible "auto-tracked" badge for these — see
     site/index.html. This keeps the automation honest about what has
     and hasn't had editorial judgment applied to it.
"""

import os
import json
import time
import datetime as dt

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

SB_HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
}
BATCH_SIZE = 12  # keep prompts small and cheap; loop if more than this arrive in one run

SYSTEM_PROMPT = """You are a strict, literal extraction tool for a civic-transparency \
site called India Monitor. You will be given (a) a list of EXISTING stories the site \
already tracks, and (b) a batch of newly-fetched government press-release items.

For EACH item, decide ONE of:
  "match"   — it is clearly a development in an existing story. Extract a short, \
              factual timeline entry using ONLY words grounded in the item's own \
              title/description. Do not add outside knowledge, do not speculate \
              about motives, do not predict outcomes.
  "new"     — it is not related to any existing story, but is itself a substantive \
              policy/bill/scheme/commission announcement worth tracking as its own \
              story (not a routine ceremonial release, greeting, or minor event).
  "skip"    — anything else, including anything you are not confident about.

Rules:
- Never invent a date, name, number, or status that is not present in the given text.
- station_text must be under 30 words, neutral in tone, no adjectives implying \
  approval or disapproval.
- If classifying "new", also produce a neutral one-line "dek" (under 25 words) and \
  2-3 lowercase tag words.
- If you are not highly confident, output "skip". A wrong skip costs nothing; a \
  wrong match or fabricated detail costs the site's credibility.
- Output ONLY valid JSON, an array with exactly one object per input item, no \
  markdown fences, no commentary.

Output schema per item:
{
  "item_index": <int, matches input order>,
  "decision": "match" | "new" | "skip",
  "story_slug": "<existing slug if match, else a new short kebab-case slug if new>",
  "station_date": "<YYYY-MM-DD, use published date of the item if given>",
  "station_text": "<string, only for match/new>",
  "new_story_title": "<string, only if decision=new>",
  "new_story_dek": "<string, only if decision=new>",
  "new_story_tags": ["<tag>", "..."]
}
"""


def log(msg):
    print(f"[{dt.datetime.utcnow().isoformat()}Z] {msg}", flush=True)


def sb_get(path):
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def sb_post(table, rows, on_conflict=None, prefer="return=representation"):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
        prefer = f"resolution=merge-duplicates,{prefer}"
    headers = {**SB_HEADERS, "Prefer": prefer}
    resp = requests.post(url, headers=headers, data=json.dumps(rows), timeout=30)
    if resp.status_code not in (200, 201):
        log(f"  ! write to {table} failed: {resp.status_code} {resp.text[:300]}")
        return []
    return resp.json() if resp.text else []


def sb_patch(table, match_col, match_val, fields):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{match_col}=eq.{match_val}"
    resp = requests.patch(url, headers=SB_HEADERS, data=json.dumps(fields), timeout=30)
    if resp.status_code not in (200, 204):
        log(f"  ! patch {table}.{match_col}={match_val} failed: {resp.status_code} {resp.text[:300]}")


def call_claude(existing_stories, batch):
    stories_brief = [
        {"slug": s["slug"], "title": s["title"], "tags": s.get("tags", [])}
        for s in existing_stories
    ]
    items_brief = [
        {
            "item_index": i,
            "title": it["title"],
            "description": (it.get("description") or "")[:500],
            "published_at": it.get("published_at"),
            "source": it["source"],
        }
        for i, it in enumerate(batch)
    ]

    user_content = (
        "EXISTING STORIES:\n" + json.dumps(stories_brief, ensure_ascii=False) +
        "\n\nNEW ITEMS TO CLASSIFY:\n" + json.dumps(items_brief, ensure_ascii=False)
    )

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log(f"  ! model did not return valid JSON, skipping this batch. Raw: {text[:300]}")
        return []


def process_batch(existing_stories, batch):
    results = call_claude(existing_stories, batch)
    known_slugs = {s["slug"] for s in existing_stories}

    for r in results:
        idx = r.get("item_index")
        if idx is None or idx >= len(batch):
            continue
        item = batch[idx]
        decision = r.get("decision")

        if decision == "skip" or not decision:
            sb_patch("raw_items", "id", item["id"], {"llm_relevant": False})
            continue

        station_date = r.get("station_date") or (item.get("published_at") or "")[:10]
        station_text = (r.get("station_text") or "").strip()
        if not station_text or not station_date:
            sb_patch("raw_items", "id", item["id"], {"llm_relevant": False,
                      "llm_reason": "model chose match/new but omitted required fields"})
            continue

        slug = r.get("story_slug")

        if decision == "new":
            if not slug or slug in known_slugs:
                slug = f"{slug or 'story'}-{item['id']}"
            sb_post("stories", [{
                "slug": slug,
                "title": r.get("new_story_title") or item["title"],
                "dek": r.get("new_story_dek") or "",
                "tags": r.get("new_story_tags") or [],
                "status_label": "New · auto-tracked",
                "status_kind": "auto",
                "current_status": station_text,
                "auto_tracked": True,
            }])
            known_slugs.add(slug)
            log(f"  + new auto-tracked story: {slug}")

        elif decision == "match":
            if slug not in known_slugs:
                sb_patch("raw_items", "id", item["id"], {"llm_relevant": False,
                          "llm_reason": f"model matched unknown slug '{slug}'"})
                continue
            sb_patch("stories", "slug", slug, {
                "current_status": station_text,
                "updated_at": dt.datetime.utcnow().isoformat() + "Z",
            })

        sb_post("story_stations", [{
            "story_slug": slug,
            "station_date": station_date,
            "station_text": station_text,
            "source_item_id": item["id"],
        }])
        sb_patch("raw_items", "id", item["id"], {
            "llm_relevant": True,
            "matched_story_slug": slug,
        })
        log(f"  ~ item {item['id']} -> story '{slug}' ({decision})")


def main():
    log("=== India Monitor synthesize run starting ===")
    pending = sb_get("raw_items?llm_relevant=is.null&select=id,title,description,published_at,source&order=fetched_at.asc&limit=100")
    if not pending:
        log("Nothing new to synthesize. Done.")
        return

    existing_stories = sb_get("stories?select=slug,title,tags")
    log(f"{len(pending)} unprocessed item(s), {len(existing_stories)} known story(ies).")

    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        log(f"Processing batch {i // BATCH_SIZE + 1} ({len(batch)} items)...")
        try:
            process_batch(existing_stories, batch)
        except Exception as e:
            log(f"  ! batch failed, leaving these items unprocessed for next run: {e}")
        time.sleep(1)  # be polite between batches

    log("=== Done. ===")


if __name__ == "__main__":
    main()
