# Handoff

## Yesterday (state at start of 2026-08-04)

2026-08-03 (`69a5d6e`) added `tracker logic check`/`resolve` (three-layer
contradiction checking — deterministic rules, source-collision resolution via
the real `upsert.resolve_field`, an optional paid LLM pass), `tracker merge`
(the one destructive command, folding duplicate campus rows into one survivor),
`tracker point "<name>"` (on-demand single-project lookup/enrichment), the
per-project AI overview briefing, 20 new utility/contractor EDGAR filers, a
`capex --by-quarter` view, `tracker cloudflare` as its own command, and a
government-sources survey that concluded against building a bulk gov-data
ingest path. 1309 tests, green offline.

Carried into today, per that HANDOFF's own "Tomorrow": the 30-required-projects
gap, ERCOT/CAISO column names in `iso_maps.py` unverified against a real
export, the two free Google CSE keys not configured, the 20 new EDGAR utility
companies wired up but not yet run, `tracker logic check`'s paid layer-3 pass
unproven, `tracker point`/`tracker merge` heuristics validated against only a
handful of known cases, and `tracker cloudflare --name` untested against a real
named tunnel with DNS already pointed at it.

## Today (2026-08-04)

Working tree held a large batch of uncommitted work, already fully written up
in `CHANGELOG.md` and `README.md` before this handoff — the biggest addition
is a new sub-project data model (`capacity_block`) plus its backfill. Test
suite grew from 1309 to 1487 tests, verified green offline (exit 0) before
writing this file. Largest first:

- **`capacity_block`: what an AI data center actually is** (migration `0009`,
  `tracker/blocks.py`, new). `project` could only say one phase, one planned
  MW, one built MW, one customer — inadequate for a modern campus that is
  several of those at once (150 MW energized serving one buyer, 150 MW under
  construction pre-leased to another, 300 MW planned unnamed). Measured on the
  live DB: 28 projects partly built, 15 in `construction` with megawatts
  already live, 12 energized while construction was mid-track, 49 with a named
  customer and nothing built. A block carries its own label, MW, status
  (including a new `shell_complete` distinction and `energized` vs `serving`),
  customer and dates, each independently cited. Identity (`block_key`) is
  derived — "Phase 1"/"Phase I"/"first phase" fold together — never a
  similarity guess; an unplaceable block is excluded from rollup rather than
  guessed at. `reconcile` only ever raises a scalar, never lowers one, so
  migration 0009 landed on 227 live rows with every existing value provably
  unchanged. Verified end to end against Iron Mountain's Q1 2026 filing and VA-9's
  two differently-dated tranches. Not yet done: `logic` rules aren't
  re-expressed per block, and the read surfaces don't show blocks yet.

- **`tracker backfill blocks`** (`tracker/backfill.py`, new). Re-reads stored
  articles (keyed by URL, not source row — 373 crawled sources are only 229
  distinct articles) to populate blocks for the 227 pre-migration rows, without
  re-extracting scalars via `ingest crawl --force`. Two guards were added after
  an unguarded version wrote wrong numbers into a copy of the live DB: matching
  on locality as well as operator name (a disagreeing city is a veto, not a
  low score — an earlier pass put an 80 MW "Portland Expansion" onto eight
  unrelated STACK rows), and detecting portfolio articles before splitting
  blocks across sibling rows by label (an earlier pass gave Core Scientific's
  six blocks to both the Denton and Dalton rows, double-counting 588 MW).
  Verified on a nine-campus article that correctly routed Colossus/Stargate/
  Prometheus to their respective rows and dropped an unattributable block
  entirely.

- **`h200_equivalent`: capacity restated as accelerators** (migration `0008`,
  `tracker/compute.py`, new). Derived by default from MW at 1.3 kW/H200
  (~770/MW, built from the H200 board's 700W, DGX H200 node draw, and a
  liquid-cooled PUE of 1.15–1.25), tiered `derived` like `county`/coordinates.
  An article-stated chip count beats the conversion. A site nobody has sized
  stays null rather than zero, so it can't corrupt a sum. `TRACKER_KW_PER_H200`
  controls the ratio; `tracker init` re-bases the whole table. Deliberately
  not a thirteenth tracked field — "9 of 12" still holds.

- **A command box on the Commands page** (`tracker/webui/catalog.py`,
  `parse_command_line` / `build_argv`): a text line for what the forms can't
  express (`merge 4 7 9 --into 2`, repeated `--url`), parsed server-side into
  the same validated argv a form produces. Not a shell — `cd`, `rm`, `;`, `|`,
  backticks are refused by name. Tab completes, ↑ recalls, running from the box
  doesn't navigate away (would unmount its own history).

- **Routines in the console** (`tracker/webui/workflows.py`, new): four named
  multi-step sequences (*Catch up on the news*, *Deepen what we already have*,
  *Tidy the database*, *Prepare a report*) run as one job/one log/one run-history
  entry, validated against the same catalog and confirmation rules as a single
  command — a routine can't reach a blocked command or spend money silently.

- **`tracker point --url URL`** (repeatable): read a specific link instead of
  searching, going through the same `crawl.run`/evidence-gate/dedup/merge path.

- **Streaming for the written briefing** (`llm.MiniMaxExtractor.stream`,
  `overview.stream`, `POST /api/overview/stream`, `tracker/prompts/overview-v2.txt`,
  new): switched the fast-briefing model to `M2-her` (new
  `TRACKER_MINIMAX_FAST_MODEL` setting) after measuring every MiniMax model on
  time-to-first-visible-word — `M2-her` is the only one that doesn't emit a
  `<think>` block (2.7s vs 12.4–46.6s for the others; `MiniMax-M3`, the old
  default, returned nothing at all). Needed an `[[END]]` sentinel in the prompt
  (the API's own `stop` param is accepted and ignored) plus stream-level
  truncation (`overview.RUNAWAY`) and a token cap, since unbounded `M2-her`
  writes 750+ words against a 110-word instruction. Known cost, measured not
  assumed: `M2-her` occasionally misreads a track or invents a detail (e.g.
  inverting "construction is the last track to finish" into "all tracks
  complete") — acceptable because the briefing is labelled a model's reading,
  never stored, never a source, can't move confidence.

- **Fixed: `logic resolve` could destroy correct data to satisfy a coarse
  `phase` enum.** Three auto-edits (`_drop_energized`,
  `_set_phase_operational`, `_built_equals_planned`) were mostly the schema
  complaining about a state (a partly-built, partly-energized campus) that one
  `phase` value can't represent — not real contradictions. Two now offer no
  edit at all (accept/skip only); 73 of 148 findings can no longer be
  auto-edited. Verified: a full `logic resolve --auto --apply` now leaves all
  67 energization milestones intact. This is explicitly called out as stage 0
  of the real fix, which is `capacity_block` above.

- **Fixed: a restart couldn't dislodge a stale front end.** Static assets were
  served at bare, unversioned URLs with `no-cache`, so a browser or CDN edge
  could keep serving last week's JS after a restart with no visible symptom.
  `assets.stamp` now rewrites every `/static/...` reference with a token
  derived from the file's mtime+size, so a changed file is a different URL;
  matching-token requests get `immutable, max-age=1y`, stale/missing ones get
  `no-cache`.

- **`TRACKER_TUNNEL_NAME`/`TRACKER_TUNNEL_HOSTNAME`**: configure a permanent
  named-tunnel hostname once so `tracker cloudflare` needs no flags. Deliberately
  not inherited by an explicit `--name` (would print a real hostname next to
  the wrong tunnel); `serve --tunnel` reads the same pair.

- **Housekeeping**: `CHANGELOG.md` and `README.md` were already updated with
  detailed Added/Changed/Fixed entries for all of the above (verified, not
  duplicated here). `docs/what-we-built.zh-CN.md` got a matching section on
  the console's routines/command-box/AI-overview work.

- **`docs/feedback-2026-08-03.md`** (new, untracked): a written response to
  five colleague questions about the live data — duplicate-counted campuses
  (24,125 MW, e.g. Abilene stored under 4 different customer keys), the flat
  `announced` phase hiding real sub-progress, OpenAI's capex summing to an
  implausible $3.2T with no dedup/cap, a missing 2029 column in the capex year
  grid, placeholder values ("TBD"/"—") accepted as evidence, and whether
  government/permit/grid sources are actually used (four bulk routes tried,
  all failed; `ingest edgar --kind utility` is the one that pays and hasn't
  been run at scale). Explicitly "no code changes proposed yet — review
  first"; carries into tomorrow.

## Tomorrow

- Review `docs/feedback-2026-08-03.md` and decide which of its 5 items to act
  on — the suggested order is: run `tracker ingest edgar --kind utility`
  first (changes the source mix everything else audits), then `tracker merge`
  the obvious Stargate/duplicate clusters, then a placeholder-value vocabulary
  in `vocab.py`, then surface the 5-track progress bar next to `phase`, then
  (multi-day) the MW-by-share attribution join table.
- `capacity_block`'s `logic` rules aren't re-expressed per block yet, and no
  read surface (console, export, CLI `show`) displays blocks yet — the model
  exists but isn't visible or checked against contradictions at block
  granularity.
- Run `tracker backfill blocks` against the full 227 pre-migration rows (only
  verified so far on a sample); watch for the two guard conditions (locality
  veto, portfolio-article detection) misfiring on unseen article shapes.
- The 20 new utility/contractor EDGAR companies in `seed/edgar-companies.toml`
  are still wired up but unrun.
- The 30-required-projects gap, unverified ERCOT/CAISO column names, and the
  two unconfigured Google CSE keys are still open, carried for a second day.
- `tracker cloudflare --name`/`TRACKER_TUNNEL_HOSTNAME` still need a real run
  against a named tunnel with DNS actually pointed at it.
- `tracker point`'s 0.7 confidence floor and `tracker merge`'s duplicate
  detection remain validated against a small number of known cases.
- None of today's work is committed yet — it's a large uncommitted batch on
  `feat/web-console` (28 modified files, ~14 new files/migrations/tests) about
  to be committed alongside this handoff.
