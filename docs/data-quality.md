# Data quality: contradictions, units and provenance

The checks that ask whether a number can be true at all, what each stored value actually rests on, and why crawl order is not publication order.

Part of the [dc-tracker documentation](README.md).

---

## Numbers that cannot be true

```bash
tracker audit                # every project
tracker audit check 72 25    # only these
tracker audit resolve        # settle what it found
```

Free, read-only, no LLM. Run it after every sync or backfill; an empty result is
the point. (The ids moved onto `check` when `resolve` arrived: a command group
cannot take a variable number of positional arguments *and* dispatch a
subcommand — `tracker audit resolve` would parse "resolve" as a project id.)

**Why this is not `logic check`.** That command asks whether a row's fields
contradict *each other*, and it cannot help here, because each of these rows was
perfectly self-consistent around a figure wrong by three orders of magnitude.
Project 72 read *Flexential, Englewood expansion, 11,250 MW* — larger than any
campus planned anywhere, for a colocation operator whose entire portfolio is under
500 MW. Nothing contradicted it, so nothing flagged it, and it sat as the largest
number in the database feeding an 8.7-million-H200 estimate and every national
total. **A unit error does not look like a lie. It looks like a big number.**

Six checks, each leaning on a stated piece of physics or economics rather than a
threshold somebody picked:

| check | what it means |
|---|---|
| `same_figure_two_units` | two sources ~1000× or ~100× apart — the same number in kW and MW, not a disagreement |
| `campus_exceeds_worlds_largest` | over 8,000 MW on one site, beyond anything actually planned |
| `block_out_of_scale` | a tranche larger than the campus containing it |
| `giant_capacity_unconfirmed` | a gigawatt claim with no quote anywhere behind it |
| `usd_per_mw_out_of_band` | outside $0.3M–$60M per MW, when real builds run $2M–$30M |
| `h200_disagrees_with_capacity` | a derived figure that drifted from the capacity it came from |

Unit errors sort first, because those are the ones that poison totals.

On the live database it returns 22 findings across 20 of 225 projects: a rate you
can actually work through, which is the difference between a check people run and
a check people mute.

### Settling them: `tracker audit resolve`

The listing was half a tool. It reported the same 11,250 MW expansion on every run
because the only repair it could offer was a sentence telling somebody to go and
read an article. `resolve` is that somebody, and it climbs only as far as it has
to:

| rung | what it is | cost |
|---|---|---|
| 1 | **Arithmetic** — the answer is not a judgement | free |
| 2 | **You**, with every claim and quote on screen and one-key answers | free |
| 3 | **A reasoning model** on what the row holds | one call |
| 4 | **The open web**, when the model says the row lacks the answer | a search + up to 4 fetches |
| 5 | **The model again**, with those passages | one call |

Each rung runs only because the one above declined, which is the whole cost
control. Rung 1 is deliberately two cases and no more: `h200_equivalent` is a fixed
ratio applied to capacity, and a tranche *labelled* "2.4 MW Lease" carrying 2400 on
a 15 MW campus is that label read as kilowatts — the correct figure is written down
beside the wrong one, so nothing is being guessed. Rung 2 offers a third answer
besides the edits, `?`, which hands the question down; not knowing is the commonest
honest response and it should cost one keystroke.

**A model's output is one key from a list a person wrote**, plus a confidence and a
sentence. It cannot type a capacity. It may answer `m` — "the row does not contain
the answer" — which is what sends the question to rung 4 rather than forcing a
guess. Pages fetched there are read and not stored: they inform one decision, and a
page skimmed for one number is not a citation for anything.

Every edit is written into the row's notes naming who made it — `rule`, `operator`,
`model` or `model after search` — and a finding settled once is not asked again
unless you pass `--again`. `--no-ask`, `--no-llm` and `--no-search` each stop the
ladder at that rung; `--no-llm --no-ask` does the free repairs and nothing else.

## One tranche wearing several names

```bash
tracker blocks                    # every project, strongest evidence first
tracker blocks 1 10                # only these projects
tracker blocks --only mergeable    # one verdict at a time
```

Free, read-only, no LLM — the shape `tracker duplicates` already established,
one level down: a campus read by 25 sources acquires a name per source.
Fairwater held `Building 2`, `Facility 2`, `Second facility` and `Area II` for
one building, because `blocks.block_key` folds ordinals hard ("Phase I" and
"first phase" already converge) but cannot fold *which noun a source chose*.

Three verdicts, and only the first proposes anything:

| verdict | what it means |
|---|---|
| `mergeable` | one tranche under several names; nothing disagrees |
| `collides` | two sources confirm *different* capacities for it — one figure told twice would agree, this doesn't, so it refuses rather than picks |
| `ambiguous` | a bare ordinal that fits two families — "Facility 1" reads as both `Building 1` and `Phase 1` |

It never writes: `block_key` is the write path's identity, and folding it
further to collapse these names would also fold `Phase 1` into `Building 1`.
On the live database: 23 groups across 16 projects, 21 mergeable (44 rows
that are 21 things), 1 collides, 1 ambiguous. The collision is worth more
than the merges — Hyperion holds `Phase 1` at 2,000 MW and `Phase 1 IT Load`
at 1,500, which are facility load and IT load, two measurements of one
phase, not a disagreement to resolve by picking.

## Values that contradict each other

```bash
tracker logic check                    # free: rules and source disagreements
tracker logic check --severity error   # only the impossible ones
tracker logic check --read 20          # also have a model read 20 rows
tracker logic check --audit 20         # audit the evidence behind 20 rows' values
tracker logic resolve                  # work through them, one at a time
tracker logic resolve --code built_exceeds_planned   # one kind at a time
tracker logic resolve --auto           # only the repairs needing no decision
tracker logic conflicts                # let a model read every source for one field
tracker logic conflicts 10 --field investment_usd --apply
```

Every other check asks whether a value is *supported*. This one asks whether the
supported values *agree*: a row can be perfectly cited and still be impossible. A
campus marked `operational` whose construction track has reached nothing is either
the wrong phase or a missing milestone, and both citations behind it can be sound.

Three layers, and only the last costs anything.

**Rules** are free and state their reasoning, so you can disagree with one without
reading code. On the live database they find 21 impossibilities and 125 warnings
across 221 projects — energised before operational, 100 MW built against 32 MW
planned, an expected-online date 944 days in the past on a project still marked
under construction.

One of them exists because of a quirk worth knowing: `tracks.standing` reads an
event's *type* and never its date, so an `energized` dated next December counts as
reached today and drags the whole power track with it. `milestone_in_the_future`
reports that rather than fixing it, because changing what "reached" means would
move every track strip in the product.

Two of them ask a narrower question than the rest: not whether the row's fields
agree with each other, but whether a stored **number** agrees with the citations
under it.

| check | what it means |
|---|---|
| `value_above_its_evidence` | the row holds more than its claims and blocks can account for |
| `value_without_evidence` | the row holds a figure no source on it claims at all |

Both are restricted to money and megawatts — the fields that feed `tracker capex`
and the national totals, where an unsupported figure misstates a rollup rather
than just one row. On the live database: **5 and 22** findings across 20 projects.

Stargate Abilene is why they exist. The row read `mw_built = 1200` while the only
`mw_built` claim on it was a well-quoted **200**. Both "1.2 GW" quotes had since
been re-extracted as `mw_planned`, correctly — committed capacity is not energised
capacity — but `mw_built` merges by MAX, and MAX counted the value already stored
among its own candidates. **So it could not come back down.** The figure outlived
the claim that produced it by 1,000 MW, against a ~0.4 GW satellite read and the
project's own `phase-1` block of 200 MW serving.

A recompute lowers that row now: MAX and MIN honour the ratchet flag they always
took, and both write paths turn it off. These rules stayed, because a repair needs
something to re-derive *from* — and the collision check below could not see this
row at all, which is the point: a collision needs *two* claims on a field to
compare. One claim and a row that disagrees with it is the cheapest possible
version of the error, and it was invisible.

Both consult the block rollup as well as the claims, because a tranche's capacity
is cited capacity whether or not `reconcile` writes the sum onto the campus. The
first cut of the rule did not, and reported 28 rows behaving exactly as designed.

**Collisions** are two sources claiming different values for one field. The winner
is read back from `upsert.resolve_field` — the same function the write path used —
and printed with its reason. That reason is **not always "the better source won"**:

| field | decided by |
| --- | --- |
| `mw_built` | the largest **claimed** figure; energised capacity only grows, but the row's own value is not one of its candidates |
| `first_announced` | the earliest **claimed** date; that is what "first" means |
| `phase` | furthest along, unless a source says it stopped |
| `name`, `company`, `city`, … | first seen, never overwritten; churn beats staleness |
| everything else | credibility, then recency |

Getting that wrong is not cosmetic. The first version re-derived the winner as
though credibility always decided, and reported **73 of 221 rows** as having
drifted from their sources. None had. After asking the real resolver: **zero** —
which is the correct answer for a write path that is working.

**Judgement** is `--read N`, one LLM call per project, off by default. It catches
what a rule cannot phrase. Every finding must name two fields and quote its
evidence or it is dropped, it is never allowed to pick a collision winner, and
nothing it says is written. Measured honestly: across four rows read during
development it returned **nothing** — the guard rails demonstrably hold, and its
usefulness on this database is still unproven. The rules are carrying the value.

**The evidence audit** is `--audit N`, the same cost, and asks the prior question:
does the sentence recorded as a value's evidence actually state that value *for
this project*? The gate that stored the quote checked mechanically — the figure
appears in the sentence — and what survives that and still goes wrong is
semantic: a programme-wide total quoted as one campus's money, a figure about the
building next door, an aspiration recorded as a schedule. Rows are read costliest
first, because the audit exists to protect the capex sums and the dollars at
stake pick the order. Findings carry one of three verdicts — `unsupported`,
`misattributed`, `hedged` — plus a reason checkable against the quoted sentence
alone, and nothing is written: a person confirms a finding by demoting the value
in `tracker review`, and an unconfirmed investment figure already stays out of
the capex sums, so the repair path existed before the audit did.

**`logic resolve` is the part that makes the other 149 worth finding.** It first
re-runs the merge policy on any row whose stored values its own sources no longer
support — arithmetic, no decision needed. Then it walks you through the rest one
at a time, with the values and the quotes behind them on screen, and the answers
as single keys:

```
#164 Cologix — COL4  Columbus, OH
built_exceeds_planned 50 MW built against 36 MW planned
  mw_built = 50.0
    "50 MW of power will be available across three data halls"
  mw_planned = 36.0
    "adding 36MW of power to its total capacity in the Region"
  u  the plan was revised — raise mw_planned to mw_built
  c  the built figure is wrong — clear it
  v  it is fine as it is — mark the row verified
  s  skip    q  stop here
```

That is the distinction the tiers actually draw. A *model* may not assert
`mw_planned = 100`; an *operator* looking at both quotes may, and until now had
nowhere to put the answer. Each decision is written into the project's notes as
plain prose — the one class of note re-ingesting never regenerates — so the record
of a human overruling the data survives the next `sync`. Committed after every
answer, because somebody triaging forty rows will stop partway.

With no terminal (the console runs commands without a keyboard) it does the
automatic repairs, reports what needs a person, and stops.

**`logic conflicts` is the one place a model compares two contradicting
sentences.** Everything else was extracted from one article in isolation, and the
disagreements between articles are settled by a sort — quote-backed first, then
source weight, then date. That is the right default and it cannot tell a
*superseded* figure from a rival one. Hyperion held Meta's 2024 $10B over its 2026
$50B because both sources are weight 3, both are quote-backed, and crawl order
decided it.

So this shows one model every quote-backed claim about **one field**: the value,
the verbatim sentence, the publisher, and when they published. 492 fields on the
live database qualify — a field is only contested when two or more *quote-backed*
claims genuinely disagree.

Four properties, and three of them are refusals to do the obvious thing:

- **It cannot type a value.** The options are figures publishers actually printed,
  already stored with their quotes. The model returns one key from a closed list,
  so a sentence nobody published has no route into the database at all.
- **Refusing is a real answer.** Two credible publishers stating two figures with
  nothing to separate them is not a coin toss. A refusal writes nothing and the
  disagreement stays disclosed in the row's notes, with both citations intact.
- **Two calls per field, hard.** One to decide, one adversarial call whose whole
  job is to knock the answer down. If it succeeds the field becomes a refusal
  carrying the objection — a third call arguing with itself is unbounded spend.
- **`--apply` never assigns the field.** It marks the losing claims `superseded`
  on their own citations and re-derives the row, so a value still equals what its
  citations imply, and the 2024 article still says what it said in 2024.

Identity fields are excluded before a call is made: "Hyperion" against "Richland
Parish Data Center" is two names for one campus, `FILL_ONLY` says churn there is
worse than staleness, and ruling against a claim would not even move the value.
That is 174 of 666 candidates removed for free.

Run `tracker backfill dates` first. Both the tiebreak and this model reason from
publication dates, and a claim with none is shown as *"publication date unknown;
crawled 2026-08-10"* rather than as a date — a model handed a bare date would
conclude the article we read second is the later one, which is the mistake the
whole change exists to stop.

`logic check` is in `LLM_COMMANDS` even though its default run is free, because
`--read 50` spends fifty calls and the console's gate gates command names, never
flags. `logic resolve` and `logic conflicts` are gated too: one rewrites fields in
bulk, the other spends up to two calls per contested field.

**`tracker enrich` now calls this machinery itself**, as its last stage — see
*Completing one project, cost no object* above. `logic conflicts` still exists
standalone for the same reason `logic resolve --auto` exists alongside `enrich`'s
own repairs: a bulk pass over the whole database on demand, not tied to any one
project's run. The default `deepseek_reasoning_model` moved to `deepseek-v4-pro`
(from `deepseek-v4-flash`) alongside this change — `infer` and `conflicts` are
both one call per project or per contested field, hundreds rather than the
thousands extraction pays for, so the heavier model is affordable exactly where
these two calls happen.

## What the stored data actually rests on

```bash
python scripts/measure_extraction.py
python scripts/measure_extraction.py --mutants
tracker ingest crawl --stale-prompt --limit 20
```

`gaps` counts empty fields. This counts *full* ones, and asks the harder
question: does the value have a sentence behind it.

Two numbers already existed and neither answers that. The evidence gate's 98.7%
exact-substring rate measures the quotes that exist — a statement about a
population it never looked at. And "66% of claims carry no quote" counts the
model's raw output, most of which the gate correctly demoted to 待确认, so it
reads as a scandal and is mostly the gate working.

The measure that matters splits values three ways, and the third is the only
defect. Measured before and after one full re-extraction:

| | before | after |
|---|---|---|
| quote-backed | 368 (49.2%) | 434 (52.0%) |
| 待确认 — no quote, **and the row says so** | 286 (38.2%) | 385 (46.1%) |
| confirmed, no quote, and nothing says so | **89 (11.9%)** | **11 (1.3%)** |
| total values measured | 748 | 835 |

The second row is the gate doing its job. A measure that lumped it in with the
third would report 286 successes as failures and hide the rows that actually
needed re-reading.

**All 89 came from prompts that no longer exist** — 61 from `extract-v1@8eb51f2a`
and 28 from `extract-v1@cef10fb4`, both predating migration `0007`, the migration
that added the column a per-field quote lives in. There was nowhere to record the
sentence when those rows were written, so they read as established ever since.
None came from the current extractor.

That is a fact about *history*, not about any row, which is why no per-row check
surfaced it: `tracker audit` asks whether a number could be true and `logic check`
asks whether two fields agree, and each of those 89 values passed both.

`--stale-prompt` is the re-read, and `source.extractor` is what makes it
possible: it has recorded which prompt produced every row since `0001` and
nothing had ever compared it to the current stamp. It serves from the article
cache by default, so it is a re-read rather than a re-fetch — the point is to
find out what a better prompt makes of the *same* article, and re-fetching would
confound that with the page having changed.

**Pair it with `--cached-only`.** Serving from the cache "by default" only covers
the URLs that are *in* the cache; a miss used to fall through to a fetch, silently,
which turns a free re-read into a paid crawl. On the live database three quarters
of the stale URLs have no cached text, so an operator asking to re-read 113 pages
would have paid for 1,754 fetches. `--cached-only` refuses the miss and reports it
as `not cached` in the run summary, so a run that skipped most of its worklist does
not read as one that covered it. Same discipline as `backfill`'s `refetch=False`.

`--mutants` is the other half: it plants known faults in a throwaway copy and
counts what gets caught. It exists because this README and `HANDOFF.md` both
cited *"16 planted mutants, all caught"* as the evidence for `tracker audit`, and
no script, test or commit contained it — the run was manual, against a copy of a
live database nobody kept. A claim about detection that cannot be re-run is not
evidence, so now it can be.

**Why 14 remain, and why re-reading cannot clear them.** The write path is keyed
on `(project_id, url)`, but which project an article describes is re-derived from
the article on every read. So when a re-read routes to a different project — or
to none — the original row keeps its old source at its old vintage, orphaned. Of
61 URLs still stale after the run, 28 now return **no project at all**, 27 route
elsewhere, 2 are refused by the prose floor and 4 are 403s with no cached copy.
The Switch/Data Foundry acquisition story is the clearest case: the old prompt
built two campuses out of it, and the current one declines it entirely. Whether
that is the gate getting stricter or a regression is a judgement, so the rows are
reported and left alone rather than deleted.

## What a value is a value of

Hyperion (#10) carries three investment figures, and they are not a disagreement:

| figure | the sentence behind it | what it measures |
|---|---|---|
| $10B | *"the buildout of the infrastructure itself"* | this site |
| $27B | the Blue Owl campus joint venture | this site, later |
| $50B | *"more than $50B of investment to the region"* | roads, water, sewage, jobs |

Every merge policy in this schema exists to pick a winner among rival claims about
one quantity. These are three measurements of three different things, so picking a
winner is the wrong operation — and the row held the oldest and smallest of them
while its own notes read *"expanded to up to $50 billion"*.

Four qualifiers now travel with each claim, in `source.claim_meta`:

- **`scope`** — this site, a named tranche (`block:Phase 1`), the programme, the
  region, the operator's portfolio, or `unnamed`. `unnamed` is a correct and
  common answer; guessing `this_site` to be helpful is the error the axis exists
  to prevent.
- **`bound`** — `exact`, `approximate`, `at_least`, `at_most`. Prompt RULE 4 used
  to say *"500-700 MW → 500 (the LOWER bound; say so in notes)"*, destroying the
  range on purpose and routing it to prose nothing could read back.
- **`modality`** — `speculated` → `targeted` → `planned` → `contracted` →
  `achieved`.
- **`as_of`** — the date the claim was true, when it differs from the article's.

**One of the four axes failed its own test, and was not promoted.** The kill
criteria were written down before the corpus was re-read: an axis whose modal
value exceeds 95% is reporting a default rather than the article.

| axis | coverage | modal value | verdict |
|---|---|---|---|
| `bound` | 32.0% | `exact` 87.4% | passes |
| `modality` | 32.0% | `planned` 84.3% | passes, weakly |
| `scope` | 32.0% | `this_site` **96.9%** | **failed** |

`scope` failed the decisive test too. Across every `investment_usd` claim in the
database it returned 44 `this_site`, 4 `unnamed`, 1 `block:VA13` — and **zero
`region`, zero `programme`**. The two values that would have separated Hyperion's
$10B buildout from its $50B regional figure never fired once. So it stays
captured and measured, and nothing is built on it.

`bound` earned its place on the same row: *"roughly $27 billion"* reads
`approximate`, *"more than $50 billion"* reads `at_least`.

**Every axis is checked against the quote.** That is the whole design, and the
reason to expect these to carry information where `risk.severity` does not —
severity is a judgement no article states, so it reads `watch` on every risk in
the database, fully populated and carrying nothing. A bound is not a judgement:
the article either hedged the number or it did not, and the hedge is a word in the
sentence. `bound: at_least` needs one. `scope: block:<label>` must resolve to a
tranche on the record. `modality: achieved` is demoted to `targeted` outright when
the date is in the future — which is Hyperion's live defect, where *"an interim
milestone of 1.5 GW is being targeted by the end of 2027"* was stored as
`announced`, dated 2027-12-31, and counted as **reached** on the track strip.

The check runs against the *stored* quote, never the model's offered text.
`_verbatim_run` may have repaired the quote to the article's own words, and
checking the model's version would let it license a hedge by writing one into a
sentence nobody published — the fabrication route the evidence gate closed,
reopened one level up.

**A refused axis never costs the value.** `axis_gate` returns labels and is never
given the figure, so the worst case is a claim described no better than it was
yesterday, and a model labelling everything `at_least` to sound careful gains
nothing. Nothing reads the axes to choose a value yet, deliberately: `source_type`
became load-bearing before anyone measured it, and `confidence.find_conflicts` is
now documented as too risky to correct.

On screen they cost almost nothing. `bound` is one glyph on the number (`~5,000`,
`≥$50B`) rather than a column that would be empty on most rows and would break the
numeric alignment on the rest. `scope` and `modality` appear as chips in the
citation popover **only when they say something** — no chip reading "this site" on
a row where nothing is in dispute. That is what the previous round of added fields
got wrong: they rendered unconditionally, so they were noise everywhere.

And dates now print at the precision the source gave. `normalize.parse_date` has
always returned `day|month|quarter|half|year`, and its docstring has always said
why it matters; nothing outside that module had ever read it, so a year-only
"in 2024" rendered as `2024-01-01`. It now renders as `2024` — shorter, and
claiming less.

## Crawl order is not publication order

The measurement reports one more thing, and it is not fixed by re-reading.

`upsert.claims_by_field` ranks claims by `(confirmed, weight, fetched_at, url)`.
Most of this corpus is trade press, so ties on credibility are the common case,
and `fetched_at` is the moment the crawler happened to visit the page. Six stored
values are decided that way against publication order — Aligned Phoenix holds
65 MW from a 2017 article over 400 MW from a 2022 one, and Hyperion holds Meta's
superseded $10B over the $27B that replaced it, on a row whose own notes read
*"expanded to up to $50 billion"*.

Re-extraction made that sharper rather than better: it gave all three Hyperion
figures quotes, so they now tie three ways and crawl order still picks.

The date was already being collected — `discover` writes it to
`ingest_url.published_at` from the feed — and nothing downstream had ever read
it, because there was no column on `source` to carry it to. Migration `0014` adds
one and backfills it by URL (241 of 553 sources).

**But only a feed supplies a date, so the tiebreak was starved.** On the live
database `published_at` is set for **326 of 2,758 citations (11.8%)**, and the six
inversions above are a floor of a floor: the measurement can only see a pair where
*both* sides carry a date. Counting every stored value whose rivals tied on
credibility and disagreed gives **506**, of which 439 are invisible today.

The reference case is the argument for fixing it at the source rather than
refining authority. Hyperion's $10B and $50B come from **the same publisher**:

| source | value | type | published | fetched |
|---|---|---|---|---|
| 661 | **$50B** | government_doc | **2026-07-13** | 2026-08-09 11:39 |
| 1116 | $10B | government_doc | **2024-12-04** | 2026-08-10 05:35 |

Both `opportunitylouisiana.gov`, both quote-backed, both weight 3. No
source-type refinement can separate them — sub-dividing `government_doc` leaves
them in the same bucket however fine the categories get. Eighteen hours of crawl
order picks the stale one; publication order picks correctly by nineteen months.

So the fetcher now reads the date out of the page itself, in
`fetch.published_date`, on the raw HTML **before** `html_to_text` discards it.
That ordering is the whole trick: the article cache stores converted text, and
none of its 585 files contains `datePublished`, `article:published_time`, `<time`
or a JSON-LD block — the metadata was already gone, so no backfill over the cache
could ever have recovered a date.

A ladder, cheapest and most explicit first, mirroring the fetcher's own:

| rung | reaches |
|---|---|
| JSON-LD `datePublished` | 5 of 10 sampled live pages |
| `article:published_time` / `og:published_time`, either attribute order | — |
| `<time datetime="…">` | 2 of 10 |
| the URL path — `/2026/07/13/slug` | 175 citations nothing else dates |

7 of 10 sampled pages carry a machine-readable date, and both sides of the
reference case resolve. The URL rung is free and offline and takes coverage to
**18.2%** on its own (project #10 from 6 dated citations to 15).

**It returns `None` rather than guessing**, and refuses a date before 2000 or more
than two days ahead — a copyright year and a scheduled-content placeholder are
the two things that selector routinely catches. `upsert._published_at` already
refused to invent a date for the same reason: a wrong timestamp does not degrade
this tiebreak, it inverts it.

`record_url` fills the column and never overwrites it. That guard is load-bearing
rather than tidy: a cached body reports no date by construction, so without it a
re-extraction pass over the cache would erase every date the original fetch found.

**Articles already stored need a backfill**, since they were fetched by a version
that discarded the metadata:

```bash
tracker backfill dates                      # what the URL paths alone would date
tracker backfill dates --apply              # write those
tracker backfill dates --refetch --apply    # ask the publishers too
```

It only considers URLs where a date changes something. Two things read the column
— `upsert._published_at` for a URL backing a citation, and `crawl.published_dates`
for one still queued — and of 5,552 undated rows only **1,778** are either. The
other 3,774 are `no_project`, `fetch_error` and orphans, so the default scope cuts
the crawl by 68%; `--all` widens it.

Report-only until `--apply`. It deliberately does **not** go through `crawl.run`:
that path consults `_split_cached` first, so a backfill routed through it would
read the local text file, find no metadata in it, and conclude the publisher states
no date. `--limit` bounds the fetching, not the run — capping the free pass the way
`backfill blocks` caps LLM calls throttled it to 25 of 5,552 rows.

```bash
TRACKER_MERGE_BY_PUBLICATION_DATE=1 python scripts/measure_extraction.py
```

**The tiebreak is off by default, and stays off — that is a measurement, not
caution.** The backfill has run: 1,011 pages gave up a date, and citation coverage
went from **11.8% to 67.6%**. With the column filled, **65** values are settled by
crawl order against publication order. Flipping the flag fixes all 65 and gets
plenty of them wrong — of the 40 numeric ones it raises 18 figures and lowers 22:

| | keeps | would take |
|---|---|---|
| #10 `investment_usd` | $10B (2024-12-04) | **$50B** (2026-07-13) — the reference case, fixed |
| #1 `mw_planned` | 450 (2025-10) | **2,000** (2026-07) |
| #20 `mw_planned` | 100 (2016) | **545** (2026) |
| #78 `customer` | **Meta** (2022-04) | Facebook (2022-10) — the older name, restored |
| #389 `investment_usd` | **$86.5M** (2025-10) | $40B (2026-03) — a programme total for a site |
| #63 `mw_planned` | **120** (2026-03-16) | 6 (2026-03-17) — one day later |
| #164 `mw_planned` | **200** (2022) | 50 (2024) |

The failure a date cannot see is a later article about a different **scope**: one
building of a campus, one phase of a programme, is newer without being a
restatement of the whole. Publication order is the more *defensible* rule and it is
still the wrong lever on its own.

So the dates earn their crawl somewhere else — `tracker logic conflicts` shows
them to a model that reads the sentences rather than ranking them by date, which
is the distinction the sort cannot make. Run the line above to see the current
list before deciding; the flag changes the policy, so stored values move as each
row is next written.

The same fact fixes a second bug. The prompt's `ARTICLE_DATE` was always
`unknown`: `extract_one`'s `published_date` parameter existed, the prompt
interpolated it, and no caller ever passed it. RULE 5 resolves relative timing
against it, so with the date unknown every "next year" was correctly forced to
null. Nothing was fabricated — the cost was silent, in schedule fields never
extracted for want of a value already in the database.
