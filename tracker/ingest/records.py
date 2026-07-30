"""The shared vocabulary between ingest paths and the upsert path.

Plain dataclasses, no ORM and no I/O, so an ingest module can be unit-tested by
inspecting the records it produces without ever opening a database.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from tracker.vocab import TRACKED_FIELDS, WRITABLE_FIELDS


@dataclass(frozen=True)
class SourceRecord:
    """One citation, plus what it actually asserts.

    ``claims`` is the load-bearing field. It records *this source's* view of the
    project, which is what makes it possible to (a) keep two conflicting
    ``mw_planned`` values instead of destroying one, (b) score agreement between
    sources, and (c) recompute every project field deterministically from the
    full set of citations rather than by order-dependent incremental merging.

    ``source.fields`` is derived from ``claims`` at upsert time, never passed in,
    so the two can never disagree.
    """

    url: str
    source_type: str
    fetched_at: dt.datetime
    excerpt: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)
    extractor: str | None = None

    def tracked_claims(self) -> dict[str, Any]:
        """Claims restricted to real project columns with a non-None value."""
        return {k: v for k, v in self.claims.items() if k in WRITABLE_FIELDS and v is not None}

    def tracked_field_count(self) -> int:
        """How many of the 12 PRD fields this source supports."""
        return sum(1 for k in TRACKED_FIELDS if self.claims.get(k) is not None)


@dataclass(frozen=True)
class EventRecord:
    """A dated milestone. ``source_url`` is resolved to a source_id at upsert."""

    event_date: dt.date
    event_type: str
    description: str
    source_url: str | None = None


@dataclass(frozen=True)
class IngestRecord:
    """One project as a single ingest path sees it.

    ``project`` holds already-normalized values keyed by column name. Identity
    fields (``company``, ``state``, and at least one of ``city``/``county``) must
    be present; everything else is optional.
    """

    project: dict[str, Any]
    sources: list[SourceRecord]
    events: list[EventRecord] = field(default_factory=list)
    #: Set by a path that knows its facts are weak (e.g. an ISO-queue keyword
    #: match). Caps the computed confidence for this record.
    confidence_cap: int | None = None
    #: Free-text disclosures the path wants recorded, e.g. that a MW range was
    #: collapsed to its lower bound, or that queue MW is generator nameplate.
    notes: list[str] = field(default_factory=list)


@dataclass
class IngestReport:
    """Counters for one ingest run, printed as the run summary.

    Every field is reported even when zero: a run that says "filtered 4,312" is
    telling the operator something a silent run does not.
    """

    read: int = 0
    filtered: int = 0
    rejected: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates_flagged: int = 0
    conflicts: int = 0
    fetch_error: int = 0
    parse_error: int = 0
    events: int = 0

    def bump(self, action: str) -> None:
        if action == "insert":
            self.inserted += 1
        elif action == "update":
            self.updated += 1
        elif action == "unchanged":
            self.unchanged += 1

    @property
    def written(self) -> int:
        return self.inserted + self.updated

    def as_rows(self) -> list[tuple[str, int]]:
        """(label, count) pairs in a stable order for table rendering."""
        return [
            ("read", self.read),
            ("filtered out", self.filtered),
            ("rejected", self.rejected),
            ("inserted", self.inserted),
            ("updated", self.updated),
            ("unchanged", self.unchanged),
            ("events", self.events),
            ("duplicates flagged", self.duplicates_flagged),
            ("field conflicts", self.conflicts),
            ("fetch errors", self.fetch_error),
            ("parse errors", self.parse_error),
        ]


__all__ = ["EventRecord", "IngestRecord", "IngestReport", "SourceRecord"]
