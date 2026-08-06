# Placeholder citations and the Fairwater evidence defects — remediation plan

Written 2026-08-06. Revised twice the same day, against the live database each
time. Status: **all four steps closed.**

| step | outcome |
|---|---|
| 1 — placeholders must not set values | done, `20f75f8` (+ two defects the first draft missed) |
| 2 — purge the three placeholder citations | done; deleted, recomputed, verified. No value moved |
| 3 — the misattributed figures | restated; the biggest needed no LLM, the two 350 MW rows were audited |
| 4 — the overlapping Fairwater blocks | already true; the proposed edit was a no-op *and* would not have fixed it |

Two defects found while verifying the plan were fixed on the way, both outside
its scope: `db7f781` (a row's disclosures outliving the claims they describe) and
`d8e2831` (a stored number its own citations cannot account for — the rule that
catches Abilene's 1,000 MW for free).

## Why this exists

Comparing HTI's *北美AI电力新趋势* deck (2026-07, Epoch AI satellite tracking of 65
US flagship AIDC projects) against the database surfaced one wrong figure on
Stargate Abilene. Pulling that thread found the cause is not one row.

**One DataCenterKnowledge article seeded `mw_built` on four projects, and three of
the four figures are hedged or misattributed:**

| row | `mw_built` | quote behind it | verdict |
|---|---|---|---|
| #1 Fairwater | 350 | "Each exceeds 350 MW and is scaling toward multi-GW" | floor, across two sites |
| #201 Microsoft Atlanta | 350 | *same sentence* | same sentence counted twice |
| #3 Stargate Abilene | 1,200 | "Committed capacity stands at 1.2 GW" | committed ≠ energised |
| #2 xAI Colossus | 150 | — | **fine**, corroborated by the TVA grid-service quote |

`logic check --severity error` catches none of this. There is no arithmetic
contradiction; the errors are semantic.

---

## Read this before executing any step below

The original draft of this plan was written against a database that has since
moved. **Three of its four steps did not survive contact with the live data.**
Every claim below was re-verified against `data/tracker.db` on 2026-08-06 at
16:03, against a consistent backup taken at that moment
(`data/tracker.db.bak-20260806-step2`, WAL checkpointed).

What changed, and why it matters more than the individual corrections: the
original plan's step 3 asks for **five LLM calls** to establish findings, and the
largest of them — a 1,000 MW error — turns out to need no model at all. It is
visible by comparing a stored scalar against the claims that support it. See
step 3.

---

## Step 1 — placeholders must not set values ✅ DONE AND COMMITTED

Committed as `20f75f8` on `claude/placeholder-remediation-plan-1492da`, a branch
off `main` at `aad5de0` — which answers the original plan's open decision #2. Two
things were added on top of the version described in the first draft; both are
below.

### The defect

`confidence.compute` drops placeholder URLs before weighting
([confidence.py:274](../tracker/confidence.py)), and the comment at
[confidence.py:84](../tracker/confidence.py) names the exact row that motivated it:

> "Observed live: a real Microsoft project reached 3 because a placeholder seed row
> contributed the 'strongest source'."

That fix stopped at the **score** and left the **values** alone — the more
dangerous half. `_weight` read `conf.SOURCE_WEIGHTS` and handed
`https://news.microsoft.com/PLACEHOLDER-.../` the `company_filing` weight of **3,
the heaviest in the system, on a URL that does not exist**.

### The fix as committed

- [tracker/upsert.py](../tracker/upsert.py) — new `is_placeholder(source)`;
  `claims_by_field` emits placeholder claims as `weight=0, confirmed=False`;
  exported in `__all__`.
- [tracker/blocks.py](../tracker/blocks.py) — same demotion in `blocks_by_key`.
- **New: the block sort is demoted too, not just the claim weight.** The first
  draft left `ordered` reading the *undemoted* `SOURCE_WEIGHTS`, and its own
  comment claimed otherwise. `label` and `parent` are not resolved by weight at
  all — `labels.setdefault` gives them to whichever source is seen first — so a
  placeholder at weight 0 still got to *name* the block.
- **New: `_conflict_notes` now applies the 待确认 rule that `resolve` applies.**
  It did not, so a claim the engine had already discarded outright was still
  disclosed as a rival. This is the actual cause of the false note on #1, and the
  first draft attributed the fix to step 1 without step 1 fixing it.
- **New: seven regression tests** (`tests/test_upsert.py`, `tests/test_blocks.py`).
  All fail at `aad5de0` except the two pinning the contract that must not move.

**Demoted, not dropped.** The first attempt deleted placeholder claims outright and
broke two CLI tests: `--allow-placeholders` exists so the shipped seed file can
smoke-test the pipeline, and a claim-less source makes that produce empty rows.

Demotion routes placeholders through a rule `resolve` already applies:

> Unconfirmed (待确认) claims are discarded outright whenever any confirmed claim
> exists for the field. Done here, once, rather than inside each policy.

That covers **every** policy including the `phase` ladder, which ranks by
progression and ignores weight — so an unquoted "construction" can no longer
outrank a cited "operational".

### Verification

```bash
.venv/Scripts/python -m pytest tests/ -q -p no:randomly
```

Green except `test_webui.py::test_forcing_colour_actually_produces_escapes`, which
is **environmental, not a regression**: it spawns a subprocess needing
`data/tracker.db`, which is gitignored and exists only in the main tree. It fails
identically in a pristine worktree at `aad5de0` and passes in the main tree.

The two failures the first draft attributed to a concurrent session now pass —
that session's `tracker audit` work landed as `aad5de0`.

---

## Step 2 — purge the three placeholder citations ✅ DONE

Step 1 stops placeholders winning. It does not remove values they already wrote,
because identity fields are never overwritten once set.

| source id | project | typed as | claims |
|---|---|---|---|
| 1 | #1 Microsoft Fairwater | `company_filing` (weight 3) | name, company, city, state, phase, county, country |
| 2 | #2 xAI Colossus | `trade_press` | name, company, city, state, phase, county, country |
| 3 | #3 Crusoe Stargate Abilene | `trade_press` | name, company, customer, city, state, phase, county, country |

Verified: exactly three such rows, all three projects carry real sources besides
them (#1 has 9, #2 has 17, #3 has 13), and **no `event`, `risk` or
`capacity_block` row references any of them** — those foreign keys are
`ON DELETE SET NULL` in any case. The deletion is safe.

### What was actually run

Backups first — `data/tracker.db.bak-before-placeholder-delete` (16:28) alongside
the earlier `bak-20260806-step2` (16:03), both consistent copies via the SQLite
backup API with the WAL checkpointed, plus the original `bak-preplaceholder`.

1. **Deleted** the three rows. 537 sources → 534, zero placeholders remaining.
2. **`tracker init`** — recomputed confidence, h200 and blocks.
3. **Re-derived projects 1–3** from the citations they now hold, which `tracker
   init` does *not* do (see below).

**Verified against a full pre-purge snapshot of all 207 projects and 257 blocks:
not one project field changed, and not one block changed** — no vanished rows, no
new rows, no altered values. Exactly as predicted: every field the placeholders
claimed is FILL_ONLY except `phase`, which resolves to `operational` from source 7
either way. The purge removed a false citation and moved no numbers.

On #1 the `ignored 1 placeholder citation(s)` clause is gone from the confidence
rationale, confidence stayed at **2**, and the false `conflict phase:` line — the
one crediting a URL that does not exist — is gone from the notes.

### Corrections to the original recipe

- **`tracker init` does not do what the first draft says it does.** Its comment
  read `recompute_from_sources / confidence / blocks / h200`; the command runs
  `recompute_confidence`, `recompute_h200` and `recompute_blocks` **only**
  ([cli.py:264](../tracker/cli.py)). Field values are not re-derived by it.
- **No value is expected to move anyway.** Every field the placeholders claim is
  FILL_ONLY except `phase`, and FILL_ONLY returns the stored value regardless. On
  #1, `phase` resolves to `operational` from source 7 with or without the
  placeholder. The purge is about removing a false citation, not about repairing
  a number.
- **The stale note cleared itself, but only after a second fix.**
  `recompute_from_sources` **computed the derived notes and discarded them** —
  `_derived` was deliberately unused, and only `upsert_record` called
  `_merge_notes`. So the line would have cleared on the next *ingest* of #1, not
  on a recompute. Fixed in `db7f781`; see the section below.
- Original open decision #3 (*delete the stale note by hand or keep it as a
  record?*) is therefore **moot**. The `investment_usd` conflict note stays,
  correctly: every `investment_usd` claim on #1 is 待确认, so none is filtered and
  they do genuinely all compete.

### Still open: `confidence.find_conflicts` has the same defect

After the purge, #1's notes correctly disclose one conflict (`investment_usd`) —
but the confidence rationale still reads `unresolved conflict on investment_usd,
**phase**`. [`confidence.find_conflicts`](../tracker/confidence.py) counts every
claim regardless of 待确认 status, so it is a **third** copy of the rule
`resolve` applies and `_conflict_notes` now applies. The two surfaces disagree
with each other on screen today.

Not fixed, deliberately: it changes `confidence` — a stored, displayed score — on
an unknown number of the 207 rows, which is a bigger blast radius than notes and
wants its own decision. Confidence on #1 is **2**, not 3 as the first draft
states, and did not move across the purge.

---

## Step 3 — the misattributed figures ✅ AUDITED, AND THE BIG ONE NEEDED NO MODEL

### #3 Stargate Abilene: `mw_built = 1200` is supported by nothing

The largest error in this plan, and **it needs no model to find**.

The first draft says the 1,200 came from "Committed capacity stands at 1.2 GW".
That was true when it was written; the sources have since been re-extracted and
**both** 1.2 GW quotes are now correctly typed as `mw_planned`:

| src | field | value | quote |
|---|---|---|---|
| 10 | `mw_planned` | 1200 | "Committed capacity stands at 1.2 GW." |
| 209 | `mw_planned` | 1200 | "…approximately 4 million square feet and a total power capacity of 1.2 GW." |
| 209 | `mw_built` | **200** | "…the initial phase comprising two buildings totaling 980,000 square feet and over 200 megawatts (MW) of power capacity." |

**No source claims `mw_built = 1200`.** The single `mw_built` claim on the row is
200, well quoted. The stored 1,200 survives only because `mw_built` is policy MAX
and `_resolve` includes `existing` in the candidate set
([upsert.py:302](../tracker/upsert.py)) — **so MAX can never come down, even after
the claim that seeded it is corrected or deleted.**

This corroborates independently: HTI's satellite read is ~0.4 GW, and our own
`capacity_block` rows say `phase-1` 200 MW `serving`, `phase-2` 1,000 MW
`planned`.

**Why nothing caught it.** `logic check` reports `stored_disagrees` — "the row has
drifted from its own sources" — but only for *collisions*, which need two or more
claims on a field. #3 has exactly one `mw_built` claim, so no collision forms and
the comparison never runs. Measured live: 226 collisions, 4 with
`stored_disagrees` (`#3 mw_planned`, `#25 mw_planned`, `#144 phase`,
`#150 mw_planned`) — and #3's `mw_built` is not among them.

> **A stored scalar that disagrees with its only supporting claim was invisible
> to every free check.** Now implemented, in `d8e2831`: `value_above_its_evidence`
> and `value_without_evidence`. Abilene's 1,000 MW is the top finding. Live rate:
> 5 and 22 across 20 of 207 projects, including **$4B of `investment_usd` on #33
> that no source claims at all**.
>
> Both rules had to consult the block rollup as well as the claims —
> `blocks.reconcile` deliberately raises a campus scalar to the sum of its
> tranches, and the first cut reported 28 rows behaving exactly as designed.

**The value itself is still 1,200 and has not been corrected.** The rule reports
it; changing it is an operator judgement in `tracker review`, and `mw_built` is
MAX so it will not come down on a recompute. That decision is still open.

### #1 Fairwater and #201 Microsoft Atlanta: one sentence, two rows, 700 MW

Confirmed exactly as the first draft describes, and worse than it says. Sources
108 and 449 are **the same URL** (verified byte-equal), each contributing
`mw_built = 350`:

- **src 449 (#201)** stores the quote: *"Both sites use closed-loop liquid cooling
  to eliminate operational water consumption. Each exceeds 350 MW and is scaling
  toward multi-GW."* A floor across two sites, read as a per-site figure on both.
- **src 108 (#1)** stores **no quotes at all** — yet `unconfirmed_fields` is
  `None`, so its 350 MW counts as *confirmed*. A confirmed value with zero
  recorded evidence is precisely what the `placeholder_quote` WARNING rule was
  added to catch, and it is not firing on this row. Worth checking why.
- src 449's `city` quote for **Atlanta** is a sentence about **Wisconsin**
  ("The Wisconsin site went live in June 2026and is linked to an earlier Atlanta
  campus…"). Also misattributed; not in the original plan.

#### The audit's verdict on both (2 LLM calls, nothing written)

**#1 — the plan's prediction, confirmed.** `mw_built` came back **hedged** at
confidence 0.6: *"The passage states the site 'exceeds 350 MW and is scaling
toward multi-GW', meaning the actual built capacity is above 350 MW and still
growing, not exactly 350 MW."*

It also returned one finding nobody was looking for, and the strongest of the run:
**`blocker` unsupported at 0.9** — the stored obstacle prose about Sturtevant
residents and humming cooling fans is not in the passage cited for it.

**#201 — the audit missed it.** On the *same sentence* it flagged `name`,
`company` and `state` (all pedantry: the passage says "Atlanta campus" not
"Microsoft Atlanta Campus", never spells "GA") and said **nothing about
`mw_built`**. So the audit found the hedge on one row and not on its twin.

That is worth recording as a limitation of `--audit` rather than a fact about the
data: on this evidence it is not reliable for the shared-sentence class, and the
misattribution here was established by reading the stored quote directly. Neither
row's value has been demoted — that is an operator call in `tracker review`.

### #1 `investment_usd`: already resolved, no action

The first draft targets `$4.7B` as an unconfirmed economic-impact figure summed
into `tracker capex`. **Both halves are now stale.** The row reads **$3.3B**, no
source claims 4.7B, and every `investment_usd` claim on #1 is already 待确认 —
so it is already excluded from the capex sums. Nothing to repair.

### What is actually left to run

`tracker logic check --audit N` costs one LLM call per row and writes nothing. It
remains worth running on the two 350 MW rows, where the question genuinely is
*"does this sentence state this value for this project"*. It is **not** needed for
#3, and it is **not** needed for `investment_usd`.

Repairs are made by demoting the value in `tracker review` — a database write, so
also currently blocked.

---

## Step 4 — the overlapping Fairwater blocks ✅ ALREADY TRUE / ⬜ PREMISE WRONG

**The proposed `UPDATE` is a no-op.** `building-1.fairwater` already has
`parent = 'Fairwater'`:

```
block_key             | label            | parent    | mw  | status             | src
building-1.fairwater  | Building 1       | Fairwater |   - | serving            |   7
building-2.fairwater  | Building 2       | Fairwater |   - | under_construction |   7
wisconsin             | Wisconsin campus | Fairwater | 350 | energized          | 108
phase-1               | Phase 1          | NULL      |   - | under_construction | 570  (generic)
```

**And it would not have fixed the stated problem anyway.** `wisconsin` also has
`parent = 'Fairwater'`, so setting Building 1's parent to `Fairwater` makes it a
*sibling* of the campus block — exactly the arrangement the first draft was trying
to get away from. Making Building 1 a child of the campus would mean parenting it
to `Wisconsin campus`, which is a different edit and a judgement call about
whether the campus block should be a container at all.

The underlying concern is real and remains open: **Building 1 (`serving`) and
Wisconsin campus (350 MW, `energized`) describe the same live capacity today.**
Building 1 has no `mw`, so nothing double-counts yet; the moment an article gives
it one, `account()` adds both, and the `overlap` residual cannot fire because it
computes a gap against a cited total and `mw_planned` is NULL on this row.

A fourth block has appeared since the first draft: **`phase-1` (id 319, generic,
`parent` NULL, from src 570)** — a bare "Phase 1" with no facility named. Already
disclosed by `block_label_ambiguous` and excluded from the campus total.

Two further things, both needing a decision and neither addressed:

- **Two energisation dates for one building.** April 2026 (Racine County Eye —
  "when Fairwater came online in April, residents reported persistent humming")
  vs June 2026 (DCK — "went live in June 2026"). Both defensible as commissioning
  vs public launch; the `2026-06-23` event says "officially opened". Two
  `energized` events sit in `event` and nothing reconciles them.
- **`mw_planned` is NULL against 350 built.** The plan is in the sources and was
  never extracted: "scaling toward multi-GW", "2.6 GW of new demand expected from
  Microsoft's I-94 corridor buildout", second building 2028, "15 more buildings
  approved". HTI puts Fairwater Wisconsin at 1,830 MW landing in 2028H1. No logic
  rule fires — `built_exceeds_planned` needs both values present.

---

## Concurrent session — resolved

The first draft warned about another process editing this repo. **That work is
committed** as `aad5de0` (`tracker audit`), and both sibling worktrees are clean.
The two test failures it attributed to that session now pass.

Its advice still stands and is worth keeping: **do not run `git stash` in a shared
tree.** Baselines in this session were taken with a throwaway worktree and with
file copies instead.

## Open decisions

Closed:

1. ~~Wait for the other session?~~ It committed as `aad5de0`.
2. ~~Where to commit step 1?~~ `20f75f8`, branch off `main`.
3. ~~Delete the stale `conflict phase:` note?~~ Moot — it clears itself now.
4. ~~`CHANGELOG.md` not updated?~~ Done, under `Fixed`.
5. ~~Permission to write to `data/tracker.db`?~~ Granted; the purge ran.
6. ~~Fix `recompute_from_sources` discarding its derived notes?~~ `db7f781`.
7. ~~Add a free check for a stored scalar disagreeing with its claim?~~ `d8e2831`.

Still open, all needing a person:

8. **`confidence.find_conflicts` counts 待确认 claims** — the third copy of a rule
   the other two now apply, and the reason #1's rationale and its notes disagree
   on screen. Changes a stored score across an unknown share of 207 rows.
9. **#3's `mw_built = 1200`** — now reported, not corrected. MAX will not lower it
   on a recompute; it needs `tracker review`.
10. **#1 and #201's `mw_built = 350`** — one hedged sentence read as a per-site
    figure on two rows. Neither demoted.
11. **#1's `blocker` prose is unsupported** (audit, 0.9) — the cited passage does
    not mention it. Found incidentally; not in the original plan.
12. **`placeholder_quote` is not firing on src 108**, which has `mw_built = 350`
    confirmed with no stored quote at all. Either the rule or this row is wrong.
13. **The two Fairwater block questions** above — the duplicate energisation dates
    and the NULL `mw_planned`.

## Rollback

```bash
cp data/tracker.db.bak-before-placeholder-delete data/tracker.db   # 16:28, immediately pre-purge
```

```bash
git revert d8e2831 db7f781 20f75f8   # the three code changes, each reviewable alone
```

`data/tracker.db.bak-20260806-step2` (16:03) and `bak-preplaceholder` (15:18) also
still exist. Note that a rollback of the database alone will restore the three
placeholder citations while leaving the code that demotes them in place — which is
a coherent state, just not the one this plan aimed at.
