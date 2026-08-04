# Handoff

## Yesterday (state at start of 2026-08-05)

2026-08-04 05:29 (`443619e`) landed the big batch already summarized in the
prior handoff: `capacity_block` (migration `0009`) as a new sub-project data
model, `tracker backfill blocks`, `h200_equivalent`, the command box and
routines in the console, streaming AI overviews on `M2-her`, `logic resolve`
no longer destroying correct data to satisfy the coarse `phase` enum, asset
fingerprinting for cache-busting, and named-tunnel config. 1487 tests, green
offline. That handoff's own "Tomorrow" was explicit that blocks existed in
the schema but weren't wired anywhere real yet: `logic` rules weren't
re-expressed per block, no read surface showed a block, and
`backfill blocks` was verified only on a sample, not the live 227 rows.

The session didn't stop there — it continued the same evening (22:53–23:32)
into nine more commits, landing before this scheduled run but after the prior
handoff was written and committed. Those nine are what "Today" below covers,
since they're the first thing this run found uncaptured.

Carried in unchanged: the 30-required-projects gap, unverified ERCOT/CAISO
column names in `iso_maps.py`, two unconfigured Google CSE keys, the 20
utility/contractor EDGAR companies from `seed/edgar-companies.toml` still
wired up but unrun, `tracker cloudflare --name` untested against a real named
tunnel, `tracker point`/`tracker merge` heuristics validated on only a
handful of cases, and `docs/feedback-2026-08-03.md` — five colleague
questions with "no code changes proposed yet, review first" — still
unreviewed.

## Today (2026-08-05)

No new commits landed during this run's window; the working tree was already
clean. What follows is this run's first accounting of the nine commits from
the tail of the 2026-08-04 session, which took `capacity_block` from a schema
nobody read to something the whole product agrees on — running the actual
backfill against live data surfaced four real bugs on the way. Test suite is
now 1514, verified green offline before writing this file. `CHANGELOG.md` and
`README.md` were already updated in-session (`98b86bb`) with full detail;
this is the shorter account.

- **Bug: an unconfirmed block capacity was getting summed into the campus
  total** (`fc90a52`). `rollup` didn't check whether a quote had actually
  named a block before adding its MW in. The first live backfill tranche
  raised Applied Digital Jamestown from 7 MW to 7,500 MW and CHI-1 from 12 to
  36, both off one unquoted block. Fixed: an unconfirmed (待确认) block still
  carries and shows its number, but is excluded from the total, and
  `reconcile` now discloses which blocks it left out and why.

- **Bug: a portfolio article's disclosure note pointed at the wrong row**
  (`e2bdf79`). The backfill splits one article's blocks across several
  project rows after the note describing "unconfirmed blocks" was already
  written — so STACK's Chicago row was told "Portland Expansion" was
  unconfirmed, a block that had actually been routed to the Portland row.
  Split into `vague_block_note`, computed after routing.

- **Bug: word order forked one tranche into two** (`0144135`). Lake Mariner
  held the same 60 MW Core42 tranche twice because one article wrote "La
  Lupa (Core42 Leases)" and another wrote "Core42 Leases (La Lupa)", and the
  key was ordered by appearance. A block's identity is now a *set* of
  designators, not a phrase — tested both orderings converge, while a true
  subset ("Core42 Leases" alone vs. "La Lupa (Core42 Leases)") is deliberately
  left unmerged and flagged by `blocks_may_double_count` instead (6 real
  instances found on live data).

- **The backfill now reuses the crawl path's retry, recovering ~20% of
  articles** (`0144135`). 5 of the first 25 articles failed to parse — every
  one a reply that spent its whole token budget inside `<think>` and never
  reached JSON. The crawl path already retries a verbatim failure with a
  doubled budget; the backfill wasn't using it and is now built on the same
  `IngestRecord`/`extract_one` path as ordinary crawling, so it no longer
  carries a second copy of the extraction logic either.

- **The four `logic` rules that treated a partly-live campus as broken now
  ask the blocks first** (`193126c`). On a modern campus — one tranche
  energised for a buyer, another still going up — the old rules fired on the
  ordinary shape of the thing: all 18 `energized_but_not_operational`
  findings and part of another 45 `past_its_own_date`, out of 144 total, were
  this. A campus with no blocks still gets the old behaviour unchanged (no
  blocks means nothing's been read, not that the row is coherent).
  `past_its_own_date` now applies per block; `operational_without_built_capacity`
  splits into a real phase regression versus a running block missing its own
  citation. Two new report-only rules: `block_label_ambiguous` (a tranche that
  can't be placed) and `blocks_may_double_count` (nested labels — Riot
  Rockdale's "AMD Lease" / "AMD Lease Initial Deployment" / "AMD Lease
  Expansion" is one lease summing three times).

- **Follow-on fix: an energisation with no tranche to hold it now reports as
  a gap in the blocks, not a phase defect** (`bb40127`). Measured on SDC
  Quincy — one "Newest Phase" block under construction beside a cited 2022
  energisation nothing represents. `energized_but_not_operational` is now
  zero on every project that actually has blocks; the 16 that remain are all
  rows the backfill hasn't reached yet.

- **Blocks are now visible everywhere** (`2318c64`): a colour-coded table
  above the sources in `tracker show`, an `mw_counted` flag per block in the
  JSON export (so 待确认 tranches don't look like a total that doesn't add
  up), and a console drawer Blocks tab — shown only when a project actually
  has blocks, so the 88% of the database not yet backfilled doesn't read as
  "this campus has one tranche." Small follow-up fix for a missing space and
  clumsy plurals in that tab's copy (`789ac53`), verified in the rendered page.

- **Capex now attributes capacity per tranche instead of per campus**
  (`0db38af`). Lake Mariner is 378 MW under construction for Fluidstack
  beside 60 MW already serving Core42 — previously all 750 MW went to
  whichever name reached `project.customer` first. A tranche's capacity now
  goes to the buyer it names; the rest of the campus stays where it was, so
  the total is conserved rather than reassigned, and it books on the
  tranche's own date rather than the campus's last-finished one. Money is
  *not* split the same way, since a tranche rarely states its own investment
  share and inventing a ratio would be worse than not splitting.

- **A shared block identity is now evidence of a duplicate row**
  (`0db38af`). Three separate rows in Andrews, TX each hold the same 70 MW
  AWS block — a derived `block_key` shared across rows is harder evidence
  than a name-resemblance match, so `capex.suspected_duplicates` now surfaces
  pairs the name test misses (generic keys like a bare "phase-1" are excluded,
  since pairing on those would pair half the database).

## Tomorrow

- **Finish the backfill.** 16 projects with a real energisation still have
  no blocks at all — the rows the backfill hasn't reached. Until that
  closes, `energized_but_not_operational` isn't actually zero, it's zero
  *on the subset that's been read*.
- **Review the 6 live `blocks_may_double_count` hits** (Riot-Rockdale-shaped
  nested labels) and the handful of `block_label_ambiguous` findings — both
  are new report-only signals from `193126c` that nobody's triaged yet.
- Review `docs/feedback-2026-08-03.md`, now two days old and still
  untouched: run `tracker ingest edgar --kind utility` first, then merge the
  obvious duplicate clusters (the new block-identity signal from `0db38af`
  should make that easier than it was), then a placeholder-value vocabulary,
  then a 5-track progress bar next to `phase`.
- The 20 utility/contractor EDGAR companies in `seed/edgar-companies.toml`
  are still wired up but unrun — third day carried.
- The 30-required-projects gap, unverified ERCOT/CAISO column names, and the
  two unconfigured Google CSE keys are still open — third day carried.
- `tracker cloudflare --name`/`TRACKER_TUNNEL_HOSTNAME` still need a real run
  against a named tunnel with DNS actually pointed at it.
- `tracker point`'s confidence floor and `tracker merge`'s duplicate
  detection remain validated against a small number of known cases — the new
  block-key duplicate signal is itself unproven beyond the one Andrews, TX
  example that motivated it.
- No AGENTS.md exists for this project and none was added today: this is a
  single CLI/data-pipeline codebase with no distinct agent roles to
  document, so the usual daily housekeeping pass found nothing there to
  maintain. `docs/architecture.md` and `docs/what-we-built.zh-CN.md` were
  checked against today's changes and don't need updates — the CLI/console
  split those describe is unchanged, and the zh-CN doc is an explicitly
  dated snapshot (2026-08-02) answering a specific review, not a living doc.
