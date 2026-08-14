# Handoff

## Yesterday (state at start of 2026-08-15)

The 2026-08-14 run (`f078818`) closed out seven unrecorded 2026-08-13 commits —
source governance and the repair pass — and found nothing new landed in its own
window: housekeeping only, suite verified green. Carried in unresolved: the
2026-08-12 live-database purge (1,189 → 300 projects, still unexplained, two days
old at that point), `tracker logic conflicts` never run against a real key,
`work/` (a teammate's Hyperion review material, its PowerPoint lock file still
present) and the untracked reference PDF in `docs/`, both unchanged since
2026-08-12 and 2026-08-06 respectively.

Sitting in the working tree, uncommitted and unmentioned in any prior handoff:
`tracker enrich` had gained a fourth stage. `tracker/conflicts.py` — shipped
2026-08-13 as the standalone `tracker logic conflicts` command, one model
comparing every quote-backed claim about a contested field — was now also wired
directly into the end of `enrich`'s own loop (`tracker/ingest/enrich.py`,
`tracker/cli.py`), a `--skip-settle` flag, five new tests
(`tests/test_enrich.py`), and a `deepseek_reasoning_model` default change
(`tracker/config.py`, `.env.example`). Not yet in CHANGELOG.md or README.md.

## Today (2026-08-15 run)

- **Found and committed the settle-step work described above.** `enrich`'s last
  stage now sends every field still contested on a project — not only ones this
  run's own harvesting touched — through `conflicts.disputes`/`solve`, the same
  logic `tracker logic conflicts` exposes standalone, and applies a resolution
  the same way `--apply` does: losing claims marked `superseded`, the row
  re-derived, never a value the citations don't state. `--skip-settle` opts out;
  `--dry-run` covers it like every other stage; a missing reasoning-model API key
  degrades to a skipped stage (recorded in `report.skipped`) rather than losing
  the articles the run already paid to fetch and read. `deepseek_reasoning_model`'s
  default moved to `deepseek-v4-pro` (was `deepseek-v4-flash`) alongside it —
  `infer` and this new step are both low-volume, one call per project or per
  contested field, so the heavier model is affordable there specifically, unlike
  on the extraction path that reads every article.
- Verified the suite green before committing: full `.venv` pytest run completed
  with no failures (exit 0), including the five new tests in `test_enrich.py`
  covering the settle step itself, a refusal, `--dry-run`, `--skip-settle`, and
  that a settled field is never asked about twice. Ruff clean.
- **Added the CHANGELOG.md and README.md entries this work hadn't gotten yet.**
  Every other feature commit in this project's history lands its docs in the
  same change; this one, sitting uncommitted, hadn't — housekeeping this run
  closed that gap rather than leaving it for the commit that finally lands the
  code. No `docs/architecture.md` change needed: it describes the CLI/database/
  console split at a level this doesn't move. No AGENTS.md exists, still
  correctly so — one CLI/data-pipeline codebase, no distinct agent roles.
- **Did not re-run the live-database measurement commands** (`audit`, `risks`,
  `duplicates`, `blocks`, `logic check`, `sources`, `feeds`) the Tomorrow list
  below depends on — this run's scope was accounting for and committing the
  settle-step work sitting in the tree, not re-measuring the database. Treat
  every count below carried from 2026-08-14 as still that old.
- `work/` and the untracked reference PDF in `docs/` are unchanged since the
  last run; the PowerPoint lock file in `work/` is still present. The
  2026-08-12 purge remains unresolved, now three days old — see Tomorrow.

## Yesterday (state at start of 2026-08-14)

The last handoff (`4d97d15`, committed 2026-08-13 05:29) reported "no new
commits landed during this run's window" — true at that moment, checked
against `git log`. The day continued anyway: seven more commits landed
between 13:08 and 23:37 that same day (2026-08-13), none recorded until this
run. Two threads, both closing out the 2026-08-12 review's backlog:

1. **Source governance, both halves** (13:08–14:55). `62f0093` measured the
   review's eight proposed fixes against the live database and found four
   premises didn't match the code: the extractor already reads full
   articles (median 7,368 of a 24,000-char limit), `phase` is model-judged
   and quote-gated rather than keyword-matched, `source_type` is a
   deterministic host classifier, and subdividing `government_doc` can't
   fix the Hyperion $10B defect because both conflicting sources are the
   same publisher — what actually separates them is a publication date.
   `dc35a52`/`bc221c6` built that: dates read from raw HTML before
   conversion discards them (JSON-LD → `article:published_time` → `<time>`
   → URL path), `tracker backfill dates` for the 2,432 citations stored
   before the reader existed (11.8% → 67.6% coverage), `tracker sources`
   ranking publishers by contested wins earned rather than a hand-set 1–10
   weight (Data Center Frontier and DataCenterDynamics, weight 2,
   out-decide almost every weight-3 host), `tracker feeds` proposing new
   feeds from robots.txt/sitemaps, and a per-run ledger at
   `data/runs/ingest.jsonl`. `merge_by_publication_date` stays off —
   capturing dates and moving stored values are separate decisions.
   `4bfe11b`/`b444117` did the converging half: `tracker feeds` also
   proposes which feeds to *retire*, on whether a feed's queued URLs ever
   backed a stored value rather than on volume — the obvious "found most,
   used least" metric would have flagged DataCenterDynamics, which
   `seed/feeds.toml` keeps on purpose. Bounded honestly: 90% of wasted calls
   come from URLs no feed found, so this reaches about a tenth of the 49%.
2. **The repair pass that reaches rows already stored** (23:36–23:37).
   `66fc81c`/`9f86dae` shipped `tracker backfill derive` — a value is a
   function of its citations, but the function was only ever *applied* at
   ingest, so six months of merge/evidence-gate/block-rollup fixes never
   reached a stored project (`enrich` only adds sources; `init` stops after
   the row it just wrote). Re-derives 322 values across 213 of 300 live
   projects — 81 of them blockers resolved but never cleared — and a
   second run moves nothing, which is the test. It found a real bug within
   a minute: confidence was scored against the pre-derivation blocker,
   since `blocker` counts toward the tracked-field total and was derived
   after the score; now computed last. Also landed: generation MW filed
   apart from campus capacity (a utility's gas/solar was inflating
   Hyperion's delivered number), `blocker` explaining its own choice via
   the function that writes it, the date tiebreak measured and
   deliberately left off (it would fix 65 values and get 22 of 40 numeric
   ones wrong — a date can't see scope), and `tracker logic conflicts` —
   the one path where a model compares contradicting sentences instead of
   sorting them by crawl order (492 fields qualify; refusing is a
   first-class answer; `--apply` marks losing claims `superseded`, never
   assigns a value the citations don't state). The two-console split rode
   along in the same commit (shared `app.js`/CHANGELOG/README lines).

Unresolved, carried in unchanged: **the live database still shows 300
projects**, down from 1,189 on 2026-08-12, still with no commit, script or
note explaining it — only the backup file as evidence. None of the seven
commits above touch project counts. `work/` (the teammate's Hyperion review
material) and the untracked reference PDF in `docs/` are both unchanged
since the last run.

## Today (2026-08-14 run)

No new commits landed during this run's window. Housekeeping only:

- Verified the suite green after the backfill pass: `.venv` pytest exit 0
  (2,103 collected in this run, in the same range as the 2,073 the commits
  themselves reported), ruff clean.
- CHANGELOG.md and README.md were updated inside the seven commits
  themselves (backfill derive, logic conflicts, dates, sources, feeds) —
  this run found no drift needing a separate edit. `docs/architecture.md`
  still needs none: it describes the CLI/database/console split at a level
  none of this touched. No AGENTS.md exists, still correctly so.
- The live-database purge (1,189 → 300 projects, first flagged in
  yesterday's handoff) is now two days unresolved, unaffected by anything
  in this run — see Tomorrow.
- `work/` and the untracked reference PDF in `docs/` are unchanged since
  the last run; see Tomorrow for updated day counts.

## Older context (state at start of 2026-08-13)

The prior handoff (`4d97d15`, committed 2026-08-13 05:29) closed out
fourteen unrecorded commits from 2026-08-11–12 — a favicon/logo fix, the
MiniMax-to-DeepSeek provider migration, an industry-context prompt block,
split reasoning-effort knobs, and the six-commit Hyperion audit response
(`tracker clean`, the gas-plant/reconcile fix, ten more Hyperion defects, a
drawer rebuild, `logic.free_answer`) — and flagged the live-database purge
(1,189 → 300 projects) as needing a person rather than a repair. That run's
own window saw no new commits. The day continued anyway: seven more commits
landed 13:08–23:37 the same day — accounted for above, under this run's
"Yesterday."

## Older context, continued (2026-08-13, catching up 08-11–12)

The last handoff entry (`4e485f5`, committed 2026-08-11 05:22) reported "no
work done this day" — true at the moment it ran, checked against `git log`
on this branch and four others. The day continued anyway: fourteen more
commits landed between 10:27 that same morning and 12:20 the next day
(2026-08-12), none recorded in any handoff until this run. Two threads:

1. **A logo/favicon commit and a provider migration** (10:27–13:53,
   2026-08-11): `8730a0d` replaced a placeholder favicon with an inline
   citation-bracket mark; `7ec24d6` merged it in; then `f1f6b64` switched
   every model call from MiniMax to DeepSeek (MiniMax silently ignored
   `thinking`/`reasoning_effort`, so the drawer's briefing had been running
   on the one model in the roster that couldn't deliberate, and it
   measurably read rows wrong); `06b575a` prepended a new
   `prompts/_industry.txt` background block to all eleven prompts, giving
   the model dimensional grounding ("how big is a data center") that no
   per-prompt rule had ever supplied; `383faab` turned reasoning on for
   extraction and `tracker infer` (off for the drawer's briefing); `80e645c`
   split one shared effort knob into `TRACKER_DEEPSEEK_EXTRACTION_EFFORT`
   and `TRACKER_DEEPSEEK_INFER_EFFORT` so `infer` (one call per project) can
   run at `max` without extraction (one call per article) paying `max`
   thousands of times over.
2. **A teammate's audit of Hyperion (#10), our best-sourced row (61
   sources), and the whole response to it** (23:35 2026-08-11 through 12:20
   2026-08-12, six commits merged as `33ac124`): `b346054` closed the gap
   that let a settled audit finding stay "settled" after a cache rebuild
   silently reverted the repair; `a33bdfe` shipped `tracker clean` — four
   tiers (SOURCED/SOUND/COMPLETE/SETTLED) composed from existing detectors,
   calibrated against a reference row rather than asserted, snapshotted to
   `data/runs/clean.jsonl`; `4e19ded` stopped a utility's gas plants and
   solar farms from being counted as campus capacity, and stopped
   `reconcile` overwriting a cited figure with a tranche sum that doesn't
   partition the campus; `f5423c1` fixed the other ten Hyperion defects
   (future milestones read as reached, a parish stored in `city`,
   cross-granularity duplicates invisible to the bucket-by-locality scan,
   no way to mark a superseded claim); `957bc2b` rebuilt the drawer's claim
   table, timeline and track cards around real provenance and deleted a 3D
   campus schematic that drew a wrong number from bad data; `7e544b9`
   shipped `logic.free_answer` (285 findings answered by comparison alone,
   no model call) and two console workflows split by whether they need a
   money confirmation.

Net effect on Hyperion, verified live: `mw_planned` 14,462 → 5,000 MW,
`phase` operational → construction, `city` "Richland Parish" → null,
`power` energized/complete → blocked (naming the risk), 72 events → 8
milestones. Corpus-wide `quote_backed` share 0.645 → 0.739.

CHANGELOG.md and README.md were updated inside these commits themselves
(the clean-tier section, the DeepSeek provider notes, the claim-table and
`tracker clean` docs) — this run found no drift needing a separate edit.
`docs/architecture.md` still needs none: it describes the CLI/database/console
split at a level none of these fourteen commits changed. No AGENTS.md
exists, still correctly so — one CLI/data-pipeline codebase, no distinct
agent roles in the software itself (the commits' `Co-Authored-By: Claude
Opus 5` trailers describe who wrote the code, not a runtime agent
architecture).

## Older context, continued (2026-08-13 run: housekeeping and the purge finding)

No new commits landed during that run's window — a housekeeping pass,
accounting for the fourteen-commit stretch above and re-verifying the
suite: 1,931 tests green (`.venv` pytest, exit 0), up from 1,884 recorded
2026-08-10 — and ruff clean.

**Found, not caused, by this run: the live database lost 889 of 1,189
projects on 2026-08-12.** `7e544b9`'s commit message notes the project
count "went from 1,189 to 300... for reasons outside this work" without
saying what the reason was; nothing in any of the fourteen commits touches
row counts, and no script in `tracker/` or `scripts/` deletes from
`project`. The evidence is a backup file, `data/tracker.backup-before-purge-20260812.db`
(1,189 rows, written 10:41) sitting two minutes before `data/tracker.db`
(300 rows, written 10:43) — a deliberate, backed-up operation, run by
hand, between `a33bdfe`'s commit (10:40) and `4e19ded`'s (10:47), with no
commit, script, or doc recording what it purged or why. Every historical
count in this file above that point (1,189 projects, 27 audit findings,
118/305 obstacles, etc.) describes a database that no longer exists in
this form. See Tomorrow — this needs a person, not a repair.

Two pieces of untracked material found sitting in the working tree, from
the same Hyperion review, neither touched:

- **`work/`** (new since 2026-08-12): a teammate's whiteboard write-up
  (`extraction-pipeline-problem-and-approach.md`) reconciling a proposed
  "one central LLM with search/web-fetch harness" architecture against what
  the code already does, plus four `.pptx` review decks and two `.pdf`
  diagrams built from it. One deck (`~$Hyperion-Tracker-Review-FINAL.pptx`)
  carries a live PowerPoint lock file, meaning it may still be open/being
  edited elsewhere. Review material, not project output — left uncommitted
  pending a person's call on whether any of it belongs in `docs/`.
- **The untracked reference PDF in `docs/`** — unchanged since 2026-08-06,
  now eight days carried (see Tomorrow).

## Older context (state at start of 2026-08-10)

The prior handoff (`218f453`, committed 2026-08-09 05:37) accounted for
`5908c68` (the milestone evidence gate, `built_capacity_uncited_in_blocks`,
`tracker enrich --all`) and, further back, `cd2d462`'s unrecorded merge-time
`ImportError` fix.

The day didn't end there. Eight more commits landed between 11:36 and 23:51
the same day — on this branch and via a merge from
`claude/data-source-coordination-analysis-66de67` — none recorded until this
run: `tracker enrich` reaching past the corpora `sync` already drains via
Wikipedia reference mining and a host-blocklist fix (`3e5e20f`, merged
`ed0bd54`); naming which search backend actually answered, after finding the
pinned one doesn't index this trade press at all (`0170c22`); two ordering
defects that made `tracker enrich N` decline a row before starting
(`f7715782`); an escalation ladder past WAFs that block the TLS handshake
rather than the crawler (`0b93988`); a plan document measuring what 26-source
projects do to `logic check` and to block identity (`f41f70e`); `tracker
blocks`, a free report finding one tranche wearing several names
(`d0ce423`); a positional fix to the floor/ceiling hedge-word reader
(`3c30e0a`); and the tranche panel rewrite from provenance ledger to site
plan (`f6ec88c`). That's what "Today" below accounts for.

## Older context (state at start of 2026-08-09)

The prior handoff (`e9be28a`, committed 2026-08-08) accounted for `b18ba5a`
(the claim envelope and quality measurement) and, via a merge, for `95a18ab`
on `claude/audit-duplicates-cli-ui-44227f` (`audit resolve`, `duplicates
park`, `risks confirm`, `queue check`/`prune`, `tracker infer`'s panel). That
merge (`8162c1c`) landed the same day, 13:09.

The day didn't end there either. At 17:26 one more commit landed —
`cd2d462`, "Name the failure when the source moves under a running
console" — fixing a production crash: the console reads its Python once at
startup but its migrations and static files from disk on every request, so
merging under a running instance split it in two, and the first request to
touch a module not yet imported (`tracker.capex`, importing `tracker.pairs`,
importing `NotDuplicate`) hit an `ImportError` against a `tracker.models`
that had been in memory since the previous evening. Never recorded in a
handoff until now; none of the backlog below was touched by it.

Past that point the session kept going but stopped committing. This run
found a second day's more work sitting uncommitted in the working tree —
CHANGELOG.md and README.md already updated in detail, 1788 tests green,
ruff clean, but nothing staged. That work is what "Today" below accounts
for. Carried in unchanged: everything the 2026-08-08 backlog listed below
except the one item today's session resolved (`events[]` and the evidence
gate — see Today).

## Older context (state at start of 2026-08-08)

The prior handoff (`b47dca4`, committed 2026-08-07 05:33) closed out ten
commits — `tracker audit`, the placeholder-citation fixes, and the
evidence-quote gate — and left a backlog: six placeholder-plan decisions
needing a person, 5 deferred merge candidates, 16 projects with no blocks
read, two untriaged block signals, `docs/feedback-2026-08-03.md` unreviewed,
20 unrun EDGAR utility/contractor companies, the 30-required-projects gap,
unverified ERCOT/CAISO column names, two unconfigured Google CSE keys, an
untested cloudflare tunnel, and an untracked 2 MB reference PDF sitting in
`docs/`.

The day didn't end at that handoff. Later the same day (2026-08-07, sometime
between 21:43 and 23:03) one more commit landed — `b18ba5a` — reading two
source-abundant rows (Fairwater #1, Hyperion #10) closely enough to find a
schema-level gap rather than just fix those two rows: every claim arrives with
a scope, a precision, a modality, a time, and a provenance, and the schema has
one scalar per field, so all four extra dimensions were stripped at write
time. That commit shipped the fix, a real measurement of the evidence gate's
actual defect rate, and several bug fixes — updating CHANGELOG.md and
README.md itself in the process — but never got its own HANDOFF entry. That's
what "Today" below accounts for. None of the backlog above was touched by it,
except that the mutant-harness reproducibility gap it also carried is now
closed.

## Older context (state at start of 2026-08-07)

The prior handoff (`f805bf5`, committed 2026-08-06 05:33) closed out `be16d37`
— durable merges (`project_alias`), capex quadruple-counting and
never-confirmed-figure fixes, a `--json` crash fix, a continuous year grid,
click-to-drill-down on every capex figure, and the digit-free buyer overview
— and left the session there for the day. It wasn't: the same day, 15:32–21:43,
ten more commits landed on this branch and on two Claude worktrees that later
merged into it, none of which made it into a handoff until now. That's what
"Today" below actually covers, despite the commit timestamps reading
2026-08-06 — this run is the first to account for them.

Carried in from further back and still true at the top of that unrecorded
stretch: the 5 deferred merge candidates in `docs/merge-review-2026-08-05.md`,
16 projects with no blocks read yet, two untriaged block signals
(`blocks_may_double_count`, `block_label_ambiguous`), `docs/feedback-2026-08-03.md`
unreviewed, the 20 EDGAR utility/contractor companies unrun, the
30-required-projects gap, unverified ERCOT/CAISO column names, two unconfigured
Google CSE keys, and `tracker cloudflare --name` untested against a real
tunnel.

## Older context (state at start of 2026-08-06)

The prior handoff (`124a562`, committed 2026-08-05) closed out the tail of the
2026-08-04 session — nine commits landing `capacity_block`, backfilling it
against live data, and surfacing blocks everywhere (`tracker show`, JSON export,
console drawer). Its "Tomorrow" flagged: the backfill still incomplete (16
projects with no blocks read yet), two new report-only signals
(`blocks_may_double_count`, `block_label_ambiguous`) untriaged, and
`docs/feedback-2026-08-03.md` still unreviewed.

The session didn't stop at that handoff either. Three more commits landed the
same evening (17:13–21:08), fixing the accounting problem the block work had
exposed: 70 of 118 itemised projects showed tranches summing to *less* than
the campus total, because unconfirmed capacity was quietly excluded rather than
shown. `blocks.account` now places every megawatt on exactly one line —
counted, or a residual with a reason (`unconfirmed`, `unplaceable`,
`unitemised`, `overlap`, `out_of_scale`) — and asserts the lines close. The
console's tranche table was rebuilt around that: a stacked bar whose full width
*is* the closure statement, labelled columns, rows ordered by progress then
size. A follow-up commit caught a tranche 250x its own campus total (kilowatts
misread as megawatts) that the arithmetic had been silently absorbing into a
meaningless "counted twice over -35,988 MW" line, and added a WAS DUE marker
for a passed date on a tranche that isn't running.

Past that point the session kept going but stopped committing. This run found
a full day's more work sitting uncommitted in the working tree — CHANGELOG.md
and README.md already updated in detail, 1578 tests green, but nothing staged.
Carried in unchanged from further back: the 30-required-projects gap,
unverified ERCOT/CAISO column names, two unconfigured Google CSE keys, the 20
utility/contractor EDGAR companies from `seed/edgar-companies.toml` still
wired up but unrun, and `tracker cloudflare --name` untested against a real
named tunnel.

## Older context, continued (2026-08-06 05:33 run)

No new commits landed during that run's window; the working tree was already
clean of code changes, just uncommitted. What follows is that run's first
accounting of that uncommitted work — one session, four independent pieces,
committed together as `be16d37` since none of it had been committed before.
Verified 1578 tests green offline first.

- **`project_alias` makes a merge outlive the next crawl** (migration `0010`,
  `tracker/merge.py`, `tracker/upsert.py`). `upsert_record` matches on exact
  `dedup_key` only, so folding the Abilene campus's four company-angled copies
  into one row used to last exactly until the next article written from one
  of the folded angles re-created it. `merge` now writes a durable alias per
  folded identity; `upsert_record` consults it after an exact-key miss and
  routes to the survivor. Aliases repoint when their own target is later
  merged, so chains stay flat. `--force-new` still bypasses it, and deleting a
  survivor cascades its aliases away.

- **21 duplicate groups merged live against the database this session**
  (`docs/merge-review-2026-08-05.md`): the Stargate Abilene/Milam/Doña
  Ana/Lordstown/Shackelford/Michigan cluster, Colossus, Camellia, Stingray,
  Reveille, the NTT and Corscale name-variant pairs, Lux, MIT, Steamboat,
  Hyperion, and two AWS-Mississippi pairs from the EDGAR utility ingest. Five
  more are logged for human review because they need a judgment `dedup.py`
  deliberately won't make automatically — mostly building-vs-campus
  granularity (Digital Ashburn Campus vs. ACC8, Iron Mountain VA-2 vs. its
  Manassas campus) — plus one utility-recorded-as-operator row and one
  locality typo.

- **The capex rollup stops quadruple-counting a campus stored four times**
  (`tracker/capex.py`). The Abilene Stargate campus was in the database once
  per company whose press covered it, so `rollup()` counted 1.2 GW four times
  against OpenAI. It now counts one representative per
  `suspected_duplicates` group and discloses the rest in per-buyer `*_skipped`
  fields — skipped, never silently merged; `tracker merge` (now durable, see
  above) is still the actual repair. Measured live: 10,293 MW and $707.9B set
  aside for disclosure.

- **Only confirmed investment dollars are summed** (`tracker/capex.py`). A
  figure asserted but never confirmed — the signature of a programme-wide
  total like "OpenAI's $500 billion Stargate" quoted in an article about one
  campus, demoted at ingest by the `$/MW` plausibility ceiling — is now
  excluded from the sum and disclosed as `investment_excluded_usd` rather than
  silently inflating one campus's number. OpenAI's investment column went from
  $3,215B to $635B counted, with $2,012.9B disclosed alongside.

- **Bug: `tracker capex --json` crashed** (`tracker/capex.py`). The payload
  read `p.h200_equivalent` off `capex.Position`, which has no such attribute —
  every JSON invocation raised `AttributeError`. Now derived via the same
  `compute.h200_equivalent` conversion the per-project column already uses.

- **The year grid is now a continuous range** (`tracker/capex.py`), so a year
  nothing is dated for renders as an empty column instead of silently
  vanishing between two years that do have capacity.

- **Every number in the capex table opens** (`tracker/webui/dataset.py`,
  `server.py`, `app.js`, `tracker/overview.py`). Clicking any figure breaks it
  into the sites behind it — seven views depending on which column: site list,
  planned-capacity share, running capacity, per-site investment with
  never-confirmed ones marked, obstructions, slippage, or a year column's
  dated sites. The panel never sums its own numbers; every figure is a stored
  per-project value, verified live to add back to the cell clicked. Clicking a
  site opens its citation drawer, so drill-down bottoms out at evidence, not
  another aggregate.

- **Hovering a buyer streams a model-written reading of their position**
  (`tracker/overview.py`, `tracker/prompts/capex-overview-v1.txt`,
  `/api/capex/overview/stream`). Same fast model as the project drawer's AI
  overview (`M2-her`), same contract — cached by a fingerprint over the
  position's figures, never stored, never evidence. First word in 1.7s
  measured live. **The prompt asks for zero figures, deliberately**: across
  four measured rounds the model wrote fluent prose but invented arithmetic —
  subtracting to get a false "3,300 MW due mid-year," summing two sites into
  one, misjudging "about a quarter" as "more than a third." Three rounds of
  tightening the rules didn't fix it; removing the job did. Every number it
  was reaching for is now computed server-side and labelled instead
  (`_SHARE_WORDS` for share-in-words, `NOTHING DATES IT` for the undated
  remainder), and the prompt's only rule is no digits at all. 5 of 7 measured
  briefings came back digit-free; both leaks were figures copied correctly
  from context already shown.

- **Placeholders are caught in more shapes, and flagged if they still reach
  storage** (`tracker/normalize.py`, `tracker/logic.py`). `is_blank` now also
  catches decorated and spelled-out placeholders — `$TBD`, `TBD (est.)`, "to
  be determined", `N.D.`, `...`, `??` — via a start-anchored pattern, so text
  merely containing those letters survives. Two new free `logic check` rules
  watch stored data itself: `placeholder_value` (ERROR — a non-answer reached
  storage, meaning some path bypassed the normalizer) and `placeholder_quote`
  (WARNING — a value's recorded evidence is empty or itself a placeholder).
  Both currently return zero on the live database, which is the normalizer
  working; the rules exist to catch a future regression.

- **`tracker logic check --audit N`: does the evidence actually say it?**
  (`tracker/logic.py`, `tracker/prompts/evidence-v1.txt`). Every existing
  check asks whether values agree with each other; this asks the prior
  question — does the sentence recorded as a value's evidence actually state
  that value *for this project*. One LLM call per row, costliest first, off
  by default, same guard rails as `--read`: an unnamed field, an
  unrecognized verdict, or a finding below the confidence floor is dropped,
  and nothing is ever written automatically. A confirmed finding is repaired
  by demoting the value in `tracker review`.

- **Bug: `logic.review` could spend without an explicit limit**
  (`tracker/logic.py`). `read_limit=None` meant "unlimited," tolerable only
  while `--read N` was the sole way to set it. The new `--audit` flag broke
  that assumption — an `--audit 20` run started contradiction-reading *every*
  row on its way to the audit, caught after ~10 wasted calls. `None` now means
  none; every model call needs an explicit limit.

- **`.claude/` added to `.gitignore`**. It held machine-specific dev-server
  launch config and a session lock file (this run's own PID), neither of
  which belongs in the shared repository.

## Older context, continued (2026-08-07 05:33 run, accounting for 2026-08-06 15:32–21:43)

Ten commits, on this branch and on two Claude worktrees
(`claude/evidence-gate-plan-2998b8`, `claude/placeholder-remediation-plan-1492da`)
that merged into it at `bc79e27`. Two threads: a new free-standing magnitude
auditor, and closing out the placeholder-citation defect the 05:33 handoff had
only diagnosed. Re-verified 1578+ tests green (`pytest -q`, exit 0) before
writing this.

- **`tracker audit`: numbers that cannot be true** (`tracker/audit.py`,
  `aad5de0`). `logic check` catches fields disagreeing with each other; nothing
  caught a self-consistent field wrong by three orders of magnitude. Project 72
  read "Flexential, Englewood expansion, 11,250 MW" — bigger than any campus
  anywhere, for an operator whose whole portfolio is under 500 MW — and no
  contradiction flagged it because nothing else in the row disagreed. Six
  checks lean on stated physics or economics rather than a picked threshold:
  sources ~1000x/~100x apart, a campus beyond anything planned, a tranche
  bigger than its own campus, a gigawatt with no quote, dollars-per-MW outside
  a generous band, a derived H200 figure drifted from its input. Read-only,
  free, scopeable to given ids. Live: 21 findings across 19 of 206 projects; 16
  planted mutants, all caught — that mutant run was manual, against a copy of the
  live database that was not kept, so the claim was never reproducible.
  `scripts/measure_extraction.py --mutants` now re-runs it on demand.

- **Stop a citation that does not exist from setting values**
  (`tracker/upsert.py`, `tracker/blocks.py`, `20f75f8`). `confidence.compute`
  already dropped placeholder URLs from the score; it left the values alone,
  which was the more dangerous half — a placeholder inherits whatever
  `source_type` the seed file gave it, and the shipped seed types them
  `company_filing`, the heaviest weight in the system. Observed live on
  Fairwater (#1): the placeholder was source 1 and took every FILL_ONLY
  identity field. Now demoted (not dropped, so `--allow-placeholders` still
  smoke-tests the pipeline) in three places — field claims, block claims and
  sort, and `_conflict_notes`, which had been reporting a contest that never
  happened. Seven regression tests.

- **Record the placeholder fix, and correct the plan against live data**
  (`docs/placeholder-remediation-plan.md`, `9627d1c`) → **Stop a row's
  disclosures outliving the claims they describe** (`tracker/upsert.py`,
  `db7f781`) → **Report a stored number its own citations cannot account for**
  (`tracker/logic.py`, `d8e2831`) → **Close out the placeholder plan against
  what was actually run** (`docs/placeholder-remediation-plan.md`, `94a8216`).
  Re-verifying the plan against a live snapshot found three of its four steps
  had already been overtaken by data drift (a no-op update, a since-corrected
  $4.7B, a since-retyped 1.2 GW). What was real and got fixed: `merge`/`resolve`
  recompute derived disclosure notes and then threw them away, so a conflict
  note could name a source no longer in dispute or outlive the merge that
  resolved it — now preserved via `_merge_notes`, with two ingest-only note
  kinds (`duplicate_of`, routed-here) carried across separately since neither
  is recoverable from the row itself. And two new free `logic check` rules,
  `value_above_its_evidence` / `value_without_evidence`, ask whether a stored
  number agrees with its own citations rather than whether two fields agree
  with each other — the gap that let Stargate Abilene's `mw_built=1200` stand
  on a single well-quoted 200 claim, because MAX-merge plus the stored value
  counting as its own candidate meant it could never come back down. Live: 5
  + 22 findings across 20 of 207 projects, including $4B of investment on #33
  with no supporting source at all. The evidence audit (`--audit`) confirmed
  the plan's #1 prediction and surfaced one nobody was looking for — #1's
  `blocker` prose isn't in its cited passage — while missing entirely on #201
  from the same sentence, recorded as a limitation of the audit on
  shared-sentence rows rather than a fact about the data. Six decisions left
  open for a person (listed below under Tomorrow); values themselves
  (Abilene's 1,200 MW, the two 350s) are reported, not corrected.

- **Stop losing evidence at the gate, and refuse pages with nothing to quote**
  (migrations `0011`–`0013`, `tracker/ingest/crawl.py`, `tracker/capex.py`,
  `247e2f4`, plus its plan retrospective `docs/plan-evidence-gate.md` in
  `2c74999`). A live sync had thrown a wall of "evidence quote... is not in
  the article" warnings; measuring first showed the quote-matching wasn't the
  problem (98.7% exact-substring over 1,250 stored quotes, plus a negative
  control at 0/3,064). Three real fixes instead: (1) refuse a non-article page
  *before* the LLM call — a 600-character teaser got read as an article, the
  model invented a project from the title, then every quote failed together,
  which was the exact log signature reported; the floor is on sentence-length
  prose, not raw length, because a genuine 590-char SEC excerpt and a fake
  598-char teaser can't be told apart by length alone. Measured over 544
  cached articles: floor of 200 refuses 20, every one confirmed junk, doesn't
  touch the 246-article trade-press corpus or 114/115 real filings. (2) a
  failed-quote risk is now kept and flagged instead of deleted outright — the
  field this database is worst at, since no press release names its own
  blocker. (3) capex now excludes only `out_of_scale` unconfirmed figures and
  discloses the rest, instead of excluding every unconfirmed figure and
  understating the column along with the programme-total it meant to catch.

- **Stop the colour probes reading the developer's own database**
  (`tests/test_webui.py`, `c9eff8c`). The only tests here that shell out to
  the real CLI ran with no `TRACKER_DB`, so they read `data/tracker.db` — fine
  until a migration lands and the dev database falls a version behind,
  `gaps` refuses to run, and two of three colour assertions (checking colour
  is *absent*) kept passing against empty stdout, having checked nothing.
  Migrations 0011–0013 triggered exactly that silent gap. Now each test gets
  its own module-scoped temp database at current schema, and asserts the
  command printed something at all.

## Older context, continued (2026-08-08 05:34 run, accounting for 2026-08-07 21:43–23:03)

One commit (`b18ba5a`), not made during this run's window — the working tree
was already clean and code-complete, CHANGELOG.md and README.md already
updated in detail, when this run started. What follows is this run's first
accounting of it. Re-verified test suite green first (`.venv` pytest, exit 0,
~1685 tests, no failures).

- **A real measurement of what the stored data rests on**
  (`tracker/quality.py`, `scripts/measure_extraction.py`,
  `tests/test_quality.py`). Two numbers already existed and neither answered
  "is this value supported": the evidence gate's 98.7% exact-substring rate
  measures only the quotes that exist, and "66% of claims carry no quote"
  counts raw model output the gate correctly demotes to 待确认. The number
  that matters — the share of *stored* values whose winning source recorded a
  sentence — splits values three ways: quote-backed, 待确认-and-flagged-as-such,
  and confirmed-with-no-quote-and-nothing-says-so. That last bucket is the
  defect. Measured live before/after a full re-extraction: silent defects 89
  (11.9%) → 11 (1.3%), quote-backed 368 (49.2%) → 434 (52.0%), of a total that
  grew 748 → 835. All 89 came from two prompt vintages predating migration
  `0007` (the migration that added the column a quote lives in); none from the
  current extractor — the gate works, and the damage was stratigraphy nothing
  had ever swept, because `source.extractor` was never compared against the
  current prompt stamp. Nothing is re-derived: winning source comes from
  `gaps.provenance`, merge order from `upsert.claims_by_field` — a hand-rolled
  version returned 83, differing on exactly the MAX/MIN/PHASE fields.
- **The claim envelope: what a value is a value *of*** (migration `0015`,
  `tracker/vocab.py`, `tracker/ingest/crawl.py`, `tracker/prompts/extract-v1.txt`,
  `docs/plan-claim-envelope.md`). Hyperion (#10) stores $10B investment while
  its own notes read "expanded to up to $50 billion" — $10B (the buildout),
  $27B (the campus JV) and $50B (regional impact) are three measurements of
  three different things, collapsed into one scalar because the schema has
  only one slot. Four axes now travel with each claim: `scope`, `bound`,
  `modality`, `as_of`, each verified against its own stored quote by
  `crawl.axis_gate` and each degrading to a neutral label rather than ever
  touching the value. Pre-registered kill criteria, measured live: `bound`
  passes (32.0% coverage, 87.4% `exact`, correctly reads "roughly $27B" as
  approximate and "more than $50B" as at_least); `modality` passes weakly;
  **`scope` failed its kill criterion at 96.9% `this_site`** — across every
  `investment_usd` claim in the database it returned zero `region` and zero
  `programme`, the two values that would have separated Hyperion's figures, so
  it stays captured and displayed but not load-bearing.
- **`source.published_at`, and a merge tiebreak that can read it** (migration
  `0014`). Most of this corpus is trade press, so credibility ties are the
  common case and the tiebreak (currently `fetched_at` — crawl order, not
  publication order) decides them; six live values were on the wrong side of
  it. `merge_by_publication_date` exists but is **off by default** — turning it
  on zeroes those six but moves #116 from 120 MW to 40 MW on a smaller figure
  published a day later, which is a judgement call left to a person.
- **Date precision is stored instead of discarded.** `normalize.parse_date` has
  always returned `day|month|quarter|half|year`; nothing outside that module
  had ever read it, so a day-precision date rendered for evidence that only
  ever said "2024." Now cached on the project and rendered at the precision
  the source actually gave.
- **Two silent bugs fixed**: `ARTICLE_DATE` was always "unknown" (the
  extractor's `published_date` parameter existed since the prompt did; no
  caller ever passed it, so every relative-date phrase like "construction
  begins next year" was silently dropped rather than resolved) — and any host
  starting `news.` was typed `company_filing` (the heaviest source weight) with
  no check on whose domain it was, which on Fairwater (#1) meant a Chinese
  gaming portal decided the stored $3.3B.
- **`tracker ingest crawl --stale-prompt`**: a fourth URL selector that
  re-reads what an older prompt wrote, served from cache by default so it
  measures "did the prompt improve" without confounding it with "did the page
  change." Run live against all 228 stale URLs: 230 LLM calls, 15 projects
  inserted, 248 updated, silent defects 89 → 14 in that pass (a second
  application brought it to the 11 reported above). Two things it could not
  fix: it doesn't resolve the recency inversions (still a tiebreak problem, see
  `published_at` above), and re-reading a URL doesn't always refresh the row it
  wrote — write is keyed on `(project_id, url)` but project identity is
  re-derived from the article each time, so 28 of 61 still-stale URLs now
  route to no project at all (clearest case: the Switch/Data Foundry story,
  which the old prompt read as two campuses and the current prompt declines
  outright — gate getting stricter, or regression, left as a judgement).
- **The planted-mutant harness now actually exists**
  (`scripts/measure_extraction.py --mutants`). HANDOFF.md and CHANGELOG.md had
  both cited "16 planted mutants, all caught" as evidence for `tracker audit`,
  and no script, test, or commit contained it — that run was manual, against a
  copy of the live database nobody kept. Now reproducible on demand.
- **Not done, by design or otherwise** (`docs/plan-claim-envelope.md`):
  promoting any axis to actually choose a value (deliberately not, for
  `scope`); three of six planned console-rendering items for the new axes;
  `events[]` still bypasses the evidence gate entirely, unquoted and
  unchecked — which is how Hyperion's 2027-12-31 `announced` milestone exists
  on the record at all.

## Older context, continued (2026-08-08 run, `claude/audit-duplicates-cli-ui-44227f`)

One session on a single theme: **every report this project has grown could
state a problem and none of them could take an answer.** `audit` printed
implausible figures and offered a sentence telling you to go and read an
article. `duplicates` proposed groups whose only possible reply was `merge`.
`risks` counted 43 obstacles as 待确认 and left it there. `queue` printed links
that 404'd because they were truncated at 60 characters. Landed as `95a18ab`;
1771 tests green, ruff clean.

- **`tracker audit resolve`** (`tracker/audit.py`, `prompts/audit-resolve-v1.txt`).
  Five rungs, each running only because the one above declined: arithmetic,
  the operator, a model on the row, a web search, the model again. Rung one is
  two cases and no more — `h200_equivalent` is a fixed ratio, and a tranche
  labelled "2.4 MW Lease" carrying 2400 is that label read as kilowatts. The
  model's whole output is one key from a list a person wrote; its fourth answer,
  `m`, means "the row does not contain the answer" and is what sends the
  question to the web instead of forcing a guess. `block_out_of_scale` gained
  two repairs in the process, having previously offered none.

- **`tracker duplicates park`** (migration `0016`, `tracker/pairs.py`). The
  missing *no*. Not cosmetic: `capex.rollup` reads the same pairs and holds one
  row of each group out of the buyer table, so a false pair took a real campus
  out of a number. Two live false pairs also forced the scan to tighten —
  "centers" was missing from the generic-word list (the singular was there), and
  a tranche key that appears in more than one town is vocabulary, not identity.
  Both false pairs are now gone without anyone parking anything; the two real
  ones (Memphis, Childress) remain, with their evidence printed.

- **`tracker risks confirm`** (`tracker/riskcheck.py`). One model call per
  unquoted obstacle, with the whole cached article and every sibling obstacle on
  the row. Its quote is accepted only if `crawl._verbatim_run` finds it verbatim
  and `crawl._risk_quote_supports` finds the category in it — the same two gates
  that refused it in the first place. A four-obstacle live dry run: 1 confirmed,
  3 quotes offered and refused, nothing written. One of the refusals was a
  Chinese-language source, which the ASCII-folding matcher can never verify.

- **`tracker queue`, and the reason the links 404'd.** The URL column was
  `url[:60]`, so every link on screen was a *prefix* of a real one — and
  `--drop --url` was the only handle offered, which the truncated string also
  could not satisfy. Whole URLs, an id per row, `--feed`, newest-first. Plus
  `queue check` (fetch each URL; 404/410 are dead, 403/429 are not) and `queue
  prune` (re-apply `feeds.toml` to rows queued under an older filter).

- **`logic resolve --llm` was spending its budget on questions no edit could
  answer.** 174 of 283 live findings come from rules that offer no action on
  purpose, and ordered by project id they filled the first `--limit 30`. They
  are now counted in one line. Separately, the triage prompt had been shown the
  wrong page: a finding about an obstacle declares `fields=("blocker",)`, so the
  model got one quote behind a derived string and correctly answered that it did
  not address the question. Findings now carry `subjects` and the context is
  built from them. Verified live — the declines that follow are substantive.

- **The console's dropdowns had lost their styling.** `stamp` versions every
  URL the page references and did nothing for the twelve files `styles.css`
  imports, so one layer could go stale behind a fresh parent. Stylesheets are
  now served with imports inlined, relative `url()`s re-anchored, and a token
  covering every constituent. Also `_walk` in the catalog dropped any group that
  gained a subcommand, which would have removed `duplicates`, `audit` and
  `risks` from the palette this session.

- **`tracker infer`** got a two-question layout with confidence bars, and a
  **Run analysis** panel in every project drawer (`POST /api/infer`). A button,
  not an automatic panel: unlike the AI overview it is never cached, so opening
  a drawer must not spend a call.

- **Live data cleaned.** Backup at `data/tracker.backup-before-cleanup.db`.
  Migration 0016 applied. `queue prune --drop` removed 417 of 1,241 queued
  candidates (NTT marketing, DataBank compliance blogs, sponsored posts, Meta AR
  contest results); `queue check` then fetched all 824 survivors and found **zero
  dead links** — 51 blocked (403), all kept, mostly datacenterdynamics and
  quantumloophole. The 404s were never in the data; they were the truncated
  column. `audit resolve --no-llm --no-ask` fixed one tranche (2400 → 2.4 MW on
  WPA-1 Boyers).

## Older context, continued (2026-08-09 05:35 run)

One session on one branch (`data-quality-adjustments`), committed as
`5908c68`. Verified 1788 tests green (`.venv` pytest, exit 0) and ruff clean
before writing this.

- **`events[]` gets the evidence gate risks already had** (migration `0017`,
  `tracker/ingest/crawl.py`, `tracker/upsert.py`, `tracker/models.py`,
  `tracker/export.py`, the console's milestones card). The last extracted
  structure with no gate behind it at all — the prompt has said "only
  milestones whose date you can quote" since v1, a request with no
  mechanism. Observed live on Fairwater (#1): a `groundbreaking` dated
  2026-06-23 whose own description reads "Open house event held to announce
  opening," feeding the track strip like any verified milestone. `event` now
  carries `quote` (verified verbatim against the fetched article, same
  recovery path as `Risk.quote`) and `unconfirmed` (from
  `vocab.UNCONFIRMED_REASONS`). The backfill deliberately differs from how
  risks were backfilled in 0012: no gate ever ran on an event before, so
  NULL-means-confirmed would misstate every existing row — all 843 pre-gate
  rows are marked `no_quote` instead. Live after backfill plus one
  `--stale-prompt` pass: 837 of 860 events still `no_quote`, 23 confirmed. A
  verified quote upgrades a `no_quote` row on re-read; a later article that
  merely fails to quote the event never downgrades one already confirmed.
  On screen, an unverified milestone carries a "not cited" chip and a
  verified one shows the article's sentence on hover.
- **`built_capacity_uncited_in_blocks`** (`tracker/logic.py`). Names the
  sibling gap its cousin `live_block_without_cited_capacity` couldn't see,
  because that rule guards on `mw_built is None` — exactly wrong for a row
  where the scalar *is* set. Fairwater (#1) again: the console read "0 MW
  delivering of 350 MW" directly above an energized 350, honestly, because
  only cited capacity is summed and the 350 is 待确认 — but nothing named the
  contradiction between the scalar, the blocks and the events. Report-only,
  like every block rule: the fix is a citation for a running tranche, and no
  automatic edit can find one. Live: 19 findings across 19 projects,
  including a 46,500 MW `mw_built` on #62 that's plainly a unit error. The
  blocks tab headline now says why a zero next to a running tranche is a
  zero ("3 tranches are running with no cited MW — 350 MW stated, 待确认")
  instead of leaving the page to disagree with itself.
- **`tracker enrich --all`** (`tracker/cli.py`, `tracker/ingest/enrich.py`).
  `--select N` needed a count picked in advance to mean "all of them."
  `--all` is `--select` with no cap — same query, same closest-first
  ordering, and `select_projects` already excludes projects at or past
  `--target`, so it never spends on finished rows. `--budget` remains the
  real ceiling; passing more than one of project ids, `--select`, `--all` is
  now a refused combination rather than a silent union.
- **Housekeeping**: this HANDOFF.md accounts for `cd2d462` (the merge-time
  `ImportError` fix), which had landed 2026-08-08 17:26 but was never
  recorded — see Yesterday above. README and CHANGELOG were already current
  for today's three changes before this run started; no drift found in
  `docs/architecture.md` (still high-level by design) or the absence of an
  AGENTS.md (still a single CLI/data-pipeline codebase, no distinct agent
  roles to document).

## Today (2026-08-10 run)

No new commits landed during this run's window — a housekeeping pass,
accounting for the 2026-08-09 11:36–23:51 stretch above (see Yesterday) and
tidying docs. Verified 1884 tests green (`.venv` pytest, exit 0) and ruff
clean before writing this — up from the ~1826 the `ed0bd54` merge counted,
mostly new coverage for search, Wikipedia mining, the escalation ladder and
`tracker blocks`.

- **Recorded the eight unrecorded commits** listed under Yesterday — this
  file had no entry for any of them until now.
- **`tracker blocks` had shipped with no README entry.** Every other free,
  read-only report command (`duplicates`, `audit`, `logic check`) gets one in
  README.md; this one didn't. Added a "One tranche wearing several names"
  section documenting verdicts and flags, and listed the command alongside
  `duplicates` in the top-level command summary. Also corrected
  `docs/README.md`'s description of `plan-scale-with-sources.md`, which still
  read "nothing built yet" after Part 2 (`tracker blocks`) had shipped.
  `docs/architecture.md` needs no change — the new modules (`blockcheck.py`,
  `vocab.py`, `ingest/wiki.py`) are pipeline-internal, not a shift in the
  CLI/database/console split it describes.
- **Live counts have moved since the plan doc's 2026-08-09 22:31
  measurement, from the same source-growth it was written to describe** —
  recorded here as a wider backlog, not investigated further this run:
  - `tracker blocks`: 35 groups on 25 projects (31 mergeable, 2 collides, 2
    ambiguous), against the doc's 23/16/21/1/1.
  - `tracker duplicates --no-weak`: 7 strong groups, against the 2 this file
    had been carrying (Memphis, Childress — both still open; five new:
    Southaven MS, West Jordan UT, Ashburn VA, Harwood ND, Manassas VA).
  - `tracker audit`: 27 findings on 25 projects, against 21 on 19.
  - `tracker risks`: 118 of 305 obstacles 待确认, against 43 previously cited.
  - `tracker logic check`: 361 projects checked (395 source collisions, 426
    contradictions, 23 impossible), against the plan doc's 252 projects.
  - `tracker queue`: 803 candidates, down from 824.

  361 projects is a real jump from the 252 the plan doc measured the same
  day — plausibly `sync`/`enrich` running unattended between commits rather
  than anything wrong, but nobody has confirmed which.
- **One unplanned model call**: `tracker risks confirm --dry-run`, run to
  check the 待确认 count above, made two live LLM calls despite the name —
  it writes nothing, but it isn't free. Noting it so it isn't repeated by
  habit; the other counts above came from free, read-only commands.

## Today (2026-08-11 run)

No work was done this day. `data-quality-adjustments` is unchanged since
`15b1f44` (2026-08-10 05:43) — no new commits, and the working tree carries
nothing beyond the same untracked reference PDF noted below under Tomorrow.
Checked, not just assumed: `git log` on this branch and on
`claude/audit-duplicates-cli-ui-44227f`/`-f60464`,
`claude/data-source-coordination-analysis-66de67` and
`claude/project-overview-logo-a8041f` all show no commits past 2026-08-10;
README.md/CHANGELOG.md/`docs/architecture.md` need no update since nothing
changed; no AGENTS.md exists, still correctly so (single CLI/data-pipeline
codebase, no distinct agent roles).

One thing outside this branch's scope, noted but not touched: the
`.claude/worktrees/audit-duplicates-cli-ui-cc70ff` worktree (on
`claude/project-overview-logo-a8041f`) carries uncommitted changes to
CHANGELOG.md, README.md, and three webui static files (`app.js`,
`index.html`, `login.html`) — a separate, apparently in-progress feature
branch, not this branch's work, left alone.

## Tomorrow

- **Top priority, now three days old: find out who ran the 2026-08-12 purge
  and why, and whether 889 of 1,189 projects were meant to go.**
  `data/tracker.backup-before-purge-20260812.db` is still the only record
  of it — no commit, script, or doc names the criteria, and nothing landed
  2026-08-13 touches project counts either. This now also caps how far to
  trust 2026-08-13's own new work: `tracker backfill derive`'s 322 values
  across 213/300 projects, `tracker backfill dates`'s 67.6% coverage, and
  `tracker sources`'/`tracker feeds`'s rankings all measured the same
  300-row database. Until someone confirms the purge was intentional or
  restores from the backup, re-run `tracker clean`, `duplicates`, `audit`,
  `risks`, `logic check`, `blocks`, `sources`, and `feeds` fresh once
  that's settled — none of these numbers are worth trusting until then.
- **`tracker logic conflicts` has never run against a real DeepSeek key**
  (492 fields qualify live) — tested only against injected fakes. Now higher
  stakes than when this was first flagged: as of 2026-08-15 the same
  `conflicts.disputes`/`solve` machinery also runs unattended as `enrich`'s new
  settle stage (see Yesterday/Today above), so the first real-key run will
  happen inside a batch `enrich --all` unless someone runs `tracker logic
  conflicts` deliberately first. Worth doing once the purge above is settled,
  since it's the intended fix for the crawl-order tiebreak problem
  (`published_at` / `merge_by_publication_date`, now measured and deliberately
  still off: flipping it fixes 65 values and gets 22 of 40 numeric ones wrong,
  because a date can't see scope).
- **`tracker feeds --no-probe` names one real retirement candidate**:
  `applied-digital-newsroom` (44 queued, 17 read, 17 none, zero cited in
  ten calls). It only proposes — `queue --drop --feed` and the
  `seed/feeds.toml` edit still need a person. DataCenterDynamics stays on
  purpose despite topping any queued-vs-cited ranking; the config
  explains why.
- **`tracker backfill derive` cleared 81 stale blockers live on
  2026-08-13.** The "27 audit findings unsettled" and "118 of 305 obstacles
  待确认" counts below predate that and are now doubly stale (purge, then
  backfill) — re-run `audit`/`risks`/`clean` together, not separately,
  once the purge above is resolved.
- The evidence gate checks only that a quote exists and is verbatim, not
  that it actually supports the *event_type* it's filed under — a verified
  "Open house event" sentence can still sit beside a `groundbreaking` chip.
  `docs/plan-claim-envelope.md` names the risk gate's `_RISK_EVIDENCE`
  vocabulary as the template if that's worth building.
- ~~`bound`'s hedge-word check is not positional~~ **Done** — `3c30e0a` made
  attachment (a hedge within 32 characters of its figure) decide it instead
  of mere presence.
- **27 audit findings are unsettled** (up from 21) and have somewhere to go —
  run `tracker audit resolve` interactively; costs nothing until you answer
  `?`.
- **118 of 305 obstacles are 待确认** (up from 43) — `tracker risks confirm
  --dry-run` first. The live sample previously refused most offered quotes,
  so expect a low confirm rate; Chinese-language sources will never confirm
  through the ASCII matcher — worth deciding whether that's a gap to close
  or a limit to state.
- **Duplicate groups grew from 2 to 7 strong (`--no-weak`) matches**, likely
  surfaced by the new search/Wikipedia reach pulling in overlapping
  coverage. Memphis (#2/#226/#291) and Childress (Iris Energy → IREN rename)
  are the two previously reviewed; Southaven MS, West Jordan UT, Ashburn VA,
  Harwood ND and Manassas VA are new and unreviewed.
- **`tracker blocks` proposes 31 mergeable groups** (up from 21) — folding
  them would retire 36 rows. It never writes; someone still has to act on
  the proposals. The 2 collides and 2 ambiguous groups need a person, not a
  merge — Hyperion's Phase 1 (2,000 MW) vs. Phase 1 IT Load (1,500 MW) is
  the collision worth reading first, since it's facility load vs. IT load
  rather than a disagreement.
- **`docs/plan-scale-with-sources.md` Part 1 is still unimplemented** — the
  deterministic grouping/normalisation for `logic check`'s quadratic source
  collisions (275 → 395 live since the doc was written), no model call
  needed per the doc's own conclusion. Part 2 (`tracker blocks`) is done.
- **803 queued candidates remain** (down from 824), all reachable.
- **The `published_at` merge tiebreak is off**, and flipping it needs a bulk
  recompute that doesn't exist yet — `recompute_from_sources` is reachable
  only through `merge` today.
- **11 silent defects remain**, all on rows whose articles no longer support
  them (mostly the 28 URLs orphaned by re-derived project identity) —
  unverified against the current, larger database.
- `news.microsoft.com`-style newsroom subdomains still read `general_media`
  unless the parent domain is already in `feeds.toml`; `datacenters.atmeta.com`
  was added as a `company="Meta"` newsroom on 08-09 — add genuine newsrooms
  as they're found the same way.
- The claim envelope pushed some replies past `max_completion_tokens`, each
  costing a corrective retry — worth watching if it gets worse as more axes
  are asked for.
- **Six decisions left open by the placeholder-plan closeout, all needing a
  person** (`docs/placeholder-remediation-plan.md`, "Open decisions") —
  still untouched: `confidence.find_conflicts` still counts 待确认 claims as
  a third copy of a rule the other two now apply; #3's `mw_built=1200` needs
  a manual `tracker review` demotion since MAX-merge won't lower it on its
  own; #1 and #201's `mw_built=350` (one hedged sentence read as a per-site
  figure on two rows); #1's `blocker` prose is unsupported by its cited
  passage; `placeholder_quote` isn't firing on source 108 despite a
  confirmed `mw_built=350` with no stored quote at all; and the two
  Fairwater block questions (duplicate energisation dates, NULL
  `mw_planned`).
- **Review the 5 deferred merge candidates** in
  `docs/merge-review-2026-08-05.md` — two building-vs-campus pairs, one
  utility-recorded-as-operator row, one Doña Ana locality-grain pair, one
  locality typo. None auto-merge for good reason; each needs a human call.
- **Finish the block backfill.** Still projects with a real energisation and
  no blocks read — carried six days now.
- **Review the two block-shaped report-only signals** —
  `blocks_may_double_count` and `block_label_ambiguous` (5 live hits) —
  still untriaged.
- Review `docs/feedback-2026-08-03.md`, now seven days old and untouched.
- The 20 utility/contractor EDGAR companies in `seed/edgar-companies.toml`
  are still wired up but unrun — ninth day carried.
- The 30-required-projects gap, unverified ERCOT/CAISO column names, and the
  two unconfigured Google CSE keys are still open — ninth day carried.
- `tracker cloudflare --name`/`TRACKER_TUNNEL_HOSTNAME` still need a real
  run against a named tunnel with DNS actually pointed at it.
- `tracker audit`, the evidence-quote gate, and `quality`/the claim envelope
  are all measured only against the live database as it stands today — worth
  rerunning after the next sync, truer than ever now that today's counts
  moved this much since 08-09.
- **An untracked 2 MB PDF still sits in `docs/`**
  (`docs/能源科技AI系列报告（三）：北美AI电力新趋势PPT+ED (1).pdf`, added
  2026-08-06 14:52, unchanged since — ten days now). Still reference
  material, not project output; still needs a human call on whether it
  belongs in the repo, `.gitignore`, or somewhere else entirely.
- **The untracked `work/` directory** (four `.pptx` decks, two `.pdf`
  diagrams, one planning `.md`, added 2026-08-12) needs the same call as
  the PDF above — plus one thing the PDF doesn't have: a live PowerPoint
  lock file (`~$Hyperion-Tracker-Review-FINAL.pptx`), still present as of
  2026-08-15 (three days unchanged), meaning a deck may still be open for
  editing elsewhere. Don't touch until confirmed closed.
- **Every prompt's stamp moved** when `_industry.txt` was prepended
  (`06b575a`), so `tracker clean`'s `vintage_current` check fails on every
  row by construction — nothing has been read by the current gate yet.
  An `ingest crawl --stale-prompt` pass (as run 2026-08-09 for the claim
  envelope) would re-read stale rows against the new prompts; worth doing
  once the purge above is resolved, since re-extracting a database that
  might still be missing 889 rows would need repeating.
- No AGENTS.md exists for this project: still a single CLI/data-pipeline
  codebase with no distinct agent roles to document. `docs/architecture.md`
  stays high-level by design and didn't need updates today (see Today).
