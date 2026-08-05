# Handoff

## Yesterday (state at start of 2026-08-06)

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

## Today (2026-08-06)

No new commits landed during this run's window; the working tree was already
clean of code changes, just uncommitted. What follows is this run's first
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

## Tomorrow

- **Review the 5 deferred merge candidates** in
  `docs/merge-review-2026-08-05.md` — two building-vs-campus pairs, one
  utility-recorded-as-operator row, one Doña Ana locality-grain pair, one
  locality typo. None auto-merge for good reason; each needs a human call.
- **Finish the block backfill.** Still 16 projects with a real energisation
  and no blocks read — carried two days now.
- **Review the two block-shaped report-only signals** —
  `blocks_may_double_count` (6 live hits) and `block_label_ambiguous` — from
  the 2026-08-04 session, still untriaged.
- Review `docs/feedback-2026-08-03.md`, now three days old and untouched:
  `tracker ingest edgar --kind utility` first, then merge the obvious
  duplicate clusters (the now-durable `project_alias` signal should help),
  then a placeholder-value vocabulary (largely done today via `is_blank`,
  worth re-checking against the five questions), then a 5-track progress bar
  next to `phase`.
- The 20 utility/contractor EDGAR companies in `seed/edgar-companies.toml`
  are still wired up but unrun — fourth day carried.
- The 30-required-projects gap, unverified ERCOT/CAISO column names, and the
  two unconfigured Google CSE keys are still open — fourth day carried.
- `tracker cloudflare --name`/`TRACKER_TUNNEL_HOSTNAME` still need a real run
  against a named tunnel with DNS actually pointed at it.
- The evidence audit (`--audit`) and the buyer hover overview are both new
  and unproven beyond the measurements quoted above — worth a wider run once
  there's time to read the output critically rather than just verify the
  guard rails hold.
- No AGENTS.md exists for this project and none was added today: still a
  single CLI/data-pipeline codebase with no distinct agent roles to
  document. `docs/architecture.md` was checked against today's changes and
  doesn't need updates — the new capex drill-down and buyer overview both
  follow the existing "browser never re-implements a judgement" rule rather
  than changing it.
