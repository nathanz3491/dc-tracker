# Handoff

## Yesterday (state at start of 2026-08-07)

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

## Yesterday's older context (state at start of 2026-08-06)

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

## Today's older context, continued (2026-08-06 05:33 run)

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

## Today (2026-08-07 run, accounting for 2026-08-06 15:32–21:43)

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

## Tomorrow

- **Six decisions left open by the placeholder-plan closeout, all needing a
  person** (`docs/placeholder-remediation-plan.md`, "Open decisions"):
  `confidence.find_conflicts` still counts 待确认 claims — a third copy of a
  rule the other two now apply, so a row's confidence rationale and its notes
  can disagree on screen, deliberately left alone since fixing it moves a
  stored score across an unknown share of 207 rows; #3's `mw_built=1200` needs
  a manual `tracker review` demotion since MAX-merge won't lower it on its
  own; #1 and #201's `mw_built=350` (one hedged sentence read as a per-site
  figure on two rows); #1's `blocker` prose is unsupported by its cited
  passage (found by `--audit`, not in the original plan); `placeholder_quote`
  isn't firing on source 108 despite a confirmed `mw_built=350` with no stored
  quote at all — rule or row, undetermined; and the two Fairwater block
  questions (duplicate energisation dates, NULL `mw_planned`).
- **Review the 5 deferred merge candidates** in
  `docs/merge-review-2026-08-05.md` — two building-vs-campus pairs, one
  utility-recorded-as-operator row, one Doña Ana locality-grain pair, one
  locality typo. None auto-merge for good reason; each needs a human call.
- **Finish the block backfill.** Still 16 projects with a real energisation
  and no blocks read — carried three days now.
- **Review the two block-shaped report-only signals** —
  `blocks_may_double_count` (6 live hits as of the 2026-08-04 session) and
  `block_label_ambiguous` — still untriaged, unchanged by this session's work.
- Review `docs/feedback-2026-08-03.md`, now four days old and untouched.
- The 20 utility/contractor EDGAR companies in `seed/edgar-companies.toml` are
  still wired up but unrun — fifth day carried.
- The 30-required-projects gap, unverified ERCOT/CAISO column names, and the
  two unconfigured Google CSE keys are still open — fifth day carried.
- `tracker cloudflare --name`/`TRACKER_TUNNEL_HOSTNAME` still need a real run
  against a named tunnel with DNS actually pointed at it.
- `tracker audit` and the evidence-quote gate are both new and measured only
  against the live database as it stands today — worth rerunning after the
  next sync to see whether the numbers hold up against fresh ingest.
- **An untracked 2 MB PDF sits in `docs/`**
  (`docs/能源科技AI系列报告（三）：北美AI电力新趋势PPT+ED (1).pdf`, added
  2026-08-06 14:52, read-only). Not committed by this run — it's reference
  material, not project output, and its filename/permissions suggest it was
  dropped there deliberately rather than a leftover to delete. Needs a human
  call on whether it belongs in the repo, `.gitignore`, or somewhere else
  entirely.
- No AGENTS.md exists for this project and none was added today: still a
  single CLI/data-pipeline codebase with no distinct agent roles to document.
  `docs/architecture.md` stays high-level by design (logic, not command-level
  detail) and doesn't need updates for `tracker audit`, the evidence gate, or
  the placeholder-plan closeout — all three are pipeline-internal behaviour
  already covered at the right altitude in README/CHANGELOG.
