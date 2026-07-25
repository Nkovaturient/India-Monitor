# India Monitor — Phase 2 (live feed)

A civic-literacy dashboard that ingests official Indian government sources
twice a day, extracts grounded "how did this get here" story timelines with
an LLM, and serves it all from a free static site + free Postgres backend.
No servers to run, no paid tier required at this scale.

```
GitHub Actions (cron, 2x/day)
        │
        ├─ ingest.py       → PIB RSS + market data → Supabase (raw_items, market_snapshots)
        └─ synthesize.py   → Claude, grounded only in fetched text → Supabase (stories, story_stations)

Supabase (Postgres + auto REST API + Row Level Security)
        │  anon key, read-only, safe to embed in the browser
        ▼
GitHub Pages (site/index.html) → queries Supabase directly on page load
```

The key property this gives you: **updates appear on the live site the
moment the Action finishes** — there's no "rebuild the site" step, because
the frontend reads Supabase at request time rather than from files baked
into the repo.

---

## 1. One-time setup (about 15 minutes)

### a) Create the database

### b) Wire up the repo

<!--
1. Push this folder to a new **public** GitHub repo (public = free unlimited
   Actions minutes; private repos get a monthly minute cap on the free plan).
2. Repo → Settings → Secrets and variables → Actions → New repository secret.
   Add:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `ANTHROPIC_API_KEY` (from [console.anthropic.com](https://console.anthropic.com))
3. Repo → Settings → Pages → Deploy from branch → `main` → `/site`.
4. Open `site/index.html`, replace the two placeholder constants at the top
   of the `<script>` tag with your real `SUPABASE_URL` and
   `SUPABASE_ANON_KEY` (the **anon** key, never the service_role key), commit.
5. Repo → Actions tab → run "Refresh India Monitor data" once manually
   (`workflow_dispatch`) to confirm it's wired correctly before waiting for
   the cron.

--> 

That's it — from here, `.github/workflows/refresh.yml` runs on its own
twice a day (06:00 and 18:00 IST) and the Pages site reflects it live.

---

## 2. What's automated vs. what isn't yet

Being upfront about this, since "real feed" can mean different things:

| Module | Status | Why |
|---|---|---|
| Wire feed (raw items) | **Automated**, 2x/day | PIB's public English RSS feed |
| Market snapshot | **Automated**, 2x/day | Yahoo Finance, free, no key |
| Story timelines | **Automated**, 2x/day | LLM extraction, grounded in that run's fetched PIB items only |
| Auto-tracked new stories | **Automated**, cautious | Only created when an item itself reads like a substantive bill/scheme/policy announcement — see `synthesize.py` docstring |
| Bill tracker (Parliament tab) | **Seeded, manual refresh** | Digital Sansad has no public API found; would need browser automation (Playwright) against a JS-rendered site — a real Phase 3 project, not a twice-daily cron job |
| Budget figures | **Seeded, annual refresh** | The Budget is published once a year, as PDFs — parsing `indiabudget.gov.in` documents automatically is a separate, lower-frequency pipeline (see Phase 3) |
| Rankings | **Seeded, occasional refresh** | Indexes (HDI, GII, etc.) update a few times a year, not daily |
| Constitution cards | **Curated, static by design** | Deliberately never LLM-authored — see the child prompt's reasoning below |
| The 4 newspapers (TOI/Hindu/HT/IE) | **Not wired in this phase** | hold off until reliability from a cloud IP is confirmed — see Phase 3 |

## 3. On the "auto-publish, no human review" choice

Currently, direct auto-publish rather than a PR-approval gate, and that's
what's built. Since nothing here gets a human look before it's live, the
pipeline leans on **structural** safeguards instead of a review step:

- **Grounding, not generation.** The LLM prompt in `synthesize.py` only ever
  sees the title/description of the specific item it's classifying — never
  "what usually happens with bills like this." If it can't point to text
  that supports a claim, it's instructed to output `skip`, not a guess.
- **Append-only history.** `story_stations` rows are never edited or deleted
  by the pipeline — a wrong entry could still get added, but yesterday's
  entries can't quietly get rewritten.
- **Every station cites its source row.** `source_item_id` traces back to
  the exact PIB release, so anything wrong is checkable and correctable —
  add a row to `story_stations` manually, or delete a bad one, directly in
  the Supabase table editor at any time; nothing here is one-way.
- **Auto-tracked stories are labelled, visibly**, so a reader can tell the
  difference between the two curated flagship stories and something the
  pipeline opened on its own.

If you ever want to add the PR-review step back in, the change is small:
have `synthesize.py` write to a `staging_stations` table instead of
`story_stations`, and add a second workflow that opens a PR (or a Slack
message, or an email) summarizing pending items for a one-click approve.

## 4. Cost, at this scale

- **Supabase free tier**: 500MB DB, 1GB storage, 5GB egress, 2 projects.
  This project uses a tiny fraction of that. Free-tier projects pause after
  7 days with *zero database activity* — irrelevant here, since the Action
  writes every 12 hours and resets that clock automatically.
- **GitHub Actions**: free and unlimited on public repos for scheduled
  workflows; this job runs a couple of minutes, twice a day.
- **Anthropic API**: the only real line item. Each run batches up to 12
  items per Claude call (Sonnet), so a typical day costs a few cents, not
  dollars — and if `ingest.py`'s keyword filter finds nothing relevant in a
  given run, `synthesize.py` has nothing to process and doesn't call the
  API at all.

## 5. Phase 3 ideas, not built yet

- **Newspapers**: once you've confirmed `timesofindia.indiatimes.com`,
  `thehindu.com`, `hindustantimes.com`, and `indianexpress.com`'s RSS feeds
  actually respond to a GitHub Actions IP (test with a one-off
  `workflow_dispatch` run first — cheap to find out), add them to
  `PIB_FEEDS`-style entries in `ingest.py`. Keep the "paraphrase, don't
  reproduce, one short quote max per source" rule from the prototype if any
  headline/snippet text ever reaches the UI directly.
- **Bill tracker automation**: Digital Sansad appears to be a JS-rendered
  SPA with no public API — would need Playwright running headless in the
  Action (slower, heavier, more fragile than RSS) or a manual weekly
  10-minute update. Worth prototyping separately before wiring into the
  2x/day job.
- **Budget PDF parsing**: `indiabudget.gov.in` publishes structured PDFs
  once a year — `pdfplumber` can likely extract the ministry-wise tables
  directly, removing the need to hand-seed `budget_sectors` next February.
- **A `/api/health` style check**: have `ingest.py` write a one-row
  `pipeline_health` table (last run time, sources that failed) so the site
  can show "last updated" honestly instead of assuming the cron worked.
