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
import hashlib
import json
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker import confidence as conf
from tracker.dedup import (
    all_keys,
    dedup_key,
    is_cross_granularity_match,
    looks_like_the_same_site,
)
from tracker.ingest.records import IngestRecord
from tracker.models import Event, Project, ProjectAlias, Risk, Source, utcnow
from tracker.vocab import (
    DEFAULT_PHASE,
    OPEN_RISK_STATUS,
    PHASE_PROGRESSION,
    PHASE_TERMINAL,
    TRACKED_FIELDS,
    WRITABLE_FIELDS,
    risk_precedence,
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

#: Derived lines only `upsert_record` can produce, because they describe the
#: *ingest* rather than the claims: which identity routed here, and which existing
#: row this one might duplicate. `recompute_from_sources` regenerates every other
#: derived line from the current claims and must not delete these on the way —
#: neither is recoverable from the row, and the duplicate proposal is the *only*
#: record that the identity question is still open.
_INGEST_ONLY_NOTES: tuple[str, ...] = (
    "possible duplicate of project #",
    "a record arriving as ",
)


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


#: Fields that are NOT merged from claims at all, because something else owns them.
#:
#: * `notes` is assembled by `_merge_notes` from derived and contributed lines.
#: * `blocker` is derived from the `risk` rows by `_derive_blocker`. Leaving it in
#:   the merge loop would make it unclearable: `_resolve` returns the existing value
#:   when a field has no claims, so a resolved obstacle could never go back to NULL.
#:   Sources still record a `blocker` claim, which is what keeps `source.fields`
#:   honest about which citation supports it — that value is written, never read.
DERIVED_FIELDS: frozenset[str] = frozenset({"notes", "blocker"})

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
    # Same reasoning as `mw_built`: a later source counting more accelerators is
    # reporting a build-out, not contradicting the earlier one. Only reached when
    # a source stated a chip count; otherwise the value is derived from capacity
    # by `apply_h200_equivalent` and never consults this table.
    "h200_equivalent": Policy.MAX,
    # `blocker` is absent on purpose — see DERIVED_FIELDS above.
}

_PHASE_RANK = {name: i for i, name in enumerate(PHASE_PROGRESSION)}


@dataclass
class UpsertResult:
    project_id: int
    action: str  # "insert" | "update" | "unchanged"
    conflicts: list[str] = dc_field(default_factory=list)
    duplicate_of: int | None = None
    events_written: int = 0
    risks_written: int = 0


def derive_fields(claims: dict[str, Any]) -> str | None:
    """Render ``source.fields`` from a claims dict, in canonical order.

    Canonical ordering makes re-ingesting the same input produce a byte-identical
    row, which is what lets the idempotence test assert on `updated_at`.
    """
    present = [f for f in WRITABLE_FIELDS if claims.get(f) is not None]
    return ",".join(present) or None


def claim_value(raw: Any) -> Any:
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
    #: False for a value the evidence gate could not tie to a quote. Such a claim
    #: is a last resort: `_resolve` uses it only when no confirmed claim exists,
    #: so a 待确认 value can never displace a quote-backed one however heavy its
    #: source or however recently it was fetched.
    confirmed: bool = True


def is_placeholder(source: Source) -> bool:
    """A seed-file citation whose URL was never replaced with a real one.

    `confidence.compute` already drops these before weighting, so a placeholder
    cannot earn trust. That fix stopped at the *score* and left the *values*
    alone, which is the more dangerous half: a placeholder carries whatever
    `source_type` the seed file gave it, and `sample-projects.json` types them
    `company_filing` — weight 3, the heaviest in the system, on a URL that does
    not exist. Observed live on Fairwater (#1), where it set every identity field
    and was then recorded in `notes` as the "higher-weighted" side of a phase
    conflict it had no standing to enter.

    Demoted rather than dropped, because `--allow-placeholders` exists so the
    shipped seed file can smoke-test the pipeline, and a claim-less source would
    make that produce empty rows. See `claims_by_field` for what demotion buys.
    """
    return conf.PLACEHOLDER_MARKER in (source.url or "")


def claims_by_field(sources: list[Source]) -> dict[str, list[_Claim]]:
    """field -> every claim about it, strongest first."""
    out: dict[str, list[_Claim]] = {}
    for s in sources:
        if not s.claims:
            continue
        # A placeholder is 待确认 by construction — its "quote" is the instruction
        # to go and paste one. Saying so routes it through the rule `resolve`
        # already applies to every policy: unconfirmed claims are discarded
        # outright the moment any confirmed claim exists. So the seed file still
        # populates a smoke-test row, and the first real source erases it —
        # including on `phase`, which ranks by progression and would otherwise
        # have let an unquoted "construction" outrank a cited "operational".
        placeholder = is_placeholder(s)
        try:
            claims = json.loads(s.claims)
        except (TypeError, ValueError):
            log.warning("source %s has unparseable claims JSON; ignoring", s.id)
            continue
        if not isinstance(claims, dict):
            continue
        unconfirmed = {f.strip() for f in (s.unconfirmed_fields or "").split(",") if f.strip()}
        for name, value in claims.items():
            if name not in WRITABLE_FIELDS or value is None:
                continue
            out.setdefault(name, []).append(
                _Claim(
                    value,
                    0 if placeholder else _weight(s.source_type),
                    s.fetched_at,
                    s.source_type,
                    s.url,
                    confirmed=not placeholder and name not in unconfirmed,
                )
            )
    # Confirmed first, then strongest source, then most recently fetched. `url` is
    # the final tiebreaker so the ordering is total and therefore reproducible —
    # without it, two equally-weighted same-timestamp sources could resolve
    # differently between runs and break idempotence.
    #
    # `confirmed` leads because a quote-backed value must never be displaced by a
    # 待确认 one, however authoritative or recent that source is.
    for claims_list in out.values():
        claims_list.sort(
            key=lambda c: (c.confirmed, c.weight, c.fetched_at or _EPOCH, c.url), reverse=True
        )
    return out


def _coerce_like(value: Any, template: Any) -> Any:
    """Bring a JSON-round-tripped claim back to the column's Python type."""
    if isinstance(template, _dt.datetime) and isinstance(value, str):
        return _dt.datetime.fromisoformat(value)
    if isinstance(template, _dt.date) and isinstance(value, str):
        return _dt.date.fromisoformat(value)
    return value


def resolve_field(field_name: str, claims: list[_Claim], existing: Any) -> Any:
    """Public alias for :func:`_resolve`.

    `tracker logic check` has to report which of two conflicting claims the database
    keeps, and the only way to report that faithfully is to ask the same function
    the write path asks. Re-deriving it produced a report that disagreed with the
    stored value on 73 rows.
    """
    return _resolve(field_name, claims, existing)


def _resolve(field_name: str, claims: list[_Claim], existing: Any) -> Any:
    """Apply the *field's* policy to choose one value. Thin wrapper over `resolve`."""
    return resolve(FIELD_POLICY.get(field_name, Policy.PREFER_WEIGHT), claims, existing)


def resolve(
    policy: Policy,
    claims: list[_Claim],
    existing: Any,
    *,
    rank: dict[str, int] | None = None,
    terminal: tuple[str, ...] = PHASE_TERMINAL,
    default: Any = None,
) -> Any:
    """Apply a merge policy to choose one value.

    Split out from `_resolve` so `capacity_block` fields go through this engine
    rather than a second copy of it. Blocks need the same discipline fields need —
    most of all the 待确认 rule below — and two implementations of "confirmed
    first, then weight, then recency" would drift.

    `rank`/`terminal`/`default` parameterise the LADDER policy, so the project
    `phase` progression and a block's own status progression share one
    implementation.

    Unconfirmed (待确认) claims are discarded outright whenever any confirmed claim
    exists for the field. Done here, once, rather than inside each policy: MAX and
    MIN scan every claim and LADDER takes the furthest-along, so any of the three
    would otherwise let an unquoted value beat a quoted one.
    """
    if not claims:
        return existing
    if any(c.confirmed for c in claims):
        claims = [c for c in claims if c.confirmed]

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
        return _resolve_ladder(
            claims,
            existing,
            rank=rank or _PHASE_RANK,
            terminal=terminal,
            default=default if default is not None else DEFAULT_PHASE,
        )

    # PREFER_WEIGHT: claims are pre-sorted by (weight, recency).
    return claims[0].value


def _resolve_ladder(
    claims: list[_Claim],
    existing: Any,
    *,
    rank: dict[str, int],
    terminal: tuple[str, ...],
    default: Any,
) -> Any:
    """Furthest along a progression, but a terminal state always wins.

    A project a newer source says is cancelled is cancelled, even though
    "operational" sits further along. Paused/cancelled are statements about the
    thing stopping, not about its degree of completion.

    Parameterised so a block's status ladder reuses it: same argument, different
    rungs.
    """
    stopped = [c for c in claims if c.value in terminal]
    if stopped:
        # Claims are sorted strongest-and-newest first.
        return stopped[0].value
    ranked = [c.value for c in claims if c.value in rank]
    if existing in rank:
        ranked.append(existing)
    if not ranked:
        return existing or default
    return max(ranked, key=lambda p: rank[p])


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
        # The same 待确认 rule `resolve` applies, and for the same reason: a claim
        # the engine discarded outright is not a rival, and reporting it as one
        # describes a contest that never happened. Observed live on Fairwater (#1),
        # whose notes credited a placeholder URL as the "higher-weighted" side of a
        # phase conflict — a source that does not exist, disputing nothing.
        if any(c.confirmed for c in claims):
            claims = [c for c in claims if c.confirmed]
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


def record_tag(urls: list[str]) -> str:
    """Short stable id for the set of sources one ingest record carries.

    Used to scope that record's note lines so re-ingesting it *replaces* its own
    disclosures instead of adding another variant beside them.
    """
    joined = "\n".join(sorted(urls))
    return hashlib.sha1(joined.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def _merge_notes(
    existing: str | None,
    derived: list[str],
    contributed: list[str],
    *,
    tag: str | None,
    preserve_derived: tuple[str, ...] = (),
) -> str | None:
    """Rebuild the notes block from three kinds of line.

    * **Operator prose** — no marker at all. Never touched.
    * **Derived** (:data:`NOTE_PREFIX`) — a pure function of the current claims,
      so it is regenerated wholesale. That is what makes a resolved conflict's
      disclosure *disappear* rather than linger forever.
    * **Contributed** (``[source:<tag>]``) — a statement about one ingest record's
      sources, e.g. "this MW figure is generator nameplate". Lines carrying *this*
      record's tag are replaced; lines from other records are preserved.

    The tag is what makes both halves of that work. Contributed lines cannot be
    regenerated wholesale: when two records resolve to one project (two queue rows
    for one site), each carries its own disclosure, and rebuilding would let them
    erase each other and churn `updated_at` on every run forever. But they cannot
    simply accumulate either — re-extracting an article whose wording changed then
    leaves the stale variant sitting next to the new one, which was observed live:
    a corrected "dropped value" note appeared beside the wrong one it replaced.

    Scoping by record gives the right answer to both.

    `tag=None` means *no ingest record is in scope* — nothing contributed is
    superseded. `recompute_from_sources` calls it that way: it re-derives a row
    from the citations it already holds, without an incoming record.

    `preserve_derived` names derived lines the caller cannot regenerate, by the
    text following the marker. Without it a caller holding only *some* of the
    derived families would silently delete the others, since derived lines are
    rebuilt wholesale. See :data:`_INGEST_ONLY_NOTES`.
    """
    mine = f"{SOURCE_NOTE_PREFIX}[{tag}]" if tag is not None else None
    human: list[str] = []
    other_contributed: list[str] = []
    kept_derived: list[str] = []
    for raw in (existing or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if mine is not None and line.startswith(mine):
            continue  # this record's own, superseded by `contributed`
        if line.startswith(SOURCE_NOTE_PREFIX):
            other_contributed.append(line)
        elif line.startswith(NOTE_PREFIX):
            body = line[len(NOTE_PREFIX) :].lstrip()
            if any(body.startswith(prefix) for prefix in preserve_derived):
                kept_derived.append(line)
            continue  # derived; rebuilt below
        else:
            human.append(line)

    all_contributed = sorted(dict.fromkeys(other_contributed + contributed))
    combined = human + sorted(dict.fromkeys(kept_derived + derived)) + all_contributed
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
    * **Same site, different party.** One campus routinely has three companies
      attached — one builds it, one leases it, one occupies it — and each spelling
      produces its own key, so the site is stored several times over. Measured:
      the Abilene Stargate campus existed four times, as Crusoe, as Oracle, as
      OpenAI and as "OpenAI/Oracle", each carrying the same 1.2 GW. Every key was
      correct and the building was one. This case does not share a company prefix
      with anything, so it needs its own query. See
      `dedup.looks_like_the_same_site` for why locality alone is not the test.
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

    # Same locality, different company. Scoped by the locality half of the key so
    # this stays one indexed-ish scan rather than a comparison against every row.
    locality_suffix = "|" + key.split("|", 1)[1]
    neighbours = session.scalars(
        select(Project).where(Project.dedup_key.like("%" + locality_suffix))
    ).all()
    for row in neighbours:
        if row.dedup_key == key:
            continue
        if looks_like_the_same_site(
            payload.get("name"),
            payload.get("company"),
            row.name,
            row.company,
            locality=payload.get("city") or payload.get("county"),
        ):
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
    routed_from: str | None = None

    if project is None and not force_new:
        # A merged-away identity. `tracker merge` records each folded row's key in
        # `project_alias`, and consulting it here is what makes a merge outlive
        # the next crawl: an article written from the folded company's angle
        # produces the folded key, and without this it would re-create the row
        # the operator just deleted. FILL_ONLY identity policies keep the routed
        # record from rewriting the survivor's name, company or locality.
        alias = session.scalar(select(ProjectAlias).where(ProjectAlias.from_dedup_key == key))
        if alias is not None:
            project = session.get(Project, alias.to_project_id)
            if project is not None:
                routed_from = key
                log.info("routing %s to project #%d per project_alias", key, project.id)

    if project is None:
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

    # A possible duplicate is a fact about the CURRENT state of the database, not
    # about this particular run, so it is recomputed on every upsert rather than
    # recorded once at insert time. Two consequences, both wanted: re-ingesting a
    # record does not erase its own duplicate warning (the ambiguity has not gone
    # away just because the row already exists), and the warning disappears on its
    # own once an operator resolves it.
    candidate = None if force_new else _find_duplicate_candidate(session, key, payload)
    # A routed record's own survivor is not a duplicate of itself: the candidate
    # scan works on the arriving key, which is not the survivor's, so without the
    # id check an aliased record would flag — and confidence-cap — its own row.
    if candidate is not None and candidate.id != project.id:
        duplicate_of = candidate.id

    before = _snapshot(project)
    # Captured before the recompute so a slip can be measured against it.
    expected_online_before = project.expected_online

    # --- Write the citations ------------------------------------------------
    existing_sources = {s.url: s for s in project.sources}
    for sr in rec.sources:
        claims = {k: claim_value(v) for k, v in sr.tracked_claims().items()}
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
        # `fields` lists only what a verbatim quote supports. Everything that reads
        # it — confidence scoring, the traceability test, the "9 of 12" count —
        # therefore keeps counting facts, not 待确认 candidates.
        row.fields = derive_fields(sr.confirmed_claims())
        row.unconfirmed_fields = derive_fields(
            {k: v for k, v in claims.items() if k in sr.unconfirmed}
        )
        # Restricted to fields actually claimed, so the reason map cannot outlive
        # the value it explains. Sorted for the same byte-identical re-ingest
        # reason as `claims`.
        why = {k: v for k, v in dict(sr.unconfirmed_reasons).items() if k in claims}
        row.unconfirmed_reasons = (
            json.dumps(why, sort_keys=True, ensure_ascii=False) if why else None
        )
        # Sorted so a re-ingest of the same article writes byte-identical JSON and
        # the idempotence test keeps holding, exactly as `claims` above.
        row.quotes = (
            json.dumps(sr.quotes, sort_keys=True, ensure_ascii=False) if sr.quotes else None
        )
        # Ordered by block key, for the same reason `claims` is sorted: a re-ingest
        # of the same article must write byte-identical JSON or the idempotence
        # test stops holding.
        row.blocks = (
            json.dumps(
                [b.as_json() for b in sorted(sr.blocks, key=lambda b: b.label.lower())],
                ensure_ascii=False,
            )
            if sr.blocks
            else None
        )
        row.extractor = sr.extractor
        # fetched_at is only advanced, never rewound: a cached re-read must not
        # make an old citation look newer than a genuinely newer one.
        if row.fetched_at is None or sr.fetched_at > row.fetched_at:
            row.fetched_at = sr.fetched_at
    session.flush()

    # --- Recompute every field from all claims ------------------------------
    by_field = claims_by_field(list(project.sources))
    for name in WRITABLE_FIELDS:
        if name in DERIVED_FIELDS:
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

    # Blocks first, then the rollup, then h200 — so `apply_h200_equivalent` sees a
    # `mw_built` that already reflects the energised tranches.
    from tracker import blocks as blocks_mod

    blocks_written = blocks_mod.rebuild(session, project)
    block_notes = blocks_mod.reconcile(project)
    apply_h200_equivalent(project, by_field)

    # --- Notes: conflicts, path disclosures, duplicate proposals ------------
    derived, conflict_fields = _conflict_notes(by_field)
    # Path disclosures and duplicate proposals are contributed, not derived: they
    # are facts about a particular citation or a particular unresolved ambiguity,
    # and must survive a later record for the same project.
    if duplicate_of is not None:
        # Derived, not contributed: recomputed from current state every run.
        derived.append(
            f"{NOTE_PREFIX} possible duplicate of project #{duplicate_of}: same company "
            "and state, locality differs only by city/county granularity. Confirm or reject in "
            "`tracker review`."
        )
    if routed_from is not None:
        derived.append(
            f"{NOTE_PREFIX} a record arriving as `{routed_from}` was routed here: that "
            "identity was merged into this row, and `project_alias` remembers the decision."
        )

    tag = record_tag([s.url for s in rec.sources])
    marker = f"{SOURCE_NOTE_PREFIX}[{tag}]"
    contributed = [f"{marker} {line}" for line in rec.notes]
    derived.extend(f"{NOTE_PREFIX} {line}" for line in block_notes)
    project.notes = _merge_notes(project.notes, derived, contributed, tag=tag)

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

    # --- Risks, and the blocker derived from them ---------------------------
    # After the sources exist, so a risk can cite one; before the change check, so
    # a newly-derived blocker counts as a change.
    risks_written = _upsert_risks(session, project, rec)
    # After the risks exist, so a measured slip can attach to the obstacle that
    # best explains it; before the blocker is derived, since neither depends on
    # the other but the ordering should read in the direction the data flows.
    events_written += _record_slippage(session, project, expected_online_before)
    project.blocker = _derive_blocker(session, project)

    # --- Did anything actually change? -------------------------------------
    if action == "update":
        if (
            _snapshot(project) == before
            and not events_written
            and not risks_written
            # A run that only added blocks did change the row. Without this it
            # reports `unchanged`, `updated_at` never moves, and the watermark
            # that tells a post-run check what to look at misses it entirely.
            and not blocks_written
        ):
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
        risks_written=risks_written,
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
            row = Event(
                project_id=project.id,
                event_date=ev.event_date,
                event_type=ev.event_type,
                description=ev.description,
                source_id=source_id,
            )
            session.add(row)
            # Registered immediately, because ONE record can carry two events
            # with the same (type, date) and the map above only knows what was
            # already in the table. Without this the second one is added too and
            # the flush below dies on uq_event_project_type_date, taking the whole
            # run with it. Seen live on an SEC filing that listed two capacity
            # expansions dated the same day — an article rarely does, which is why
            # this survived until filings became a source.
            existing[(ev.event_type, ev.event_date)] = row
            inserted += 1
        else:
            found.description = ev.description
            if source_id is not None:
                found.source_id = source_id
    session.flush()
    return inserted


def _upsert_risks(session: Session, project: Project, rec: IngestRecord) -> int:
    """Write obstacles, deduplicating on (category, first_seen) per the schema.

    Returns the number of rows inserted; updating an existing risk does not count as
    a write, so a re-ingest still reports `unchanged`.

    Deduplication happens here rather than being left to the UNIQUE constraint
    because `first_seen` is nullable and SQLite treats NULLs as distinct — two runs
    of the same undated risk would insert two rows. A Python dict keyed on
    ``(category, first_seen)`` handles ``None`` correctly, which is what makes
    re-ingest idempotent for undated obstacles.

    A risk is never deleted here. An article that has stopped mentioning an obstacle
    is not evidence the obstacle is gone, so clearing one is an operator action
    (`tracker review`) or a later source explicitly reporting the resolution.
    """
    if not rec.risks:
        return 0
    url_to_id = {s.url: s.id for s in project.sources}
    # Queried rather than read off `project.risks`, for the same reason as events:
    # rows added earlier in this session may not be on the relationship yet, and a
    # stale view here re-inserts and trips the UNIQUE constraint.
    existing = {
        (r.category, r.first_seen): r
        for r in session.scalars(select(Risk).where(Risk.project_id == project.id)).all()
    }
    inserted = 0
    for risk in rec.risks:
        source_id = url_to_id.get(risk.source_url) if risk.source_url else None
        found = existing.get((risk.category, risk.first_seen))
        if found is None:
            row = Risk(
                project_id=project.id,
                category=risk.category,
                severity=risk.severity,
                status=OPEN_RISK_STATUS,
                summary=risk.summary,
                quote=risk.quote,
                first_seen=risk.first_seen,
                delay_days=risk.delay_days,
                source_id=source_id,
                unconfirmed=risk.unconfirmed,
            )
            session.add(row)
            # Registered immediately, for the same reason as in `_upsert_events`:
            # the map only knows what was already stored, so two same-key risks in
            # one record would both insert and trip the UNIQUE constraint. The
            # crawl path happens to dedup its own risks before getting here, but
            # `ingest manual` does not, and this write path should not depend on
            # every caller remembering.
            existing[(risk.category, risk.first_seen)] = row
            inserted += 1
        else:
            # Re-reading an edited article updates the wording and the severity, but
            # never revives a risk an operator resolved: `status` is theirs, not the
            # extractor's.
            found.severity = risk.severity
            found.summary = risk.summary
            # A confirmed reading never loses to a later unconfirmed one. Two
            # sources report the same obstacle and only one quotes it usably; the
            # citation is the thing worth keeping, so an unconfirmed re-read
            # refreshes the wording without demoting an obstacle already evidenced.
            if risk.unconfirmed is None or found.unconfirmed is not None:
                found.quote = risk.quote
                found.unconfirmed = risk.unconfirmed
            if risk.delay_days is not None:
                found.delay_days = risk.delay_days
            if source_id is not None:
                found.source_id = source_id
    session.flush()
    return inserted


def _record_slippage(session: Session, project: Project, previous: _dt.date | None) -> int:
    """Record `expected_online` moving later. Returns events written.

    Why an event and not a changed merge policy: `expected_online` stays on
    PREFER_WEIGHT, so the strongest source still wins the *value*. Switching to
    newest-wins to make slips visible would throw away the source-quality ordering
    everywhere else. Recording the movement as history keeps both.

    **`delay_days` is only attributed across a year boundary.** The column stores no
    precision, and `norm_date_detail` coarsens hedged dates into it: a bare "2027"
    lands on 2027-01-01 and "late 2027" on 2027-10-01, so a source merely restating
    the same year more precisely is indistinguishable from a 273-day delay. Every
    coarsening stays inside the stated year, so a move into a later year cannot be a
    precision artefact and a move within one year might be. The event is written
    either way — the tracked value did move, and that is a fact worth logging — but
    a number is only attached when it means something.

    No risk is invented when none is open. A date moving is not a report of *why*,
    and manufacturing an obstacle from it would put an uncited guess into the field
    an operator acts on.
    """
    current = project.expected_online
    if previous is None or current is None or current <= previous:
        return 0

    slipped_days = (current - previous).days
    across_years = current.year > previous.year
    detail = (
        f"expected_online moved from {previous} to {current} (+{slipped_days} days)"
        if across_years
        else (
            f"expected_online moved from {previous} to {current} within {previous.year}; "
            "this may be a more precise restatement rather than a delay"
        )
    )

    written = 0
    # Keyed on the NEW target date, so the (project, type, date) unique constraint
    # gives one row per revised timeline rather than one per run.
    existing = session.scalar(
        select(Event).where(
            Event.project_id == project.id,
            Event.event_type == "delayed",
            Event.event_date == current,
        )
    )
    if existing is None:
        session.add(
            Event(
                project_id=project.id,
                event_date=current,
                event_type="delayed",
                description=detail,
            )
        )
        written = 1
    else:
        existing.description = detail

    if across_years:
        open_risks = session.scalars(
            select(Risk)
            .where(Risk.project_id == project.id, Risk.status == OPEN_RISK_STATUS)
            .order_by(Risk.id.asc())
        ).all()
        if open_risks:
            # The most severe open obstacle is the one already presented as this
            # project's blocker, so it is where a measured slip belongs.
            worst = max(open_risks, key=risk_precedence)
            worst.delay_days = slipped_days

    session.flush()
    return written


def _derive_blocker(session: Session, project: Project) -> str | None:
    """`project.blocker` = the most severe open risk's summary, else NULL.

    Derived rather than merged, which is what finally lets an obstacle be *cleared*:
    the old column went through `_resolve`, and that returns the existing value when
    a field has no claims, so a blocker could be replaced but never set back to NULL.

    Ties break on the lowest id, so the value is stable across runs rather than
    depending on which row the database happened to return first.
    """
    open_risks = session.scalars(
        select(Risk)
        .where(Risk.project_id == project.id, Risk.status == OPEN_RISK_STATUS)
        .order_by(Risk.id.asc())
    ).all()
    if not open_risks:
        return None
    return max(open_risks, key=risk_precedence).summary


def apply_h200_equivalent(project: Project, by_field: dict[str, list] | None = None) -> None:
    """Set the accelerator count: a cited chip count if there is one, else derived.

    Recomputed on every write rather than remembered, because it is a restatement
    of the capacity rather than an independent fact. A row whose megawatts move
    and whose accelerator count does not would be quietly self-contradictory, and
    `logic check` would be right to flag it.

    The derivation prefers *built* capacity where there is any, because that is
    the compute a site actually has today; planned capacity answers a different
    question and is the fallback. A row with neither, and no cited count, keeps
    null — a site nobody has sized gets no number rather than a zero that would be
    summed.
    """
    from tracker.compute import h200_equivalent

    cited = _resolve("h200_equivalent", (by_field or {}).get("h200_equivalent", []), None)
    if cited is not None:
        try:
            project.h200_equivalent = int(cited)
            return
        except (TypeError, ValueError):
            pass  # fall through to the derivation rather than storing rubbish

    basis = project.mw_built if project.mw_built else project.mw_planned
    project.h200_equivalent = h200_equivalent(basis)


def recompute_from_sources(session: Session, project: Project) -> list[str]:
    """Re-derive every field of one project from the citations it now holds.

    Returns the key fields where sources materially disagree.

    Extracted from :func:`upsert_record` so `tracker merge` gets the *same* merge
    policy rather than a second implementation of it. Folding two rows together is
    exactly the situation the recompute-from-claims design was built for: after
    the sources move, the surviving row's values are whatever the full set of
    citations now supports, computed by the declared per-field policy — not the
    values either row happened to be carrying.

    The derived notes are rewritten here too. They used to be computed and thrown
    away, which left a row's *disclosures* describing the claim set it had before
    the recompute: after a `tracker merge` or a `logic resolve` repair, values
    moved and the prose explaining them did not. Only the two ingest-only families
    survive untouched — see :data:`_INGEST_ONLY_NOTES`.
    """
    by_field = claims_by_field(list(project.sources))
    for name in WRITABLE_FIELDS:
        if name in DERIVED_FIELDS:
            continue
        current = getattr(project, name)
        chosen = _resolve(name, by_field.get(name, []), current)
        if chosen is not None:
            chosen = _coerce_like(chosen, current if current is not None else _template_for(name))
        if name in {"state", "country"} and isinstance(chosen, str):
            chosen = chosen.upper()
        if chosen != current:
            setattr(project, name, chosen)
    if project.phase is None:
        project.phase = DEFAULT_PHASE

    from tracker import blocks as blocks_mod

    blocks_mod.rebuild(session, project)
    block_notes = blocks_mod.reconcile(project)
    apply_h200_equivalent(project, by_field)

    derived, conflict_fields = _conflict_notes(by_field)
    derived.extend(f"{NOTE_PREFIX} {line}" for line in block_notes)
    project.notes = _merge_notes(
        project.notes, derived, [], tag=None, preserve_derived=_INGEST_ONLY_NOTES
    )

    views = [conf.SourceView.from_row(s) for s in project.sources]
    populated = sum(1 for f in TRACKED_FIELDS if getattr(project, f, None) is not None)
    project.confidence = conf.compute(
        views,
        operator_verified=project.last_verified_at is not None,
        populated_tracked_fields=populated,
    ).value
    project.blocker = _derive_blocker(session, project)
    session.flush()
    return conflict_fields


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


def recompute_blocks(session: Session) -> int:
    """Rebuild every project's blocks from its sources. Returns rows changed.

    Blocks are a cache of `source.blocks`, the same status `confidence` and
    `h200_equivalent` have, so they inherit the same obligation: `tracker init`
    recomputes them, and `test_blocks_cache_is_consistent` asserts a second pass is
    a no-op. If running it twice keeps changing rows then either the rebuild is not
    a pure function of what is stored, or `init` is reporting churn that is not
    real.
    """
    from tracker import blocks as blocks_mod

    changed = 0
    for project in session.scalars(select(Project)).all():
        touched = blocks_mod.rebuild(session, project)
        notes = blocks_mod.reconcile(project)
        if touched or notes:
            changed += 1
    session.flush()
    return changed


def recompute_h200(session: Session) -> int:
    """Restate every project's capacity as accelerators. Returns rows changed.

    Same reasoning as `recompute_confidence`: a cache of a pure function, which
    drifts when the function's inputs change. Here that includes a *setting* —
    `kw_per_h200` — so re-basing the whole table is running this rather than
    writing a migration.

    Idempotent, and `test_h200_cache_is_consistent` asserts it is a no-op on a
    database that is already current.
    """
    changed = 0
    for project in session.scalars(select(Project)).all():
        before = project.h200_equivalent
        apply_h200_equivalent(project, claims_by_field(list(project.sources)))
        if project.h200_equivalent != before:
            changed += 1
    session.flush()
    return changed


__all__ = [
    "DERIVED_FIELDS",
    "FIELD_POLICY",
    "NOTE_PREFIX",
    "Policy",
    "UpsertResult",
    "apply_h200_equivalent",
    "claim_value",
    "claims_by_field",
    "derive_fields",
    "is_placeholder",
    "recompute_blocks",
    "recompute_confidence",
    "recompute_from_sources",
    "recompute_h200",
    "record_tag",
    "resolve_field",
    "upsert_record",
]
