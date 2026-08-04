# India Monitor

**Tagline:** Facts over fog — the civic signal India can trust.

**One-liner:** An agentic civic-intelligence system that surfaces only what matters for education, governance, power, and people’s development — with official sources, timelines of how each story got here, and end-to-end insight so Indians speak from data, not rumour or folktale.

**Current phase: 3**

```
GitHub Actions (cron, 2×/day)
        │
        ├─ ingest.py      → PIB + 4 newspapers + markets → raw_items, market_snapshots, pipeline_health
        └─ synthesize.py  → Claude (grounded in fetched text only) → stories, story_stations

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

### Phase 3 — Current
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

---

## Status by module

| Module | Status | Notes |
|---|---|---|
| Wire feed (PIB + newspapers) | **Automated**, 2×/day | Phase 3 |
| Market snapshot | **Automated**, 2×/day | Yahoo Finance |
| Story timelines | **Automated**, 2×/day | Grounded LLM; see § safeguards |
| Auto-tracked new stories | **Automated**, cautious | Only substantive bill/scheme/policy-style items |
| Pipeline health | **Automated**, 2×/day | Written by ingest; readable from DB/site |
| Bill tracker | **Seeded + schema ready** | History table exists; status still manual |
| Budget figures | **Seeded**, annual | PDF pipeline not built |
| Rankings | **Seeded** (18 indexes) | Candidates table ready; no auto-refresh job yet |
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

1. Create / use the Supabase project; run base schema + seeds, then Phase 3 migration + rankings seed.
2. Push to a **public** GitHub repo. Add Actions secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ANTHROPIC_API_KEY`.
3. Point Pages (or your host) at the static site; put **anon** URL + key in the frontend (never service_role).
4. Run **Refresh India Monitor data** once via `workflow_dispatch`. Cron: 06:00 and 18:00 IST.

---

## Forward ahead — not built yet

- **Bill tracker automation** — Digital Sansad / PRS scrape; wire status changes into `bill_status_history` (schema is ready) 

> PRS's Bill Track page is fully server-rendered plain HTML — no JavaScript needed, requests + a parser is enough. That closes out the Playwright question entirely for now, and I can see live statuses right in it (VBSA is "In Committee", Electricity Amendment is "Draft" — matches our seed data). Good sign the pipeline design is sound; something in the fetch layer is the likely culprit for "only 2 stories." I'll harden that and add observability so you're not flying blind next time.

- **Rankings refresh job** — periodic fetch into `rankings_candidates`; human promote to `rankings` [since world index rankings happens annually, we just need to run this fetch only on the day the rankings for the 18records are updated. for isntance, suppose, when the rankng for 'Henley passport index' is updated, then only refresh job runs strategically, timely (not blind shot). 
  - There are no fixed, permanent calendar dates for these releases, as the publishing organizations alter their exact launch dates each year based on data compilation timelines. However, a few specific reports target symbolic global events, such as **the World Happiness Report on March 20th and the World Press Freedom Index on May 3rd.**  ] Take help from these:-


While these reports apply globally, the updated metrics and rankings for India are disclosed simultaneously at the time of each global launch.

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


- **Budget PDF parsing** — yearly `indiabudget.gov.in` tables via something like `pdfplumber` → `budget_sectors`
- **Health on the UI** — surface `pipeline_health` as an honest “last updated / source failed” strip
- **Review gate (optional)** — staging table + approve path if auto-publish for stories becomes too loose
