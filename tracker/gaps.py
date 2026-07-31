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

import json
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

#: How a stored value came to be. The PRD draws exactly this line —
#: 不能直接把AI的回答当作事实 — so a reader must never have to guess which they are
#: looking at.
REPORTED: Final = "reported"  # a verbatim quote in a fetched article supports it
DERIVED: Final = "derived"  # computed from reference data (Census), deterministic
UNCONFIRMED: Final = "unconfirmed"  # 待确认: extracted, but nothing quotable backs it
INFERRED: Final = "inferred"  # a model's judgement over the recorded facts
#: The column is NOT NULL, nobody asserted anything, and the schema default is what
#: is sitting there. Distinct from UNCONFIRMED, which the value used to be reported
#: as, and the distinction is not pedantic: 待确认 says a source claimed this and
#: could not prove it, so rendering a defaulted `phase` that way told the reader
#: somebody had said "announced". Nobody had. `vocab.DEFAULT_PHASE` already relies
#: on ingest paths omitting the field from `source.fields` precisely so this case
#: stays visible; this names it.
DEFAULTED: Final = "defaulted"

#: field -> the value the schema puts there when no source speaks. Only columns
#: that are NOT NULL with a server default can be defaulted; everything else is
#: simply NULL and has no provenance to report.
_SCHEMA_DEFAULTS: Final[dict[str, object]] = {"phase": "announced", "country": "US"}


def _asserted(source, field: str) -> bool:
    """Does this source list `field` at all, confirmed or 待确认?"""
    listed = f"{source.fields or ''},{source.unconfirmed_fields or ''}"
    return field in {f.strip() for f in listed.split(",") if f.strip()}


def _tier_of(source, field: str) -> str:
    """The tier one source's assertion about one field sits at."""
    confirmed = {f.strip() for f in (source.fields or "").split(",") if f.strip()}
    if field not in confirmed:
        # Either explicitly 待确认, or listed nowhere — both mean nothing quotable
        # backs it, which is what the tier says.
        return UNCONFIRMED
    extractor = source.extractor or ""
    if extractor.startswith("derived:"):
        return DERIVED
    if extractor.startswith("inferred:"):
        return INFERRED
    return REPORTED


@dataclass(frozen=True)
class FieldProvenance:
    """Where one field's stored value came from, and the words behind it."""

    field: str
    tier: str
    quote: str | None = None
    #: True when `quote` is the sentence recorded for *this field*
    #: (`source.quotes`). False when it is the source's whole `excerpt`, which
    #: covers everything that source asserted and may not mention this value at
    #: all. A reader must be told which they are looking at — showing a paragraph
    #: as though it were the sentence behind one number is the failure this whole
    #: tier system exists to prevent.
    quote_is_exact: bool = False
    source_url: str | None = None
    #: Position in the project's sources sorted by url, matching the order
    #: `export.to_json_object` emits them in, so a consumer can index straight in.
    source_index: int | None = None


def _winning_source(project, field: str, value):
    """The source whose claim actually became the stored value.

    Not simply the strongest source that mentions the field. For a field two
    sources disagree on, "the source with the best tier" and "the source whose
    number won the merge" are different sources, and quoting the first would
    print a sentence stating a figure the row does not hold.

    So the merge order is asked for, not re-derived: `upsert.claims_by_field`
    applies the same confirmed-first, weight, recency, url ordering the write path
    used, and the first claim in it equal to the stored value is the one that won.
    """
    from tracker.upsert import claim_value, claims_by_field

    sources = list(getattr(project, "sources", ()) or ())
    by_url = {s.url: s for s in sources}

    claims = claims_by_field(sources).get(field, [])
    # `_resolve` discards 待确认 claims whenever a confirmed one exists; mirror that
    # or an unquoted claim could be credited with a quoted value.
    if any(c.confirmed for c in claims):
        claims = [c for c in claims if c.confirmed]

    target = claim_value(value)
    for claim in claims:
        if _same_value(claim.value, target) and claim.url in by_url:
            return by_url[claim.url]

    # No claim holds this value. Reachable for the columns `upsert` derives rather
    # than merges (`blocker` comes from the risk rows, `notes` is assembled), and
    # for a row edited by hand. Fall back to the strongest source that at least
    # says it supports the field — the citation is still the right one to show,
    # only its exact wording is not pinned down.
    supporting = [s for s in sources if _asserted(s, field)]
    if not supporting:
        return None
    order = {tier: i for i, tier in enumerate((REPORTED, DERIVED, INFERRED, UNCONFIRMED))}
    supporting.sort(key=lambda s: (order[_tier_of(s, field)], -_source_weight(s), s.url))
    return supporting[0]


def _source_weight(source) -> int:
    from tracker.confidence import SOURCE_WEIGHTS

    return SOURCE_WEIGHTS.get(source.source_type, 1)


def _same_value(claim_value_, stored) -> bool:
    """Compare a JSON-round-tripped claim against the stored column value.

    Claims go through JSON, so a date is an ISO string and an integer may have
    become a float. `state` is also upper-cased on write. Loose comparison here is
    correct: the question is "did this claim become this value", not "are these the
    same object".
    """
    if isinstance(claim_value_, str) and isinstance(stored, str):
        return claim_value_.strip().lower() == stored.strip().lower()
    if isinstance(claim_value_, bool) or isinstance(stored, bool):
        return claim_value_ is stored
    if isinstance(claim_value_, int | float) and isinstance(stored, int | float):
        return float(claim_value_) == float(stored)
    return claim_value_ == stored


def provenance(project, field: str) -> FieldProvenance | None:
    """Which tier the value rests on, the sentence behind it, and whose it is.

    ``None`` when the field is NULL — there is nothing to justify.

    Derived rather than stored, for the same reason `confidence` is recomputed: a
    column recording provenance would drift the moment a source changed.
    """
    value = getattr(project, field, None)
    if value is None:
        return None

    source = _winning_source(project, field, value)
    if source is None:
        # Nothing claims this value and nothing says it supports the field. Either
        # the NOT NULL column fell back to its schema default, or the row was
        # edited by hand. Say which — "defaulted" is checkable and 待确认 would be
        # an untrue statement about a source.
        if _SCHEMA_DEFAULTS.get(field) == value:
            return FieldProvenance(field, DEFAULTED)
        return FieldProvenance(field, UNCONFIRMED)

    ordered = sorted(getattr(project, "sources", ()) or (), key=lambda s: s.url)
    index = next((i for i, s in enumerate(ordered) if s.url == source.url), None)

    quote: str | None = None
    exact = False
    if source.quotes:
        try:
            recorded = json.loads(source.quotes)
        except (TypeError, ValueError):
            recorded = {}
        if isinstance(recorded, dict):
            candidate = recorded.get(field)
            if isinstance(candidate, str) and candidate.strip():
                quote, exact = candidate.strip(), True
    if quote is None:
        quote = source.excerpt

    return FieldProvenance(field, _tier_of(source, field), quote, exact, source.url, index)


def basis(project, field: str) -> str | None:
    """Which tier the project's current value for `field` rests on.

    The tier half of :func:`provenance`, kept as its own name because most callers
    want only this and reading it should not require knowing about quotes.
    """
    result = provenance(project, field)
    return result.tier if result is not None else None


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
    "DEFAULTED",
    "DERIVED",
    "FILLED",
    "INFERRED",
    "MISSING",
    "NOT_APPLICABLE",
    "OFTEN_ABSENT",
    "REPORTED",
    "REPORT_FIELDS",
    "UNCONFIRMED",
    "UNMEASURABLE",
    "FieldGap",
    "FieldProvenance",
    "FieldState",
    "basis",
    "for_project",
    "measure",
    "provenance",
    "worst",
]
