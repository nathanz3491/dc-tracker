"""The single write path. All three ingest paths converge here.

The PRD names no home for dedup matching, field-level merge policy, or its own
open question Q2 ("if two sources disagree on mw_planned, which wins?"), so this
module exists outside the PRD's file layout. Putting it in `normalize.py` would
break that module's side-effect-free, one-function-per-field contract.

The central design choice: **project fields are recomputed from the full set of
`source.claims`, not merged incrementally.** Each source records what it
asserts; after the source rows are written, every field is derived afresh from
all of them by a declared policy. Three consequences, all of which the PRD asks
for and none of which incremental merging gives you:

* **Idempotence.** Re-ingesting the same input recomputes the same values, so
  `updated_at` genuinely does not move. Incremental merge drifts on re-runs.
* **Order independence.** Ingesting PJM then news gives the same result as news
  then PJM. With incremental merge, whichever ran last quietly wins.
* **Q2 for free.** Both conflicting values persist in their own `source` rows;
  the project field takes the higher-weighted one; the disagreement is disclosed
  in `notes`. Nothing is destroyed to make the merge work.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker import confidence as conf
from tracker.dedup import all_keys, dedup_key, is_cross_granularity_match
from tracker.ingest.records import IngestRecord
from tracker.models import Event, Project, Source, utcnow
from tracker.vocab import (
    DEFAULT_PHASE,
    PHASE_PROGRESSION,
    PHASE_TERMINAL,
    TRACKED_FIELDS,
    WRITABLE_FIELDS,
)

log = logging.getLogger(__name__)

#: Sort floor for a missing timestamp, so claim ordering never has to compare a
#: datetime against a string.
_EPOCH = _dt.datetime(1970, 1, 1)

#: Marks a note line as *derived* — recomputed from the current claims on every
#: upsert, so stale entries vanish on their own.
NOTE_PREFIX = "[tracker]"

#: Marks a note line as *contributed by a source* — a disclosure about one
#: citation, which accumulates rather than being regenerated. See _merge_notes.
SOURCE_NOTE_PREFIX = "[source]"


class Policy(Enum):
    """How to pick one value for a field from several sources' claims."""

    #: Highest-weighted source wins; ties broken by most recently fetched.
    #: For contested quantitative facts.
    PREFER_WEIGHT = "prefer_weight"
    #: First non-null claim wins and is never overwritten. For identity fields,
    #: where churn is worse than staleness.
    FILL_ONLY = "fill_only"
    #: Largest claimed value. Energized capacity only grows as phases come online,
    #: and a source describing an earlier state should not walk it back.
    MAX = "max"
    #: Earliest claimed date. "First announced" means the first one anybody saw.
    MIN = "min"
    #: Furthest along the phase progression, unless a source asserts a terminal
    #: state (paused/cancelled), which overrides regardless of progression.
    PHASE = "phase"


#: Per-field merge policy. Any WRITABLE_FIELD absent here defaults to
#: PREFER_WEIGHT; the explicit table exists so the choices are reviewable.
FIELD_POLICY: dict[str, Policy] = {
    "name": Policy.FILL_ONLY,
    "company": Policy.FILL_ONLY,
    "city": Policy.FILL_ONLY,
    "county": Policy.FILL_ONLY,
    "state": Policy.FILL_ONLY,
    "country": Policy.FILL_ONLY,
    "lat": Policy.FILL_ONLY,
    "lon": Policy.FILL_ONLY,
    "customer": Policy.PREFER_WEIGHT,
    "mw_planned": Policy.PREFER_WEIGHT,
    "mw_built": Policy.MAX,
    "investment_usd": Policy.PREFER_WEIGHT,
    "phase": Policy.PHASE,
    "first_announced": Policy.MIN,
    "expected_online": Policy.PREFER_WEIGHT,
    "blocker": Policy.PREFER_WEIGHT,
}

_PHASE_RANK = {name: i for i, name in enumerate(PHASE_PROGRESSION)}


@dataclass
class UpsertResult:
    project_id: int
    action: str  # "insert" | "update" | "unchanged"
    conflicts: list[str] = dc_field(default_factory=list)
    duplicate_of: int | None = None
    events_written: int = 0


def derive_fields(claims: dict[str, Any]) -> str | None:
    """Render ``source.fields`` from a claims dict, in canonical order.

    Canonical ordering makes re-ingesting the same input produce a byte-identical
    row, which is what lets the idempotence test assert on `updated_at`.
    """
    present = [f for f in WRITABLE_FIELDS if claims.get(f) is not None]
    return ",".join(present) or None


def _claim_value(raw: Any) -> Any:
    """Coerce a claim to something JSON can round-trip losslessly."""
    if hasattr(raw, "isoformat"):
        return raw.isoformat()
    return raw


def _weight(source_type: str) -> int:
    return conf.SOURCE_WEIGHTS.get(source_type, 1)


@dataclass(frozen=True)
class _Claim:
    """One source's assertion about one field, with its tiebreakers."""

    value: Any
    weight: int
    fetched_at: Any
    source_type: str
    url: str


def _gather(sources: list[Source]) -> dict[str, list[_Claim]]:
    """field -> every claim about it, strongest first."""
    out: dict[str, list[_Claim]] = {}
    for s in sources:
        if not s.claims:
            continue
        try:
            claims = json.loads(s.claims)
        except (TypeError, ValueError):
            log.warning("source %s has unparseable claims JSON; ignoring", s.id)
            continue
        if not isinstance(claims, dict):
            continue
        for name, value in claims.items():
            if name not in WRITABLE_FIELDS or value is None:
                continue
            out.setdefault(name, []).append(
                _Claim(value, _weight(s.source_type), s.fetched_at, s.source_type, s.url)
            )
    # Strongest source first, then most recently fetched. `url` is the final
    # tiebreaker so the ordering is total and therefore reproducible — without
    # it, two equally-weighted same-timestamp sources could resolve differently
    # between runs and break idempotence.
    for claims_list in out.values():
        claims_list.sort(key=lambda c: (c.weight, c.fetched_at or _EPOCH, c.url), reverse=True)
    return out


def _coerce_like(value: Any, template: Any) -> Any:
    """Bring a JSON-round-tripped claim back to the column's Python type."""
    if isinstance(template, _dt.datetime) and isinstance(value, str):
        return _dt.datetime.fromisoformat(value)
    if isinstance(template, _dt.date) and isinstance(value, str):
        return _dt.date.fromisoformat(value)
    return value


def _resolve(field_name: str, claims: list[_Claim], existing: Any) -> Any:
    """Apply the field's policy to choose one value."""
    if not claims:
        return existing
    policy = FIELD_POLICY.get(field_name, Policy.PREFER_WEIGHT)

    if policy is Policy.FILL_ONLY:
        return existing if existing is not None else claims[0].value

    if policy is Policy.MAX:
        numeric = [c.value for c in claims if isinstance(c.value, (int, float))]
        candidates = numeric + ([existing] if isinstance(existing, (int, float)) else [])
        return max(candidates) if candidates else existing

    if policy is Policy.MIN:
        # Dates are ISO strings in claims, so lexical order is chronological.
        # `existing` participates: an earlier date already on the row is still
        # the earliest anybody saw.
        values = [str(c.value) for c in claims]
        if existing is not None:
            values.append(existing.isoformat() if hasattr(existing, "isoformat") else str(existing))
        return min(values) if values else existing

    if policy is Policy.PHASE:
        return _resolve_phase(claims, existing)

    # PREFER_WEIGHT: claims are pre-sorted by (weight, recency).
    return claims[0].value


def _resolve_phase(claims: list[_Claim], existing: Any) -> Any:
    """Furthest-along phase, but a terminal state always wins.

    A project that a newer source says is cancelled is cancelled, even though
    "operational" sits further along the progression. Paused/cancelled are
    statements about the project stopping, not about its degree of completion.
    """
    terminal = [c for c in claims if c.value in PHASE_TERMINAL]
    if terminal:
        # Claims are sorted strongest-and-newest first.
        return terminal[0].value
    ranked = [c.value for c in claims if c.value in _PHASE_RANK]
    if existing in _PHASE_RANK:
        ranked.append(existing)
    if not ranked:
        return existing or DEFAULT_PHASE
    return max(ranked, key=lambda p: _PHASE_RANK[p])


def _conflict_notes(by_field: dict[str, list[_Claim]]) -> tuple[list[str], list[str]]:
    """Disclosure lines for fields where sources materially disagree.

    Implements PRD open question Q2: both values stay in their own `source` rows,
    the spread is disclosed here when it exceeds the tolerance, and the project
    field takes the higher-weighted claim.
    """
    lines: list[str] = []
    fields: list[str] = []
    for field_name in conf.KEY_FIELDS:
        claims = by_field.get(field_name, [])
        if len(claims) < 2:
            continue
        best = claims[0]
        others = [c for c in claims[1:] if conf.values_conflict(best.value, c.value)]
        if not others:
            continue
        fields.append(field_name)
        rival = others[0]
        detail = f"{best.value!r} ({best.source_type}) vs {rival.value!r} ({rival.source_type})"
        if isinstance(best.value, (int, float)) and isinstance(rival.value, (int, float)):
            scale = max(abs(float(best.value)), abs(float(rival.value)))
            if scale:
                spread = abs(float(best.value) - float(rival.value)) / scale
                detail += f" [{spread:.0%} spread]"
        lines.append(f"{NOTE_PREFIX} conflict {field_name}: {detail}; kept higher-weighted value")
    return lines, fields


def _merge_notes(existing: str | None, derived: list[str], contributed: list[str]) -> str | None:
    """Rebuild the notes block from three kinds of line.

    * **Operator prose** — no marker at all. Never touched.
    * **Derived** (:data:`NOTE_PREFIX`) — a pure function of the current claims,
      so it is regenerated wholesale. That is what makes a resolved conflict's
      disclosure *disappear* rather than linger forever.
    * **Contributed** (:data:`SOURCE_NOTE_PREFIX`) — a statement about one
      source, e.g. "this MW figure is generator nameplate". These accumulate,
      deduplicated.

    The distinction is load-bearing. When two ingest records resolve to the same
    project (two queue rows for one site), each carries its own disclosure. If
    contributed lines were regenerated like derived ones, the second record would
    erase the first's disclosure, the first would restore it on the next run, and
    `updated_at` would churn on every ingest forever.
    """
    human: list[str] = []
    kept_contributed: list[str] = []
    for raw in (existing or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(SOURCE_NOTE_PREFIX):
            kept_contributed.append(line)
        elif line.startswith(NOTE_PREFIX):
            continue  # derived; rebuilt below
        else:
            human.append(line)

    all_contributed = sorted(dict.fromkeys(kept_contributed + contributed))
    combined = human + sorted(dict.fromkeys(derived)) + all_contributed
    return "\n".join(combined) or None


def _find_duplicate_candidate(
    session: Session, key: str, payload: dict[str, Any]
) -> Project | None:
    """A project that may be the same site at a different location granularity.

    Never merged automatically — surfaced so `review` can ask a human. See the
    `dedup.py` module docstring for why string matching cannot decide this.

    Two ways to match, both needed:

    * **Shared alternate key.** Either row knowing both a city and its county has
      two possible identities, so a city row and a county row for one site
      intersect even though their locality *names* differ ("mount pleasant" vs
      "racine"). This is the PRD's own example.
    * **Cross-granularity name match.** The locality names agree once the
      County/Parish word is discounted ("Racine" vs "Racine County").
    """
    incoming = all_keys(
        payload.get("company"), payload.get("city"), payload.get("county"), payload.get("state")
    )
    same_company_prefix = key.split("|", 1)[0] + "|%"
    rows = session.scalars(select(Project).where(Project.dedup_key.like(same_company_prefix))).all()
    for row in rows:
        if row.dedup_key == key:
            continue
        existing = all_keys(row.company, row.city, row.county, row.state)
        if incoming & existing or is_cross_granularity_match(key, row.dedup_key):
            return row
    return None


def _snapshot(project: Project) -> tuple:
    """Every value that `updated_at` is meant to track."""
    return tuple(getattr(project, f) for f in (*WRITABLE_FIELDS, "confidence", "dedup_key"))


def upsert_record(session: Session, rec: IngestRecord, *, force_new: bool = False) -> UpsertResult:
    """Insert or update one project and its citations.

    Args:
        force_new: bypass cross-granularity duplicate detection and insert a
            fresh project even when a candidate match exists. The operator's
            escape hatch for two genuinely separate campuses in one locality.
    """
    payload = dict(rec.project)
    key = dedup_key(
        payload.get("company"), payload.get("city"), payload.get("county"), payload.get("state")
    )

    project = session.scalar(select(Project).where(Project.dedup_key == key))
    action = "update"
    duplicate_of: int | None = None

    if project is None:
        candidate = None if force_new else _find_duplicate_candidate(session, key, payload)
        if candidate is not None:
            duplicate_of = candidate.id
        project = Project(
            name=payload.get("name") or payload.get("company") or "unnamed",
            company=payload.get("company") or "",
            state=(payload.get("state") or "").upper(),
            city=payload.get("city"),
            county=payload.get("county"),
            country=(payload.get("country") or "US").upper(),
            dedup_key=key,
            phase=DEFAULT_PHASE,
            confidence=0,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(project)
        session.flush()
        action = "insert"

    before = _snapshot(project)

    # --- Write the citations ------------------------------------------------
    existing_sources = {s.url: s for s in project.sources}
    for sr in rec.sources:
        claims = {k: _claim_value(v) for k, v in sr.tracked_claims().items()}
        blob = json.dumps(claims, sort_keys=True, ensure_ascii=False) if claims else None
        row = existing_sources.get(sr.url)
        if row is None:
            row = Source(project_id=project.id, url=sr.url)
            session.add(row)
            project.sources.append(row)
            existing_sources[sr.url] = row
        row.source_type = sr.source_type
        row.excerpt = sr.excerpt
        row.claims = blob
        row.fields = derive_fields(claims)
        row.extractor = sr.extractor
        # fetched_at is only advanced, never rewound: a cached re-read must not
        # make an old citation look newer than a genuinely newer one.
        if row.fetched_at is None or sr.fetched_at > row.fetched_at:
            row.fetched_at = sr.fetched_at
    session.flush()

    # --- Recompute every field from all claims ------------------------------
    by_field = _gather(list(project.sources))
    for name in WRITABLE_FIELDS:
        if name == "notes":
            continue
        current = getattr(project, name)
        chosen = _resolve(name, by_field.get(name, []), current)
        if chosen is not None:
            chosen = _coerce_like(chosen, current if current is not None else _template_for(name))
        if name == "state" and isinstance(chosen, str):
            chosen = chosen.upper()
        if name == "country" and isinstance(chosen, str):
            chosen = chosen.upper()
        if chosen != current:
            setattr(project, name, chosen)

    if project.phase is None:
        project.phase = DEFAULT_PHASE

    # --- Notes: conflicts, path disclosures, duplicate proposals ------------
    derived, conflict_fields = _conflict_notes(by_field)
    # Path disclosures and duplicate proposals are contributed, not derived: they
    # are facts about a particular citation or a particular unresolved ambiguity,
    # and must survive a later record for the same project.
    contributed = [f"{SOURCE_NOTE_PREFIX} {line}" for line in rec.notes]
    if duplicate_of is not None:
        contributed.append(
            f"{SOURCE_NOTE_PREFIX} possible duplicate of project #{duplicate_of}: same company "
            "and state, locality differs only by city/county granularity. Confirm or reject in "
            "`tracker review`."
        )
    project.notes = _merge_notes(project.notes, derived, contributed)

    # --- Confidence ---------------------------------------------------------
    views = [conf.SourceView.from_row(s) for s in project.sources]
    populated = sum(1 for f in TRACKED_FIELDS if getattr(project, f, None) is not None)
    score = conf.compute(
        views,
        operator_verified=project.last_verified_at is not None,
        populated_tracked_fields=populated,
    )
    value = score.value
    if rec.confidence_cap is not None:
        value = min(value, rec.confidence_cap)
    if duplicate_of is not None:
        # An unresolved identity question is itself a reason to distrust the row.
        value = min(value, 1)
    project.confidence = value

    # --- Events -------------------------------------------------------------
    events_written = _upsert_events(session, project, rec)

    # --- Did anything actually change? -------------------------------------
    if action == "update":
        if _snapshot(project) == before and not events_written:
            action = "unchanged"
        else:
            project.updated_at = utcnow()
    session.flush()

    return UpsertResult(
        project_id=project.id,
        action=action,
        conflicts=conflict_fields,
        duplicate_of=duplicate_of,
        events_written=events_written,
    )


def _template_for(name: str) -> Any:
    """A typed exemplar for a column, so JSON strings coerce back correctly."""
    if name in {"first_announced", "expected_online"}:
        return _dt.date(2000, 1, 1)
    return None


def _upsert_events(session: Session, project: Project, rec: IngestRecord) -> int:
    """Write events, deduplicating on (project, type, date) per the schema.

    Returns the number of rows inserted; updates to existing events do not count
    as writes, so a re-ingest reports `unchanged`.
    """
    if not rec.events:
        return 0
    url_to_id = {s.url: s.id for s in project.sources}
    # Queried rather than read off `project.events`: rows added earlier in this
    # same session are not necessarily reflected on the relationship yet, and a
    # stale view here means a second upsert re-inserts and trips the
    # (project, type, date) unique constraint.
    existing = {
        (e.event_type, e.event_date): e
        for e in session.scalars(select(Event).where(Event.project_id == project.id)).all()
    }
    inserted = 0
    for ev in rec.events:
        source_id = url_to_id.get(ev.source_url) if ev.source_url else None
        found = existing.get((ev.event_type, ev.event_date))
        if found is None:
            session.add(
                Event(
                    project_id=project.id,
                    event_date=ev.event_date,
                    event_type=ev.event_type,
                    description=ev.description,
                    source_id=source_id,
                )
            )
            inserted += 1
        else:
            found.description = ev.description
            if source_id is not None:
                found.source_id = source_id
    session.flush()
    return inserted


def recompute_confidence(session: Session) -> int:
    """Recompute stored confidence for every project.

    `confidence` is a cache of a pure function, so it can drift — after a manual
    edit, or after the scoring rules change. `tracker init` calls this, and
    `test_confidence_cache_is_consistent` asserts it is a no-op on a fresh DB.
    """
    changed = 0
    for project in session.scalars(select(Project)).all():
        score = conf.compute_for_project(project, project.sources)
        if project.confidence != score.value:
            project.confidence = score.value
            changed += 1
    session.flush()
    return changed


__all__ = [
    "FIELD_POLICY",
    "NOTE_PREFIX",
    "Policy",
    "UpsertResult",
    "derive_fields",
    "recompute_confidence",
    "upsert_record",
]
