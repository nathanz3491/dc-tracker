"""One table of every statement every source makes: the master data the client asked for.

Step 4.5 of the client plan (2026-10-21, A4: "every channel in one table, the
relationships defined, the data classified"). The statements already existed — as JSON
on each citation (`claims`, `quotes`, `claim_meta`, `blocks`, `parties`) and as the
`event` and `risk` rows — in five shapes. `fact` puts them in one, with the same
columns whatever the statement is about, so "every figure anyone has given for
Abilene's capacity, how far each can be trusted, and which the page shows" is one
query, and a reader can download the lot (`tracker export facts`, `/v1/projects/{id}/
facts`).

**A cache the readers read, never an input the merge reads.** Rebuilt wholesale from
the sources by `upsert`, `recompute_from_sources` and `tracker init`, like the
tranches. Nothing reads it to decide a value; the merge engine still decides from the
sources, and this records what it had to decide between.

**What reads it** (step 4.5's second half): every surface that shows where a figure
came from — the export's `basis`, `prov` and `claims_by_field`, so the API's
`fields` and the console's project page, its 待确认 reasons and the table's "quoted
only" filter. They read it through `Statements`, which answers the questions
`gaps.provenance` asks of a citation (`gaps.Said`) and gives the merge engine's
claim lists in the merge engine's order (`upsert.rank_claims`). A campus statement
therefore carries all its citation's JSON said about it (migration 0048): the value
typed as written (`value_json`), and the two lists it was on (`quoted`, `doubted`).
The JSON readers (`gaps.FromCitations`, `upsert.claims_by_field`) remain for
`tracker show` and the nightly checks until the JSON paths are retired, and
`tests/test_facts.py` holds the two to the same answers.

**Freshness is the risk**, and is not left to each write path. `follow` — on every
session `db.session_scope` opens — notes each flush's changes to a campus's inputs
and rebuilds those campuses at commit (`settle`), and a reader in the writing
transaction rebuilds the one it asks for. `drifted` is the check: the campuses whose
stored statements are not what a rebuild would write now.

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
from sqlalchemy import event as orm_event
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
#: Columns only a campus statement fills (migration 0048), NULL on every other.
_CAMPUS_ONLY: Final[dict[str, None]] = {"value_json": None, "quoted": None, "doubted": None}

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


def _listed(raw: str | None) -> set[str]:
    """The field names in one of a citation's comma-separated lists."""
    return {f.strip() for f in (raw or "").split(",") if f.strip()}


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


def stated(
    stored: Any, sources: Iterable[Any], field: str, *, tiers: Iterable[str] = ("confirmed",)
) -> bool:
    """Whether a citation states the value a campus's `field` holds, in its own words.

    A `confirmed` statement of exactly that value (:func:`_held`): one whose quote
    the evidence gate verified, from a source that is neither a directory nor
    ruled out. `tiers` widens it -- `delays` also asks whether a directory lists a
    campus as operational. The tier every surface shows for it, computed from the same citation
    columns and the same :func:`_source_tier` as the stored statements, so a reader
    of the citations already loaded (`capex.working_set`) and a reader of this table
    cannot answer differently.

    What `delays.assess` asks before calling a campus late: on 2026-10-24 five of
    ten campuses past their date rested on a date nobody had verified -- 待确认
    (Carter's Store, Compass Hoffman Estates, Savvis MP2, DC Blox Conyers) or a
    directory's copy of a misread (NTT Gainesville). See `delays` rule 1.
    """
    from tracker.upsert import is_placeholder

    wanted = set(tiers)
    for source in sources:
        claims = _load(getattr(source, "claims", None), {})
        value = claims.get(field) if isinstance(claims, dict) else None
        if value is None or not _held(stored, value):
            continue
        reasons = _load(getattr(source, "unconfirmed_reasons", None), {})
        reason = reasons.get(field) if isinstance(reasons, dict) else None
        unconfirmed = _listed(getattr(source, "unconfirmed_fields", None))
        confirmed = not is_placeholder(source) and field not in unconfirmed
        if _source_tier(source, confirmed=confirmed, reason=reason) in wanted:
            return True
    return False


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
        listed = _listed(source.fields)
        unconfirmed = _listed(source.unconfirmed_fields)
        placeholder = is_placeholder(source)

        # `notes` is never a claim — the crawl and the manual path both drop it, and
        # the row's notes are assembled — so there is no statement to record.
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
                    # In the order the citation recorded them, not re-sorted: the
                    # export hands this envelope on as written (`prov[field].axes`).
                    "axes": json.dumps(axes) if axes else None,
                    "held": _held(getattr(project, field, None), value),
                    "value_json": json.dumps(value, ensure_ascii=False),
                    "quoted": field in listed,
                    "doubted": field in unconfirmed,
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


def _milestones_and_obstacles(session: Session, project: Project) -> tuple[list, list]:
    """The campus's milestones and obstacles as stored, as plain rows.

    Read from the table, never from `project.events` / `project.risks`. Those are
    written beside the relationship (`session.add(Event(project_id=...))`), so a
    collection loaded earlier in the transaction is silently short — which left one
    campus on the 2026-10-22 copy of production with a milestone the table did not
    list. Plain rows, too, because lazy-loading them here, inside the write, puts
    objects in the session that a dry run's rolled-back savepoint then re-creates
    under the same ids (SQLAlchemy warns, and is right to).
    """
    from tracker.models import Event, Risk

    events = session.execute(
        select(
            Event.event_type,
            Event.event_date,
            Event.quote,
            Event.unconfirmed,
            Event.source_id,
        )
        .where(Event.project_id == project.id)
        .order_by(Event.id)
    ).all()
    risks = session.execute(
        select(
            Risk.category,
            Risk.severity,
            Risk.status,
            Risk.unconfirmed,
            Risk.quote,
            Risk.source_id,
            Risk.first_seen,
        )
        .where(Risk.project_id == project.id)
        .order_by(Risk.id)
    ).all()
    return events, risks


def rebuild(session: Session, project: Project) -> int:
    """Replace one campus's statements with what its sources say now. Returns rows."""
    # `fetch`, not the default `evaluate`: both drop the deleted statements from the
    # session, but `evaluate` finds them by testing every object the session holds
    # against the WHERE clause in Python — 17,000 objects per campus during a
    # whole-table `backfill derive`, which was 11 of its 29 seconds. `fetch` asks
    # the database which ids it deleted and looks those up. The session is flushed
    # before either runs, so the two cannot disagree about which rows match.
    session.execute(
        delete(Fact).where(Fact.project_id == project.id),
        execution_options={"synchronize_session": "fetch"},
    )
    events, risks = _milestones_and_obstacles(session, project)
    rows = statements(project, events=events, risks=risks)
    if rows:
        session.execute(
            Fact.__table__.insert(),
            [{"project_id": project.id, **_CAMPUS_ONLY, **row} for row in rows],
        )
    # A set `preload` read before this write is the old one. Forgotten, the next
    # reader in this transaction — an export straight after an ingest — reads what
    # was just written. And the campus is current, so the commit need not rebuild it.
    session.info.get(_PRELOADED, {}).pop(project.id, None)
    session.info.get(_STALE, set()).discard(project.id)
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


#: Every column a statement is compared on: all but its row id and its campus.
_STATEMENT_COLUMNS: Final = (
    "entity",
    "entity_key",
    "field",
    "value",
    "number",
    "unit",
    "source_id",
    "as_of",
    "tier",
    "reason",
    "quote",
    "axes",
    "held",
    "value_json",
    "quoted",
    "doubted",
)


def _comparable(row: Any) -> tuple[Any, ...]:
    out = []
    for name in _STATEMENT_COLUMNS:
        value = row.get(name) if isinstance(row, dict) else getattr(row, name)
        if name in ("held", "quoted", "doubted") and value is not None:
            value = bool(value)
        out.append(value)
    return tuple(out)


def drifted(session: Session, projects: Iterable[Project] | None = None) -> list[int]:
    """Campuses whose stored statements are not what a rebuild would write now.

    The check behind the cache's one promise: every write that changes what a
    campus's sources say, or what the campus holds, leaves its statements current.
    Empty when the promise holds. Writes nothing; flushes what is pending, and reads
    each campus's citations, tranches and parties afresh from the database, so a
    collection loaded before a write cannot vouch for the rows written after it.
    """
    session.flush()
    if projects is None:
        projects = session.scalars(select(Project).order_by(Project.id)).all()
    columns = [getattr(Fact, name) for name in _STATEMENT_COLUMNS]
    out = []
    for project in projects:
        session.expire(project, ["sources", "blocks", "parties"])
        stored = session.execute(select(*columns).where(Fact.project_id == project.id)).all()
        events, risks = _milestones_and_obstacles(session, project)
        fresh = [{**_CAMPUS_ONLY, **row} for row in statements(project, events=events, risks=risks)]
        if sorted(map(_comparable, stored), key=repr) != sorted(map(_comparable, fresh), key=repr):
            out.append(project.id)
    return out


#: What `Statements` reads of a campus statement. Plain rows, not `Fact` objects:
#: the console's list payload reads 42,000 of them, and building an ORM object for
#: each cost it 1.7 seconds where the rows cost 0.3.
_READ: Final = (
    Fact.project_id,
    Fact.source_id,
    Fact.field,
    Fact.value,
    Fact.value_json,
    Fact.quoted,
    Fact.doubted,
    Fact.quote,
    Fact.reason,
    Fact.axes,
)

#: `session.info` key: campus statements `preload` read ahead, by project id. Each
#: campus's set is dropped when `rebuild` rewrites it, and the whole map when the
#: transaction ends, so a rolled-back write never leaves its rows behind here.
_PRELOADED: Final = "tracker.facts.preloaded"

#: Projects per `IN (...)` when preloading, under SQLite's bound-parameter limit.
_CHUNK: Final = 500


def _campus_rows(session: Session, ids: list[int]) -> dict[int, list[Any]]:
    out: dict[int, list[Any]] = {pid: [] for pid in ids}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        stmt = (
            select(*_READ)
            .where(Fact.entity == "campus", Fact.project_id.in_(chunk))
            .order_by(Fact.id)
        )
        for row in session.execute(stmt):
            out[row.project_id].append(row)
    return out


def preload(session: Session, projects: Iterable[Project]) -> None:
    """Read many campuses' statements in one query, for `Statements` to find.

    `export.fetch_projects` calls it for everything it loads, the way it eagerly
    loads the citations, so a payload of 780 campuses asks once and not 780 times.
    """
    ids = [p.id for p in projects if p.id is not None]
    if ids:
        session.info.setdefault(_PRELOADED, {}).update(_campus_rows(session, ids))


def campus_statements(project: Project) -> list[Any]:
    """One campus's statements about its own fields, as stored, in the order written.

    Preloaded if `preload` read them, else read now. A campus outside any session,
    or not yet saved, has none stored.
    """
    from sqlalchemy.orm import object_session

    session = object_session(project)
    if session is None or project.id is None:
        return []
    rows = session.info.get(_PRELOADED, {}).get(project.id)
    if rows is None:
        rows = _campus_rows(session, [project.id])[project.id]
    # Read inside the transaction that changed it: rebuilt first, so the export
    # straight after an edit shows the edit (see `follow`).
    if project.id in session.info.get(_STALE, ()):
        rebuild(session, project)
        rows = _campus_rows(session, [project.id])[project.id]
    return rows


# --- keeping the table current ------------------------------------------------

#: `session.info` key: the campuses a flushed write in this transaction changed an
#: input of, and whose statements are therefore not yet rebuilt. Emptied by
#: `rebuild`, one campus at a time, and by `settle`, all of them, at commit.
_STALE: Final = "tracker.facts.stale"


def _inputs() -> tuple[type, ...]:
    """The rows a campus's statements are read from, beside the campus itself."""
    from tracker.models import CapacityBlock, Event, ProjectParty, Risk, Source

    return (Source, CapacityBlock, ProjectParty, Event, Risk)


def _campuses_changed(session: Session, obj: Any) -> set[int]:
    """The campuses one flushed row is an input of: where it is, and where it was."""
    from sqlalchemy import inspect

    from tracker.vocab import WRITABLE_FIELDS

    state = inspect(obj)
    if isinstance(obj, Project):
        # Each statement records whether the campus holds its value (`held`). Not
        # `notes`, which no statement is about and every ruling appends to.
        if state.deleted or obj.id is None:
            return set()
        if state.pending or any(
            state.attrs[name].history.has_changes() for name in WRITABLE_FIELDS if name != "notes"
        ):
            return {obj.id}
        return set()
    if not isinstance(obj, _inputs()):
        return set()
    if not (state.pending or state.deleted or session.is_modified(obj)):
        return set()
    ids = {getattr(obj, "project_id", None)}
    # A row moved between campuses — a citation in a merge — changes both.
    ids.update(state.attrs.project_id.history.deleted or ())
    project_history = state.attrs.project.history
    ids.update(getattr(p, "id", None) for p in (project_history.deleted or ()))
    ids.update(getattr(p, "id", None) for p in (project_history.added or ()))
    ids.discard(None)
    return ids


def _note_changed(session: Session, _flush_context: Any) -> None:
    changed: set[int] = set()
    for obj in (*session.new, *session.dirty, *session.deleted):
        changed |= _campuses_changed(session, obj)
    if changed:
        session.info.setdefault(_STALE, set()).update(changed)


def settle(session: Session) -> int:
    """Rebuild every campus whose statements a write in this transaction made stale.

    Run at every commit by `follow`. Returns how many campuses it rebuilt.
    """
    session.flush()
    stale = session.info.get(_STALE)
    done = 0
    while stale:
        project = session.get(Project, stale.pop())
        if project is not None:  # merged away or excluded: its rows went with it
            rebuild(session, project)
            done += 1
    return done


def _transaction_ended(session: Session, transaction: Any) -> None:
    session.info.pop(_PRELOADED, None)
    if transaction.parent is None:
        # Committed, so settled; or rolled back, so the writes it marked are gone.
        session.info.pop(_STALE, None)


def follow(session: Session) -> None:
    """Keep this session's writes and the fact table in step. `db.session_scope` calls it.

    The table is a cache of the citations, tranches, parties, milestones and
    obstacles, and of the campus's own fields; the export, the API and the project
    page read it. A cache is wrong whenever a write forgets to rebuild it, and on
    2026-10-22 six kinds of write did: a party's role ended or resumed, an alias
    added or taken back, a publication date learned, a backfill rewriting a claim's
    envelope or a gate's verdict, an obstacle confirmed or closed, a milestone
    refiled. So it no longer depends on each remembering. Every flush notes the
    campuses whose inputs it changed; every commit rebuilds those not rebuilt since
    (`settle`); and a reader in the same transaction rebuilds the one it asks for
    (`campus_statements`). The writes that already rebuild — an ingest, a recompute
    — leave nothing for the commit to do.
    """
    orm_event.listen(session, "after_flush", _note_changed)
    orm_event.listen(session, "before_commit", settle)
    orm_event.listen(session, "after_transaction_end", _transaction_ended)


def _axis(entry: dict[str, Any], axis: str) -> str | None:
    """One axis of a claim's envelope, as `upsert._recorded_axis` reads it."""
    value = entry.get(axis)
    return str(value) if value else None


class Statements:
    """One campus's statements, as the surfaces that show a figure's evidence read them.

    The export's `prov` and claim table, the API's `fields` and the project page's
    evidence all read this (client plan step 4.5), not each citation's JSON. Built
    from the campus statements `rebuild` wrote (`campus_statements`), and from the
    campus's citations only for what is the citation's own — its URL, type, dates,
    excerpt, extractor.

    It answers `gaps.Said`, so `gaps.provenance` runs its one algorithm over these
    rows instead of the JSON, and it gives the merge engine's claim lists in the
    merge engine's order (`claims_by_field`, ranked by `upsert.rank_claims`), so the
    page cannot list the claims differently from how the merge weighed them.

    A statement whose citation has left the campus — moved by a merge, deleted — is
    not one of its statements, whatever a row not yet rebuilt still says.
    """

    def __init__(self, project: Project) -> None:
        self._sources = list(getattr(project, "sources", ()) or ())
        ids = {s.id for s in self._sources}
        self._rows: dict[tuple[int, str], Any] = {}
        self._by_source: dict[int, list[Any]] = {}
        for fact in campus_statements(project):
            if fact.source_id not in ids:
                continue
            self._rows[(fact.source_id, fact.field)] = fact
            self._by_source.setdefault(fact.source_id, []).append(fact)

    def _row(self, source: Any, field: str) -> Any:
        return self._rows.get((getattr(source, "id", None), field))

    # --- `gaps.Said` -------------------------------------------------------------

    def tier(self, source: Any, field: str) -> str:
        from tracker.gaps import tier_for

        row = self._row(source, field)
        return tier_for(bool(row is not None and row.quoted), source)

    def asserted(self, source: Any, field: str) -> bool:
        row = self._row(source, field)
        return row is not None and bool(row.quoted or row.doubted)

    def states(self, source: Any, field: str, value: Any) -> bool:
        from tracker.gaps import _same_value
        from tracker.upsert import claim_value

        row = self._row(source, field)
        return row is not None and _same_value(_claimed(row), claim_value(value))

    def quote(self, source: Any, field: str) -> Any:
        row = self._row(source, field)
        return row.quote if row is not None else None

    def axes(self, source: Any, field: str) -> dict[str, str]:
        entry = _envelope(self._row(source, field))
        return {k: str(v) for k, v in entry.items() if v is not None}

    # --- what the claim table adds ----------------------------------------------

    def reason(self, source: Any, field: str) -> Any:
        """Why the citation's claim is 待确认 or ruled out, as recorded; None if neither."""
        row = self._row(source, field)
        return row.reason if row is not None else None

    def reasons(self, source: Any) -> list[tuple[str, Any]]:
        """`(field, reason)` for each of the citation's claims that carries one.

        By field name, the order every writer stores `unconfirmed_reasons` in, so a
        reader taking the first reason per field takes the one it always took.
        """
        rows = self._by_source.get(getattr(source, "id", None), ())
        return sorted((row.field, row.reason) for row in rows if row.reason is not None)

    def claims_by_field(self, *, settings: Any = None) -> dict[str, list[Any]]:
        """field -> every claim about it, strongest first: `upsert.claims_by_field`'s
        answer, from the rows instead of the JSON."""
        from tracker import confidence
        from tracker.upsert import (
            _CAPACITY_FIELDS,
            _DATE_PRECISION_FIELDS,
            _Claim,
            _weight,
            is_placeholder,
            rank_claims,
        )
        from tracker.upsert import DECIDED_REASONS as decided
        from tracker.vocab import PART_FIELDS

        out: dict[str, list[_Claim]] = {}
        # In the citations' own order, as the JSON reader walks them, so the ranking
        # starts from the same list and its stable sort cannot tell the two apart.
        for source in self._sources:
            rows = self._by_source.get(source.id)
            if not rows:
                continue
            placeholder = is_placeholder(source)
            tertiary = confidence.is_tertiary(source)
            weight = 0 if placeholder else _weight(source.source_type)
            for row in rows:
                entry = _envelope(row)
                out.setdefault(row.field, []).append(
                    _Claim(
                        _claimed(row),
                        weight,
                        source.fetched_at,
                        source.source_type,
                        source.url,
                        confirmed=not placeholder and not row.doubted,
                        published_at=getattr(source, "published_at", None),
                        decided_against=row.reason in decided,
                        tertiary=tertiary,
                        part=row.field in PART_FIELDS and entry.get("scope") == "building",
                        floor=row.field in _CAPACITY_FIELDS and _axis(entry, "bound") == "at_least",
                        precision=(
                            _axis(entry, "date_precision")
                            if row.field in _DATE_PRECISION_FIELDS
                            else None
                        ),
                        as_of=_axis(entry, "as_of"),
                        quote=row.quote,
                    )
                )
        return rank_claims(out, settings=settings)


def _claimed(row: Any) -> Any:
    """A campus statement's value as its citation recorded it, type and all."""
    return json.loads(row.value_json) if row.value_json is not None else row.value


def _envelope(row: Any) -> dict[str, Any]:
    """A campus statement's claim envelope (`claim_meta[field]`), or an empty one."""
    if row is None or not row.axes:
        return {}
    entry = _load(row.axes, {})
    return entry if isinstance(entry, dict) else {}


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
    "Statements",
    "campus_statements",
    "drifted",
    "follow",
    "preload",
    "rebuild",
    "recompute",
    "render_csv",
    "rows_for",
    "settle",
    "statements",
    "summary",
]
