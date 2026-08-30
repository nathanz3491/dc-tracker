"""SQLAlchemy 2.0 models mirroring `migrations/*.sql`.

The SQL files are authoritative at runtime — `init_db` applies them, not
`create_all`. These models exist so queries are typed and readable, and
`tests/test_db.py::test_models_match_migrations` fails the build if they drift
from the SQL. That test is why maintaining two definitions is safe rather than
merely duplicated.

Column types are chosen to render the same DDL keywords the SQL uses: `REAL`
(not SQLAlchemy's default `Float`, which renders `FLOAT`), `DATE`, `DATETIME`.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    REAL,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from tracker.vocab import (
    BLOCK_STATUSES,
    EVENT_TYPES,
    PHASES,
    RISK_CATEGORIES,
    RISK_SEVERITIES,
    RISK_STATUSES,
    SOURCE_TYPES,
    URL_STATUSES,
    sql_in,
)

#: Server-side default matching the SQL, so the drift test sees identical
#: `dflt_value`. Application code sets timestamps explicitly (upsert.py needs
#: precise control so a no-op re-ingest leaves `updated_at` untouched); this is
#: the safety net for rows written by hand or by a migration.
_NOW = text("CURRENT_TIMESTAMP")


def utcnow() -> dt.datetime:
    """Naive UTC, matching SQLite's CURRENT_TIMESTAMP.

    Every timestamp in this schema is naive UTC. Mixing aware and naive values
    in SQLite produces silently unorderable columns, so there is exactly one
    place that produces "now".
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    company: Mapped[str] = mapped_column(Text, nullable=False)
    customer: Mapped[str | None] = mapped_column(Text)

    # city XOR-ish county: ISO queues report County, news reports a
    # municipality. See ck_project_locality.
    city: Mapped[str | None] = mapped_column(Text)
    county: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'US'"))
    lat: Mapped[float | None] = mapped_column(REAL)
    lon: Mapped[float | None] = mapped_column(REAL)

    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)

    mw_planned: Mapped[float | None] = mapped_column(REAL)
    mw_built: Mapped[float | None] = mapped_column(REAL)
    #: Capacity restated as accelerators. Derived from MW unless a source
    #: stated a chip count outright. See `tracker/compute.py`.
    h200_equivalent: Mapped[int | None] = mapped_column(Integer)
    investment_usd: Mapped[int | None] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'announced'"))
    first_announced: Mapped[dt.date | None] = mapped_column(Date)
    expected_online: Mapped[dt.date | None] = mapped_column(Date)
    #: How precisely the source stated each date — `day|month|quarter|half|year`,
    #: from `normalize.parse_date`, which has always computed it and which nothing
    #: outside that module has ever read. "Q3 2025" and "2025-07-01" are stored
    #: identically and mean very different things, and the row was rendering the
    #: second when the article said the first. Caches, recomputed on every upsert
    #: exactly like `confidence` and `h200_equivalent`.
    first_announced_precision: Mapped[str | None] = mapped_column(Text)
    expected_online_precision: Mapped[str | None] = mapped_column(Text)
    blocker: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    confidence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=_NOW)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=_NOW)
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    sources: Mapped[list[Source]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    events: Mapped[list[Event]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    risks: Mapped[list[Risk]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    #: Capacity blocks. A cache rebuilt wholesale from `source.blocks` on every
    #: upsert, so `delete-orphan` is correct here in a way it would not be for
    #: `risks`: a block's absence from the sources means the description changed,
    #: whereas a risk's absence from one article is not evidence it cleared.
    blocks: Mapped[list[CapacityBlock]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(sql_in("phase", PHASES), name="ck_project_phase"),
        CheckConstraint("confidence BETWEEN 0 AND 3", name="ck_project_confidence"),
        CheckConstraint("length(state) = 2 AND state = upper(state)", name="ck_project_state"),
        CheckConstraint(
            "length(country) = 2 AND country = upper(country)", name="ck_project_country"
        ),
        CheckConstraint("city IS NOT NULL OR county IS NOT NULL", name="ck_project_locality"),
        CheckConstraint("mw_planned IS NULL OR mw_planned >= 0", name="ck_project_mw_planned"),
        CheckConstraint("mw_built IS NULL OR mw_built >= 0", name="ck_project_mw_built"),
        CheckConstraint(
            "investment_usd IS NULL OR investment_usd >= 0", name="ck_project_investment"
        ),
        CheckConstraint("lat IS NULL OR lat BETWEEN -90 AND 90", name="ck_project_lat"),
        CheckConstraint("lon IS NULL OR lon BETWEEN -180 AND 180", name="ck_project_lon"),
        Index("uq_project_dedup_key", "dedup_key", unique=True),
        Index("ix_project_company", "company"),
        Index("ix_project_state", "state"),
        Index("ix_project_phase", "phase"),
        Index("ix_project_confidence", "confidence"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Project {self.id} {self.company}/{self.name} {self.state} c={self.confidence}>"


class Source(Base):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=_NOW)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt: Mapped[str | None] = mapped_column(Text)
    fields: Mapped[str | None] = mapped_column(Text)
    claims: Mapped[str | None] = mapped_column(Text)
    #: Values this source asserted with no quote the gate could verify. Kept
    #: separate from `fields` so `confidence`, the traceability test and the
    #: "9 of 12" definition of done keep counting only quote-backed facts, while
    #: the value itself survives to be shown as 待确认 rather than deleted.
    unconfirmed_fields: Mapped[str | None] = mapped_column(Text)
    #: JSON object, field -> why the gate refused it, from
    #: `vocab.UNCONFIRMED_REASONS`. Same keys as `unconfirmed_fields`; that column
    #: answers "is this confirmed" and is what the merge path reads, this one
    #: answers "why not" for the readers that need to tell a figure nobody quoted
    #: from one the `$/MW` ceiling demoted. NULL before migration 0013 and on any
    #: path with no gate behind it.
    unconfirmed_reasons: Mapped[str | None] = mapped_column(Text)
    #: JSON object, field -> the verbatim sentence the evidence gate verified for
    #: that value. Parallel to `claims`, which holds the values themselves. NULL on
    #: every row written before migration 0007 and on any source whose path has no
    #: per-field quotes to give (ISO queues, the Census lookup, manual seeds);
    #: `gaps.provenance` falls back to `excerpt` and says so.
    quotes: Mapped[str | None] = mapped_column(Text)
    #: JSON array of the capacity blocks this source described. Its own column and
    #: deliberately not a key inside `claims`: that map is flat field->scalar and
    #: six places iterate it assuming so, including a migration matching it with
    #: `claims LIKE '%"blocker"%'`.
    blocks: Mapped[str | None] = mapped_column(Text)
    extractor: Mapped[str | None] = mapped_column(Text)
    #: When the publisher published it, as against `fetched_at`, which is when the
    #: crawler visited. The merge tiebreak wants this one: two trade-press articles
    #: tie on credibility constantly, and breaking that tie on crawl order decided
    #: six stored values against publication order — Hyperion keeping Meta's
    #: superseded $10B among them. NULL for a URL nobody found in a feed, so the
    #: sort falls back to `fetched_at` rather than treating it as infinitely old.
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    #: JSON object, field -> {scope, bound, modality, as_of} — what the value is a
    #: value *of*, how exactly the article stated it, whether it has happened, and
    #: when it was true. Parallel to `quotes`, which holds the sentence; each axis
    #: is verified against that sentence by `crawl.axis_gate` and degrades to a
    #: neutral value rather than rejecting the figure. NULL on every source written
    #: before migration 0015, and not backfillable: the axes are facts about how an
    #: article was worded, recoverable only by re-reading it.
    claim_meta: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project] = relationship(back_populates="sources")

    __table_args__ = (
        UniqueConstraint("project_id", "url", name="uq_source_project_url"),
        CheckConstraint(sql_in("source_type", SOURCE_TYPES), name="ck_source_type"),
        CheckConstraint("excerpt IS NULL OR length(excerpt) <= 500", name="ck_source_excerpt_len"),
        Index("ix_source_project_id", "project_id"),
        Index("ix_source_type", "source_type"),
        Index("ix_source_published_at", "published_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Source {self.id} p={self.project_id} {self.source_type} {self.url[:48]}>"


class IngestUrl(Base):
    __tablename__ = "ingest_url"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    via: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    content_sha1: Mapped[str | None] = mapped_column(Text)

    # Discovery metadata: NULL for a URL supplied by hand via --urls. The title is
    # what makes `tracker queue` triageable — a bare URL is not enough to judge
    # whether an article is worth an LLM call.
    title: Mapped[str | None] = mapped_column(Text)
    feed: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    first_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, server_default=_NOW
    )
    last_tried_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, server_default=_NOW
    )

    __table_args__ = (
        UniqueConstraint("url", name="uq_ingest_url_url"),
        CheckConstraint(sql_in("status", URL_STATUSES), name="ck_ingest_url_status"),
        CheckConstraint("attempts >= 0", name="ck_ingest_url_attempts"),
        Index("ix_ingest_url_status", "status"),
        Index("ix_ingest_url_published_at", "published_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IngestUrl {self.status} {self.url[:60]}>"


class Event(Base):
    __tablename__ = "event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    event_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    #: The verbatim sentence the milestone stands on, verified against the fetched
    #: article exactly like `Risk.quote`. NULL plus a NULL `unconfirmed` would mean
    #: "confirmed with no sentence", which is why the 0017 backfill marks every
    #: pre-gate row `no_quote` instead of leaving it ambiguous.
    quote: Mapped[str | None] = mapped_column(Text)
    #: Why the gate did not confirm this milestone, from
    #: `vocab.UNCONFIRMED_REASONS`. NULL means it did — a claim that is only true
    #: for rows written after migration 0017, which is what the backfill encodes.
    unconfirmed: Mapped[str | None] = mapped_column(Text)
    #: When this row entered OUR database, as against `event_date`, which is when
    #: the milestone happened. The briefing's "new since I last looked" reads
    #: this; nothing else can answer that question, because a crawl of one
    #: article imports a project's whole back-history at once (migration 0018).
    #:
    #: Nullable with no server default because SQLite's ALTER TABLE refuses a
    #: CURRENT_TIMESTAMP default, so `upsert` sets it. NULL means "we do not know
    #: when we learned this", which the feed treats as undated rather than new.
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    source_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("source.id", ondelete="SET NULL")
    )

    project: Mapped[Project] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint(
            "project_id", "event_type", "event_date", name="uq_event_project_type_date"
        ),
        CheckConstraint(sql_in("event_type", EVENT_TYPES), name="ck_event_type"),
        CheckConstraint("quote IS NULL OR length(quote) <= 500", name="ck_event_quote_len"),
        Index("ix_event_project_id", "project_id"),
        Index("ix_event_date", "event_date"),
        Index("ix_event_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Event {self.event_type}@{self.event_date} p={self.project_id}>"


class Risk(Base):
    """One obstacle to one project, typed and cited.

    Replaces the single free-text `project.blocker`, which could hold only one
    obstacle, could never be cleared once set, and could not be aggregated. That
    column survives as a value derived from these rows — see
    `upsert._derive_blocker`.
    """

    __tablename__ = "risk"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))

    #: Allowed to be a paraphrase; `quote` beside it is the evidence.
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    quote: Mapped[str | None] = mapped_column(Text)

    #: Why the evidence gate did not confirm this obstacle, from
    #: `vocab.UNCONFIRMED_REASONS`. NULL means it did — which is every row that
    #: predates migration 0012, since the old gate deleted whatever it refused.
    unconfirmed: Mapped[str | None] = mapped_column(Text)

    first_seen: Mapped[dt.date | None] = mapped_column(Date)
    resolved_at: Mapped[dt.date | None] = mapped_column(Date)
    delay_days: Mapped[int | None] = mapped_column(Integer)

    #: When we learned of this obstacle, as against `first_seen`, which is the
    #: date the *source* puts on it. See `Event.created_at`; same column, same
    #: reason, same migration.
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    source_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("source.id", ondelete="SET NULL")
    )

    project: Mapped[Project] = relationship(back_populates="risks")

    __table_args__ = (
        UniqueConstraint(
            "project_id", "category", "first_seen", name="uq_risk_project_category_seen"
        ),
        CheckConstraint(sql_in("category", RISK_CATEGORIES), name="ck_risk_category"),
        CheckConstraint(sql_in("severity", RISK_SEVERITIES), name="ck_risk_severity"),
        CheckConstraint(sql_in("status", RISK_STATUSES), name="ck_risk_status"),
        CheckConstraint("quote IS NULL OR length(quote) <= 500", name="ck_risk_quote_len"),
        CheckConstraint("delay_days IS NULL OR delay_days >= 0", name="ck_risk_delay_days"),
        CheckConstraint("resolved_at IS NULL OR status <> 'open'", name="ck_risk_resolved_at"),
        Index("ix_risk_project_id", "project_id"),
        Index("ix_risk_category", "category"),
        Index("ix_risk_status", "status"),
        Index("ix_risk_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Risk {self.category}/{self.severity} {self.status} p={self.project_id}>"


class CapacityBlock(Base):
    """One tranche of a campus, with its own state, customer and dates.

    The row `project` could not be. A modern AI campus is several states at once —
    150 MW energised and serving one buyer, 150 MW under construction pre-leased to
    another, 300 MW planned with nobody named — and a single `phase` enum, a single
    `mw_planned` and a single `customer` cannot express that.

    **A cache, not a fact of record.** Rebuilt wholesale from `source.blocks` on
    every upsert, exactly like `confidence` and `h200_equivalent`, which is what
    makes re-ingest idempotent and gives `merge` and `recompute_from_sources` the
    right behaviour without a second implementation.

    See `tracker/blocks.py` for how `block_key` is derived and why `parent` and
    `generic` exist.
    """

    __tablename__ = "capacity_block"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )

    #: Derived by `blocks.block_key`. Identity rests here, never on `label`, so a
    #: source rewording its phase name cannot re-key the block.
    block_key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    #: The facility a generic label belongs to. What lets an article's "Phase 3"
    #: meet a filing's "AZP-3 Phase 3".
    parent: Mapped[str | None] = mapped_column(Text)
    #: True when the label names a kind of thing rather than which campus.
    generic: Mapped[bool] = mapped_column(Integer, nullable=False, server_default=text("0"))

    mw: Mapped[float | None] = mapped_column(REAL)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'planned'"))
    customer: Mapped[str | None] = mapped_column(Text)
    expected_online: Mapped[dt.date | None] = mapped_column(Date)
    energized_on: Mapped[dt.date | None] = mapped_column(Date)
    investment_usd: Mapped[int | None] = mapped_column(Integer)

    #: JSON object, block field -> the verbatim sentence the gate verified. Per
    #: field, because project 39's failure was money from one facility sitting
    #: beside capacity from another.
    quotes: Mapped[str | None] = mapped_column(Text)
    unconfirmed_fields: Mapped[str | None] = mapped_column(Text)

    source_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("source.id", ondelete="SET NULL")
    )

    project: Mapped[Project] = relationship(back_populates="blocks")

    __table_args__ = (
        UniqueConstraint("project_id", "block_key", name="uq_capacity_block_project_key"),
        CheckConstraint(sql_in("status", BLOCK_STATUSES), name="ck_capacity_block_status"),
        CheckConstraint("mw IS NULL OR mw >= 0", name="ck_capacity_block_mw"),
        CheckConstraint(
            "investment_usd IS NULL OR investment_usd >= 0", name="ck_capacity_block_investment"
        ),
        CheckConstraint("generic IN (0, 1)", name="ck_capacity_block_generic"),
        CheckConstraint("length(label) > 0", name="ck_capacity_block_label"),
        Index("ix_capacity_block_project_id", "project_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CapacityBlock {self.block_key} {self.mw}MW {self.status} p={self.project_id}>"


class BlockAlias(Base):
    """An operator's statement that two block keys are one block.

    Has to be a table rather than a note. No string function can know that one
    source's "Phase 1" and another's "AZP-2" are the same tranche, and blocks are
    rebuilt wholesale on every upsert — so a hand-merge with nowhere durable to
    live would evaporate on the next crawl.
    """

    __tablename__ = "block_alias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    from_key: Mapped[str] = mapped_column(Text, nullable=False)
    to_key: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'operator'"))
    decided_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=_NOW)

    __table_args__ = (
        UniqueConstraint("project_id", "from_key", name="uq_block_alias_project_from"),
        CheckConstraint("from_key <> to_key", name="ck_block_alias_not_self"),
        Index("ix_block_alias_project_id", "project_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BlockAlias {self.from_key}->{self.to_key} p={self.project_id}>"


class ProjectAlias(Base):
    """A merged-away identity, and the row it now belongs to.

    `upsert_record` matches on exact `dedup_key` only, so without this a merge
    lasts exactly until the next crawl: an article written from the folded
    company's angle re-creates the folded key as a fresh row. `merge` writes one
    of these per folded identity; the upsert consults it after an exact-key miss.

    Global, unlike `block_alias` — a dedup key is already unique table-wide. The
    target is an id, not another key, and `merge` repoints aliases when their
    target is itself folded, so chains stay flat and resolution is one lookup.
    """

    __tablename__ = "project_alias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    to_project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    decided_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'operator'"))
    decided_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=_NOW)

    __table_args__ = (
        UniqueConstraint("from_dedup_key", name="uq_project_alias_from"),
        CheckConstraint("length(from_dedup_key) > 0", name="ck_project_alias_from"),
        Index("ix_project_alias_to_project_id", "to_project_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ProjectAlias {self.from_dedup_key}->#{self.to_project_id}>"


class NotDuplicate(Base):
    """Two rows somebody has looked at and ruled are different sites.

    The counterpart to `ProjectAlias`, which records a yes. `tracker duplicates`
    proposes pairs and never merges, so without somewhere to put a *no* a false
    pair returns on every run — and, because `capex.rollup` reads the same pairs
    and sets one row of each group aside, quietly keeps a real campus out of the
    buyer table.

    Pairwise on purpose: a group is a transitive closure computed at read time,
    and "1, 2 and 3 are distinct" says nothing about a fourth row that pairs with
    2 next week. `a_id < b_id` is a CHECK, so a pair has one spelling and UNIQUE
    means what it says; `pairs.park` orders the ids for its callers.
    """

    __tablename__ = "not_duplicate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    a_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    b_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    #: "operator" or "model (0.82)". A model may park a pair; a reader must be
    #: able to tell that one did.
    decided_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'operator'"))
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=_NOW)

    __table_args__ = (
        UniqueConstraint("a_id", "b_id", name="uq_not_duplicate_pair"),
        CheckConstraint("a_id < b_id", name="ck_not_duplicate_order"),
        Index("ix_not_duplicate_a_id", "a_id"),
        Index("ix_not_duplicate_b_id", "b_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<NotDuplicate #{self.a_id} != #{self.b_id}>"


class Account(Base):
    """One person who may sign in to the console.

    The identity is an email, stored twice: `email` as typed for display,
    `email_key` trimmed and lowercased for every lookup and for UNIQUE. Same
    reasoning as `watch.entry`/`company_key` — a normalized key cannot be shown
    and text as typed cannot be matched.

    **Not a role table.** Every account can do the same things, because what the
    console may do at all is a property of the server (`--ai`, `--watch-edits`)
    rather than of whoever signed in. Migration 0020 has the argument.
    """

    __tablename__ = "account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: As typed. Display only.
    email: Mapped[str] = mapped_column(Text, nullable=False)

    #: Trimmed, lowercased. The identity, and the only thing looked up.
    email_key: Mapped[str] = mapped_column(Text, nullable=False)

    #: Optional display name; absent is normal.
    name: Mapped[str | None] = mapped_column(Text)

    #: `scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`, from `accounts.hash_password`.
    #: Self-describing so the cost parameters can be raised without a migration.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=_NOW)

    #: When they last signed in. Distinct from `created_at` for the reason
    #: `project.last_verified_at` is distinct from `updated_at`.
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    #: Read the whole database rather than only what `watches` names.
    #:
    #: **Off by default, and that is the whole point of migration 0022.** An empty
    #: watchlist used to mean "everything", so two people who had asked for nothing
    #: saw identical pages and neither had chosen it. Wanting all 456 projects is a
    #: legitimate thing to want; it is now a thing somebody turns on.
    watch_all: Mapped[bool] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )

    watches: Mapped[list[Watch]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("email_key", name="uq_account_email_key"),
        CheckConstraint(
            "email_key = lower(email_key) AND email_key LIKE '_%@_%'",
            name="ck_account_email_key",
        ),
        CheckConstraint("length(email) > 0", name="ck_account_email"),
        CheckConstraint("length(password_hash) > 0", name="ck_account_password_hash"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Account {self.email!r}>"


class Invite(Base):
    """One single-use code that lets somebody create their own account.

    The code itself is never stored — only its sha256 — because this database
    travels between machines through `scripts/sync_db.py` and sits in backups,
    where a plaintext code would be a live credential in every copy.

    `redeemed_at` alone says whether it is spent. `redeemed_by` is an audit link
    and goes NULL if that account is deleted, so the two are deliberately not
    constrained to agree.
    """

    __tablename__ = "invite"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: sha256 of the code, hex. Not scrypt: a 160-bit random code has no
    #: guessable keyspace for a work factor to slow anyone down in.
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)

    #: Who it was minted for, free text, so an outstanding invite is still
    #: recognisable a fortnight later.
    note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=_NOW)

    #: Not nullable: an invite that never expires is a permanent hole.
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    redeemed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    redeemed_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("account.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("code_hash", name="uq_invite_code_hash"),
        CheckConstraint("length(code_hash) > 0", name="ck_invite_code_hash"),
        Index("ix_invite_redeemed_at", "redeemed_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Invite {self.note!r} redeemed={self.redeemed_at is not None}>"


class Watch(Base):
    """One entity whose news the briefing page is about, for one account.

    A company ("xAI"), or one project of one company ("xAI | Colossus"). Read by
    `tracker.watchlist`, which resolves each row to the projects it covers with
    the same normalization `dedup` and `required.match` use.

    **Data rather than a seed file**, unlike `seed/required-projects.txt`. That
    file encodes the PRD's definition of done, so it belongs in a diff; a
    watchlist is one reader's current interest, turns over monthly, and is edited
    from the console by the person reading it. Migration 0019 has the argument.

    **It has an owner**, added by 0021, which is that argument taken one step
    further: with several accounts on one console, a shared list shows everybody
    everybody else's interests and lets any of them delete yours.
    """

    __tablename__ = "watch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: Whose list this is. Deleting the account deletes the list with it — it is
    #: a statement of that person's interest and means nothing without them.
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )

    #: As typed, separator included: "xAI" or "xAI | Colossus". What gets shown
    #: back to whoever wrote it — a normalized key cannot be displayed.
    entry: Mapped[str] = mapped_column(Text, nullable=False)

    #: `dedup.company_key()` of the company part, so "Microsoft Corporation" and
    #: "Microsoft" are one watch. Never empty; the CHECK enforces it.
    company_key: Mapped[str] = mapped_column(Text, nullable=False)

    #: Lowercased project name, or '' for every project of the company. Empty
    #: string rather than NULL so UNIQUE actually refuses a duplicate — SQLite
    #: treats NULLs as distinct.
    project_key: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))

    note: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=_NOW)

    account: Mapped[Account] = relationship(back_populates="watches")

    __table_args__ = (
        UniqueConstraint("account_id", "company_key", "project_key", name="uq_watch_entity"),
        CheckConstraint("length(company_key) > 0", name="ck_watch_company_key"),
        Index("ix_watch_company_key", "company_key"),
        Index("ix_watch_account_id", "account_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Watch {self.entry!r} of account {self.account_id}>"
