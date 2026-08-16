"""The shared vocabulary between ingest paths and the upsert path.

Plain dataclasses, no ORM and no I/O, so an ingest module can be unit-tested by
inspecting the records it produces without ever opening a database.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from tracker.vocab import DEFAULT_BLOCK_STATUS, TRACKED_FIELDS, WRITABLE_FIELDS


@dataclass(frozen=True)
class BlockRecord:
    """One capacity block as a single source described it.

    A tranche of a campus with its own state, customer and dates — the thing
    `project`'s single `phase` / `mw_planned` / `customer` cannot express. See
    `tracker/blocks.py`.

    `parent` is what lets an article's "Phase 3" meet a filing's "AZP-3 Phase 3":
    a label naming only a phase cannot be placed without knowing of which facility,
    and one project row routinely holds several campuses.
    """

    label: str
    parent: str | None = None
    mw: float | None = None
    status: str = DEFAULT_BLOCK_STATUS
    customer: str | None = None
    expected_online: dt.date | None = None
    energized_on: dt.date | None = None
    investment_usd: int | None = None
    #: block field -> the verbatim sentence that got it through the gate. Per field,
    #: because project 39's failure was money from one facility sitting beside
    #: capacity from another.
    quotes: dict[str, str] = field(default_factory=dict)
    #: Fields this block asserted with no quote the gate could verify — 待确认 at
    #: block granularity, same meaning and same consequence as on a source.
    unconfirmed: frozenset[str] = frozenset()

    def as_json(self) -> dict[str, Any]:
        """The shape stored in `source.blocks`. Sorted, so re-ingest is byte-equal."""
        out: dict[str, Any] = {"label": self.label}
        for name in (
            "parent",
            "mw",
            "status",
            "customer",
            "expected_online",
            "energized_on",
            "investment_usd",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            out[name] = value.isoformat() if hasattr(value, "isoformat") else value
        if self.quotes:
            out["quotes"] = dict(sorted(self.quotes.items()))
        if self.unconfirmed:
            out["unconfirmed"] = sorted(self.unconfirmed)
        return out


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
    #: Claim names this source asserted with no quote the evidence gate could
    #: verify — the PRD's 待确认 tier. They live in `claims` like any other value so
    #: they can be resolved and displayed, but they are excluded from
    #: `source.fields`, which is what `confidence` and the "9 of 12" count read.
    #: A project field takes an unconfirmed value only when no confirmed one exists.
    unconfirmed: frozenset[str] = frozenset()
    #: field -> why the gate refused it, from `vocab.UNCONFIRMED_REASONS`. Same
    #: keys as `unconfirmed`, which stays a set because almost everything reading
    #: it only asks "is this one confirmed". The reason is for the two readers
    #: that need more: the capex exclusion, which means to drop `out_of_scale`
    #: figures and not merely unquoted ones, and the console, which shows a
    #: different chip for "go and find a source" than for "this is the wrong
    #: number". A tuple rather than a dict so the record stays hashable.
    unconfirmed_reasons: tuple[tuple[str, str], ...] = ()
    #: field -> the verbatim sentence that got that value through the evidence gate.
    #: `excerpt` is up to three of these concatenated for display, so it cannot say
    #: which sentence evidenced which value; this can. Empty for a path with nothing
    #: quotable to offer — an ISO queue row or a Census lookup asserts values without
    #: any prose behind them, and inventing one would be the fabrication the gate
    #: exists to prevent.
    quotes: dict[str, str] = field(default_factory=dict)
    #: field -> {scope, bound, modality, as_of}: what the value is a value *of*,
    #: how exactly the article stated it, whether it has happened, and when it was
    #: true. Each axis is verified against that field's stored quote by
    #: `crawl.axis_gate` and degrades to a neutral value rather than rejecting the
    #: figure, so this can never reduce coverage. Only fields whose envelope says
    #: something appear — an entry neutral on every axis carries no information and
    #: would inflate the very coverage measurement that decides whether the axes
    #: are worth keeping.
    claim_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Capacity blocks this source described — the tranches of the campus that have
    #: their own state, customer and dates. Belongs to the *source* rather than the
    #: project, unlike `events` and `risks`, because two sources routinely describe
    #: different phases of one site and both descriptions have to survive to be
    #: merged. Empty is the common and correct answer: a campus an article treats
    #: as one thing is one thing.
    blocks: list[BlockRecord] = field(default_factory=list)

    def tracked_claims(self) -> dict[str, Any]:
        """Claims restricted to real project columns with a non-None value."""
        return {k: v for k, v in self.claims.items() if k in WRITABLE_FIELDS and v is not None}

    def confirmed_claims(self) -> dict[str, Any]:
        """Tracked claims that a verbatim quote actually supports."""
        return {k: v for k, v in self.tracked_claims().items() if k not in self.unconfirmed}

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
    #: The verbatim sentence the milestone stands on, article's own words, or None
    #: when the gate could not verify one. Mirrors `RiskRecord.quote`.
    quote: str | None = None
    #: Why the gate did not confirm it (`vocab.UNCONFIRMED_REASONS`); None means it
    #: did. Events had no gate at all until migration 0017, which is why the
    #: backfill there marks every older row `no_quote` rather than leaving NULL to
    #: make a claim nobody checked.
    unconfirmed: str | None = None


@dataclass(frozen=True)
class RiskRecord:
    """One obstacle as a single source reports it.

    ``summary`` and ``quote`` are deliberately separate. The summary is one
    sentence and is allowed to be a paraphrase; the quote is a verbatim sentence
    the evidence gate has verified against the fetched article. Keeping both is
    what let obstacles become storable without loosening the gate: a paraphrase can
    never be a verbatim substring, so demanding that the summary itself be quotable
    discarded every obstacle the model correctly identified.

    ``source_url`` is resolved to a source_id at upsert, as on ``EventRecord``.
    """

    category: str
    severity: str
    summary: str
    quote: str | None = None
    first_seen: dt.date | None = None
    delay_days: int | None = None
    source_url: str | None = None
    #: Why the gate did not confirm this, from `vocab.UNCONFIRMED_REASONS`, or
    #: None when it did. An obstacle the gate refuses is kept and flagged rather
    #: than deleted — the same answer migration 0006 gave for field values, and
    #: the reason matters because `no_quote` wants another source while
    #: `quote_off_target` wants the category corrected.
    unconfirmed: str | None = None


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
    #: Obstacles this path found. `project.blocker` is derived from the stored rows,
    #: never written directly, so an ingest path that finds none leaves any existing
    #: obstacle alone rather than clearing it.
    risks: list[RiskRecord] = field(default_factory=list)
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
    #: Pages fetched successfully that were navigation furniture rather than an
    #: article, refused before the LLM call. Counted separately from the errors
    #: because it is a saving, not a failure — and because a run reporting eight
    #: of these from one host is how the operator sees the pattern.
    thin_content: int = 0
    #: URLs `--cached-only` declined to fetch. A saving, like `thin_content`, and
    #: reported for the same reason: a re-extraction run that silently skipped
    #: three quarters of its worklist reads as a run that covered it.
    skipped_uncached: int = 0
    #: Projects `--existing-only` declined to create. A saving on a re-read and a
    #: *finding* on any other run: an article naming a campus this database does
    #: not track is a candidate to add deliberately, so the number is reported
    #: rather than left to be inferred from a flat project count.
    refused_new: int = 0
    #: URLs skipped because `seed/sources.toml` ignores their publisher. A saving,
    #: reported for the same reason `refused_new` is: a run that silently declined
    #: a quarter of its worklist reads as a run that covered it.
    skipped_ignored: int = 0
    events: int = 0
    risks: int = 0
    #: What the run actually spent. `ExtractionOutcome` has carried these per URL
    #: since it was written and nothing ever added them up, so "how much did that
    #: cost" had no answer short of the provider's dashboard.
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

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
        """(label, count) pairs in a stable order for table rendering.

        `new projects refused` appears only when there are some. Every other line
        is worth showing at zero — "0 fetch errors" is a result — but only
        `ingest crawl` has `--existing-only`, so on the other paths it is a row
        that can never be anything but zero, widening every report to say nothing.
        """
        refused = [("new projects refused", self.refused_new)] if self.refused_new else []
        ignored = [("publisher ignored", self.skipped_ignored)] if self.skipped_ignored else []
        return [
            ("read", self.read),
            ("filtered out", self.filtered),
            ("rejected", self.rejected),
            ("inserted", self.inserted),
            ("updated", self.updated),
            ("unchanged", self.unchanged),
            ("events", self.events),
            ("risks", self.risks),
            ("duplicates flagged", self.duplicates_flagged),
            ("field conflicts", self.conflicts),
            ("fetch errors", self.fetch_error),
            ("parse errors", self.parse_error),
            ("not an article", self.thin_content),
            ("not cached", self.skipped_uncached),
            *ignored,
            *refused,
        ]


__all__ = ["EventRecord", "IngestRecord", "IngestReport", "RiskRecord", "SourceRecord"]
