# India Monitor

**Tagline:** Facts over fog — the civic signal India can trust.

**One-liner:** An agentic civic-intelligence system that surfaces only what matters for education, governance, power, and people’s development — with official sources, timelines of how each story got here, and end-to-end insight so Indians speak from data, not rumour or folktale.

**Current phase: 4**

```
GitHub Actions
        │
        ├─ refresh.yml (2×/day)
        │     ingest.py       → PIB + 4 newspapers + markets → raw_items, market_snapshots, pipeline_health
        │     track_bills.py  → PRS Bill Track → bills, bill_status_history
        │     synthesize.py   → Claude (grounded in fetched text only) → stories, story_stations
        ├─ rankings-refresh.yml (monthly, 1st)
        │     rankings_refresh.py → rankings_candidates (never auto-writes rankings)
        └─ budget-refresh.yml (2 Feb reminder / manual)
              budget_parse.py → budget_facts, budget_sectors (no-op unless BUDGET_PDF_URL is set)

Supabase (Postgres + REST + RLS)
        │  anon key, read-only in the browser
        ▼
Static site (index.html) → queries Supabase on load
```

---

## Phases

### Phase 1 — Foundation
Schema, seed data, static UI. Manual / curated content for stories, bills, budget, rankings, and constitution cards.

### Phase 2 — Live feed
Twice-daily automation: PIB RSS + Yahoo Finance markets → Supabase; LLM synthesis into grounded story stations. Site reads live from the DB (no rebuild step).

### Phase 3 — Live expansion
What shipped on top of Phase 2:

| Change | Detail |
|---|---|
| Newspaper feeds | TOI, The Hindu, HT, Indian Express RSS in `scripts/ingest.py`, each isolated (one failure does not stop the others) |
| Fetch hardening | Browser-like headers + retries via `requests`; parse with `feedparser` on raw bytes so HTTP failures are visible |
| Relevance allowlist | Education / governance / energy / budget keywords — drops sports, crime blotter, celebrity noise before DB or LLM |
| `pipeline_health` | Per-source ok/fail, counts, error text (`migration_phase3.sql`) |
| Bills provenance | `detail_url`, `category`, `prs_slug`, `last_checked_at` + append-only `bill_status_history` |
| Rankings expansion | Categories + publishers; 9 Phase-2 rows backfilled + 9 new indexes (`seed_phase3_rankings.sql`) |
| Rankings staging | `rankings_candidates` — proposed updates, not public-read; human promote only |
| Wire topic tags | `raw_items.topic` on ingest + UI tabs (High signal, Governance, Education, Health, Economy, Energy) |
| Rankings UI filters | Category tabs + glass grid cards (Development, Climate, Economy, etc.) |

✅ Applied Phase 3 DB changes in order: `supabase/migration_phase3.sql`, then `supabase/seed_phase3_rankings.sql`, then `supabase/migration_phase3_topic.sql`.

### Phase 4 — Current
No new tables. Reuses Phase 3 schema.

| Change | Detail |
|---|---|
| Shared helpers | `scripts/common.py` — HTTP + Supabase + `pipeline_health` writes used by all jobs |
| Health strip | Latest per-source ok/fail on the site chrome (`loadPipelineHealth`) |
| Bill tracker | `track_bills.py` scrapes PRS HTML; appends `bill_status_history` only on change; backfill slugs via `supabase/seed_bill_prs_slugs.sql` |
| Rankings refresh | Monthly calendar job; real fetchers for passport / happiness / press / CPI / GHI / GII / AQI; others get a **stub candidate** (no invented rank). Promote with `python scripts/promote_ranking.py --candidate-id N` |
| Budget PDF | `budget_parse.py` upserts matched `budget_facts` / `budget_sectors` slugs; skips entirely if `BUDGET_PDF_URL` is unset |

Done and Ran `supabase/seed_bill_prs_slugs.sql` once in the SQL editor so existing bills have `prs_slug` values.

---

## Status by module

| Module | Status | Notes |
|---|---|---|
| Wire feed (PIB + newspapers) | **Automated**, 2×/day | Phase 3 |
| Market snapshot | **Automated**, 2×/day | Yahoo Finance |
| Story timelines | **Automated**, 2×/day | Grounded LLM; see § safeguards |
| Auto-tracked new stories | **Automated**, cautious | Only substantive bill/scheme/policy-style items |
| Pipeline health | **Automated + UI** | Strip under the header; written by ingest / bills / rankings / budget |
| Bill tracker | **Automated**, 2×/day | PRS scrape; history append-only; Digital Sansad still a manual cross-check |
| Budget figures | **Seeded + yearly parser** | Set `BUDGET_PDF_URL` (e.g. `https://www.indiabudget.gov.in/doc/eb/vol1.pdf`) and run the budget workflow |
| Rankings | **Candidates automated**, public ranks human-promoted | 18 indexes; calendar windows in `scripts/rankings_registry.py` |
| Constitution cards | **Curated**, static by design | Never LLM-authored |
| Newspaper → UI quotes | **Paraphrase / cite carefully** | Prefer links; short quotes only if shown |

---

## Safeguards (auto-publish)

No human gate before news/story writes. Structural controls instead:

- **Grounding** — `synthesize.py` only sees that item’s title/description; unsure → `skip`
- **Append-only** — `story_stations` (and `bill_status_history`) are never rewritten by the pipeline
- **Cite the source row** — stations carry `source_item_id` back to the wire item
- **Label auto-tracked stories** — distinct from curated flagships
- **Rankings differ** — numeric rank claims go to `rankings_candidates` first, not straight to `rankings`

Optional later: write stations to a staging table and approve via PR / Slack.

---

## Cost (this scale)

- **Supabase free tier** — well within limits; cron writes keep the project active
- **GitHub Actions** — free on public repos for this short 2×/day job
- **Anthropic** — main cost; skipped entirely when ingest finds nothing relevant

---

## Setup (one-time)

1. Create / use the Supabase project; run base schema + seeds, then Phase 3 migration + rankings seed, then `supabase/seed_bill_prs_slugs.sql`.
2. Push to a **public** GitHub repo. Add Actions secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ANTHROPIC_API_KEY`. Optional: `BUDGET_PDF_URL` for the yearly budget job.
3. Point Pages (or your host) at the static site; put **anon** URL + key in the frontend (never service_role).
4. Run **Refresh India Monitor data** once via `workflow_dispatch`. Cron: 06:00 and 18:00 IST.
5. Rankings: after a monthly run, inspect `rankings_candidates` and promote with `python scripts/promote_ranking.py --candidate-id N` (needs the same Supabase env vars).

Dry-run locally (no writes):

```bash
python scripts/track_bills.py --dry-run
python scripts/rankings_refresh.py --dry-run --all
python scripts/budget_parse.py --dry-run --url https://www.indiabudget.gov.in/doc/eb/vol1.pdf
```

---

## Forward ahead — not built yet

- **Digital Sansad scrape** — PRS covers status well enough for now; Sansad remains a linked cross-check, not a second parser.
- **Review gate (optional)** — staging table + approve path if auto-publish for stories becomes too loose.

Rankings release calendar (used by `rankings_registry.py`; publishers shift dates year to year):

| #  | Index / Report Name                  | Publishing Organization                          | Usual Release Timeline                          |
|----|--------------------------------------|--------------------------------------------------|-------------------------------------------------|
| 1  | World Air Quality Report (PM2.5)     | IQAir                                            | March (Annually)                                |
| 2  | Climate Change Performance Index     | Germanwatch / NewClimate Institute               | November / December (Annually)                  |
| 3  | Corruption Perceptions Index         | Transparency International                       | January / February (Annually)                   |
| 4  | Environmental Performance Index      | Yale / Columbia University                       | June / July (Biennially / Every 2 years)        |
| 5  | Global Firepower Index               | Global Firepower                                 | January (Annually)                              |
| 6  | GDP size (nominal)                   | IMF / World Bank                                 | April & October (Updated twice a year)          |
| 7  | Global Gender Gap Index              | World Economic Forum (WEF)                       | June / July (Annually)                          |
| 8  | Global Hunger Index                  | Concern Worldwide / Welthungerhilfe              | October (Annually)                              |
| 9  | Global Innovation Index              | WIPO (UN)                                        | September (Annually)                            |
| 10 | Gender Inequality Index              | UNDP                                             | March (Released with the HDR report)            |
| 11 | Global Peace Index                   | Institute for Economics and Peace (IEP)          | June (Annually)                                 |
| 12 | World Happiness Report               | UN Sustainable Development Solutions Network     | March 20 (International Day of Happiness)       |
| 13 | Human Development Index              | UNDP                                             | March / December (Every 1 to 2 years)           |
| 14 | Logistics Performance Index          | World Bank                                       | Variable (Every 2 to 3 years)                   |
| 15 | Henley Passport Index                | Henley & Partners                                | Monthly / Quarterly (Continuous updates)        |
| 16 | World Press Freedom Index            | Reporters Without Borders (RSF)                  | May 3 (World Press Freedom Day)                 |
| 17 | Rule of Law Index                    | World Justice Project (WJP)                      | October (Annually)                              |
| 18 | Global Soft Power Index              | Brand Finance                                    | February / March (Annually)                     |
