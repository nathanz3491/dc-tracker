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
    investment_usd: Mapped[int | None] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'announced'"))
    first_announced: Mapped[dt.date | None] = mapped_column(Date)
    expected_online: Mapped[dt.date | None] = mapped_column(Date)
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
    #: JSON object, field -> the verbatim sentence the evidence gate verified for
    #: that value. Parallel to `claims`, which holds the values themselves. NULL on
    #: every row written before migration 0007 and on any source whose path has no
    #: per-field quotes to give (ISO queues, the Census lookup, manual seeds);
    #: `gaps.provenance` falls back to `excerpt` and says so.
    quotes: Mapped[str | None] = mapped_column(Text)
    extractor: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project] = relationship(back_populates="sources")

    __table_args__ = (
        UniqueConstraint("project_id", "url", name="uq_source_project_url"),
        CheckConstraint(sql_in("source_type", SOURCE_TYPES), name="ck_source_type"),
        CheckConstraint("excerpt IS NULL OR length(excerpt) <= 500", name="ck_source_excerpt_len"),
        Index("ix_source_project_id", "project_id"),
        Index("ix_source_type", "source_type"),
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
    source_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("source.id", ondelete="SET NULL")
    )

    project: Mapped[Project] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint(
            "project_id", "event_type", "event_date", name="uq_event_project_type_date"
        ),
        CheckConstraint(sql_in("event_type", EVENT_TYPES), name="ck_event_type"),
        Index("ix_event_project_id", "project_id"),
        Index("ix_event_date", "event_date"),
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

    first_seen: Mapped[dt.date | None] = mapped_column(Date)
    resolved_at: Mapped[dt.date | None] = mapped_column(Date)
    delay_days: Mapped[int | None] = mapped_column(Integer)

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
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Risk {self.category}/{self.severity} {self.status} p={self.project_id}>"
