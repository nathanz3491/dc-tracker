"""Re-derive stored projects from the citations they already hold.

**The gap this closes.** Every value on a project is a function of its sources —
that is the central design choice `upsert` opens with. But the function is only
*applied* at ingest, so improving the function does not improve the rows. Six
months of fixes to the merge policy, the evidence gate and the block rollup reach
a project only when something writes to it again, and `enrich` — the command
actually reached for — only ever *adds* a source. It cannot correct a row it did
not create.

So this is the command to run after any change to how something is derived. No
LLM, no network, no migration: it re-reads `source.claims`, which is already on
disk, and rewrites what those claims imply.

**Nothing new is computed here.** `upsert.recompute_from_sources` already does the
whole job, and it is the same function `tracker merge` uses so that folding two
rows cannot apply a different merge policy than ingesting them did. This module is
a loop, a diff and a report.

**Running it twice must change nothing.** That is the property the test asserts,
and it is not decoration: if a second pass keeps moving rows then the derivation is
not a pure function of what is stored, and every number in the database is
whichever pass ran last. `recompute_confidence`, `recompute_blocks` and
`recompute_h200` all carry the same obligation — see
`test_confidence_cache_is_consistent` and its two siblings.

**What it does not do.** It never fetches, never calls a model, and never creates
or deletes a project. A project with no sources is left exactly as it is rather
than emptied: the row may have been entered by hand, and reading "no claims" as
"no facts" would delete an operator's work on the way past.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import Project
from tracker.vocab import WRITABLE_FIELDS

log = logging.getLogger(__name__)

#: Everything compared before and after. `WRITABLE_FIELDS` is what the claims
#: decide — it already contains `blocker` and `h200_equivalent` — and `confidence`
#: is a cache of a pure function of the same claims, recomputed in the same call.
#: A repair that moved a confidence without moving a value is worth seeing.
COMPARED: tuple[str, ...] = (*WRITABLE_FIELDS, "confidence")


@dataclass
class Change:
    """One field that moved on one project."""

    project_id: int
    field: str
    before: Any
    after: Any

    def render(self) -> str:
        return f"#{self.project_id} {self.field}: {_short(self.before)} → {_short(self.after)}"


def _short(value: Any) -> str:
    """A value in one line, so a report of 300 rows stays readable."""
    if value is None:
        return "—"
    text = str(value).replace("\n", " ")
    return text if len(text) <= 48 else text[:47] + "…"


@dataclass
class DeriveReport:
    """What re-deriving found, and what it wrote."""

    #: Projects considered.
    projects: int = 0
    #: Projects with no citations at all, left untouched. Counted rather than
    #: skipped silently: on a database where this number is large, the repair is
    #: not reaching most of the table and the operator should know why.
    unsourced: int = 0
    #: Projects where at least one field moved.
    changed: int = 0
    #: Blocks inserted, updated or deleted by the rebuild.
    blocks_touched: int = 0
    #: Every field that moved, in project order.
    changes: list[Change] = dc_field(default_factory=list)
    #: Fields the sources still disagree about, as `recompute_from_sources`
    #: reports them. Not a failure — the disagreement is disclosed in the row's
    #: notes and both values keep their own citation.
    conflicts: dict[int, list[str]] = dc_field(default_factory=dict)

    @property
    def by_field(self) -> dict[str, int]:
        """field -> how many rows it moved on, largest first."""
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change.field] = counts.get(change.field, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("projects considered", self.projects),
            ("no citations, left alone", self.unsourced),
            ("projects changed", self.changed),
            ("field values moved", len(self.changes)),
            ("blocks rebuilt", self.blocks_touched),
        ]


def _snapshot(project: Project) -> dict[str, Any]:
    return {name: getattr(project, name, None) for name in COMPARED}


def run(session: Session, *, project_id: int | None = None) -> DeriveReport:
    """Re-derive every project (or one) from its stored claims.

    Writes through the session. The caller decides whether to commit — which is
    what makes `--dry-run` a `commit=False` scope rather than a second code path
    that could disagree with the real one.
    """
    from tracker.upsert import recompute_from_sources

    query = select(Project).order_by(Project.id.asc())
    if project_id is not None:
        query = query.where(Project.id == project_id)

    report = DeriveReport()
    for project in session.scalars(query).all():
        report.projects += 1
        if not project.sources:
            report.unsourced += 1
            continue

        before = _snapshot(project)
        blocks_before = _block_fingerprint(project)
        conflicts = recompute_from_sources(session, project)
        after = _snapshot(project)

        moved = [
            Change(project.id, name, before[name], after[name])
            for name in COMPARED
            if before[name] != after[name]
        ]
        blocks_after = _block_fingerprint(project)
        report.blocks_touched += sum(
            1
            for key in blocks_before.keys() | blocks_after.keys()
            if blocks_before.get(key) != blocks_after.get(key)
        )
        if moved:
            report.changed += 1
            report.changes.extend(moved)
        if conflicts:
            report.conflicts[project.id] = list(conflicts)

    return report


#: Every column `blocks.rebuild` writes. Compared column by column rather than
#: trusting the rebuild's own return value, which counts a row rewritten with
#: identical values — and this module's whole claim is that a second pass is a
#: no-op.
_BLOCK_COLUMNS: tuple[str, ...] = (
    "block_key",
    "label",
    "parent",
    "generic",
    "mw",
    "status",
    "customer",
    "expected_online",
    "energized_on",
    "investment_usd",
    "quotes",
    "unconfirmed_fields",
    "source_id",
)


def _block_fingerprint(project: Project) -> dict[str, tuple]:
    """The tranches keyed by identity, so an edit counts once rather than twice."""
    return {
        b.block_key: tuple(getattr(b, name, None) for name in _BLOCK_COLUMNS)
        for b in (getattr(project, "blocks", ()) or ())
    }


__all__ = ["COMPARED", "Change", "DeriveReport", "run"]
