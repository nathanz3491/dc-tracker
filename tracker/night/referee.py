"""What a director may ask for, enforced. Pure code: no model, no database of its own.

A director proposes steps; :func:`review` returns the steps that will actually run and
one plain sentence for every change it made — a size clamped, a step refused, a step
moved, a floor added. Nothing a director says reaches a command line except through
here, and nothing here reads what the director wrote except the fields of a step.

The rules, and why each exists (the brainstorm of 2026-11-05 has the edge cases):

* **The menu only.** Unknown kinds and the frame's steps (news, conditions, twins,
  free) are refused: the frame is placed by the night (:func:`frame`), never chosen.
* **The frame.** Round 1 starts with the news, the rule reader right after it (it
  reads the pages the news just saved), and the twin pass; every round starts with
  the free phase. The director sizes the news and nothing else of it.
* **Once a night** for the news, the rule reader, the twins, the suspects, reread and
  recheck — a frame step already run is not placed again either.
* **Ranges.** Every size and article budget is clamped to its kind's range.
* **Drift.** A kind's units across the night may not pass one and a half times what
  fixed nights ask of it (the median of the record's fixed nights, or today's
  defaults until there are three). One night's odd result cannot swing the plan.
* **The second enrich pass is fresh, and there is no third** of either selecting
  pass: a second pass over the same rows paid for the same pages in September.
* **Named rows** must exist and must not have been asked about today; a merge may have
  removed one since it was named. At most forty a step, six such steps a night.
* **Effort** may be raised anywhere; on a step that rules on published values it is
  never below today's. 09-30 at `low` replaced a campus's $600M with "$20B+ in Ohio".
* **A note** is cut to 300 characters. The gate is code, so a note can steer where
  effort goes, never what counts as evidence.
* **Order** within a round: duplicates before every step that pays to read a row it
  may fold; logic and the suspects before the tranche review.
* **Floors**, added to round 1 when the plan leaves a kind short: the duplicates,
  audit, risks, logic and tranche passes, the suspects, and the reread and recheck
  backlogs. Logic and the suspects since 2026-11-07: the first directed night ran six
  findings and no suspects, and open findings rose by 17.
"""

from __future__ import annotations

import dataclasses
import re
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from tracker.night.steps import (
    AFTER_DUPLICATES,
    AFTER_LOGIC,
    EFFORT_RANK,
    EFFORTS,
    KINDS,
    NOTE_CHARS,
    SELECTING,
    Step,
    defaults_per_night,
    fixed_round,
)

#: How far a kind's night may pass its baseline.
DRIFT: Final = 1.5
#: Fixed nights the drift baseline needs before it replaces today's defaults.
DRIFT_NIGHTS: Final = 3
#: Selecting enrich passes of one kind a night.
SELECTING_PASSES: Final = 2
#: Steps naming their own rows, a night.
ROWS_STEPS: Final = 6
#: Steps a night runs at most, frame included.
MAX_STEPS: Final = 60
#: The news watch's searches may not go below this: the morning email needs them.
NEWS_SEARCH_FLOOR: Final = 400
#: Articles per row an enrich step gets when the director names no budget: the fixed
#: schedule's ratio (180 for 45, 300 for 75).
ARTICLES_PER_ROW: Final = 4

_CODE: Final = re.compile(r"^[a-z][a-z0-9_]{2,60}$")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass
class Tonight:
    """What the night has done so far, as the referee needs it."""

    #: Units run (and planned, while reviewing) per kind.
    totals: dict[str, int] = field(default_factory=dict)
    #: Selecting enrich passes run per kind.
    passes: dict[str, int] = field(default_factory=dict)
    rows_steps: int = 0
    steps: int = 0

    def add(self, step: Step) -> None:
        self.totals[step.kind] = self.totals.get(step.kind, 0) + step.size
        if step.kind in SELECTING:
            self.passes[step.kind] = self.passes.get(step.kind, 0) + 1
        if step.kind == "rows":
            self.rows_steps += 1
        self.steps += 1


@dataclass
class Rules:
    """The night's limits, fixed when the night starts."""

    options: Any
    #: Today's effort per tier (env name -> effort): the floor for ruling steps.
    efforts: dict[str, str]
    #: Units per night per kind the drift rule allows.
    allowance: dict[str, int]
    #: Of these row ids, the ones that still exist (a merge tonight may have removed one).
    existing: Callable[[Sequence[int]], set[int]] = lambda ids: set(ids)
    #: Of these row ids, the ones enrich already asked about today.
    asked_today: Callable[[Sequence[int]], set[int]] = lambda ids: set()
    fillable: frozenset[str] = frozenset()

    def rows_ok(self, ids: Sequence[int], *, askable: bool) -> tuple[list[int], list[str]]:
        """The ids a step may name, and a sentence for each one dropped."""
        there = self.existing(ids)
        asked = self.asked_today([i for i in ids if i in there]) if askable else set()
        kept, dropped = [], []
        for row in ids:
            if row not in there:
                dropped.append(f"#{row} no longer exists (merged or removed); dropped")
            elif row in asked:
                dropped.append(f"#{row} was already asked about today; dropped")
            else:
                kept.append(row)
        return kept, dropped

    def floor(self, kind: str) -> int:
        """The night's floor for `kind`, unless the night's options turned it off."""
        o = self.options
        floor = KINDS[kind].floor
        if kind == "duplicates" and not o.pairs:
            return 0
        if kind in ("reread", "recheck") and not getattr(o, kind):
            return 0
        if kind in ("audit", "risks") and not getattr(o, kind):
            return 0
        if kind == "tranches" and not o.nest:
            return 0
        if kind == "logic" and not o.findings:
            return 0
        if kind == "suspects" and not o.suspects:
            return 0
        return floor


def allowance(options: Any, fixed_nights: dict[str, list[int]]) -> dict[str, int]:
    """Units a night may ask of each kind: 1.5x its baseline, never below its floor."""
    defaults = defaults_per_night(options)
    out = {}
    for key, kind in KINDS.items():
        seen = fixed_nights.get(key, [])
        base = (
            statistics.median(seen)
            if len(seen) >= DRIFT_NIGHTS
            else defaults.get(key, kind.default)
        )
        out[key] = max(int(base * DRIFT), kind.floor)
    return out


@dataclass
class Verdict:
    steps: list[Step]
    notes: list[str] = field(default_factory=list)


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def review_step(step: Step, rules: Rules, tonight: Tonight, notes: list[str]) -> Step | None:
    """One proposed step as it may run, or None. Every change is said in `notes`."""
    kind = KINDS.get(step.kind)
    if kind is None:
        notes.append(f"refused {step.kind!r}: not a step on the menu")
        return None
    if not kind.choosable:
        notes.append(f"refused {step.kind}: the night places it, the director does not choose it")
        return None
    if tonight.steps >= MAX_STEPS:
        notes.append(f"refused {step.kind}: the night has run its {MAX_STEPS} steps")
        return None
    if kind.once and tonight.totals.get(step.kind, 0) > 0:
        notes.append(f"refused {step.kind}: once a night, and it has run")
        return None
    out = dataclasses.replace(step, origin=step.origin or "director")

    if out.kind == "rows":
        if tonight.rows_steps >= ROWS_STEPS:
            notes.append(f"refused rows: {ROWS_STEPS} named-row steps a night")
            return None
        rows = list(dict.fromkeys(int(r) for r in out.rows))[: kind.hi]
        if len(out.rows) > kind.hi:
            notes.append(f"rows: kept the first {kind.hi} of {len(out.rows)} named rows")
        kept, dropped = rules.rows_ok(rows, askable=True)
        notes += [f"rows: {why}" for why in dropped]
        if not kept:
            notes.append("refused rows: none of the named rows can be asked about tonight")
            return None
        fields = (
            tuple(f for f in out.fields if f in rules.fillable) if rules.fillable else out.fields
        )
        if len(fields) != len(out.fields):
            gone = sorted(set(out.fields) - set(fields))
            notes.append(f"rows: dropped field(s) the agent cannot fill: {', '.join(gone)}")
        out = dataclasses.replace(out, rows=tuple(kept), fields=fields, size=len(kept))
    elif out.size <= 0:
        notes.append(f"skipped {out.kind}: size 0")
        return None

    size = _clamp(out.size, max(kind.lo, 1), kind.hi)
    if size != out.size:
        notes.append(f"{out.kind}: size {out.size} clamped to {size} (range {kind.lo}-{kind.hi})")
    left = rules.allowance.get(out.kind, kind.hi) - tonight.totals.get(out.kind, 0)
    if size > left:
        if left <= 0:
            notes.append(
                f"refused {out.kind}: the night has used its allowance of {rules.allowance.get(out.kind)} {kind.unit}"
            )
            return None
        notes.append(
            f"{out.kind}: size {size} cut to {left}, what is left of the night's allowance "
            f"({rules.allowance.get(out.kind)} {kind.unit}, {DRIFT:g}x a fixed night)"
        )
        size = left
    if out.kind == "rows" and size < len(out.rows):
        out = dataclasses.replace(out, rows=out.rows[:size])
    out = dataclasses.replace(out, size=size)

    if kind.budget_hi:
        budget = out.budget or size * ARTICLES_PER_ROW
        budget = _clamp(budget, 1, kind.budget_hi)
        if out.budget and budget != out.budget:
            notes.append(f"{out.kind}: budget {out.budget} clamped to {budget}")
        out = dataclasses.replace(out, budget=budget)
    else:
        out = dataclasses.replace(out, budget=0)

    if out.kind in SELECTING:
        passes = tonight.passes.get(out.kind, 0)
        if passes >= SELECTING_PASSES:
            notes.append(
                f"refused {out.kind}: {SELECTING_PASSES} passes a night, and both have run"
            )
            return None
        if passes and not out.fresh:
            notes.append(f"{out.kind}: a second pass tonight is --fresh, on rows not yet asked")
            out = dataclasses.replace(out, fresh=True)
    elif out.fresh:
        out = dataclasses.replace(out, fresh=False)

    if out.kind == "logic":
        code = out.code if out.code and _CODE.match(out.code) else ""
        if out.code and not code:
            notes.append(f"logic: {out.code!r} is not a finding code; dropped")
        rows: tuple[int, ...] = ()
        if out.rows:
            kept, dropped = rules.rows_ok(list(dict.fromkeys(out.rows))[:120], askable=False)
            notes += [f"logic: {why}" for why in dropped]
            rows = tuple(kept)
            if not rows:
                notes.append("refused logic: none of the named rows exists")
                return None
        out = dataclasses.replace(out, code=code, rows=rows)
    elif out.kind != "rows":
        out = dataclasses.replace(out, rows=(), fields=(), code="")

    out = dataclasses.replace(out, effort=_effort(out, rules, notes))
    note = _CONTROL.sub(" ", out.note or "").strip()
    if len(note) > NOTE_CHARS:
        notes.append(f"{out.kind}: note cut to {NOTE_CHARS} characters")
        note = note[:NOTE_CHARS]
    out = dataclasses.replace(
        out, note=note if kind.effort_env == "TRACKER_DEEPSEEK_AGENT_EFFORT" else ""
    )
    if note and not out.note:
        notes.append(f"{out.kind}: a note reaches only the agent steps; dropped")
    if out.skip_search and out.kind != "rows":
        out = dataclasses.replace(out, skip_search=False)
    return out


def _effort(step: Step, rules: Rules, notes: list[str]) -> str | None:
    kind = KINDS[step.kind]
    if not step.effort:
        return None
    if step.effort not in EFFORTS or kind.effort_env is None:
        notes.append(f"{step.kind}: effort {step.effort!r} ignored")
        return None
    today = rules.efforts.get(kind.effort_env, "high")
    if not kind.lowerable and EFFORT_RANK[step.effort] < EFFORT_RANK.get(today, 1):
        notes.append(
            f"{step.kind}: effort {step.effort} raised to {today} — it rules on published values"
        )
        return today
    return step.effort


def order(steps: list[Step], notes: list[str]) -> list[Step]:
    """Duplicates before what reads a row it may fold; logic before the tranche review.

    Stable: a step only moves when a rule needs it to, and every move is said.
    """
    out = list(steps)
    for first, later in (
        ("duplicates", AFTER_DUPLICATES),
        ("logic", AFTER_LOGIC),
        ("suspects", AFTER_LOGIC),
    ):
        moved = True
        while moved:
            moved = False
            for i, step in enumerate(out):
                if step.kind != first:
                    continue
                ahead = next((j for j in range(i) if out[j].kind in later), None)
                if ahead is not None:
                    out.insert(ahead, out.pop(i))
                    notes.append(f"moved {first} ahead of {out[ahead + 1].kind}")
                    moved = True
                    break
    return out


def review(
    proposed: Sequence[Step],
    rules: Rules,
    tonight: Tonight,
    *,
    first_round: bool = False,
) -> Verdict:
    """The steps that will run, in order, and every change made to the proposal."""
    notes: list[str] = []
    planning = Tonight(
        dict(tonight.totals), dict(tonight.passes), tonight.rows_steps, tonight.steps
    )
    kept: list[Step] = []
    for step in proposed:
        reviewed = review_step(step, rules, planning, notes)
        if reviewed is not None:
            kept.append(reviewed)
            planning.add(reviewed)
    if first_round:
        for key in KINDS:
            floor = rules.floor(key)
            short = floor - planning.totals.get(key, 0)
            if floor and short > 0:
                kind = KINDS[key]
                size = _clamp(short, kind.lo or 1, kind.hi)
                kept.append(
                    Step(
                        key,
                        size=size,
                        origin="floor",
                        why=f"the night's floor of {floor} {kind.unit}",
                    )
                )
                planning.add(kept[-1])
                notes.append(f"added {key} {size}: the night's floor is {floor} {kind.unit}")
    return Verdict(order(kept, notes), notes)


def news_step(proposal: dict[str, Any] | None, options: Any, fixed: Step, notes: list[str]) -> Step:
    """The night's news step: the fixed one, with the director's searches and sizes clamped."""
    if not proposal:
        return fixed
    out = dataclasses.replace(fixed, origin="director")
    cap = options.search_cap
    if "searches" in proposal:
        asked = int(proposal["searches"])
        searches = _clamp(asked, min(NEWS_SEARCH_FLOOR, cap), cap)
        if searches != asked:
            notes.append(
                f"news: searches {asked} clamped to {searches} (floor {NEWS_SEARCH_FLOOR}, cap {cap})"
            )
        out = dataclasses.replace(out, searches=searches)
    if "queue" in proposal:
        kind = KINDS["news"]
        asked = int(proposal["queue"])
        queue = _clamp(asked, kind.lo, kind.hi)
        if queue != asked:
            notes.append(f"news: queue {asked} clamped to {queue}")
        out = dataclasses.replace(out, size=queue)
    if "baselines" in proposal:
        asked = int(proposal["baselines"])
        baselines = _clamp(asked, 0, 600)
        if baselines != asked:
            notes.append(f"news: first looks {asked} clamped to {baselines}")
        out = dataclasses.replace(out, baselines=baselines)
    if proposal.get("effort") in EFFORTS and EFFORT_RANK[proposal["effort"]] >= EFFORT_RANK["high"]:
        out = dataclasses.replace(out, effort=proposal["effort"])
    return out


def frame(
    news: dict[str, Any] | None,
    options: Any,
    number: int,
    tonight: Tonight,
    notes: list[str],
) -> list[Step]:
    """The steps the night itself places at the start of round `number`, in order.

    Round 1: the news (with the director's searches and sizes, :func:`news_step`), the
    rule reader right after it — it reads the pages the news step has just saved, and
    one that ran before the news would read only yesterday's — then the twin pass.
    Every round: the free phase. Each is the fixed schedule's own step, so a directed
    night and a fixed one place the same frame; a once-a-night step that has already
    run tonight is not placed again.
    """
    out: list[Step] = []
    if number == 1:
        for fixed in fixed_round(options, 1):
            kind = KINDS[fixed.kind]
            if kind.choosable:
                continue
            if kind.once and tonight.totals.get(fixed.kind, 0) > 0:
                notes.append(f"{fixed.kind}: once a night, and it has run")
                continue
            if fixed.kind == "news":
                out.append(news_step(news, options, fixed, notes))
            elif fixed.kind != "free":
                out.append(dataclasses.replace(fixed, origin="frame"))
    out.append(Step("free", origin="frame"))
    return out


__all__ = [
    "ARTICLES_PER_ROW",
    "DRIFT",
    "MAX_STEPS",
    "NEWS_SEARCH_FLOOR",
    "ROWS_STEPS",
    "SELECTING_PASSES",
    "Rules",
    "Tonight",
    "Verdict",
    "allowance",
    "frame",
    "news_step",
    "order",
    "review",
    "review_step",
]
