# Plan — what breaks when a project has 26 sources instead of 2

Written 2026-08-09, after search, Wikipedia reference mining and the escalation
ladder took the best-covered rows from 8 citations to 21–26.

> **Status.** Nothing here is implemented. This is the measurement and the design
> argument, recorded before building anything, because two of this project's four
> added axes rotted for want of exactly that — see "The rule that should govern
> both" below.

Every number in this document is from `data/tracker.db` on 2026-08-09: 252
projects, median **2** sources per project, best-covered rows at **26** (#10
Hyperion) and **25** (#1 Fairwater).

---

## Part 1 — `logic check` grows quadratically, not linearly

**The claim, corrected.** It grows **quadratically** in sources per project, not
exponentially. That distinction is the whole reason this is fixable: a quadratic
term can be pruned, capped and grouped, where an exponential one cannot be
managed at all, only avoided.

A collision needs *two* claims on one field to compare, so a field with `n`
claims implies `n(n-1)/2` comparisons. Measured today:

| | today |
|---|---|
| source collisions reported | **275** |
| of which impossible | 16 |
| claim-bearing (project, field) cells | 2,547 |
| pairwise comparisons implied | **6,610** |

The worst cells are already the well-covered rows, and they are dominated by
fields nobody disputes:

```
#1 city       25 claims ->  300 pairs
#1 company    25 claims ->  300 pairs
#1 country    25 claims ->  300 pairs
#1 name       25 claims ->  300 pairs
#1 state      25 claims ->  300 pairs
#1 phase      24 claims ->  276 pairs
```

**Extrapolated**, if every project reached #10's 26 sources: 325 pairs per field,
2,275 per project across the seven contested fields, **~573,000 comparisons**
across the database. The cost is not the arithmetic — that is cheap — it is that
each surviving finding is a question addressed to a person, and 275 already
exceeds what anybody works through. `logic resolve` was built precisely because
"149 findings" was unusable, and this multiplies the input to it.

### What is actually wrong, and what merely looks wrong

Three distinct problems hide inside that 275, and they want opposite treatments:

1. **Identity fields are not contested.** `city`, `company`, `country`, `name`,
   `state` account for 1,500 of the 6,610 pairs on one project, and they are
   `FILL_ONLY` — the merge policy never overwrites them, so a "collision" there
   is 25 sources agreeing about Mount Pleasant with different capitalisation.
   These should not be compared at all, or should be compared after
   normalisation. `confidence.KEY_FIELDS` already restricts *scoring* to the five
   that get contested; the collision report does not inherit that restriction.

2. **Syndication is counted as disagreement.** Most of this corpus is trade press
   rewriting one announcement. Twenty articles carrying the same figure produce
   190 pairs that agree, and one carrying a different figure produces 20 that do
   not — so the *count* of findings tells you nothing about how contested a value
   is. What a reader needs is one finding per (project, field) naming the
   distinct values and who holds each, not one per pair.

3. **A genuine multi-way disagreement is one question, not N.** #10's
   `investment_usd` holds $3B, $10B, $21B, $27B, $28.8B, $50B and $500B. That is
   one question — *what is this figure a figure of* — and the claim envelope
   already named the mechanism (`scope`, which failed its kill criterion). It is
   reported today as a pile of pairs.

### The shape of the fix, when we get to it

Deliberately not built yet. Recorded so the decision is not re-derived:

- **Group by (project, field), not by pair.** One finding listing the distinct
  values, each with its holders and its winning-or-not status. This alone should
  take 275 findings to something near the number of genuinely contested cells.
- **Compare after normalisation, and only where a policy could act.** A field
  whose policy is `FILL_ONLY` cannot be changed by a later source, so a
  disagreement on it is a *duplicate-row* signal, not a value question — route it
  to `duplicates`, not to `logic resolve`.
- **Collapse agreeing domains before counting.** `confidence.find_agreements`
  already reduces by registrable domain; the collision path should too, so twenty
  syndicated copies read as one voice.
- **Rank by what a wrong answer costs.** `logic check --audit` already orders by
  dollars at stake. The rule engine does not, and with 573,000 comparisons
  ordering is the only thing that makes the output finishable.

None of this needs an LLM. All of it is grouping and normalisation over data the
row already holds.

---

## Part 2 — blocks: many sources means many names for one building

The live case, Fairwater (#1), 25 sources and **16 blocks**. Stored keys and
labels, which is what makes the problem legible:

```
serving              building-1        Building 1                  src 7
serving              1.fairwater       Facility 1                  src 674   parent="Fairwater Datacenter"
serving              1                 First datacenter facility   src 677
energized            phase-1           Phase 1            400 MW   src 693
energized            wisconsin         Wisconsin campus   350 MW   src 108   parent="Fairwater"   待确认
energized            mke-3.mke-4       MKE03/MKE04        337 MW   src 680   待确认
energized            fairwater         Fairwater                   src 683
under_construction   building-2        Building 2                  src 7
under_construction   2.fairwater       Facility 2                  src 674   parent="Fairwater Datacenter"
under_construction   2                 Second facility             src 675
under_construction   area-2            Area II                     src 680
under_construction   original          Original Data Center        src 690
permitting           area-3            Area 3-A                    src 690
planned              avenue.durand-1   Campus One (Durand Avenue)  src 683
planned              drive.international-2  Campus Two (Internat…)  src 683
paused               acres.expansion-900    Expansion site (900 ac) src 693
```

Four separate defects are visible, and they are not one problem:

- **Aliases.** `building-1` / `1.fairwater` / `1` are three sources naming one
  building. So are `building-2` / `2.fairwater` / `2` / `area-2` — note `Area II`
  is a Roman numeral for the same ordinal.
- **Two spellings of one parent.** `Fairwater` and `Fairwater Datacenter`, so the
  hierarchy has two roots for one campus.
- **A container stored as a tranche.** `fairwater` and `wisconsin` are the campus,
  not a sector within it, yet they sit in the same list as `Phase 1`.
- **Three energised capacities with no shared identity.** 400, 350 (待确认), 337
  (待确认). Whether that is 1,087 MW of distinct tranches or one figure counted
  three ways is the question the whole table exists to answer, and nothing asks
  it. #10 reaching **13,620 MW** — caught by `tracker audit` as
  `campus_exceeds_worlds_largest` — is what this looks like once it reaches a
  total.

### Scale, which argues against the obvious design

| | |
|---|---|
| projects with any block | 126 |
| blocks in total | 286 |
| most on one project | 16 (#1), then 11, 9, 8, 8 |
| projects with more than 8 blocks | **3** |

**One LLM call per project is 126 calls to do nothing on 123 of them.**

---

## My assessment of "yet another LLM"

**Agreed on the residue, and it is aimed at the wrong part.** Three arguments,
in the order that matters.

### 1. The bulk of this is deterministic, and the evidence is already in the key

`building-1`, `1.fairwater` and `1` all carry the ordinal **1**. `building-2`,
`2.fairwater`, `2` and `area-2` all carry **2**. `normalize_haystack` already
folds separators and splits digit-letter boundaries for the feed filter; the same
treatment plus Roman numerals and written ordinals (`first`→1, `II`→2) would
collapse most of those 16 rows with no call at all. Paying a model to decide
whether "Building 1" is "Facility 1" is paying it to read a number we wrote into
the key ourselves.

So: **a free pass first**, and the model only on what it cannot settle. On
Fairwater that is the interesting residue — is `MKE03/MKE04` the same physical
thing as `Wisconsin campus`, is `Original Data Center` the second building or a
third — which is three or four questions, not sixteen.

### 2. The rule that should govern both

`docs/plan-claim-envelope.md` states it, and its own scoreboard is the reason to
take it seriously: **an added field is only worth having if something can check
it.**

| axis | outcome |
|---|---|
| `quotes` | works — 98.7% exact substring, because the string is either in the article or not |
| `risk.severity` | rotted — `watch` on every risk in the database; no article states a severity |
| `source_type` | worse than rotted — an unverifiable regex became load-bearing and is now too entangled to correct |
| `scope` | **failed its pre-registered kill criterion** at 96.9% `this_site`, zero `region`, zero `programme` |

A block-alias axis with no verifier becomes the fifth row of that table.

### 3. But blocks are unusually checkable — better than `scope` was

This is why I think the model *can* earn its place here, where it did not for
`scope`. Three verifiers already exist and cost nothing:

- **`blocks.account()` asserts closure** — every megawatt lands on exactly one
  line (counted, or a residual with a reason). A proposed merge that breaks
  closure is refuted for free.
- **`tracker audit`'s `block_out_of_scale`** already catches a tranche larger
  than the campus containing it, which is what a wrong parent edge produces.
- **The 待确认 tier already exists on a block's `mw`**, and `reconcile` already
  excludes unconfirmed capacity from the campus total while saying what it left
  out.

### The shape I would build

- **The model proposes an equivalence relation and a parent edge. Nothing else.**
  Output is pairs of *existing* labels plus a reason — never a capacity, never a
  date, never a new label. Same contract that makes `audit resolve` safe ("one
  key from a list a person wrote") and `riskcheck` safe (a quote that must verify
  verbatim *and* carry its category's wording).
- **Free gates on both sides.** Ordinal and parent normalisation before the call;
  closure and out-of-scale checks after it.
- **Called only where the free pass leaves ambiguity** — on today's data, the 3
  projects with more than 8 blocks, not all 126.
- **Proposals are stored, not applied.** `tracker duplicates` is the precedent,
  including `park` — the ability to say *no* permanently, which that report
  needed badly enough to earn migration 0016.
- **Merging two blocks with different confirmed capacities must refuse, not
  pick.** 400 vs 350 vs 337 is a collision, and resolving it silently is the same
  failure as `mw_built` MAX that put 1,200 MW on Abilene and 13,620 on Hyperion —
  one level down, and harder to see because nothing sums blocks in public yet.
- **Pre-register the kill criterion before writing the prompt.** Mine would be:
  *if more than ~90% of accepted proposals are pairs the free normaliser already
  found, the model is decoration and should be removed rather than kept "in case".*
  That is the discipline that caught `scope`, and it only works if the number is
  written down first.

### What I would not do

- Let the model name a *new* block. Sixteen labels for one campus is the disease;
  a seventeenth invented by a model is not the cure.
- Let it write `mw`, `customer` or `energized_on`. Those are 关键数字 and belong
  to `infer.INFERABLE`'s exclusion list for the same reason.
- Run it per project on a schedule. Blocks change only when an article about that
  campus is read, so this belongs where `backfill blocks` already lives — keyed on
  the article, resumable, in tranches.

---

## Open, and needing a person

- **The two problems share a cause and a cure.** Both are "one thing described by
  many sources, and the schema has one slot". Part 1 is the scalar version, Part 2
  the sub-entity version. Fixing grouping in `logic check` and identity in
  `blocks` with unrelated mechanisms would be the third and fourth copies of a
  rule this codebase already has three copies of (`confidence.find_conflicts` is
  documented as one of them).
- **#1's three energised figures and #10's 13,620 MW are live now.** `tracker
  audit resolve` can settle the second; the first needs the block work above.
- **`logic check`'s 275 findings are already past usable.** Grouping is worth
  doing before the next `enrich` run widens it further.
