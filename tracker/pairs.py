"""Pairs of projects somebody has ruled are *not* the same site.

`tracker duplicates` proposes and never merges, which is right: silently folding
two campuses together destroys data that no re-crawl recovers. The cost of that
caution is that the report has to be answerable, and until this module there was
only one answer available — merge — so a wrong pair came back on every run, ahead
of the right ones.

Two facts make parking more than cosmetic:

* `capex.rollup` reads the same suspected pairs and holds one row of every group
  out of the buyer table, disclosed in the `*_skipped` fields. A false pair
  therefore removes a real campus's capacity from a number somebody quotes. A
  parked pair is dropped before that happens.
* Nothing here deletes or edits a project. Parking is a statement about two rows,
  stored beside them, and unparking puts the question straight back.

Everything is keyed on ids because that is what the operator has in front of them
when they read the report — the ids are printed on every line.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import NotDuplicate, Project


class UnknownProject(LookupError):
    """A project id that is not in the database."""


@dataclass(frozen=True)
class ParkedPair:
    """One recorded "these are different sites", with both rows named."""

    a_id: int
    b_id: int
    a_label: str
    b_label: str
    decided_by: str
    reason: str
    decided_at: str

    def as_json(self) -> dict[str, object]:
        return {
            "a_id": self.a_id,
            "b_id": self.b_id,
            "a": self.a_label,
            "b": self.b_label,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "decided_at": self.decided_at,
        }


def canonical(a_id: int, b_id: int) -> tuple[int, int]:
    """The one spelling of a pair. Enforced by a CHECK, ordered here."""
    return (a_id, b_id) if a_id < b_id else (b_id, a_id)


def parked_keys(session: Session) -> set[tuple[int, int]]:
    """Every parked pair as an ordered id tuple, for a fast membership test.

    Read once per report rather than per candidate pair: the scan is O(rows in a
    locality squared) and a query inside that loop is how a free check becomes a
    slow one.
    """
    return {canonical(row.a_id, row.b_id) for row in session.scalars(select(NotDuplicate)).all()}


def is_parked(session: Session, a_id: int, b_id: int) -> bool:
    return canonical(a_id, b_id) in parked_keys(session)


def _require(session: Session, ids: list[int]) -> dict[int, Project]:
    found = {p.id: p for p in session.scalars(select(Project).where(Project.id.in_(ids))).all()}
    missing = [i for i in ids if i not in found]
    if missing:
        raise UnknownProject(
            f"no project with id {', '.join(str(i) for i in missing)}. "
            "Run `tracker duplicates` for the ids it is asking about."
        )
    return found


def park(
    session: Session,
    ids: list[int],
    *,
    reason: str = "",
    by: str = "operator",
) -> list[tuple[int, int]]:
    """Record that every pair among `ids` describes a different site.

    Takes a *set* rather than a pair because that is the shape of the report: a
    group of three rows in one city produces three pairs, and an operator who has
    read all three should answer once. Every pair is stored separately, so a
    fourth row appearing later is still asked about.

    Idempotent — re-parking a pair leaves the original decision, including who
    made it, rather than overwriting an operator's judgement with a model's.
    Returns the pairs newly written.
    """
    if len(ids) < 2:
        raise ValueError("parking needs at least two project ids")
    _require(session, ids)

    already = parked_keys(session)
    written: list[tuple[int, int]] = []
    for a_id, b_id in combinations(sorted(set(ids)), 2):
        key = canonical(a_id, b_id)
        if key in already:
            continue
        session.add(NotDuplicate(a_id=key[0], b_id=key[1], decided_by=by, reason=reason or None))
        already.add(key)
        written.append(key)
    session.flush()
    return written


def unpark(session: Session, ids: list[int]) -> list[tuple[int, int]]:
    """Put every pair among `ids` back in the report. Returns the pairs removed.

    With exactly two ids this reopens one question. With more, it reopens all the
    pairs among them, mirroring `park` — an operator undoing a decision should not
    have to work out which spellings it wrote.
    """
    if len(ids) < 2:
        raise ValueError("unparking needs at least two project ids")
    wanted = {canonical(a, b) for a, b in combinations(sorted(set(ids)), 2)}
    removed: list[tuple[int, int]] = []
    for row in session.scalars(select(NotDuplicate)).all():
        key = canonical(row.a_id, row.b_id)
        if key in wanted:
            session.delete(row)
            removed.append(key)
    session.flush()
    return sorted(removed)


def listing(session: Session) -> list[ParkedPair]:
    """Every parked pair, newest decision first, with both rows named."""
    rows = session.scalars(select(NotDuplicate)).all()
    ids = {i for row in rows for i in (row.a_id, row.b_id)}
    labels = {
        p.id: f"{p.company} — {p.name}"
        for p in session.scalars(select(Project).where(Project.id.in_(ids))).all()
    }
    out = [
        ParkedPair(
            a_id=row.a_id,
            b_id=row.b_id,
            a_label=labels.get(row.a_id, "(deleted)"),
            b_label=labels.get(row.b_id, "(deleted)"),
            decided_by=row.decided_by,
            reason=row.reason or "",
            decided_at=str(row.decided_at or "")[:19],
        )
        for row in rows
    ]
    return sorted(out, key=lambda p: (p.decided_at, p.a_id, p.b_id), reverse=True)


__all__ = [
    "ParkedPair",
    "UnknownProject",
    "canonical",
    "is_parked",
    "listing",
    "park",
    "parked_keys",
    "unpark",
]
