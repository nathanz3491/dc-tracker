# Known limitations

What this database gets wrong, or cannot yet say, as of the dates below.

Part of the [dc-tracker documentation](README.md).

---

This page exists because most of these were found more than once. Each was
measured, written into a plan or a changelog entry, and then re-derived months
later by somebody reading the same rows and reaching the same conclusion. One
register with a date on every line is cheaper than that.

**What belongs here.** A defect in the *result* — a number a reader would act on
that is wrong, missing, or not what its column name says. Not code quality, not
a wish list, not anything the code already says plainly about itself.

**Committed deliberately**, unlike the plans and reviews `CLAUDE.md` §6 keeps
local. Those are snapshots of a decision in progress and go stale when the code
lands. This is a standing statement about the dataset, in the same class as
[Government sources](government-sources.md): a fact a reader needs, and one they
would otherwise have to rediscover.

**Every line carries a date.** `first observed` is when the problem was first
measured, not when it started. `status` is `open`, or `fixed` with the date.

---

## Open

### 2 — Around half of stored values are not quote-backed

| | |
| --- | --- |
| first observed | 2026-08-06 |
| status | **open** |
| measured | 435 of 808 stored values quote-backed after the second full re-extraction; 11 confirmed with no quote at all |

The README opens by saying "every non-null fact traces back to a source URL with
a confidence score". The evidence gate makes that true of what it *confirms*, and
the rest of the database is the 待确认 tier — extracted, kept, and honestly
labelled as unproven. That is the gate working, not a defect.

The defect is the narrower bucket: **confirmed, with no quote**. Nothing on those
rows says the value is unsupported, so every reader and every rollup treats it as
a fact. It fell 89 → 14 → 11 across two re-extraction runs. The 11 that remain
are on rows whose articles no longer support them, which is a judgement about
stored values rather than a bug to fix.

Read `tracker clean`'s `values_backed` condition for the current figure rather
than trusting this one.

### 3 — The merge tiebreak is crawl order

| | |
| --- | --- |
| first observed | 2026-08-07 |
| status | **open, deliberately** — needs a person with the report in front of them |
| measured | six stored values decided against publication order |

Most of this corpus is trade press, so two sources tie on credibility constantly
and the tiebreak decides. That tiebreak is `fetched_at` — when the crawler
happened to visit — which is arbitrary with respect to the truth.

The live consequence is on record: **Hyperion (#10) holds Meta's superseded $10B
over the $27B that replaced it.** Re-extraction did not fix it, because giving
all three figures quotes left them tied.

`source.published_at` exists (migration `0014`) and
`Settings.merge_by_publication_date` reads it. It is **off**, and that is a
judgement rather than an oversight: turning it on takes the six inversions to
zero, and also moves #116 from 120 MW to 40 MW because the smaller figure was
published a day later. Publication order is the more *defensible* rule, not the
one that always yields the better number. Flipping it also needs a bulk
recompute, which `tracker backfill derive` now provides.

### 5 — The schedule columns cannot answer the schedule question

| | |
| --- | --- |
| first observed | 2026-07-31 |
| status | **open** |
| measured | `expected_online` on 29 of 124 projects, 17 of those already past; 34% of dated projects land on 1 January |

"Whose pipeline lands next quarter" is the question `tracker capex --by-quarter`
exists to answer, and it cannot. A source saying "sometime in 2027" normalises to
2027-01-01, so a third of the dated rows carry a date nobody stated.
`capex.date_precision` measures this and the footer discloses it — read the
quarters as a shape and the years as the number.

Migration `0015` added `first_announced_precision` and `expected_online_precision`
so a reader can at least see which dates are the vague ones. What is still
missing is a pipeline view that buckets by stated precision rather than by the
normalised day.

### 6 — Corroboration counts syndication as independence

| | |
| --- | --- |
| first observed | 2026-09-10 |
| status | **open** |

`confidence.compute` counts independent sources by registrable domain, and
reaching 3 requires two of them. One operator press release reprinted by three
trade outlets is three domains, so it reaches the top of the scale on what is
really one underlying claim.

The tertiary rule already handles the narrow version of this — a Wikipedia
paragraph written from a press release cannot corroborate it — but syndication
between publishers is not covered, and trade press dominates the corpus.

Fixing it needs a way to tell a republished wire story from independent
reporting. The cheap signals are a near-identical body and a publication date
within a day of another source's.

### 7 — The one verified structured government source is not ingested

| | |
| --- | --- |
| first observed | 2026-07-31 |
| status | **open** |
| measured | 211 data-center buildings and 70 campuses available, naming tenants |

[Government sources](government-sources.md) records four bulk routes tested and
four rejected, which is the honest state of that search. It also records the one
route that *did* verify: Prince William County's ArcGIS layers, free and
unauthenticated, with `BuildingStatus`, `PlannedGFA`, `ApprovedGFA`, `OCCDate`
and a `CampusName` that names the tenant.

Nothing reads it. `tracker/ingest/` has no ArcGIS path, so Loudoun and Prince
William — between them the largest data-center market on earth — are covered by
whatever trade press wrote about them.

Two constraints for whoever builds it, both already established: **GFA is square
feet, not megawatts**, and converting is an estimate this project does not store
as fact, so it must be its own field and never `mw_planned`. And a county's own
`BuildingStatus` is a government document's claim, which is `government_doc`
weight — genuinely authoritative, unlike the ISO queue keyword match.

The symptom of having no power-side sources is visible in the risk table:
**2 open `transmission` risks across 124 projects**, which is obviously wrong for
this industry.

### 8 — The definition of done cannot be evaluated, and nothing is checked against reality

| | |
| --- | --- |
| first observed | 2026-09-10 |
| status | **open** |

Two halves, and the second is the larger.

`tracker/seed/required-projects.txt` is **empty**. The PRD's definition of done
names 30 specific projects, that list is not in the PRD text and was never
captured, so `tracker verify` reports progress against a target *count* instead
of the actual acceptance criterion. `tracker/required.py` is built and waiting
for somebody to paste the list in.

More broadly: **every quality measurement in this repo is internal.** Quotes are
checked against their own article, negative controls run across publishers,
duplicate detection is replayed against the project's own past merges,
`tracker audit` asks whether a number can be true. Nothing compares a stored
figure to an external record of the same site. A perfectly evidence-gated
database can still faithfully inherit an industry's promotional numbers, and
today nothing would notice.

### 10 — Re-reading an article can orphan the source it wrote

| | |
| --- | --- |
| first observed | 2026-08-07 |
| status | **open** |
| measured | of 61 URLs still stale after a full re-extraction, 28 yield no project at all |

The write path is keyed on `(project_id, url)` and project identity is
re-derived from the article every time. So a re-read that routes to a different
project — or to none — leaves the original `source` row sitting at its old prompt
vintage, and no amount of re-running the backfill will reach it.

Whether that is the gate getting stricter or a regression is a judgement about
the affected stored values, so those rows are reported and left alone. The
Switch/Data Foundry acquisition story is the clearest case: it built two campuses
under an older prompt and is declined outright by the current one.

### 11 — The `bound` hedge check is not positional

| | |
| --- | --- |
| first observed | 2026-08-07 |
| status | **open** |

`crawl.axis_gate` asks whether a hedge word appears in the sentence, not whether
it attaches to *this* number. Source 12's quote reads *"require more than $50
billion in investment, up from the roughly $27 billion plan"* — two figures, two
hedges — and the gate licensed `approximate` from a "roughly" belonging to the
other number.

`vocab.bound_from_quote` is positional and is what the display path uses, so the
two disagree. The gate cannot simply adopt it: `axis_gate` is deliberately never
handed the figure, which is what keeps it a pure function of the entry and the
quote.

The new `basis` axis has the same shape of exposure and floors it rather than
solving it — where a sentence contrasts two bases, the ordering prefers the
reading that keeps a figure *out* of `it_load`, because that is the direction
that cannot inflate a capacity total.

### 12 — An operator's sign-off never expires

| | |
| --- | --- |
| first observed | 2026-09-10 |
| status | **open** |

`project.last_verified_at` records that a person checked a row, and
`confidence.compute` reads it as `operator_verified`, which is one of the two ways
a row reaches 3. It has no expiry. A row verified once carries that bonus
indefinitely, however far its sources have since moved.

More generally there is no staleness concept per row: nothing re-checks a project
whose `phase` was last read a year ago, and `tracker clean`'s `vintage_current`
condition measures the *prompt* that read it rather than the age of the reading.

### 13 — `/api/health` cannot report the commit from a worktree checkout

| | |
| --- | --- |
| first observed | 2026-09-10 |
| status | **open, low priority** — production is unaffected |

`webui.deployed_commit` reads `home()/".git"/"HEAD"` as a path, which is correct
for an ordinary checkout and for the production host. In a **git worktree** `.git`
is a pointer *file*, not a directory, so the read fails and the endpoint reports
`None` while `git rev-parse` in the same directory answers correctly.

`tests/test_webui.py::test_health_reports_the_commit_it_is_serving` fails for this
reason when the suite is run from a worktree. The fix is to follow the
`gitdir:` pointer when `.git` is a file.

---

## Fixed

### 1 — One `company` column, no roles, so one campus became four rows

| | |
| --- | --- |
| first observed | 2026-08-03 (colleague review) |
| status | **fixed 2026-09-10** — migration `0023`, `tracker/parties.py` |
| measured | 48 of 90 hand-performed merges had no key-level signal connecting the two rows |

The extraction prompt defined `company` as "who builds **and** operates the
site", so two roles arrived in one string, `customer` carried a third, and the
utility and the landowner had nowhere to go. The Abilene campus was stored four
times — as Crusoe, as Oracle, as OpenAI and as "OpenAI/Oracle" — and each name
minted its own `dedup_key`. Richland Parish was stored twice, as Meta and as
Entergy Louisiana. Every one of those rows contributed its full capacity to a
buyer's position.

`project_party` records who plays which role, with a quote per party.
`capex.attribute` reads it, and the duplicate report gains the signal a
comparison of company strings structurally cannot see — where four articles each
named one party and the composite string was never written anywhere.

`project.company` is **not** derived from it, deliberately: `dedup_key` is
`company|locality|state`, it is UNIQUE, and nothing re-keys it, so a `company`
that moved after insert would leave the key naming a company the row does not.

Not fixed by this: one operator's two distinct campuses in one municipality still
share a row. AZP-2 and AZP-3 are two campuses, and
[design decisions](design-decisions.md) names a `campus` column as the remedy.
That is a re-key of every live row and belongs in its own change.

### 4 — A megawatt and a dollar each meant several different things

| | |
| --- | --- |
| first observed | 2026-09-10 |
| status | **fixed 2026-09-10** — migration `0024`, `vocab.CLAIM_BASES`, `capex.out_of_scope_investment_ids` |

Two halves.

**Capacity.** `mw_planned` received the computing load, the whole facility's draw
and a generator's nameplate — three quantities 30% or more apart. The prompt
explained the difference to the model and `ingest/pjm.py` refused to put a queue
row's nameplate in the column, and then the schema discarded the distinction
everywhere else. `claim_meta` now carries a `basis`, read out of the sentence
already stored beside the figure and **never asked of the model**: that is the
correction migration `0015`'s `scope` axis earned by failing its own
pre-registered kill criterion at 96.9% `this_site`. `tracker backfill basis`
fills it for free, and the share still at `unspecified` is disclosed in the
`capex` footer — that share is the measurement, not a gap in it.

**Money.** The `scope` axis had recorded whether a figure was this site's,
a programme's, a region's or a portfolio's **since migration 0015, and nothing
read it.** `capex` excluded only `out_of_scale`, the `$/MW` ratio ceiling, so a
figure the gate had already labelled `programme` still counted in a buyer's
position whenever the ratio happened not to fire. It is now excluded and
disclosed separately. `unnamed` still counts, and must: it is the envelope's
default, so excluding it would drop most of the database's capex because nobody
wrote a qualifier.

Because the `programme` label itself never fired once — zero `programme` and zero
`region` across the whole corpus — `capex.programme_figures` adds the mechanism
that plan asked for: the same rounded figure, for the same operator, standing as
the winning `investment_usd` on two or more distinct sites. A site's own cost
cannot be identical to another site's. It carries a pre-registered kill criterion
in its docstring: if it flags nothing on production it is decoration and must not
become load-bearing.

### 9 — The live database went from 1,189 rows to 300

| | |
| --- | --- |
| first observed | 2026-08-12 |
| status | **not a defect — explained 2026-09-10** |

Recorded here because `HANDOFF.md` carried it as "unexplained" for eight
consecutive daily entries and it is the kind of thing a reader finds and worries
about.

It was **deliberate**. The rows removed were duplicates of a kind this schema
produces readily: one data center stored repeatedly, or a single building inside
one stored as though it were its own campus. A backup was written two minutes
before (`data/tracker.backup-before-purge-20260812.db`, 1,189 rows), which is why
the drop is visible at all.

Problem 1 above is the structural cause, and the reason the purge was manual work
rather than a merge the tool could perform.

---

## Reading this against the code

Three commands answer "is this still true", and none of them costs an API call:

```bash
tracker clean --snapshot
tracker capex
tracker audit
```

`scripts/measure_extraction.py` and `scripts/eval_pairs.py` re-run the two
measurements most of these entries quote. Both are read-only, so both can be run
on the production host — which is where they should be run, because a development
copy is a different corpus rather than a smaller view of the same one. That
mistake reversed a headline conclusion once; `duplicate-shapes.md` carries the
warning.
