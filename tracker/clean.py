"""Is one project row trustworthy, and how far short of trustworthy is it.

**This module composes and reimplements nothing.** Every condition below is an
existing detector — `logic.check_rules`, `audit.check_project`, `quality`'s census
buckets, `gaps.for_project`, `blockcheck.scan`, `Risk.unconfirmed`, the prompt
vintage on `source.extractor`. A second implementation of any of them would drift
from the one the write path uses, which is the failure `logic.check_collisions`
exists to avoid.

It sits on the other side of the line `quality` draws in its own docstring: that
module asks a question about the **database**, every other module asks about one
row. A per-row gate belongs here, and putting it in `quality` is how `quality`
becomes another `logic`.

**Why a tier and not a boolean.** Reading Hyperion (#10) by hand turned up eleven
defects across six subsystems; "clean" as a single flag would have said `False`
before and `False` after nine of them were fixed. The four tiers are ordered by
what a reader can safely *do* with the row:

    T0 SOURCED    something real cites it, and nothing on it is self-contradictory
    T1 SOUND      nothing in a total is a lie          <- the campaign bar
    T2 COMPLETE   the fields a reader acts on are there, and each is backed
    T3 SETTLED    every open question has been answered

T1 is the bar because the numbers this tool exists to publish are sums. A row that
is merely incomplete makes a total *smaller*; a row carrying an implausible figure,
a duplicate, or a value decided by crawl order makes it **wrong**, and one wrong
row discredits the whole table.

**The definition is calibrated against a row, not asserted.** `tracker clean
--project 10` on the hand-cleaned Hyperion must reach T3. If it does not, the
definition is wrong and this file changes — not the row. That is what makes this a
measurement rather than a wish, and it is why `tests/test_clean.py` carries a
Hyperion-shaped fixture.

Nothing here writes, and nothing here needs the write lock: the whole free sweep
over 1,171 rows takes about 4.5 seconds, so there is no cache to keep and no
progress to store. Where progress *is* stored is `data/runs/clean.jsonl`, because
the one thing a column cannot be is a time series.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import Project

#: Tier names, and the conditions each one adds to the tier below it. A row is at
#: tier N when every condition of tiers 0..N holds, so the tiers are cumulative by
#: construction and `tier` cannot report 3 for a row failing a T0 condition.
TIERS: Final[tuple[tuple[int, str, tuple[str, ...]], ...]] = (
    (0, "SOURCED", ("has_source", "no_silent_defect", "no_errors")),
    (1, "SOUND", ("audit_clear", "no_inversions", "duplicates_answered")),
    (2, "COMPLETE", ("fields_present", "values_backed")),
    (3, "SETTLED", ("warnings_settled", "blocks_settled", "risks_confirmed", "vintage_current")),
)

#: What each condition means, and the command that answers it. The remedy is part
#: of the definition: a scorecard naming a failure an operator cannot act on is a
#: complaint, and `tracker clean --plan` renders these as runnable lines.
REMEDIES: Final[dict[str, str]] = {
    "has_source": "tracker point '<name>'",
    "no_silent_defect": "tracker ingest crawl --stale-prompt --cached-only --limit 50",
    "no_errors": "tracker logic resolve --project {id}",
    "audit_clear": "tracker audit resolve --project {id} --llm",
    "no_inversions": "tracker logic resolve --auto --apply",
    "duplicates_answered": "tracker duplicates  # then merge or park",
    "fields_present": "tracker enrich {id} --target 0",
    "values_backed": "tracker ingest crawl --stale-prompt --cached-only --limit 50",
    "warnings_settled": "tracker logic resolve --project {id}",
    "blocks_settled": "tracker blocks {id}",
    "risks_confirmed": "tracker risks confirm --project {id}",
    "vintage_current": "tracker ingest crawl --stale-prompt --cached-only --limit 50",
}


#: What each condition means in a phrase, for a reader who has not read this file.
#:
#: Beside `REMEDIES` and for the same reason it gives: a scorecard naming a failure
#: an operator cannot act on is a complaint. A bare key like `vintage_current` is
#: the same problem one step earlier — it names the failure in this module's
#: vocabulary rather than in the reader's. Every phrase says what is *wrong*, not
#: what the condition tests, so it reads correctly in a list of things to fix.
CONDITION_LABELS: Final[dict[str, str]] = {
    "has_source": "no citation at all",
    "no_silent_defect": "a value presented as fact with no sentence behind it",
    "no_errors": "a contradiction the row fails on",
    "audit_clear": "a figure that cannot be true",
    "no_inversions": "a value that has drifted from its own sources",
    "duplicates_answered": "a possible duplicate nobody has ruled on",
    "fields_present": "tracked fields still empty",
    "values_backed": "a stored value with no quote",
    "warnings_settled": "an open logic warning",
    "blocks_settled": "tranches that may be one thing counted twice",
    "risks_confirmed": "an obstacle with no usable quote",
    "vintage_current": "last read by a superseded prompt",
}


@dataclass(frozen=True)
class Condition:
    """One question about one row, and the answer with its reason."""

    key: str
    ok: bool
    detail: str = ""

    def remedy(self, project_id: int) -> str:
        return REMEDIES.get(self.key, "").format(id=project_id)


@dataclass(frozen=True)
class CleanCard:
    """Every condition for one project, and the tier that follows from them."""

    project_id: int
    name: str
    conditions: tuple[Condition, ...] = ()

    @property
    def by_key(self) -> dict[str, Condition]:
        return {c.key: c for c in self.conditions}

    @property
    def tier(self) -> int:
        """The highest tier whose conditions — and every lower tier's — all hold.

        Returns -1 for a row failing T0, so "not even sourced" is distinguishable
        from "sourced", rather than both reading as 0.
        """
        answers = self.by_key
        reached = -1
        for level, _label, keys in TIERS:
            if not all(answers[k].ok for k in keys if k in answers):
                return reached
            reached = level
        return reached

    @property
    def label(self) -> str:
        for level, name, _keys in TIERS:
            if level == self.tier:
                return name
        return "UNSOURCED"

    @property
    def failed(self) -> tuple[Condition, ...]:
        return tuple(c for c in self.conditions if not c.ok)

    @property
    def blocking(self) -> tuple[Condition, ...]:
        """The failures at the tier immediately above this row — what to fix next.

        A row at T0 with eight failures does not need eight commands; it needs the
        three that reach T1. Ordering the work is most of what makes a 1,171-row
        campaign tractable.
        """
        answers = self.by_key
        for level, _label, keys in TIERS:
            if level <= self.tier:
                continue
            bad = tuple(answers[k] for k in keys if k in answers and not answers[k].ok)
            if bad:
                return bad
        return ()

    def as_json(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "tier": self.tier,
            "label": self.label,
            "conditions": {c.key: {"ok": c.ok, "detail": c.detail} for c in self.conditions},
        }


@dataclass(frozen=True)
class Sweep:
    """Every row's card, plus the histogram a scorecard prints."""

    cards: tuple[CleanCard, ...] = ()
    #: condition key -> how many rows fail it.
    failures: dict[str, int] = dc_field(default_factory=dict)
    #: tier -> how many rows sit there.
    histogram: dict[int, int] = dc_field(default_factory=dict)

    def at_or_above(self, tier: int) -> int:
        return sum(1 for c in self.cards if c.tier >= tier)

    def as_json(self) -> dict[str, Any]:
        return {
            "projects": len(self.cards),
            "histogram": {str(k): v for k, v in sorted(self.histogram.items())},
            "failures": dict(sorted(self.failures.items())),
            "at_or_above": {str(t): self.at_or_above(t) for t, _n, _k in TIERS},
        }


def attention(sweep: Sweep, *, limit: int | None = None) -> list[dict[str, Any]]:
    """What is keeping rows out of the next tier, worst first.

    One entry per failing condition: how many projects fail it, the phrase that
    says what is wrong, the tier it gates, and the command that answers it.

    Here rather than in the console for the reason `docs/architecture.md` gives —
    the console makes no judgements of its own — and here rather than in
    `webui/dataset.py` because the CLI wants the same list.
    """
    tier_of = {key: (level, label) for level, label, keys in TIERS for key in keys}
    out = [
        {
            "condition": key,
            "label": CONDITION_LABELS.get(key, key),
            "projects": count,
            "tier": tier_of.get(key, (None, ""))[0],
            "tier_name": tier_of.get(key, (None, ""))[1],
            # `{id}` becomes `<id>` rather than being left as-is. The count spans
            # projects, so there is no single id to interpolate — but printing the
            # raw template reads as a bug that failed to substitute, where `<id>`
            # reads as the placeholder it is. `CleanCard.remedy` fills a real one
            # in per row, which is where an id exists.
            "remedy": REMEDIES.get(key, "").replace("{id}", "<id>"),
        }
        for key, count in sweep.failures.items()
        if count
    ]
    out.sort(key=lambda item: (-item["projects"], item["condition"]))
    return out[:limit] if limit else out


def _current_stamp() -> str:
    from tracker.prompts import load_prompt

    return load_prompt("extract-v1").stamp


@dataclass
class _Shared:
    """Whole-database answers, computed once and reused for every card.

    Three of the eleven conditions are only answerable across rows — a duplicate is
    a statement about a pair, and `blockcheck.scan` and `recency_inversions` both
    query. Computing them per card would turn a 4.5-second sweep into 1,171 of them.
    """

    defects: set[int] = dc_field(default_factory=set)
    inversions: set[int] = dc_field(default_factory=set)
    block_groups: dict[int, list[str]] = dc_field(default_factory=dict)
    suspected: set[int] = dc_field(default_factory=set)
    stamp: str = ""


def _shared(session: Session, project_ids: list[int] | None = None) -> _Shared:
    from tracker import blockcheck, capex, quality

    got = _Shared(stamp=_current_stamp())
    got.defects = {d.project_id for d in quality.silent_defects(session, project_ids=project_ids)}
    got.inversions = {
        i.project_id for i in quality.recency_inversions(session, project_ids=project_ids)
    }
    for group in blockcheck.scan(session, project_ids=project_ids):
        got.block_groups.setdefault(group.project_id, []).append(group.verdict)
    # Parked pairs are already excluded — `suspected_duplicates` drops them — so a
    # pair an operator has answered with "these are different" stops counting.
    for pair in capex.suspected_duplicates(session):
        got.suspected.add(pair.a_id)
        got.suspected.add(pair.b_id)
    return got


def card(session: Session, project: Project, *, shared: _Shared | None = None) -> CleanCard:
    """Every condition for one row. Free, read-only, no LLM, no network."""
    from tracker import audit, gaps, logic, quality

    got = shared if shared is not None else _shared(session, [project.id])
    checks: list[Condition] = []

    def add(key: str, ok: bool, detail: str = "") -> None:
        # `detail` describes the FAILURE, so a passing condition carries none.
        # Printing "a value is stored with no sentence behind it" beside a PASS
        # reads as the opposite of what it says.
        checks.append(Condition(key, ok, "" if ok else detail))

    # --- T0: is there anything here, and does it contradict itself? -------------
    # A `derived:` row cites a real checkable document but is not testimony about
    # the project — the Census confirms a county, it says nothing about a campus.
    # `confidence.compute` already refuses to let one corroborate; so does this.
    real = [s for s in project.sources if not (s.extractor or "").startswith("derived:")]
    add("has_source", bool(real), "nothing cites this row but reference data")

    add(
        "no_silent_defect",
        project.id not in got.defects,
        "a value is stored as established with no sentence recorded for it",
    )

    findings = logic.check_rules(project)
    settled = audit.settled_codes(project)
    errors = [f for f in findings if f.severity == logic.ERROR and f.code not in settled]
    add("no_errors", not errors, ", ".join(sorted({f.code for f in errors})))

    # --- T1: is anything in a total a lie? --------------------------------------
    open_audit = [f for f in audit.check_project(project) if f.code not in settled]
    add("audit_clear", not open_audit, ", ".join(sorted({f.code for f in open_audit})))

    add(
        "no_inversions",
        project.id not in got.inversions,
        "a value was decided by crawl order against publication order",
    )
    add(
        "duplicates_answered",
        project.id not in got.suspected,
        "another row may be the same campus; merge it or park the pair",
    )

    # --- T2: are the fields a reader acts on there, and backed? -----------------
    #
    # NOT_APPLICABLE counts as satisfied. `mw_built` on an announced project is
    # correctly null, and treating 12-of-12 as the bar reported 1,101 of 1,171 rows
    # as broken — a target nothing could reach, which is a target nobody uses.
    states = gaps.for_project(project)
    gapped = [s.field for s in states if s.is_gap and s.field not in gaps.UNMEASURABLE]
    add("fields_present", not gapped, f"missing {', '.join(gapped)}" if gapped else "")

    # A 待确认 value is acceptable here and a silent one is not: the tier asks
    # whether the row is honest about what stands behind each value, not whether
    # every value is quoted. Demanding the latter would fail rows for correctly
    # reporting that a figure is unconfirmed.
    unbacked = [
        b.field
        for b in quality.value_bases(session, project_ids=[project.id])
        if b.bucket == quality.SILENT_DEFECT
    ]
    add("values_backed", not unbacked, f"unbacked: {', '.join(unbacked)}" if unbacked else "")

    # --- T3: has every open question been answered? -----------------------------
    warnings = [f for f in findings if f.severity == logic.WARNING and f.code not in settled]
    add("warnings_settled", not warnings, ", ".join(sorted({f.code for f in warnings})))

    groups = got.block_groups.get(project.id, [])
    add(
        "blocks_settled",
        not groups,
        f"{len(groups)} block group(s) unresolved: {', '.join(sorted(set(groups)))}",
    )

    unconfirmed = [r for r in project.risks if r.unconfirmed and r.status == "open"]
    add("risks_confirmed", not unconfirmed, f"{len(unconfirmed)} obstacle(s) with no usable quote")

    stale = sorted(
        {
            quality.vintage(s.extractor)
            for s in project.sources
            if (s.extractor or "").startswith("crawl:")
            and quality.vintage(s.extractor) != got.stamp
        }
    )
    add("vintage_current", not stale, f"read by {', '.join(stale)}" if stale else "")

    return CleanCard(project_id=project.id, name=project.name, conditions=tuple(checks))


def scan(session: Session, *, project_ids: list[int] | None = None) -> Sweep:
    """Every row's card. About 4.5 seconds over the whole database."""
    got = _shared(session, project_ids)
    query = select(Project)
    if project_ids:
        query = query.where(Project.id.in_(project_ids))

    cards: list[CleanCard] = []
    failures: collections.Counter[str] = collections.Counter()
    histogram: collections.Counter[int] = collections.Counter()
    for project in session.scalars(query).all():
        one = card(session, project, shared=got)
        cards.append(one)
        histogram[one.tier] += 1
        for bad in one.failed:
            failures[bad.key] += 1

    return Sweep(cards=tuple(cards), failures=dict(failures), histogram=dict(histogram))


def worklist(sweep: Sweep, *, tier: int, limit: int | None = None) -> list[CleanCard]:
    """Rows short of `tier`, closest first.

    Closest-first for the reason `enrich.select_projects` gives: the run is judged
    on how many rows clear the bar, and taking a row from one failure to none is
    cheaper than taking one from eight. Ties break on capacity-free grounds — id —
    so a resumed run continues rather than reshuffling.
    """
    short = [c for c in sweep.cards if c.tier < tier]
    short.sort(key=lambda c: (len(c.blocking), -c.tier, c.project_id))
    return short[:limit] if limit else short


__all__ = [
    "CONDITION_LABELS",
    "REMEDIES",
    "TIERS",
    "CleanCard",
    "Condition",
    "Sweep",
    "attention",
    "card",
    "scan",
    "worklist",
]
