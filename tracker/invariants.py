"""The relationships every figure must keep (勾稽关系), declared once and checked.

The client's review of 2026-10-21 (A4) asked for the data's relationships to be
"defined clearly" — company, campus, building, phase — and found figures that
contradicted each other on one page because nothing stated how they must relate.
This module is that statement. Each `Invariant` is a sentence a reader could check
by hand ("a campus's running capacity is never more than its planned capacity"),
a level it applies to, and a check that names every place the data breaks it.

Two kinds, and the difference is what a violation means:

* **held** — the write path guarantees it (`upsert`, `blocks.account`, `capex`).
  A violation is a bug in this codebase, never a fact about the world, and the
  test suite runs these over the fixtures so that it cannot ship.
* **reported** — the data as published does not close yet, and each violation
  says what is missing: most often the campus → building → phase hierarchy that
  Batch 4 of the client plan builds, without which a campus described once as a
  whole and again building by building counts the same megawatts twice.

Read-only, free, no network. `tracker invariants` prints the register; the
report is what the hierarchy and the fact table are measured against as they
land, one invariant at a time moving from *reported* to *held*.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final

from tracker import blocks as blocks_mod

HELD: Final = "held"
REPORTED: Final = "reported"

#: Megawatts below which two figures are the same figure. Capacity is published to
#: whole megawatts at best, and the accounting rounds to half of one.
_TOLERANCE_MW: Final[float] = 0.51


@dataclass(frozen=True)
class Violation:
    """One place the data breaks an invariant, in a reader's words."""

    project_id: int | None
    subject: str
    detail: str
    #: How many megawatts the break involves, for ranking; 0 when it is not a size.
    mw: float = 0.0

    def as_json(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "subject": self.subject,
            "detail": self.detail,
            "mw": round(self.mw, 1),
        }


@dataclass(frozen=True)
class Invariant:
    key: str
    #: campus, tranche or company — which part of the hierarchy it relates.
    level: str
    kind: str
    statement: str
    zh: str
    #: What a violation means, and what would make it hold.
    remedy: str
    check: Callable[[list[Any], Any], list[Violation]] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Finding:
    invariant: Invariant
    violations: tuple[Violation, ...]
    checked: int

    @property
    def holds(self) -> bool:
        return not self.violations

    @property
    def mw(self) -> float:
        return sum(v.mw for v in self.violations)

    def as_json(self, *, limit: int | None = None) -> dict[str, Any]:
        ranked = sorted(self.violations, key=lambda v: (-v.mw, v.project_id or 0))
        shown = ranked if limit is None else ranked[:limit]
        inv = self.invariant
        return {
            "key": inv.key,
            "level": inv.level,
            "kind": inv.kind,
            "statement": inv.statement,
            "zh": inv.zh,
            "remedy": inv.remedy,
            "holds": self.holds,
            "checked": self.checked,
            "violations": len(self.violations),
            "mw": round(self.mw, 1),
            "examples": [v.as_json() for v in shown],
        }


def _mw(value: float | None) -> str:
    return f"{value:,.0f} MW" if value is not None else "no figure"


def _name(project: Any) -> str:
    return f"#{project.id} {project.name}"


# --- campus ---------------------------------------------------------------------------


def _running_le_planned(projects: list[Any], _context: Any) -> list[Violation]:
    out = []
    for p in projects:
        if (
            p.mw_built is not None
            and p.mw_planned is not None
            and p.mw_built > p.mw_planned + _TOLERANCE_MW
        ):
            out.append(
                Violation(
                    p.id,
                    _name(p),
                    f"{_mw(p.mw_built)} running against {_mw(p.mw_planned)} planned",
                    p.mw_built - p.mw_planned,
                )
            )
    return out


def _lines_add_up(projects: list[Any], context: Any) -> list[Violation]:
    out = []
    for p in projects:
        got = context.accounting(p)
        if not got.closes:
            out.append(
                Violation(
                    p.id,
                    _name(p),
                    f"the lines add up to {_mw(got.accounted_mw)} against a total of "
                    f"{_mw(got.total)}",
                    abs(got.accounted_mw - (got.total or 0.0)),
                )
            )
    return out


def _delivering_le_total(projects: list[Any], context: Any) -> list[Violation]:
    out = []
    for p in projects:
        got = context.accounting(p)
        delivering = blocks_mod.delivering(p, got)
        if (
            delivering is not None
            and got.total is not None
            and delivering > got.total + _TOLERANCE_MW
        ):
            out.append(
                Violation(
                    p.id,
                    _name(p),
                    f"{_mw(delivering)} delivering of a {_mw(got.total)} campus",
                    delivering - got.total,
                )
            )
    return out


_BEFORE_ANY_TRANCHE_LIVE: Final = frozenset({"announced", "permitting"})


def _phase_not_behind_tranches(projects: list[Any], _context: Any) -> list[Violation]:
    out = []
    for p in projects:
        if (p.phase or "") not in _BEFORE_ANY_TRANCHE_LIVE:
            continue
        # Named tranches only, as `blocks.phase_after_blocks` reads them when it sets
        # the phase: an unnamed "Phase 1" says nothing about which facility it is,
        # and Switch's The Rock carries two from a city page that put another
        # operator's building on it.
        live = [
            b
            for b in (p.blocks or ())
            if b.status in blocks_mod.LIVE_STATUSES
            and not getattr(b, "generic", False)
            and not blocks_mod.is_generation(b.label, getattr(b, "parent", None))
        ]
        if live:
            out.append(
                Violation(
                    p.id,
                    _name(p),
                    f"phase {p.phase}, but {len(live)} of its tranches "
                    f"({', '.join(sorted(b.label for b in live)[:3])}) are running",
                )
            )
    return out


def _announced_before_online(projects: list[Any], _context: Any) -> list[Violation]:
    out = []
    for p in projects:
        if p.first_announced and p.expected_online and p.expected_online < p.first_announced:
            out.append(
                Violation(
                    p.id,
                    _name(p),
                    f"expected online {p.expected_online.isoformat()}, before it was first "
                    f"announced ({p.first_announced.isoformat()})",
                )
            )
    return out


# --- tranche --------------------------------------------------------------------------


def _residual(projects: list[Any], context: Any, reason: str) -> list[tuple[Any, Any, Any]]:
    """`(project, accounting, residual)` for every campus carrying `reason`."""
    out = []
    for p in projects:
        got = context.accounting(p)
        for r in got.residuals:
            if r.reason == reason and r.mw > _TOLERANCE_MW:
                out.append((p, got, r))
    return out


def _tranche_le_campus(projects: list[Any], context: Any) -> list[Violation]:
    """Out-of-scale tranches whose figure still stands as a confirmed one.

    The nightly `audit resolve` marks such a figure 待确认 when the tranche's own label
    states the real capacity ("2.4 MW Lease" holding 2,400) — the durable record that
    it cannot be counted. One so marked is settled, and counting it here would keep
    the register reporting work the loop has already done.
    """
    out = []
    for p, got, r in _residual(projects, context, "out_of_scale"):
        open_ = [
            b
            for b in (p.blocks or ())
            if b.label in r.labels and blocks_mod.mw_is_confirmed(b) and b.mw is not None
        ]
        if not open_:
            continue
        mw = sum(b.mw for b in open_)
        out.append(
            Violation(
                p.id,
                _name(p),
                f"{', '.join(sorted(b.label for b in open_)[:3])} — {_mw(mw)} against a "
                f"{_mw(got.total)} campus; left out of every figure",
                mw,
            )
        )
    return out


def _tranches_once(projects: list[Any], context: Any) -> list[Violation]:
    return [
        Violation(
            p.id,
            _name(p),
            f"its tranches describe {_mw((got.total or 0.0) + r.mw)} against a "
            f"{_mw(got.total)} campus — {_mw(r.mw)} counted twice",
            r.mw,
        )
        for p, got, r in _residual(projects, context, "overlap")
    ]


def _tranches_placed(projects: list[Any], context: Any) -> list[Violation]:
    return [
        Violation(
            p.id,
            _name(p),
            f"{_mw(r.mw)} in {', '.join(r.labels[:3])}"
            f"{' and more' if len(r.labels) > 3 else ''} — no facility named",
            r.mw,
        )
        for p, got, r in _residual(projects, context, "unplaceable")
    ]


def _live_tranches_le_running(projects: list[Any], context: Any) -> list[Violation]:
    out = []
    for p in projects:
        got = context.accounting(p)
        delivering = blocks_mod.delivering(p, got)
        if got.live_mw and delivering is not None and got.live_mw > delivering + _TOLERANCE_MW:
            out.append(
                Violation(
                    p.id,
                    _name(p),
                    f"tranches marked live add up to {_mw(got.live_mw)}; the campus runs "
                    f"{_mw(delivering)}",
                    got.live_mw - delivering,
                )
            )
    return out


# --- company --------------------------------------------------------------------------


def _company_sums_campuses(projects: list[Any], context: Any) -> list[Violation]:
    """Each counted campus credited once and in full, and the positions summing to them."""
    tally = context.tally()
    if tally is None:
        return []
    from tracker import capex

    by_id = {p.id: p for p in projects}
    out = []
    campus_credit = 0.0
    for pid in sorted(tally.counted - set(tally.set_aside)):
        project = by_id.get(pid)
        if project is None or project.phase in ("paused", "cancelled"):
            continue
        credited = sum(planned for planned, _ in capex.credits(project).values())
        campus_credit += credited
        planned = float(project.mw_planned or 0.0)
        if abs(credited - planned) > _TOLERANCE_MW:
            out.append(
                Violation(
                    pid,
                    _name(project),
                    f"credited {_mw(credited)} across buyers against {_mw(planned)} planned",
                    abs(credited - planned),
                )
            )
    positions = sum(p.mw_planned for p in tally.positions.values())
    if abs(positions - campus_credit) > _TOLERANCE_MW * 10:
        out.append(
            Violation(
                None,
                "every company",
                f"companies add up to {_mw(positions)}; their campuses to {_mw(campus_credit)}",
                abs(positions - campus_credit),
            )
        )
    return out


#: The register, in the order a reader meets the hierarchy: a campus, its parts, its
#: owner. The statements are the reader docs' (`webui/docs/*/invariants.md`).
INVARIANTS: Final[tuple[Invariant, ...]] = (
    Invariant(
        "running_le_planned",
        "campus",
        HELD,
        "A campus's running capacity is never more than its planned capacity.",
        "园区运行容量不超过规划容量。",
        "Enforced when a row is derived: the better-supported figure decides which "
        "of the two describes something else (`upsert.settle_capacity`).",
        _running_le_planned,
    ),
    Invariant(
        "lines_add_up",
        "campus",
        HELD,
        "A campus's capacity lines — cited per tranche, unconfirmed, unplaced, not yet "
        "itemised, less what is counted twice — add up to its total.",
        "园区各分项容量之和（按分期引用、待确认、未对应设施、尚未细分，减去重复计算）等于园区总容量。",  # noqa: RUF001 - Chinese punctuation in Chinese text
        "Holds by construction (`blocks.account`); a break is a bug in the arithmetic.",
        _lines_add_up,
    ),
    Invariant(
        "delivering_le_total",
        "campus",
        HELD,
        "What a campus delivers is never more than its total.",
        "园区正在交付的容量不超过园区总容量。",
        "The headline is the campus's own running figure (`blocks.delivering`), "
        "which the rule above caps.",
        _delivering_le_total,
    ),
    Invariant(
        "company_sums_campuses",
        "company",
        HELD,
        "A company's capacity is the sum of the campuses credited to it: each campus "
        "counted once, and in full, across its buyers.",
        "公司容量等于其名下各园区之和：每个园区只计一次，且在各买方之间完整分配。",  # noqa: RUF001 - Chinese punctuation in Chinese text
        "The company pages, Capex and the map all read one rollup (`capex.credits`).",
        _company_sums_campuses,
    ),
    Invariant(
        "tranche_le_campus",
        "tranche",
        REPORTED,
        "No tranche is larger than its campus.",
        "任何分期都不大于其所属园区。",
        "Almost always kilowatts read as megawatts. Excluded from every figure already; "
        "ruling the claim out (`tracker logic rule-out`) removes it from the record.",
        _tranche_le_campus,
    ),
    Invariant(
        "tranches_once",
        "tranche",
        REPORTED,
        "A campus's tranches describe it once: its buildings and phases add up to the "
        "campus, not past it.",
        "园区的楼栋与分期只描述园区一次：其容量之和等于园区总容量，而不超过它。",  # noqa: RUF001 - Chinese punctuation in Chinese text
        "Sources describe a campus as a whole and again building by building, or one "
        "building under two names. Holds once tranches nest in a campus → building → "
        "phase hierarchy (Batch 4).",
        _tranches_once,
    ),
    Invariant(
        "tranches_placed",
        "tranche",
        REPORTED,
        "Every tranche belongs to a named facility of its campus.",
        "每个分期都属于园区内一个具名设施。",
        '"Phase 2" without saying of which building. Holds once each tranche is tied '
        "to a facility in the hierarchy (Batch 4).",
        _tranches_placed,
    ),
    Invariant(
        "live_tranches_le_running",
        "tranche",
        REPORTED,
        "The tranches marked live add up to no more than the campus's running capacity.",
        "已投运分期的容量之和不超过园区运行容量。",
        "The same double description as above, seen in the running figure: Abilene's "
        "live tranches sum past its 421 MW running. Holds with the hierarchy (Batch 4).",
        _live_tranches_le_running,
    ),
    Invariant(
        "phase_not_behind_tranches",
        "campus",
        REPORTED,
        "A campus is not at an earlier phase than its own running tranches.",
        "园区所处阶段不早于其已投运的分期。",
        "A phase the tranches outran; `backfill derive` re-reads it from the tranches.",
        _phase_not_behind_tranches,
    ),
    Invariant(
        "announced_before_online",
        "campus",
        REPORTED,
        "A campus is announced before it is expected online.",
        "园区宣布时间早于预计上线时间。",
        "Usually the earliest article found is later than the real announcement — an "
        "existing site first reported when it expanded. Worth a search for the original.",
        _announced_before_online,
    ),
)


class _Context:
    """What several checks share, computed once per run."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self._accounting: dict[int, Any] = {}
        self._tally: Any = None
        self._tallied = False

    def accounting(self, project: Any) -> Any:
        got = self._accounting.get(project.id)
        if got is None:
            got = self._accounting[project.id] = blocks_mod.account(project)
        return got

    def tally(self) -> Any:
        if not self._tallied:
            self._tallied = True
            if self.session is not None:
                from tracker import capex

                self._tally = capex.counting(self.session)
        return self._tally


def check(
    session: Any,
    *,
    keys: Iterable[str] | None = None,
    projects: list[Any] | None = None,
) -> list[Finding]:
    """Run the register, or the named invariants of it, over every campus. Never writes."""
    if projects is None:
        from tracker.export import fetch_projects

        projects = fetch_projects(session)
    wanted = set(keys) if keys else None
    context = _Context(session)
    out = []
    for invariant in INVARIANTS:
        if wanted is not None and invariant.key not in wanted:
            continue
        violations = invariant.check(projects, context)
        out.append(Finding(invariant, tuple(violations), checked=len(projects)))
    return out


def report_json(findings: list[Finding], *, limit: int | None = 10) -> dict[str, Any]:
    return {
        "as_of": dt.date.today().isoformat(),
        "holds": all(f.holds for f in findings if f.invariant.kind == HELD),
        "invariants": [f.as_json(limit=limit) for f in findings],
    }


__all__ = [
    "HELD",
    "INVARIANTS",
    "REPORTED",
    "Finding",
    "Invariant",
    "Violation",
    "check",
    "report_json",
]
