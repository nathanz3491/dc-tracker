"""Hand-curated JSON ingest.

For projects we know about but no feed carries — the PRD's example is xAI's
Colossus, which existed long before it appeared in any queue.

Two choices worth defending:

* **``extra="forbid"``.** A hand-typed file *will* eventually contain
  ``mw_plannned``. Silently dropping an unknown key is worse than failing: the
  operator would believe they had recorded a capacity they had not.
* **``--strict`` is the default.** The whole file validates before anything is
  written. A curated file is a single considered document, so partial application
  leaves the database in a state nobody intended.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.orm import Session

from tracker.ingest.records import (
    EventRecord,
    IngestRecord,
    IngestReport,
    RiskRecord,
    SourceRecord,
)
from tracker.models import utcnow
from tracker.normalize import (
    NormalizationError,
    norm_date,
    norm_date_detail,
    norm_excerpt,
    norm_money_detail,
    norm_mw_detail,
    norm_phase,
    norm_state,
    norm_text,
    norm_url,
)
from tracker.upsert import upsert_record
from tracker.vocab import (
    DEFAULT_PHASE,
    DEFAULT_RISK_SEVERITY,
    EVENT_TYPES,
    PHASES,
    RISK_CATEGORIES,
    RISK_SEVERITIES,
    SOURCE_TYPES,
    severity_rank,
)

log = logging.getLogger(__name__)

#: Literal appearing in a seed file that has not been filled in yet. Rejected by
#: default so the sample file cannot quietly become "data".
PLACEHOLDER = "PLACEHOLDER"


class ManualError(ValueError):
    """The seed file is malformed. Message is operator-facing."""


class ManualSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    source_type: str = "manual"
    excerpt: str | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        try:
            return norm_url(v) or ""
        except NormalizationError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("source_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {', '.join(SOURCE_TYPES)}")
        return v


class ManualEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_date: str
    event_type: str
    description: str
    source_url: str

    @field_validator("event_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in EVENT_TYPES:
            raise ValueError(f"event_type must be one of {', '.join(EVENT_TYPES)}")
        return v

    @field_validator("event_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        try:
            if norm_date(v, field="event_date") is None:
                raise ValueError("event_date must be a resolvable date, not vague language")
        except NormalizationError as exc:
            raise ValueError(str(exc)) from exc
        return v


class ManualRisk(BaseModel):
    """One curated obstacle. Field names match `risk` columns exactly."""

    model_config = ConfigDict(extra="forbid")

    category: str
    summary: str
    severity: str = DEFAULT_RISK_SEVERITY
    quote: str | None = None
    first_seen: str | None = None
    delay_days: int | None = None
    source_url: str | None = None

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: str) -> str:
        if v not in RISK_CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(RISK_CATEGORIES)}")
        return v

    @field_validator("severity")
    @classmethod
    def _check_severity(cls, v: str) -> str:
        if v not in RISK_SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(RISK_SEVERITIES)}")
        return v

    @field_validator("first_seen")
    @classmethod
    def _check_first_seen(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            if norm_date(v, field="first_seen") is None:
                raise ValueError("first_seen must be a resolvable date, not vague language")
        except NormalizationError as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("delay_days")
    @classmethod
    def _check_delay(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("delay_days cannot be negative")
        return v


class ManualProject(BaseModel):
    """One curated project. Field names match `project` columns exactly."""

    model_config = ConfigDict(extra="forbid")

    name: str
    company: str
    state: str
    city: str | None = None
    county: str | None = None
    customer: str | None = None
    country: str = "US"
    lat: float | None = None
    lon: float | None = None
    mw_planned: float | str | None = None
    mw_built: float | str | None = None
    investment_usd: int | str | None = None
    phase: str = DEFAULT_PHASE
    first_announced: str | None = None
    expected_online: str | None = None
    #: Accepted for backward compatibility with curated files written before the
    #: `risk` table existed. Becomes one `unclassified` risk cited to this record's
    #: first source — the same thing migration 0004 did to the stored column. Prefer
    #: `risks` below, which can say which kind of obstacle it is.
    blocker: str | None = None
    notes: str | None = None
    sources: list[ManualSource] = Field(min_length=1)
    events: list[ManualEvent] = Field(default_factory=list)
    risks: list[ManualRisk] = Field(default_factory=list)

    @field_validator("state")
    @classmethod
    def _check_state(cls, v: str) -> str:
        try:
            code = norm_state(v)
        except NormalizationError as exc:
            raise ValueError(str(exc)) from exc
        if not code:
            raise ValueError("state is required")
        return code

    @field_validator("phase")
    @classmethod
    def _check_phase(cls, v: str) -> str:
        if v in PHASES:
            return v
        try:
            resolved = norm_phase(v)
        except NormalizationError as exc:
            raise ValueError(str(exc)) from exc
        return resolved or DEFAULT_PHASE

    @model_validator(mode="after")
    def _needs_a_locality(self) -> ManualProject:
        if not (self.city or self.county):
            raise ValueError("at least one of city or county is required")
        return self

    @model_validator(mode="after")
    def _events_cite_own_sources(self) -> ManualProject:
        """An event must cite a URL this record actually lists.

        A dangling reference means the operator mistyped one of the two, and
        silently storing the event with source_id NULL would hide that.
        """
        urls = {s.url for s in self.sources}
        dangling = [e.source_url for e in self.events if e.source_url not in urls]
        dangling += [r.source_url for r in self.risks if r.source_url and r.source_url not in urls]
        if dangling:
            raise ValueError(f"source_url not listed in this record's sources: {dangling}")
        return self

    @model_validator(mode="after")
    def _one_risk_per_category_and_date(self) -> ManualProject:
        """The stored UNIQUE is (project, category, first_seen), so two entries
        agreeing on both would fail on insert. Say so here, where the operator can
        see which two lines collide."""
        keys = [(r.category, r.first_seen) for r in self.risks]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise ValueError(f"two risks share a category and first_seen: {duplicates}")
        return self


class ManualFile(BaseModel):
    """The seed file envelope."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    defaults: dict[str, Any] = Field(default_factory=dict)
    projects: list[ManualProject]
    #: Free-text warning the sample file uses to shout about placeholders.
    warning: str | None = Field(default=None, alias="_warning")


def _contains_placeholder(model: ManualProject) -> bool:
    blob = model.model_dump_json()
    return PLACEHOLDER in blob


def load(path: Path, *, allow_placeholders: bool = False) -> tuple[ManualFile, str]:
    """Parse and validate a seed file. Returns (parsed, content sha1).

    Raises :class:`ManualError` with every problem found, not just the first —
    an operator fixing a hand-written file wants the whole list.
    """
    raw_bytes = path.read_bytes()
    digest = hashlib.sha1(raw_bytes, usedforsecurity=False).hexdigest()[:8]
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManualError(f"{path.name} is not valid UTF-8 JSON: {exc}") from exc

    # The PRD describes "a list of hand-curated project records"; the envelope
    # adds versioning and defaults. Accept either shape.
    if isinstance(data, list):
        data = {"projects": data}
    if not isinstance(data, dict):
        raise ManualError(f"{path.name}: top level must be an object or a list of projects")

    try:
        parsed = ManualFile.model_validate(data)
    except ValidationError as exc:
        raise ManualError(_format_errors(path, exc)) from exc

    if not allow_placeholders:
        offenders = [
            f"  record {i} ({p.name!r})"
            for i, p in enumerate(parsed.projects)
            if _contains_placeholder(p)
        ]
        if offenders:
            raise ManualError(
                f"{path.name} still contains {PLACEHOLDER} values in:\n"
                + "\n".join(offenders)
                + "\n\nReplace them with values you have verified against the cited source, "
                "or pass --allow-placeholders to ingest the file as-is for a smoke test."
            )
    return parsed, digest


def _format_errors(path: Path, exc: ValidationError) -> str:
    lines = [f"{path.name}: {exc.error_count()} validation problem(s)"]
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"])
        lines.append(f"  {where}: {err['msg']}")
    return "\n".join(lines)


def to_records(parsed: ManualFile, *, digest: str, source_name: str) -> list[IngestRecord]:
    """Turn validated seed records into the shared IngestRecord shape."""
    fetched_at = utcnow()
    defaults = parsed.defaults or {}
    default_type = defaults.get("source_type", "manual")
    extractor = f"manual:{source_name}@{digest}"

    records: list[IngestRecord] = []
    for model in parsed.projects:
        project, notes = _project_payload(model, defaults)
        claims = {k: v for k, v in project.items() if k != "notes" and v is not None}

        risks = _risk_records(model, default_url=model.sources[0].url)
        if risks:
            # Same bookkeeping as the crawl path: record which citation supports the
            # derived `blocker`, so `source.fields` stays honest about it. The value
            # is written and never read — `upsert` derives the column from the risk
            # rows themselves.
            claims["blocker"] = max(risks, key=lambda r: severity_rank(r.severity)).summary

        sources = [
            SourceRecord(
                url=s.url,
                source_type=s.source_type or default_type,
                fetched_at=fetched_at,
                excerpt=norm_excerpt(s.excerpt),
                # Every source on a manual record vouches for the whole record:
                # the operator curated it as one statement, and splitting the
                # claims per-URL would be inventing attribution they never gave.
                claims=claims,
                extractor=extractor,
            )
            for s in model.sources
        ]
        events = [
            EventRecord(
                event_date=norm_date(e.event_date, field="event_date"),
                event_type=e.event_type,
                description=e.description,
                source_url=e.source_url,
            )
            for e in model.events
        ]
        records.append(
            IngestRecord(project=project, sources=sources, events=events, risks=risks, notes=notes)
        )
    return records


def _risk_records(model: ManualProject, *, default_url: str) -> list[RiskRecord]:
    """Curated obstacles, plus the legacy `blocker` string if one is present."""
    out = [
        RiskRecord(
            category=r.category,
            severity=r.severity,
            summary=r.summary,
            quote=norm_excerpt(r.quote),
            first_seen=norm_date(r.first_seen, field="first_seen") if r.first_seen else None,
            delay_days=r.delay_days,
            source_url=r.source_url or default_url,
        )
        for r in model.risks
    ]
    blocker = norm_text(model.blocker)
    if blocker and not any(r.category == "unclassified" and r.first_seen is None for r in out):
        # `unclassified` because a bare sentence does not say which kind it is, and
        # guessing from keywords would put an uncheckable inference into a field an
        # operator acts on. `material` because a human bothered to type it.
        out.append(
            RiskRecord(
                category="unclassified",
                severity="material",
                summary=blocker,
                source_url=default_url,
            )
        )
    return out


def _project_payload(
    model: ManualProject, defaults: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Normalize one curated record into column values plus disclosure notes."""
    notes: list[str] = []

    def _num(raw: Any, fn, field: str) -> Any:
        if raw is None:
            return None
        detail = fn(raw, field=field)
        if detail.note:
            notes.append(detail.note)
        return detail.value

    payload: dict[str, Any] = {
        "name": norm_text(model.name),
        "company": norm_text(model.company),
        "customer": norm_text(model.customer),
        "city": norm_text(model.city),
        "county": norm_text(model.county),
        "state": model.state,
        "country": (model.country or defaults.get("country") or "US").upper(),
        "lat": model.lat,
        "lon": model.lon,
        "mw_planned": _num(model.mw_planned, norm_mw_detail, "mw_planned"),
        "mw_built": _num(model.mw_built, norm_mw_detail, "mw_built"),
        "investment_usd": None,
        "phase": model.phase,
        # `blocker` is deliberately absent: the column is derived from the `risk`
        # rows, and a curated `blocker` string becomes one of those in
        # `_risk_records`.
        "notes": norm_text(model.notes),
    }
    if model.investment_usd is not None:
        detail = norm_money_detail(model.investment_usd, field="investment_usd")
        if detail.note:
            notes.append(detail.note)
        payload["investment_usd"] = None if detail.value is None else int(detail.value)

    for field in ("first_announced", "expected_online"):
        raw = getattr(model, field)
        if raw is None:
            payload[field] = None
            continue
        detail = norm_date_detail(raw, field=field)
        if detail.note:
            notes.append(detail.note)
        payload[field] = detail.value

    return {k: v for k, v in payload.items() if v is not None}, notes


def run(
    session: Session,
    path: Path,
    *,
    allow_placeholders: bool = False,
    strict: bool = True,
    force_new: bool = False,
) -> IngestReport:
    """Ingest a seed file. Whole file in one transaction.

    Args:
        strict: when True (the default) a single invalid record aborts before any
            write. When False, invalid records are skipped and counted.
    """
    report = IngestReport()
    try:
        parsed, digest = load(path, allow_placeholders=allow_placeholders)
    except ManualError:
        if strict:
            raise
        log.error("seed file rejected: %s", path)
        report.rejected += 1
        return report

    records = to_records(parsed, digest=digest, source_name=path.name)
    report.read = len(records)

    for rec in records:
        try:
            result = upsert_record(session, rec, force_new=force_new)
        except Exception:
            if strict:
                raise
            log.exception("record %r failed to upsert", rec.project.get("name"))
            report.rejected += 1
            continue
        report.bump(result.action)
        report.events += result.events_written
        report.risks += result.risks_written
        if result.duplicate_of is not None:
            report.duplicates_flagged += 1
        report.conflicts += len(result.conflicts)
    return report


__all__ = [
    "PLACEHOLDER",
    "ManualError",
    "ManualEvent",
    "ManualFile",
    "ManualProject",
    "ManualRisk",
    "ManualSource",
    "load",
    "run",
    "to_records",
]
