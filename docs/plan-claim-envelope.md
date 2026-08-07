# Plan — the claim envelope, and the stratigraphy underneath it

Written 2026-08-07, after examining Fairwater (#1) and Hyperion (#10) closely to
infer what is structurally wrong rather than what is wrong with those two rows.

> **Status.** Phases 0–4 implemented. **Phase 5 is not done, and for `scope` it
> should not be** — its pre-registered kill criterion fired at 96.9% `this_site`,
> and the decisive Hyperion test it was designed for failed. The axes are
> captured, verified and displayed; nothing reads them to choose a value.
> Four of this plan's own assumptions were wrong on measurement, corrected inline.
>
> Measured over two full re-extractions of the corpus (467 LLM calls total):
> silent defects **89 → 14 → 11**, quote-backed values 368 → 434 of a total that
> grew 748 → 835.

## The specimens, and what they are a sample of

Both rows are 8-source flagship campuses — the top few percent of a 207-row
database by coverage. So they show the failures of the *abundant* regime: scope
collision, contested merges, syndication. They say almost nothing about the
sparse regime, which is most of the database. That sampling bias is worth
holding onto, because every rule in this system is collision-based and needs two
claims to compare, which means quality assurance is strongest exactly where the
data is already best.

## The thesis

Every claim arrives carrying five things — a **scope**, a **precision**, a
**modality**, a **time**, and a **provenance**. The schema has one slot per
(project, field). So all five are stripped at write time, and the merge policy is
the stripping function.

Three pieces of evidence that the pipeline already computes what the schema
discards:

1. `normalize.parse_date` returns `day|month|quarter|half|year` and documents why
   an operator needs it. **Nothing outside that module had ever read it.**
2. Prompt RULE 4 said `"500-700 MW" -> 500 (the LOWER bound; say so in "notes")` —
   the range destroyed on purpose and routed to prose.
3. The extractor's own summaries contain the revision history the structured
   output throws away: Hyperion's notes read *"Original 2024 announcement was $10
   billion; expanded to up to $50 billion"* while the column holds $10B.

Supporting evidence from the repo's own history: the reporting layer had to be
rewired to call the writer's `resolve_field` because re-deriving it disagreed
**on 73 rows**; `confidence.find_conflicts` is a documented third copy of one
rule, already inconsistent and too entangled to fix; and `tracker audit` exists
as a second rule engine because "this number cannot be true" was not expressible
in the first. Meanwhile the migrations tile the missing axes almost exactly —
`capacity_block` reconstructs scope, `unconfirmed_reasons` provenance,
`milestone_in_the_future` modality, `ingest_url.published_at` time.

## What measurement changed

**Wrong #1 — "66% of claims carry no quote" was mostly an artifact.** That counts
the model's raw output, most of which the evidence gate correctly demoted to
待确认. The number that matters is the share of *stored* values whose winning
source recorded a sentence, and the defect is the narrower bucket: **confirmed,
and no quote**. 89 of 748, and every one from two prompt vintages predating
migration `0007`, the migration that added the column a quote lives in. None from
the current extractor. The gate works; the damage was stratigraphy.

**Wrong #2 — a hand-rolled version of that measurement said 83, not 89.** The
difference is `mw_built`, `first_announced` and `phase`, whose merge policies are
MAX/MIN/PHASE rather than PREFER_WEIGHT: a re-derived sort picks a different
winning source than the write path did. Which is why `tracker/quality.py`
re-implements nothing and asks `gaps.provenance` instead.

**Wrong #3 — the plan proposed a new `tracker reextract` command.** It became a
fourth URL selector on `ingest crawl`, parallel to `--from-queue`, because
choosing URLs is the only thing that differs; the extractor setup, dry-run, cache
handling and reporting are then shared rather than duplicated.

## What the re-extraction did and did not fix

The first full run (228 URLs, 230 LLM calls) took silent defects **89 → 14** and
quote-backed values 368 → 435 of a total that grew 748 → 808.

It did **not** fix the recency inversions, and made one sharper: giving Hyperion's
$50B claim a quote left three quote-backed trade-press figures tied, still decided
by crawl order. That is a tiebreak problem, not an evidence problem, and is why
`published_at` (migration `0014`) is a separate phase.

It also surfaced something nobody had asked about. **Re-reading a URL does not
always refresh the row it wrote.** The write path is keyed on `(project_id, url)`
and project identity is re-derived from the article every time, so a re-read that
routes to a different project — or to none — orphans the original source at its
old vintage. Of 61 URLs still stale afterwards, 28 now yield no project at all:
the Switch/Data Foundry acquisition story built two campuses under the old prompt
and is declined outright by the current one. Whether that is the gate getting
stricter or a regression is a judgement about six stored values, so those rows are
reported and left alone.

## The design rule that decides whether the axes survive

An added field is only worth having if something can check it. The split in this
codebase is stark:

- **`quotes` works** — 98.7% exact-substring, because the string is either in the
  article or it is not.
- **`severity` rotted** — `watch` on every risk in the database. No article ever
  states a severity, so nothing could ever check it.
- **`source_type` is worse than rotted** — a regex made an unverifiable judgement
  about authority, became load-bearing for value selection *and* confidence, and
  is now too entangled to correct.

So every axis carries a verification predicate in `crawl.axis_gate`, and a
refused axis degrades to a neutral label without ever touching the value. A model
labelling everything `at_least` gains nothing.

Ranked by how checkable they are before the run, with the measured verdict after
it. **The kill criteria were pre-registered and one of them fired.**

| axis | coverage | modal | verdict |
|---|---|---|---|
| `bound` | 32.0% | `exact` 87.4% | **passes** |
| `modality` | 32.0% | `planned` 84.3% | passes, weakly |
| `as_of` | 5.1% | — | passes |
| `date_precision` | 0.1% | `year` 100% | too few to judge |
| `scope` | 32.0% | `this_site` **96.9%** | **FAILED** |

### `scope` failed, and the decisive test is the one it was built for

The pre-registered prediction was that scope would separate Hyperion's three
investment figures into `this_site` / `region` / `programme`. It did not:

| figure | the sentence | scope assigned |
|---|---|---|
| $10B | *"the buildout of the infrastructure itself"* | `this_site` ✅ |
| $27B | *"roughly $27 billion in total development costs"* | `this_site` ✅ |
| $50B | *"more than $50 billion in investment"* | `unnamed` ❌ |
| $50B | *"…campus will have 5 gigawatts"* | `unnamed` ❌ |

Across the whole corpus, `investment_usd` claims came back **44 `this_site`, 4
`unnamed`, 1 `block:VA13` — and zero `region`, zero `programme`.** The two values
that would have solved the case never fired once. An axis whose informative
values are never produced is decoration, however carefully it is verified, so
**`scope` must not become load-bearing** and the `region`/`programme` distinction
needs a different mechanism than asking the model for a label.

The one part that worked is the part with referential integrity: `block:VA13`
resolved against a real tranche. That is the checkable half and is worth keeping.

### `bound` passed, on exactly the case it was built for

*"roughly $27 billion"* → `approximate`. *"more than $50 billion"* → `at_least`.
That is the range information prompt RULE 4 used to destroy.

**One weakness found and not yet fixed:** the predicate asks whether a hedge word
is in the sentence, not whether it attaches to *this* number. Source 12's quote
reads *"require more than $50 billion in investment, up from the roughly $27
billion plan"* — two figures, two hedges, and the gate licensed `approximate`
from a "roughly" belonging to the other number. The check needs to be positional,
not merely present.

## Open decisions, for a person

- **The `published_at` tiebreak is off.** Turning it on takes six inversions to
  zero, but #116 moves from 120 MW to 40 MW because the smaller figure was
  published a day later. Publication order is the more *defensible* rule, not the
  one that yields the larger number.
- **Flipping it needs a bulk recompute that does not exist.** The flag changes the
  policy; stored values only move as each row is next written.
  `recompute_from_sources` exists but is reachable only through `merge`.
- **The envelope costs tokens.** The longer schema pushed replies past
  `max_completion_tokens` on some articles, each costing a corrective retry.
- **`events[]` still bypasses the evidence gate entirely** — no quote required or
  checked, which is how Hyperion's 2027-12-31 `announced` event exists. Fixing it
  needs an `unconfirmed` column on `event`, so it is named here rather than
  half-done.
- **14 silent defects remain**, all on rows whose articles no longer support them.
- **`news.microsoft.com` is now `general_media`.** The subdomain rule required a
  known operator domain; a genuine newsroom absent from `feeds.toml` loses weight.
  Adding it there is the designed fix.
