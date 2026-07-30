"""Which fields are actually missing, measured against an honest denominator.

Raw coverage over all projects misdirects effort, because for several fields a
NULL is the *correct* answer rather than a hole in our data:

* ``mw_built`` on an announced project is right — nothing is built yet. Counting
  61 announced projects as `mw_built` misses made the field look 13% covered when
  the reachable figure was already most of the way there.
* ``blocker`` is absent from almost every project because almost every project has
  no blocker. Chasing coverage here rewards inventing obstacles.
* ``county``, ``lat`` and ``lon`` follow deterministically from a known locality,
  so their denominator is "projects whose city or county we know". Today the
  schema's `ck_project_locality` makes that every row, but stating the rule keeps
  the number honest if that ever changes.

So each field declares what it is measurable *over*, and anything whose absence
carries no information is reported as unmeasurable instead of as a low score.
That keeps the number the operator chases pointed at work that can succeed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from tracker.models import Project

#: Fields where coverage is not a meaningful target, and why. Reported separately
#: so a low count reads as "expected" rather than as a backlog.
UNMEASURABLE: Final[dict[str, str]] = {
    "blocker": "absence is usually the truth — most projects have no blocker",
    "customer": "a self-built hyperscaler campus has no external customer",
}

#: Fields whose denominator is narrower than "every project", with the predicate
#: that defines it and the reason.
_RESTRICTED: Final[dict[str, tuple[str, str]]] = {
    "mw_built": ("built", "only projects with something built"),
    "county": ("has_locality", "derivable from a known locality"),
    "lat": ("has_locality", "derivable from a known locality"),
    "lon": ("has_locality", "derivable from a known locality"),
}

#: Order the report in, chosen so the fields an operator can actually move sit
#: together rather than being interleaved with the always-100% identity columns.
REPORT_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "company",
    "city",
    "state",
    "phase",
    "county",
    "lat",
    "lon",
    "mw_planned",
    "mw_built",
    "investment_usd",
    "first_announced",
    "expected_online",
    "customer",
    "blocker",
)


def _predicate(kind: str) -> ColumnElement[bool]:
    if kind == "built":
        return Project.phase.in_(("construction", "operational"))
    if kind == "has_locality":
        # City *or* county: a project known only by county already has its county,
        # and scoping to `city IS NOT NULL` would drop those rows from the
        # numerator while they sat in the "filled" column, understating coverage.
        return Project.city.is_not(None) | Project.county.is_not(None)
    raise ValueError(f"unknown applicability rule {kind!r}")


@dataclass(frozen=True)
class FieldGap:
    """Coverage of one field over the rows where it could legitimately be set."""

    field: str
    filled: int
    applicable: int
    note: str | None = None
    measurable: bool = True

    @property
    def missing(self) -> int:
        return max(0, self.applicable - self.filled)

    @property
    def pct(self) -> int | None:
        if not self.measurable or not self.applicable:
            return None
        return round(100 * self.filled / self.applicable)


def measure(session: Session, fields: tuple[str, ...] = REPORT_FIELDS) -> list[FieldGap]:
    """Coverage per field, each against the rows where it is applicable."""
    total = session.scalar(select(func.count()).select_from(Project)) or 0
    out: list[FieldGap] = []

    for name in fields:
        column = getattr(Project, name)
        rule = _RESTRICTED.get(name)
        if rule is None:
            applicable, note = total, None
        else:
            kind, note = rule
            where = _predicate(kind)
            applicable = session.scalar(select(func.count()).select_from(Project).where(where)) or 0
        stmt = select(func.count(column))
        if rule is not None:
            stmt = stmt.where(_predicate(rule[0]))
        filled = session.scalar(stmt) or 0

        if name in UNMEASURABLE:
            out.append(FieldGap(name, filled, applicable, UNMEASURABLE[name], measurable=False))
        else:
            out.append(FieldGap(name, filled, applicable, note))
    return out


#: Per-project applicability. Whole-database coverage uses a SQL predicate over
#: many rows; for one project the same question is answered from the row itself,
#: and the *reason* has to be reportable so an all-out enrichment run can say
#: "this field cannot be filled" rather than "this field failed".
#:
#: Only `mw_built` is decidable this way. `blocker` and `customer` genuinely may
#: not exist, but nothing on the row proves it, so they stay MISSING and the
#: caller is told that a null may well be correct.
_NOT_BUILT_PHASES: Final[frozenset[str]] = frozenset(
    {"announced", "permitting", "paused", "cancelled"}
)

#: A null here is frequently the truth, so an enrichment run must not report
#: failure when it comes back empty.
OFTEN_ABSENT: Final[dict[str, str]] = {
    "blocker": "most projects have no obstacle to report",
    "customer": "a self-built campus has no external customer",
}

FILLED: Final = "filled"
MISSING: Final = "missing"
NOT_APPLICABLE: Final = "not_applicable"


@dataclass(frozen=True)
class FieldState:
    """One field of one project: what it holds, and whether a null is a gap."""

    field: str
    value: object = None
    status: str = MISSING
    reason: str | None = None

    @property
    def is_gap(self) -> bool:
        """True when this field is worth spending an LLM call on."""
        return self.status == MISSING


def for_project(project: Project, fields: tuple[str, ...] = REPORT_FIELDS) -> list[FieldState]:
    """Field-by-field state for a single project.

    Pure: takes the loaded row, touches no session. That keeps it usable both
    before and after an enrichment round without re-querying.
    """
    out: list[FieldState] = []
    for name in fields:
        value = getattr(project, name, None)
        if value is not None:
            out.append(FieldState(name, value, FILLED))
            continue
        if name == "mw_built" and (project.phase or "") in _NOT_BUILT_PHASES:
            out.append(
                FieldState(
                    name,
                    None,
                    NOT_APPLICABLE,
                    f"phase is {project.phase} — nothing is built yet, so null is correct",
                )
            )
            continue
        out.append(FieldState(name, None, MISSING, OFTEN_ABSENT.get(name)))
    return out


def worst(gaps: list[FieldGap], limit: int = 3) -> list[FieldGap]:
    """The measurable fields with the most missing rows — where effort pays."""
    ranked = [g for g in gaps if g.measurable and g.missing]
    ranked.sort(key=lambda g: (-g.missing, g.field))
    return ranked[:limit]


__all__ = [
    "FILLED",
    "MISSING",
    "NOT_APPLICABLE",
    "OFTEN_ABSENT",
    "REPORT_FIELDS",
    "UNMEASURABLE",
    "FieldGap",
    "FieldState",
    "for_project",
    "measure",
    "worst",
]
