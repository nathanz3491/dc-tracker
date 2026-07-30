# Handoff

First-time HANDOFF.md — no prior version existed to diff against, so "Yesterday"
below summarizes end-of-day 2026-07-30 from commit history rather than a prior
handoff note.

## Yesterday (state at start of 2026-07-31)

2026-07-30 was the whole v1 build in one day (`270bead` through `ea4f4a7`, ~24
commits): schema + migrations, `tracker/normalize.py` and `tracker/confidence.py`,
the manual/PJM/crawl ingest paths, MiniMax LLM extraction with the evidence gate,
feed discovery and `tracker sync`, sitemap-based `--deep` backfill, Google CSE
`tracker search`, Census-derived `tracker ingest geo`, the `risk` table replacing
the free-text `blocker` column, `tracker risks`/`tracker exposure`, and schedule
slippage tracking. 579 tests, green offline. README and CHANGELOG were kept
current commit-by-commit throughout.

Open items carried into today, per README "Known gaps": the 30 required projects
from the PRD's definition of done are not populated; ERCOT/CAISO column names in
`iso_maps.py` are unverified assumptions; `project.country` is a dead column.

## Today (2026-07-31)

- **Added `tracker enrich ID`** (`9226d1c`): inverts `tracker sync`'s
  spread-a-budget-across-the-database model into recruit-every-method-for-one-project.
  Six harvesters run cheapest-and-most-certain-first (derive → queue → retry →
  archive → search → refresh), looping until a round fills nothing new. Verified
  live on project #93 (OpenAI Stargate, Abilene): round 1 filled `county` and
  lifted confidence 2→3 on a third independent domain, round 2 found nothing and
  stopped. `gaps.py` gained `for_project()` and `geo.run()` gained
  `only_project_id` so the single-project command can't rewrite the whole table.
  568 new test lines.
- **Housekeeping**: README's "Known gaps" still claimed the crawl path had only
  been run against fixtures, never live — stale since `de4821c`'s live-run defect
  fixes on 07-30, and doubly so after today's live enrich verification. Corrected
  in [README.md](README.md) and noted in [CHANGELOG.md](CHANGELOG.md).

## Tomorrow

- The 30-required-projects gap is still open; `tracker verify` measures it and
  `tracker discover`/`tracker enrich` now both supply candidates to close it.
- `tracker enrich`'s own measurement found 17 of 94 projects have unread archive
  articles and 77 have none — the two free Google CSE keys are what would lift
  that ceiling; not yet configured.
- ERCOT/CAISO column-name assumptions in `iso_maps.py` remain unverified against
  a real export.
- No other in-progress or blocked work was left open at end of day.
