# Placeholder citations and the Fairwater evidence defects — remediation plan

Written 2026-08-06. Revised 2026-08-06 (later the same day) after re-reading the
plan against the live database. Status: **step 1 done and committed, step 4
already true, steps 2–3 restated and blocked on a write permission.**

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

## Step 2 — purge the three placeholder citations ⬜ BLOCKED ON PERMISSION

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

The script is written and reviewed at
`<scratchpad>/step2_purge_placeholders.py`. **It has not been run**: the sandbox
classifier declined the `DELETE`, twice, and the database is unchanged
(3 placeholders still present, 537 sources). This needs an explicit go-ahead.

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
- **The stale note should now clear itself, contrary to the first draft.** With
  `_conflict_notes` applying the 待确认 rule, #1's phase claims reduce to a single
  confirmed one (source 7, `operational`), so no conflict is disclosed. But
  `recompute_from_sources` **computes the derived notes and discards them** —
  `_derived` at [upsert.py:1003](../tracker/upsert.py) is deliberately unused, and
  only `upsert_record` calls `_merge_notes`. So the line clears on the next
  ingest of #1, not on a recompute. **This is a defect in its own right**: after a
  `tracker merge` or a `logic resolve` repair, a row's fields are re-derived while
  its disclosure notes keep describing the old claim set. Not fixed here; it needs
  its own change and its own tests.
- Original open decision #3 (*delete the stale note by hand or keep it as a
  record?*) is therefore **moot** for the phase note. The `investment_usd`
  conflict note stays, correctly: every `investment_usd` claim on #1 is 待确认,
  so none is filtered and they do genuinely all compete.

**Watch for:** confidence on #1 is **2**, not 3 as the first draft states. The
rationale reads `ignored 1 placeholder citation(s); ... unresolved conflict on
investment_usd, phase`. Removing the placeholder should drop the first clause. If
the score *rises* because the phase conflict stops being counted, that is the
`_conflict_notes` fix working, not a second bug.

---

## Step 3 — the misattributed figures ⬜ RESTATED, LLM NO LONGER NEEDED FOR THE BIG ONE

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

> **A stored scalar that disagrees with its only supporting claim is currently
> invisible to every free check.** That is a gap worth its own rule, and it would
> have caught a 1,000 MW error for free. Not implemented here.

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

1. ~~Wait for the other session?~~ **Resolved** — it committed.
2. ~~Where to commit step 1?~~ **Resolved** — `20f75f8`, branch off `main`.
3. ~~Delete the stale `conflict phase:` note?~~ **Moot** — see step 2.
4. ~~`CHANGELOG.md` not updated?~~ **Done** — under `Fixed`.
5. **New: permission to write to `data/tracker.db`.** Steps 2 and 3's repairs both
   need it and are blocked.
6. **New: fix `recompute_from_sources` discarding its derived notes?** Own change,
   own tests. See step 2.
7. **New: add a free check for a stored scalar disagreeing with its only claim?**
   Would have caught #3's 1,000 MW error. See step 3.

## Rollback

```bash
cp data/tracker.db.bak-20260806-step2 data/tracker.db   # 2.6 MB, 16:03, consistent, pre-step-2
git revert 20f75f8                                       # step 1, reviewable in isolation
```

The older `data/tracker.db.bak-preplaceholder` (15:18) also still exists.
