"""One table of every statement every source makes: the master data the client asked for.

Step 4.5 of the client plan (2026-10-21, A4: "every channel in one table, the
relationships defined, the data classified"). The statements already existed — as JSON
on each citation (`claims`, `quotes`, `claim_meta`, `blocks`, `parties`) and as the
`event` and `risk` rows — in five shapes. `fact` puts them in one, with the same
columns whatever the statement is about, so "every figure anyone has given for
Abilene's capacity, how far each can be trusted, and which the page shows" is one
query, and a reader can download the lot (`tracker export facts`, `/v1/projects/{id}/
facts`).

**A cache, never an input.** Rebuilt wholesale from the sources whenever a row is
written (`upsert`, `recompute_from_sources`) and by `tracker init`, like the tranches.
Nothing reads it to decide a value; the merge engine still decides from the sources,
and this records what it had to decide between.

**Tiers**, per statement, most trusted first:

* ``confirmed`` — quoted, first-hand;
* ``directory`` — quoted, but from a directory or digest that aggregates others;
* ``derived`` — computed from a public dataset (the Census place lookup), not stated;
* ``inferred`` — a role nobody stated, read off the company or customer column;
* ``unconfirmed`` — 待确认: stated, but no sentence the gate could verify;
* ``ruled_out`` — decided against by a person or a ruling (superseded, a misread).

``held`` marks the statements whose value is the one the row holds.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from typing import Any, Final

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from tracker.models import Fact, Project

#: The unit a campus field is in.
UNITS: Final[dict[str, str]] = {
    "mw_planned": "MW",
    "mw_built": "MW",
    "investment_usd": "USD",
    "h200_equivalent": "H200",
    "first_announced": "date",
    "expected_online": "date",
    "lat": "deg",
    "lon": "deg",
}
#: The unit a tranche field is in.
TRANCHE_UNITS: Final[dict[str, str]] = {
    "mw": "MW",
    "investment_usd": "USD",
    "expected_online": "date",
    "energized_on": "date",
}
#: Tranche fields recorded as statements. `label` and `parent` name the tranche.
TRANCHE_FIELDS: Final[tuple[str, ...]] = (
    "mw",
    "status",
    "customer",
    "expected_online",
    "energized_on",
    "investment_usd",
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _load(raw: str | None, default: Any) -> Any:
    try:
        got = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return got if isinstance(got, type(default)) else default


def _source_tier(source: Any, *, confirmed: bool, reason: str | None) -> str:
    from tracker import confidence
    from tracker.upsert import DECIDED_REASONS

    if reason in DECIDED_REASONS:
        return "ruled_out"
    if (getattr(source, "extractor", None) or "").startswith("derived:"):
        return "derived"
    if not confirmed:
        return "unconfirmed"
    if confidence.is_tertiary(source):
        return "directory"
    return "confirmed"


def _as_of(source: Any, axes: dict[str, Any]) -> dt.date | None:
    """When a statement was true: the date its sentence gives, else when published."""
    return (
        _date(axes.get("as_of"))
        or _date(getattr(source, "published_at", None))
        or _date(getattr(source, "fetched_at", None))
    )


def _held(stored: Any, value: Any) -> bool:
    """Whether a statement's value is the one the row holds — exactly, not nearly.

    Not `confidence.values_conflict`, whose tolerance is for deciding whether two
    sources disagree: under it 1,000 MW and 1,200 MW are one figure, and a reader
    asking which statement the page shows needs the one it shows.
    """
    if stored is None or value is None:
        return False
    if isinstance(stored, dt.date):
        return stored == _date(value)
    if isinstance(stored, (int, float)) and not isinstance(stored, bool):
        number = _number(value)
        return number is not None and abs(float(stored) - number) < 0.005
    return str(stored).strip().casefold() == str(value).strip().casefold()


def statements(
    project: Project,
    *,
    events: Iterable[Any] | None = None,
    risks: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    """Every statement about one campus, as `fact` rows. Never writes.

    `events` and `risks` default to the campus's own; `rebuild` passes plain rows
    instead, so writing the cache never loads them as objects mid-write.
    """
    from tracker import blocks as blocks_mod
    from tracker.upsert import is_placeholder
    from tracker.vocab import WRITABLE_FIELDS

    rows: list[dict[str, Any]] = []
    sources = list(getattr(project, "sources", ()) or ())
    tranches = {b.block_key: b for b in getattr(project, "blocks", ()) or ()}
    current = {
        (p.party_key, p.role)
        for p in getattr(project, "parties", ()) or ()
        if getattr(p, "ended_reason", None) is None
    }

    for source in sources:
        claims = _load(source.claims, {})
        quotes = _load(source.quotes, {})
        meta = _load(source.claim_meta, {})
        reasons = _load(source.unconfirmed_reasons, {})
        unconfirmed = {f.strip() for f in (source.unconfirmed_fields or "").split(",") if f.strip()}
        placeholder = is_placeholder(source)

        for field in WRITABLE_FIELDS:
            if field in ("notes",) or claims.get(field) is None:
                continue
            value = claims[field]
            axes = meta.get(field) if isinstance(meta.get(field), dict) else {}
            reason = reasons.get(field) if isinstance(reasons, dict) else None
            confirmed = not placeholder and field not in unconfirmed
            rows.append(
                {
                    "entity": "campus",
                    "entity_key": "",
                    "field": field,
                    "value": _text(value),
                    "number": _number(value),
                    "unit": UNITS.get(field),
                    "source_id": source.id,
                    "as_of": _as_of(source, axes),
                    "tier": _source_tier(source, confirmed=confirmed, reason=reason),
                    "reason": reason,
                    "quote": quotes.get(field) if isinstance(quotes, dict) else None,
                    "axes": json.dumps(axes, sort_keys=True) if axes else None,
                    "held": _held(getattr(project, field, None), value),
                }
            )

        ruled_out = blocks_mod.ruled_out(source)
        for entry in _load(source.blocks, []):
            if not isinstance(entry, dict) or not str(entry.get("label") or "").strip():
                continue
            key = blocks_mod.block_key(entry["label"], entry.get("parent")).value
            tranche = tranches.get(key)
            missing = set(entry.get("unconfirmed") or ())
            entry_quotes = entry.get("quotes") if isinstance(entry.get("quotes"), dict) else {}
            for field in TRANCHE_FIELDS:
                value = entry.get(field)
                if value is None or value == "":
                    continue
                if ruled_out:
                    tier, reason = "ruled_out", "ruled out of this campus"
                else:
                    tier = _source_tier(
                        source, confirmed=not placeholder and field not in missing, reason=None
                    )
                    reason = "no_quote" if field in missing else None
                rows.append(
                    {
                        "entity": "tranche",
                        "entity_key": key,
                        "field": field,
                        "value": _text(value),
                        "number": _number(value),
                        "unit": TRANCHE_UNITS.get(field),
                        "source_id": source.id,
                        "as_of": _as_of(source, {}),
                        "tier": tier,
                        "reason": reason,
                        "quote": entry_quotes.get(field),
                        "axes": None,
                        "held": tranche is not None and _held(getattr(tranche, field, None), value),
                    }
                )

        for entry in _load(source.parties, []):
            if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
                continue
            from tracker.dedup import company_key

            key = company_key(entry["name"])
            role = str(entry.get("role") or "")
            if not key or not role:
                continue
            gate = entry.get("unconfirmed")
            rows.append(
                {
                    "entity": "party",
                    "entity_key": key,
                    "field": role,
                    "value": str(entry["name"]).strip(),
                    "number": None,
                    "unit": None,
                    "source_id": source.id,
                    "as_of": _as_of(source, {}),
                    "tier": "inferred"
                    if gate == "role_inferred"
                    else _source_tier(
                        source, confirmed=gate is None and not placeholder, reason=None
                    ),
                    "reason": gate,
                    "quote": entry.get("quote"),
                    "axes": None,
                    "held": (key, role) in current,
                }
            )

    by_id = {s.id: s for s in sources}
    if events is None:
        events = getattr(project, "events", ()) or ()
    if risks is None:
        risks = getattr(project, "risks", ()) or ()
    for event in events:
        source = by_id.get(event.source_id)
        rows.append(
            {
                "entity": "milestone",
                "entity_key": event.event_type,
                "field": "event_date",
                "value": _text(event.event_date),
                "number": None,
                "unit": "date",
                "source_id": event.source_id,
                "as_of": _date(event.event_date),
                "tier": "unconfirmed"
                if event.unconfirmed
                else (_source_tier(source, confirmed=True, reason=None) if source else "confirmed"),
                "reason": event.unconfirmed,
                "quote": event.quote,
                "axes": None,
                "held": True,
            }
        )
    for risk in risks:
        source = by_id.get(risk.source_id)
        rows.append(
            {
                "entity": "obstacle",
                "entity_key": risk.category,
                "field": "severity",
                "value": risk.severity,
                "number": None,
                "unit": None,
                "source_id": risk.source_id,
                "as_of": _date(risk.first_seen),
                "tier": "ruled_out"
                if risk.status == "superseded"
                else "unconfirmed"
                if risk.unconfirmed
                else (_source_tier(source, confirmed=True, reason=None) if source else "confirmed"),
                "reason": risk.unconfirmed or (None if risk.status == "open" else risk.status),
                "quote": risk.quote,
                "axes": None,
                "held": risk.status == "open",
            }
        )
    return rows


def rebuild(session: Session, project: Project) -> int:
    """Replace one campus's statements with what its sources say now. Returns rows."""
    from sqlalchemy import inspect

    from tracker.models import Event, Risk

    session.execute(delete(Fact).where(Fact.project_id == project.id))
    # Milestones and obstacles as plain rows unless already loaded: lazy-loading them
    # here, inside the write, puts objects in the session that a dry run's rolled-back
    # savepoint then re-creates under the same ids (SQLAlchemy warns, and is right to).
    unloaded = inspect(project).unloaded
    events = (
        session.execute(
            select(
                Event.event_type,
                Event.event_date,
                Event.quote,
                Event.unconfirmed,
                Event.source_id,
            ).where(Event.project_id == project.id)
        ).all()
        if "events" in unloaded
        else None
    )
    risks = (
        session.execute(
            select(
                Risk.category,
                Risk.severity,
                Risk.status,
                Risk.unconfirmed,
                Risk.quote,
                Risk.source_id,
                Risk.first_seen,
            ).where(Risk.project_id == project.id)
        ).all()
        if "risks" in unloaded
        else None
    )
    rows = statements(project, events=events, risks=risks)
    if rows:
        session.execute(
            Fact.__table__.insert(),
            [{"project_id": project.id, **row} for row in rows],
        )
    return len(rows)


def recompute(session: Session, projects: Iterable[Project] | None = None) -> int:
    """`rebuild` every campus. Returns how many statements the table holds."""
    from sqlalchemy.orm import selectinload

    if projects is None:
        projects = session.scalars(
            select(Project).options(
                selectinload(Project.sources),
                selectinload(Project.blocks),
                selectinload(Project.parties),
                selectinload(Project.events),
                selectinload(Project.risks),
            )
        ).all()
    total = 0
    for project in projects:
        total += rebuild(session, project)
    session.flush()
    return total


def summary(session: Session) -> dict[str, Any]:
    """Statements by what they are about and how far they can be trusted."""
    by = session.execute(
        select(Fact.entity, Fact.tier, func.count()).group_by(Fact.entity, Fact.tier)
    ).all()
    out: dict[str, dict[str, int]] = {}
    for entity, tier, count in by:
        out.setdefault(entity, {})[tier] = count
    return {
        "statements": sum(c for _e, _t, c in by),
        "by_entity": out,
        "campuses": session.scalar(select(func.count(func.distinct(Fact.project_id)))) or 0,
    }


#: Columns of the facts download, in order. A contract: add at the end, never rename.
CSV_COLUMNS: Final[tuple[str, ...]] = (
    "project_id",
    "company",
    "campus",
    "entity",
    "entity_key",
    "field",
    "value",
    "number",
    "unit",
    "tier",
    "reason",
    "held",
    "as_of",
    "source_url",
    "source_type",
    "quote",
    "axes",
)


def rows_for(session: Session, project_ids: Iterable[int] | None = None) -> list[dict[str, Any]]:
    """The download's rows: each statement with its campus and citation spelled out."""
    from tracker.models import Source

    stmt = (
        select(Fact, Project.company, Project.name, Source.url, Source.source_type)
        .join(Project, Project.id == Fact.project_id)
        .outerjoin(Source, Source.id == Fact.source_id)
        .order_by(Fact.project_id, Fact.entity, Fact.entity_key, Fact.field, Fact.id)
    )
    if project_ids is not None:
        stmt = stmt.where(Fact.project_id.in_(list(project_ids)))
    out = []
    for fact, company, campus, url, source_type in session.execute(stmt):
        out.append(
            {
                "project_id": fact.project_id,
                "company": company,
                "campus": campus,
                "entity": fact.entity,
                "entity_key": fact.entity_key,
                "field": fact.field,
                "value": fact.value,
                "number": fact.number,
                "unit": fact.unit,
                "tier": fact.tier,
                "reason": fact.reason,
                "held": bool(fact.held),
                "as_of": fact.as_of.isoformat() if fact.as_of else None,
                "source_url": url,
                "source_type": source_type,
                "quote": fact.quote,
                "axes": fact.axes,
            }
        )
    return out


def render_csv(rows: list[dict[str, Any]]) -> str:
    """The download as CSV with a frozen header and LF line endings, like the export's."""
    import csv
    import io

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row[k]) for k in CSV_COLUMNS})
    return buffer.getvalue()


__all__ = [
    "CSV_COLUMNS",
    "TRANCHE_FIELDS",
    "UNITS",
    "rebuild",
    "recompute",
    "render_csv",
    "rows_for",
    "statements",
    "summary",
]
