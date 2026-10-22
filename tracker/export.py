"""Export the database to Markdown, CSV or JSON.

Determinism is the design constraint. These files get diffed, re-generated and
pasted into chat, so running the same export twice on unchanged data must produce
byte-identical output:

* a fixed ``ORDER BY``, never the database's natural order;
* a frozen CSV header tuple, because downstream consumers depend on column order;
* ``lineterminator="\\n"`` — Python's csv module writes ``\\r\\n`` by default, and
  on Windows that becomes ``\\r\\r\\n``;
* ``sort_keys=True`` for JSON;
* no timestamp inside the payload unless the caller passes one in.

The renderers are pure functions of rows to strings, so the golden-file tests do
not need a database at all.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session, selectinload

from tracker import blocks as blocks_mod
from tracker import dedup, delays
from tracker import facts as facts_mod
from tracker import parties as parties_mod
from tracker.models import Project
from tracker.upsert import NOTE_PREFIX, SOURCE_NOTE_PREFIX
from tracker.vocab import bound_from_quote

if TYPE_CHECKING:
    from tracker.gaps import Said

#: CSV columns, frozen. Appending is safe for consumers; reordering or removing
#: is not, so a test asserts this tuple exactly.
CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "company",
    "name",
    "customer",
    "city",
    "county",
    "state",
    "country",
    "phase",
    "mw_planned",
    "mw_built",
    "investment_usd",
    "first_announced",
    "expected_online",
    "blocker",
    "confidence",
    "sources",
    "source_urls",
    "last_verified_at",
    # Appended, not slotted in beside `blocker` where it reads better: this tuple
    # is a positional contract, and moving `confidence` along by one would break a
    # consumer indexing into the row.
    "risks",
    # Appended for the same reason. `mw_*_basis` says which kind of megawatt the
    # column two places left of it holds — the computing load, the whole site's
    # draw, or a generator's rating — and two rows' capacities are only comparable
    # when they agree. Empty means no citation recorded one.
    "mw_planned_basis",
    "mw_built_basis",
    "parties",
)

#: Schema tag on JSON exports, so a downstream consumer can detect a change.
#: Bumped to 2 when `risks` was added to both formats — appending a CSV column is
#: safe for consumers, but a reader keying on the tag should still be able to tell.
#: 4 adds per-field `prov` (tier + the sentence behind the value) and `quotes` on
#: each source. Both are additive; `basis` is unchanged for readers keying on it.
#: 5 adds `sources[].id` and `claims_by_field` — every claim any citation made for
#: a field, in the merge engine's own order, with the winner marked. Also additive.
#: 6 adds `sources[].published_at`, which existed on the row but was only exposed
#: inside `claims_by_field`. Additive.
#: 7 adds `parties` — who plays which role on the site, with the sentence behind
#: each role — and `mw_planned_basis`/`mw_built_basis`, which say WHICH KIND of
#: megawatt a capacity figure is. Both additive, and the second is the one to read
#: before comparing two campuses' capacities: they are only comparable when the
#: bases agree.
#: 8 adds `claims_by_field[].claims[].tertiary` — true for a directory, wiki or
#: digest, whose claim only fills a field nothing first-hand states. Additive.
#: 9 adds `claims_by_field[].claims[].part` — true for a capacity or investment
#: figure its own sentence gives to one building or phase. Additive.
#: 10 adds `suspected_delay` — null, or why a campus whose online date has passed
#: reads as late — and `sections[].overdue`. Additive. Both are relative to the day
#: the file was written, which `suspected_delay.as_of` states. It also adds
#: `parties[].first_cited` (when a citation first named the party) and
#: `accounting.delivering` / `accounting.live_mw` (the campus's running figure,
#: and what its live tranches describe). Additive.
#: 11 adds how a campus's tranches nest: `sections[].within` (the section it is part
#: of), `sections[].relation` (`same_as`, `superseded` or `elsewhere` for a section set
#: aside), `sections[].ruled_by`, `sections[].ruling` and `sections[].tranche` (the
#: tranche key a ruling on the section names), and three residual reasons
#: in `accounting` — `same_as`, `superseded`, `elsewhere` — and `accounting.by_state`,
#: the counted capacity by tranche state. Additive.
#: 12 adds `company_key`, the company the operator is filed under — the key of the
#: company registry (migration 0043), the same as `parties[].key`, and
#: `parties[].ended_on` / `parties[].ended_reason` for a role that has ended — a
#: former tenant (migration 0044). Additive.
JSON_SCHEMA_TAG = "tracker/12"

FORMATS = ("md", "csv", "json", "html")


@dataclass(frozen=True)
class ExportFilter:
    company: str | None = None
    state: str | None = None
    phase: str | None = None
    min_confidence: int | None = None

    def apply(self, stmt):
        from sqlalchemy import func

        if self.company:
            stmt = stmt.where(func.lower(Project.company).like(f"%{self.company.lower()}%"))
        if self.state:
            stmt = stmt.where(Project.state == self.state.upper())
        if self.phase:
            stmt = stmt.where(Project.phase == self.phase)
        if self.min_confidence is not None:
            stmt = stmt.where(Project.confidence >= self.min_confidence)
        return stmt


def fetch_projects(
    session: Session,
    flt: ExportFilter | None = None,
    *,
    only: Collection[int] | None = None,
) -> list[Project]:
    """Projects in a stable order, with sources, events and risks eagerly loaded.

    The ordering is by content, not by id, so inserting a project does not
    reshuffle the whole export and produce a noisy diff.

    `only` restricts it to a set of ids, for the console's paged table: that
    request wants thirty rows hydrated exactly as the export hydrates them, and
    loading four hundred to discard all but thirty is the cost server-side paging
    exists to remove. It goes through this function rather than beside it so the
    eager-loading list above stays the only one — a second copy would lazy-load
    whatever relation was added to this one later, silently, one query per row.
    """
    stmt = select(Project).options(
        selectinload(Project.sources),
        selectinload(Project.events),
        selectinload(Project.risks),
        selectinload(Project.blocks),
        selectinload(Project.parties),
        selectinload(Project.nesting),
    )
    if flt is not None:
        stmt = flt.apply(stmt)
    if only is not None:
        if not only:
            return []
        stmt = stmt.where(Project.id.in_(list(only)))
    stmt = stmt.order_by(Project.state, Project.company, Project.name, Project.id)
    projects = list(session.scalars(stmt))
    # And what `prov` and the claim table are read from (`facts.Statements`), in one
    # query for the lot rather than one per campus.
    facts_mod.preload(session, projects)
    return projects


# --- Row shaping ------------------------------------------------------------


def iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return str(value)


def _mw_bound(block: Any) -> str:
    """The hedge a block's own quote puts on its capacity: `exact` when none.

    A block has no `claim_meta`, so unlike a project field there is no stored axis
    to read — but it does store the verbatim sentence, and the hedge is a word in
    that sentence. Deriving it here gives every tranche a bound where the claim
    envelope's axis reached only 32% of project claims.
    """

    if block.mw is None or not block.quotes:
        return "exact"
    try:
        quotes = json.loads(block.quotes)
    except (TypeError, ValueError):
        return "exact"
    if not isinstance(quotes, dict):
        return "exact"
    return bound_from_quote(quotes.get("mw"), block.mw)


def _sorted_risks(project: Project) -> list:
    """Risks in a content-stable order, so exports stay byte-identical."""
    return sorted(project.risks, key=lambda r: (r.category, str(r.first_seen or ""), r.status))


def _risk_cell(project: Project) -> str:
    """Open risks as ``category:severity`` pairs, for the flat formats.

    Only open ones: a resolved obstacle is history, and a flat cell has no room to
    say so without reading as though the project were still blocked.
    """
    return ";".join(
        f"{r.category}:{r.severity}" for r in _sorted_risks(project) if r.status == "open"
    )


def _party_cell(project: Project) -> str:
    """Parties as ``role:name`` pairs, for the flat formats.

    Roles whose evidence the gate refused are marked with a trailing ``?``, so a
    flat cell still distinguishes "an article says Oracle leases it" from "we
    inferred it from the operator column". Losing that distinction is how a
    spreadsheet turns an inference into a fact.
    """
    from tracker.parties import INFERRED_ROLE

    return ";".join(
        f"{p.role}:{p.name}" + ("" if p.unconfirmed is None else "?")
        for p in sorted(project.parties, key=lambda p: (p.role, p.party_key))
        # An inferred role restates `company`/`customer`, which are already their
        # own columns in this row. Repeating them here would pad every cell in the
        # export with what the reader can see two columns to the left.
        if p.unconfirmed != INFERRED_ROLE
    )


def to_row(project: Project) -> dict[str, Any]:
    """One flat dict per project, for CSV and Markdown."""
    urls = sorted(s.url for s in project.sources)
    return {
        "id": project.id,
        "company": project.company,
        "name": project.name,
        "customer": project.customer,
        "city": project.city,
        "county": project.county,
        "state": project.state,
        "country": project.country,
        "phase": project.phase,
        "mw_planned": project.mw_planned,
        "mw_built": project.mw_built,
        "h200_equivalent": project.h200_equivalent,
        "investment_usd": project.investment_usd,
        "first_announced": iso(project.first_announced),
        "expected_online": iso(project.expected_online),
        "blocker": project.blocker,
        "risks": _risk_cell(project),
        "mw_planned_basis": project.mw_planned_basis,
        "mw_built_basis": project.mw_built_basis,
        "parties": _party_cell(project),
        "confidence": project.confidence,
        "sources": len(urls),
        "source_urls": " ".join(urls),
        "last_verified_at": iso(project.last_verified_at),
    }


def _standing_json(project: Project) -> dict[str, Any]:
    """Per-track position, the binding constraint, and the signal to watch.

    Derived, not stored, so it is computed here rather than read from a column. A
    consumer — a web page, a spreadsheet, another script — should not have to
    reimplement the reasoning in `tracker.tracks` to know where a project stands.
    """
    from tracker.tracks import standing

    stand = standing(project.id, project.events, project.risks)
    binding = stand.binding_blocker
    return {
        "tracks": [
            {
                "track": state.track,
                "status": state.status,
                "reached": list(state.reached),
                "implied": sorted(state.implied),
                "complete": state.complete,
                "blockers": list(state.blockers),
                "blocker_severity": state.blocker_severity,
                # Which obstacle, not just which kind. Every surface could say a
                # track was blocked and none could say by what.
                "blocking_risks": [
                    {"id": rid, "category": cat, "summary": summary}
                    for rid, cat, summary in state.blocking_risks
                ],
                "next_milestone": state.next_milestone,
            }
            for state in stand.tracks
        ],
        "binding_blocker": binding.track if binding else None,
        "watch_for": stand.watch_for,
        "timeline": _timeline_json(project, stand),
    }


def _timeline_json(project: Project, stand: Any) -> list[dict[str, Any]]:
    """The milestones actually reached, one row each, restatements folded in.

    Hyperion holds 72 events including eleven `announced`, eight `groundbreaking`
    and eight `permit_approved` — the same real-world moment recorded once per
    article, each with its own date and quote. `uq_event_project_type_date` dedups
    exact matches, so eight groundbreakings means eight distinct *dates* for one
    milestone. A flat list of all 72 is not a timeline, it is a log.

    So: group by type, take the **earliest confirmed** event as the milestone and
    count the rest as restatements. Five tracks by at most three rungs caps this at
    eleven rows. Nothing is deleted — the flat list is still in `events[]` — which
    is what makes this curation rather than suppression.

    Where two *confirmed* events of one type disagree by more than a year, both
    dates are shown and neither is chosen. That is the case LouisianAI gets wrong
    on this very campus, dating the groundbreaking to January 2026 when site work
    began in December 2024. Same discipline as `sections[].capacity_conflict`.
    """
    from tracker.tracks import TRACK_MILESTONES

    by_type: dict[str, list[Any]] = {}
    for event in project.events:
        by_type.setdefault(event.event_type, []).append(event)

    out: list[dict[str, Any]] = []
    for state in stand.tracks:
        for milestone in TRACK_MILESTONES[state.track]:
            if milestone not in state.reached:
                continue
            if milestone in state.implied:
                # A deduction is not a citation, so it carries no date, quote or
                # source — the console already renders implied at half strength.
                out.append(
                    {
                        "track": state.track,
                        "milestone": milestone,
                        "implied": True,
                        "date": None,
                        "quote": None,
                        "source_id": None,
                        "restatements": 0,
                        "conflicting_dates": [],
                    }
                )
                continue

            group = [e for e in by_type.get(milestone, []) if e.event_date]
            confirmed = [e for e in group if not e.unconfirmed] or group
            if not confirmed:
                continue
            confirmed.sort(key=lambda e: e.event_date)
            first = confirmed[0]
            # The RANGE, not every date in it. Listing all of them put eight dates
            # under `announced` on Hyperion, which is a wall rather than a warning:
            # an announcement genuinely is restated over years, and a reader needs
            # to know the dates disagree, not to read all of them. `events[]` still
            # carries every one for anybody who wants them.
            last = confirmed[-1]
            spread = (
                [iso(first.event_date), iso(last.event_date)]
                if (last.event_date - first.event_date).days > 365
                else []
            )
            out.append(
                {
                    "track": state.track,
                    "milestone": milestone,
                    "implied": False,
                    "date": iso(first.event_date),
                    "description": first.description,
                    "quote": first.quote,
                    "unconfirmed": first.unconfirmed,
                    "source_id": first.source_id,
                    "restatements": len(group) - 1,
                    "conflicting_dates": spread,
                }
            )
    return out


def _provenance_json(
    project: Project, said: Said, by_field: dict[str, list]
) -> tuple[dict[str, str], dict[str, Any]]:
    """Per-field tier, and the evidence behind it.

    The PRD's central rule is that a model's answer must not be taken as fact, so a
    machine-readable consumer needs this as much as the terminal does. Without it a
    web page would render a 待确认 guess identically to a quoted figure.

    Two shapes, because they answer different questions and cost different amounts:
    ``basis`` is the flat field -> tier map consumers have keyed on since schema 2
    and is kept unchanged; ``prov`` adds the sentence, whether that sentence is the
    one recorded for this field or the source's whole excerpt, and which citation
    it came from.

    Read from the fact table (`said`, and `by_field` from it), not the citations'
    JSON: the statements a reader downloads are the ones this credits.
    """
    from tracker.gaps import provenance
    from tracker.vocab import WRITABLE_FIELDS

    basis_out: dict[str, str] = {}
    prov_out: dict[str, Any] = {}
    for field in WRITABLE_FIELDS:
        result = provenance(project, field, by_field, said)
        if result is None:
            continue
        basis_out[field] = result.tier
        prov_out[field] = {
            "tier": result.tier,
            "quote": result.quote,
            "quote_is_exact": result.quote_is_exact,
            "source_url": result.source_url,
            "source_index": result.source_index,
        }
        if result.via:
            prov_out[field]["via"] = result.via
        # The claim envelope, only when it says something. Emitted inside `prov`
        # rather than as a sibling map because the axes qualify the value the way
        # the quote does, and every consumer that wants one wants the other —
        # keeping them together is what stops a figure and its hedge being
        # rendered from two different places and drifting apart.
        if result.axes:
            prov_out[field]["axes"] = dict(result.axes)
        # Where no axis recorded a hedge, read the stored quote. The axis reached
        # 32% of claims and `exceeds` was missing from its marker list until that
        # list moved into `vocab`, so rows extracted earlier say `exact` and will
        # until re-read — and Fairwater's `mw_built` rests on "Each exceeds 350 MW".
        # Derived here, not in the page: the browser draws judgements, never makes
        # them.
        if (prov_out[field].get("axes") or {}).get("bound", "exact") == "exact" and result.quote:
            derived = bound_from_quote(result.quote, getattr(project, field, None))
            if derived != "exact":
                prov_out[field].setdefault("axes", {})["bound"] = derived
    return basis_out, prov_out


def _claims_json(
    project: Project,
    said: Said | None = None,
    everything: dict[str, list] | None = None,
) -> dict[str, Any]:
    """Every claim every citation made for a field, with the winner marked.

    `prov` answers "what does this row believe, and on what sentence". This answers
    the question a reader actually asks when a figure looks wrong: **what else did
    anybody say, and why did this one win?**

    Hyperion (#10) is the case. It stored $10B for a campus whose current figure is
    $50B+, and the page showed one number with no way to learn the other existed —
    both claims are in the database, one just lost. LouisianAI, tracking the same
    campus, prints a source-linked claim table beside the headline for exactly this
    reason: the superseded figure stays visible and attributed and non-authoritative.

    Three rules, each of which a re-implementation would get wrong:

    * **The order is the merge engine's own.** `upsert.claims_by_field` sorts by
      `(confirmed, whole site, first-hand, weight, recency, url)` and resolves `merge_by_publication_date`
      centrally. Re-sorting here — in Python or in the browser — would let the page
      disagree with the write path about which claim won, which is the failure
      `logic.check_collisions` was built to avoid.
    * **The winner is resolved, never assumed.** `claims[0]` is the winner only for
      PREFER_WEIGHT fields. `mw_built` takes the largest, `first_announced` the
      earliest, `phase` the furthest along, and the identity fields the first ever
      seen. Ask `resolve_field`.
    * **`decided_by` is only filled when there is a real rival.** Hyperion's $10B,
      $27B and $50B are three *scopes*, not a disagreement, and labelling that
      "credibility won" would be a plausible sentence and the wrong one.

    Every claim, its sentence and its 待确认 reason are read from the fact table
    (`facts.Statements`), not the citations' JSON — the rows `tracker export facts`
    downloads. `said` and `everything` are passed in by a caller that already built
    them for `prov`.
    """
    from tracker.confidence import values_conflict
    from tracker.logic import decision
    from tracker.upsert import (
        DERIVED_FIELDS,
        FIELD_POLICY,
        Policy,
        resolve_field,
    )
    from tracker.vocab import WRITABLE_FIELDS

    by_url = {s.url: s for s in project.sources}
    ordered = sorted(project.sources, key=lambda s: s.url)
    index_of = {s.url: i for i, s in enumerate(ordered)}
    if said is None:
        said = facts_mod.Statements(project)
    if everything is None:
        everything = said.claims_by_field()

    out: dict[str, Any] = {}
    for field in WRITABLE_FIELDS:
        # `blocker` comes from the risk rows and `notes` is assembled, so neither
        # has claims to compare. Reporting them here once made two projects look
        # as though they had drifted from sources that never spoke about them.
        if field in DERIVED_FIELDS:
            continue
        claims = everything.get(field, [])
        if not claims:
            continue

        stored = getattr(project, field, None)
        chosen = resolve_field(field, claims, None)
        winner_seen = False
        rendered: list[dict[str, Any]] = []
        for claim in claims:
            source = by_url.get(claim.url)
            is_winner = not winner_seen and _same_value(claim.value, chosen)
            winner_seen = winner_seen or is_winner
            rendered.append(
                {
                    "value": claim.value,
                    "source_id": getattr(source, "id", None),
                    "source_url": claim.url,
                    "source_index": index_of.get(claim.url),
                    "source_type": claim.source_type,
                    "weight": claim.weight,
                    "confirmed": claim.confirmed,
                    # A directory, wiki or digest: it fills a field nothing
                    # first-hand states and never displaces one that does.
                    "tertiary": claim.tertiary,
                    # One building's or phase's figure, by its own sentence: it fills
                    # the campus column only when nothing describes the whole site.
                    "part": claim.part,
                    "unconfirmed_reason": said.reason(source, field),
                    # The sentence recorded for THIS field only. Never the source's
                    # excerpt: that fallback belongs to `prov`, where it is labelled
                    # `quote_is_exact: false`. Showing a paragraph as one claim's
                    # sentence is the failure the label exists to prevent, so null
                    # is the honest answer.
                    "quote": said.quote(source, field),
                    "fetched_at": iso(claim.fetched_at),
                    "published_at": iso(claim.published_at),
                    "is_winner": is_winner,
                }
            )

        envelope: dict[str, Any] = {
            "policy": FIELD_POLICY.get(field, Policy.PREFER_WEIGHT).value,
            "claims": rendered,
            # The row holds something no claim supports. `logic` reports this as
            # `value_without_evidence`; surfacing it here puts it where a reader
            # looking at the value can see it.
            "stored_unsupported": stored is not None and not winner_seen,
        }

        # The row can hold something other than what the claims resolve to — a
        # campus phase its named buildings lifted after the merge. Said here, so the
        # panel never marks one claim "kept" beside a different stored value without
        # saying why the two differ.
        if stored is not None and chosen is not None and not _same_value(stored, chosen):
            envelope["stored_differs"] = True
            envelope["claims_say"] = chosen

        confirmed = [c for c in claims if c.confirmed] or claims
        rival = next(
            (c for c in confirmed[1:] if values_conflict(confirmed[0].value, c.value)), None
        )
        if rival is not None:
            code, why = decision(
                FIELD_POLICY.get(field, Policy.PREFER_WEIGHT), confirmed[0], rival, confirmed
            )
            envelope["decided_by"] = code
            envelope["why"] = why
        elif len(confirmed) < len(claims):
            # No rival among the quoted claims, but unquoted ones that disagree are
            # not "different scopes" — they were left out for having no usable
            # quote. The panel said the first when it meant the second.
            envelope["unquoted_rivals"] = sum(
                1
                for c in claims
                if not c.confirmed and values_conflict(confirmed[0].value, c.value)
            )
        out[field] = envelope
    return out


def _same_value(a: Any, b: Any) -> bool:
    """Whether a claim is the one the merge chose, across JSON round-tripping."""
    if a is None or b is None:
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return str(a).strip().lower() == str(b).strip().lower()


def to_json_object(project: Project, *, claims: bool = True) -> dict[str, Any]:
    """Nested dict per project, preserving the citation structure.

    `claims=False` omits `claims_by_field`, which is **48% of the console's whole
    payload** — 9.2 MB of 19 MB across 300 projects — and is read by exactly one
    component, the claim table inside a project's drawer, for one project at a
    time. The file export keeps it: a downloaded JSON has no second request to
    make, and the guarantee there is that everything is in the file.

    `basis`, `prov` and `claims_by_field` are read from the fact table (migration
    0045, `facts.Statements`); `sources` is each citation as stored.
    """
    said = facts_mod.Statements(project)
    # Once per project, not once per field: `provenance` needs the whole claim map
    # to answer about one field, and the claim table needs it again.
    by_field = said.claims_by_field()
    basis_map, prov_map = _provenance_json(project, said, by_field)
    first_cited = parties_mod.first_cited(
        list(project.sources), parties_mod.known_keys(object_session(project))
    )
    return {
        "basis": basis_map,
        "prov": prov_map,
        **({"claims_by_field": _claims_json(project, said, by_field)} if claims else {}),
        "standing": _standing_json(project),
        "id": project.id,
        "name": project.name,
        "company": project.company,
        # The company it is filed under (migration 0043), as `parties[].key` is.
        "company_key": dedup.company_key(project.company) or None,
        "customer": project.customer,
        "city": project.city,
        "county": project.county,
        "state": project.state,
        "country": project.country,
        "lat": project.lat,
        "lon": project.lon,
        "mw_planned": project.mw_planned,
        "mw_built": project.mw_built,
        # Which KIND of megawatt each figure is — the computing load, the whole
        # facility's draw, or a generator's rating. Three quantities 30% or more
        # apart that all arrive in this one column, so two campuses' capacities
        # are only comparable when these agree. NULL means no citation recorded
        # one, which covers both "the sentence did not say" and "nothing has read
        # it yet"; `tracker backfill basis` separates them. See `vocab.CLAIM_BASES`.
        "mw_planned_basis": project.mw_planned_basis,
        "mw_built_basis": project.mw_built_basis,
        "h200_equivalent": project.h200_equivalent,
        "investment_usd": project.investment_usd,
        "phase": project.phase,
        "first_announced": iso(project.first_announced),
        "expected_online": iso(project.expected_online),
        # Null, or why the campus reads as late: its own online date has passed —
        # the whole period it names, not merely the day it is stored as — and
        # nothing recorded since says it is running. Decided by `delays.assess`, the
        # same function behind `tracker logic check`'s `past_its_own_date`, so the
        # file and the command cannot disagree. Relative to `as_of` inside it.
        "suspected_delay": _delay_json(project),
        "blocker": project.blocker,
        "notes": project.notes,
        "confidence": project.confidence,
        "dedup_key": project.dedup_key,
        "created_at": iso(project.created_at),
        "updated_at": iso(project.updated_at),
        "last_verified_at": iso(project.last_verified_at),
        "sources": [
            {
                # The row id, so `events[].source_id`, `risks[].source_id`,
                # `blocks[].source_id` and `sections[].source_ids` can be resolved
                # to a citation. All four have shipped as DB ids since they existed
                # and nothing on the page could look one up, which is why a
                # milestone's own source has never been reachable from it.
                "id": s.id,
                "url": s.url,
                "source_type": s.source_type,
                "fetched_at": iso(s.fetched_at),
                # When the PUBLISHER published it, as against when we visited.
                # Carried on the row since migration 0014 and, until now, only
                # reachable inside `claims_by_field` — so a page listing citations
                # could show the crawl date and nothing else, which is the exact
                # confusion `backfill dates` exists to remove.
                "published_at": iso(s.published_at),
                "excerpt": s.excerpt,
                "fields": s.fields,
                "unconfirmed_fields": s.unconfirmed_fields,
                "claims": json.loads(s.claims) if s.claims else None,
                # Parallel to `claims`: the same field names, mapped to the
                # sentence that got each value through the evidence gate. NULL for
                # every citation stored before migration 0007.
                "quotes": json.loads(s.quotes) if s.quotes else None,
                "extractor": s.extractor,
            }
            for s in sorted(project.sources, key=lambda s: s.url)
        ],
        "events": [
            {
                "event_date": iso(e.event_date),
                "event_type": e.event_type,
                "description": e.description,
                # The sentence the milestone stands on, and why there is none.
                # NULL `unconfirmed` means the gate verified the quote — a claim
                # that is only ever true for rows written after migration 0017,
                # because the backfill marked every older one `no_quote`.
                "quote": e.quote,
                "unconfirmed": e.unconfirmed,
                "source_id": e.source_id,
            }
            for e in sorted(project.events, key=lambda e: (e.event_date, e.event_type))
        ],
        "risks": [
            {
                "category": r.category,
                "severity": r.severity,
                "status": r.status,
                # `summary` may be a paraphrase; `quote` is the verified verbatim
                # sentence. A consumer that needs evidence wants the quote.
                "summary": r.summary,
                "quote": r.quote,
                "first_seen": iso(r.first_seen),
                "resolved_at": iso(r.resolved_at),
                "delay_days": r.delay_days,
                "source_id": r.source_id,
            }
            for r in _sorted_risks(project)
        ],
        # A consumer asking "what state is this campus in" cannot answer from
        # `phase` and `mw_planned` alone once a campus is partly live, which most
        # now are. `mw_counted` says whether this tranche's capacity is inside
        # `mw_planned` above or excluded as 待确认 — without it the numbers appear
        # not to add up.
        "blocks": [
            {
                "block_key": b.block_key,
                "label": b.label,
                "parent": b.parent,
                "generic": bool(b.generic),
                "mw": b.mw,
                "mw_counted": blocks_mod.mw_is_confirmed(b),
                # The hedge the source put on this capacity, read from the stored
                # (verbatim) quote. Computed here rather than in the page for the
                # same reason every other judgement is: two implementations of one
                # rule eventually disagree and nothing tells you when. Fairwater's
                # 350 MW rests on "Each exceeds 350 MW", so a bare "350" reports a
                # floor as a point value.
                "mw_bound": _mw_bound(b),
                "status": b.status,
                "customer": b.customer,
                "expected_online": iso(b.expected_online),
                "energized_on": iso(b.energized_on),
                "investment_usd": b.investment_usd,
                "quotes": json.loads(b.quotes) if b.quotes else None,
                "unconfirmed_fields": b.unconfirmed_fields,
                "source_id": b.source_id,
            }
            for b in sorted(project.blocks, key=lambda b: b.block_key)
        ],
        # Who plays which role on this site. `company` and `customer` above are
        # two slots for what is routinely four or five parties — one builds it,
        # one owns it, one occupies it, one sells it power — which is why one
        # campus used to arrive as several rows, once per company named.
        #
        # `unconfirmed` is the field to read before trusting a role. NULL means a
        # sentence in the article says this company plays this role, and the
        # sentence is in `quote`. `role_inferred` means nobody said it: the party
        # is what a citation's own `company` or `customer` claim *means*, so it
        # carries no quote and `capex` will not attribute on it. Anything else is
        # a refusal — the gate read a sentence and would not let it license the
        # role — and those are worth a reader's attention.
        #
        # Sorted by (role, name) so a re-export of an unchanged row is byte-equal.
        "parties": [
            {
                "name": p.name,
                # The normalised identity, so a consumer can group Meta with
                # Facebook and AWS with Amazon without reimplementing the folding.
                "key": p.party_key,
                "role": p.role,
                "quote": p.quote,
                "unconfirmed": p.unconfirmed,
                "source_id": p.source_id,
                # When one of this row's citations first named the party, so the
                # page can mark the ones that are news. See `parties.first_cited`.
                "first_cited": iso(first_cited.get(p.party_key)),
                # A role that has ended (migration 0044): a former tenant. Null for
                # a current party. Never counted as a buyer.
                "ended_on": iso(p.ended_on),
                "ended_reason": p.ended_reason,
            }
            for p in sorted(project.parties, key=lambda p: (p.role, p.party_key))
        ],
        # Sent rather than recomputed in the page. The console could add these up
        # itself, and then there would be two definitions of "what is in the campus
        # total" free to disagree — the objection `webui/dataset.py` opens with.
        "accounting": _accounting_json(project),
        # The campus as its real subdivisions, one entry per section however many
        # sources named it. This is the shape a reader wants — "Building 2, under
        # construction, delivering 0 of 150 MW" — and the flat tranche list could
        # not give it: identity is the primary key of a block and state is one of
        # its attributes, where the list was ordered by state and grouped by
        # evidence tier. Computed here because deciding that `Area II` is
        # `Building 2` is a judgement, and the browser draws judgements rather than
        # making them.
        "sections": _sections_json(project),
    }


def _delay_json(project: Project) -> dict[str, Any] | None:
    found = delays.suspected_delay(project)
    return found.as_json() if found is not None else None


def _sections_json(project: Project) -> list[dict[str, Any]]:
    """Sections in identity order, each carrying what it delivers of what it holds.

    And where it sits in the campus (`blocks.structure`): the section it is part of,
    or why it is set aside, and who decided — a rule, a model or a person.
    """
    from tracker.blockcheck import sections

    today = delays.as_of()
    found = sections(project.id, list(project.blocks))
    nested = blocks_mod.structure(project)
    placed = _placement(project, found, nested)
    return [
        {
            "key": s.key,
            "label": s.label,
            "aliases": list(s.aliases),
            "class": s.klass,
            "ordinal": s.ordinal,
            "status": s.status,
            "capacity": s.capacity,
            "capacity_confirmed": s.capacity_confirmed,
            "delivering": s.delivering,
            "capacity_conflict": list(s.capacity_conflict),
            "verdict": s.verdict,
            "parent": s.parent,
            "generic": s.generic,
            "customer": s.customer,
            "energized_on": iso(s.energized_on),
            "expected_online": iso(s.expected_online),
            # Whether the section's own date has passed with nothing running, read
            # by the rule `tracker logic check` reads tranches with: the period has
            # to be over, and a paused or cancelled section is not late.
            "overdue": delays.tranche_state(s, today) == delays.LATE,
            "source_ids": list(s.source_ids),
            # The tranche a ruling on this section names: the one its other names
            # are folded into, so a ruling moves the figure the sums count.
            "tranche": _counted_key(s, nested),
            **placed.get(s.key, _UNRULED),
        }
        for s in found
    ]


def _counted_key(section: Any, nested: Any) -> str:
    keys = list(section.block_keys)
    inside = set(keys)
    for key in keys:
        link = nested.link(key)
        if link is None or link.relation != "same_as" or nested.canonical(key) not in inside:
            return key
    return keys[0] if keys else section.key


#: What a section with no link carries: top level, counted, nobody ruled.
_UNRULED: dict[str, Any] = {"within": None, "relation": None, "ruled_by": None, "ruling": None}


def _placement(project: Project, found: list[Any], nested: Any) -> dict[str, dict[str, Any]]:
    """Section key -> where it sits: `within`, `relation`, `ruled_by`, `ruling`.

    A section stands for one or more tranche keys (several names for one building),
    so a link on any of them is the section's, read off the one the display names.
    A section whose every tranche is set aside carries that relation; a part's
    `within` is the section holding its parent, never itself.
    """
    if not nested.links:
        return {}
    section_of = {k: s.key for s in found for k in s.block_keys}
    out: dict[str, dict[str, Any]] = {}
    for s in found:
        # A name the section already folds — "Facility 1" inside "Building 1" — is
        # the section itself, not a link out of it.
        links = [
            link
            for link in (nested.link(k) for k in s.block_keys)
            if link is not None
            and not (
                link.relation == "same_as" and section_of.get(nested.canonical(link.key)) == s.key
            )
        ]
        if not links:
            continue
        aside = [link for link in links if link.relation in blocks_mod.SET_ASIDE]
        # Set aside only when no name for it is still counted: a name the section
        # folds points at another of its names, so one unlinked name keeps it in.
        if aside and all(nested.link(k) is not None for k in s.block_keys):
            link = aside[0]
            out[s.key] = {
                "within": (
                    section_of.get(nested.canonical(link.key))
                    if link.relation == "same_as"
                    else None
                ),
                "relation": link.relation,
                "ruled_by": link.by,
                "ruling": link.reason or None,
            }
            continue
        part = next(
            (
                link
                for link in links
                if link.relation == "part_of"
                and section_of.get(nested.parent(link.key) or "") not in (None, s.key)
            ),
            None,
        )
        ruled = part or links[0]
        out[s.key] = {
            "within": section_of.get(nested.parent(part.key) or "") if part else None,
            "relation": None,
            "ruled_by": ruled.by,
            "ruling": ruled.reason or None,
        }
    return out


def _accounting_json(project: Project) -> dict[str, Any] | None:
    """Every megawatt of the campus on one line each, or None with no tranches."""
    if not project.blocks:
        return None
    got = blocks_mod.account(project)
    return {
        "total": got.total,
        "total_is_floor": got.total_is_floor,
        "counted_mw": got.counted_mw,
        # The headline's running figure, the same one the cards show, and what the
        # live tranches describe beside it. See `blocks.delivering`.
        "delivering": blocks_mod.delivering(project, got),
        "live_mw": got.live_mw,
        # The counted capacity by tranche state, read off the tree, for the bar.
        "by_state": dict(got.by_state),
        "closes": got.closes,
        "residuals": [
            {"reason": r.reason, "mw": r.mw, "labels": list(r.labels), "note": r.note}
            for r in got.residuals
        ],
    }


# --- Renderers --------------------------------------------------------------


def _fmt_number(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return f"{value:,}"


def _fmt_usd(value: int | None) -> str:
    if value is None:
        return ""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B".replace(".00B", "B")
    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    return f"${value:,}"


def _md_escape(text: str | None) -> str:
    """Escape pipes so a value containing one cannot break the table."""
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_csv(projects: Sequence[Project]) -> str:
    """CSV with a frozen header and LF line endings."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=CSV_COLUMNS, lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for project in projects:
        row = to_row(project)
        writer.writerow({k: ("" if row.get(k) is None else row[k]) for k in CSV_COLUMNS})
    return buffer.getvalue()


def render_json(projects: Sequence[Project], *, generated_at: str | None = None) -> str:
    """JSON with sorted keys and full nested citations.

    ``generated_at`` is omitted unless supplied, so the default output is
    reproducible; the CLI passes a timestamp when writing to a file.
    """
    payload: dict[str, Any] = {
        "schema": JSON_SCHEMA_TAG,
        "count": len(projects),
        "projects": [to_json_object(p) for p in projects],
    }
    if generated_at:
        payload["generated_at"] = generated_at
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


#: Where the dataset is spliced into the template.
_HTML_DATA_TOKEN = "__DATA__"


def template_path() -> Path:
    """The dashboard template, resolved next to the installed package."""
    from tracker.config import package_root

    return package_root() / "templates" / "dashboard.html"


def render_html(projects: Sequence[Project], *, generated_at: str | None = None) -> str:
    """One self-contained HTML file: the dataset inlined into the template.

    Inlined rather than fetched from a sibling `.json` deliberately. `fetch()` from
    a `file://` page is blocked as cross-origin, so a two-file build would open to
    an empty table unless the reader happened to be running a web server — which
    defeats the point of a deliverable someone can double-click.

    `</script>` inside the payload has to be broken up: the HTML tokenizer ends a
    script element at that byte sequence regardless of JSON or JavaScript context,
    so a project whose notes quoted a script tag would truncate the whole dataset
    and produce a page that fails to parse.
    """
    payload = json.dumps(
        {
            "schema": JSON_SCHEMA_TAG,
            "count": len(projects),
            "projects": [to_json_object(p) for p in projects],
            **({"generated_at": generated_at} if generated_at else {}),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload = payload.replace("</", "<\\/")

    template = template_path().read_text(encoding="utf-8")
    if _HTML_DATA_TOKEN not in template:
        raise ValueError(f"{template_path().name} has no {_HTML_DATA_TOKEN} placeholder")
    return template.replace(_HTML_DATA_TOKEN, payload, 1)


def render_md(projects: Sequence[Project], *, generated_at: str | None = None) -> str:
    """Markdown: a summary table, then one section per project with its citations.

    The table is designed to be pasted somewhere with limited width, so it carries
    the headline facts only and the per-project sections carry the evidence.
    """
    lines: list[str] = ["# US data center projects", ""]
    if generated_at:
        lines += [f"_Generated {generated_at}._", ""]

    if not projects:
        lines += ["_No projects match._", ""]
        return "\n".join(lines)

    cited = sum(1 for p in projects if p.mw_planned is not None)
    total_mw = sum(p.mw_planned or 0 for p in projects)
    total_usd = sum(p.investment_usd or 0 for p in projects)
    lines += [
        f"{len(projects)} project(s). "
        f"Planned capacity {_fmt_number(total_mw)} MW across {cited} project(s) that cite one; "
        f"announced investment {_fmt_usd(total_usd) or '$0'}.",
        "",
        "Totals cover only projects where the figure is cited, so they are a floor, "
        "not a sum of the industry.",
        "",
        "| # | Company | Project | Location | Phase | MW | Investment | Online | Conf |",
        "|---|---------|---------|----------|-------|---:|-----------:|--------|:----:|",
    ]
    for p in projects:
        location = _md_escape(
            f"{p.city}, {p.state}"
            if p.city
            else (f"{p.county}, {p.state}" if p.county else p.state)
        )
        lines.append(
            f"| {p.id} | {_md_escape(p.company)} | {_md_escape(p.name)} | {location} "
            f"| {p.phase} | {_fmt_number(p.mw_planned)} | {_fmt_usd(p.investment_usd)} "
            f"| {iso(p.expected_online) or ''} | {p.confidence} |"
        )

    lines += ["", "## Detail", ""]
    for p in projects:
        lines += [f"### {p.company} — {p.name} ({p.state})", ""]
        facts = [
            ("Customer", p.customer),
            ("Location", f"{p.city}, {p.state}" if p.city else f"{p.county} (county), {p.state}"),
            ("Phase", p.phase),
            ("Planned MW", _fmt_number(p.mw_planned) or None),
            ("Built MW", _fmt_number(p.mw_built) or None),
            ("H200-equivalent", _fmt_number(p.h200_equivalent) or None),
            ("Investment", _fmt_usd(p.investment_usd) or None),
            ("First announced", iso(p.first_announced)),
            ("Expected online", iso(p.expected_online)),
            ("Blocker", p.blocker),
            ("Confidence", f"{p.confidence}/3"),
        ]
        lines += [f"- **{label}:** {value}" for label, value in facts if value]

        auto = [
            line
            for line in (p.notes or "").splitlines()
            if line.startswith((NOTE_PREFIX, SOURCE_NOTE_PREFIX))
        ]
        human = [
            line
            for line in (p.notes or "").splitlines()
            if line.strip() and not line.startswith((NOTE_PREFIX, SOURCE_NOTE_PREFIX))
        ]
        if human:
            lines += ["", "Notes:", ""] + [f"> {line}" for line in human]
        if auto:
            lines += ["", "<details><summary>Data-quality notes</summary>", ""]
            lines += [f"- {line}" for line in auto]
            lines += ["", "</details>"]

        lines += ["", "Sources:", ""]
        for s in sorted(p.sources, key=lambda s: s.url):
            lines.append(f"- [{s.source_type}] <{s.url}> — supports: {s.fields or 'nothing'}")
            if s.excerpt:
                lines.append(f"  > {_md_escape(s.excerpt)}")

        if p.events:
            lines += ["", "Timeline:", ""]
            for e in sorted(p.events, key=lambda e: (e.event_date, e.event_type)):
                lines.append(f"- {iso(e.event_date)} — **{e.event_type}** — {e.description}")
        lines.append("")

    return "\n".join(lines)


RENDERERS = {"md": render_md, "csv": render_csv, "json": render_json, "html": render_html}


def render(fmt: str, projects: Sequence[Project], *, generated_at: str | None = None) -> str:
    if fmt not in RENDERERS:
        raise ValueError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")
    if fmt == "csv":
        return render_csv(projects)
    return RENDERERS[fmt](projects, generated_at=generated_at)


def write_export(text: str, out: Path | None) -> None:
    """Write to a file, or leave it to the caller to print to stdout.

    Always UTF-8 with LF endings, so a Windows export is byte-identical to one
    produced anywhere else.
    """
    if out is None:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")


__all__ = [
    "CSV_COLUMNS",
    "FORMATS",
    "JSON_SCHEMA_TAG",
    "RENDERERS",
    "ExportFilter",
    "fetch_projects",
    "render",
    "render_csv",
    "render_html",
    "render_json",
    "render_md",
    "to_json_object",
    "to_row",
    "write_export",
]


def claims_for(session: Any, project_id: int) -> dict[str, Any] | None:
    """One project's claim table, or None when there is no such project.

    The console's per-drawer fetch. `to_json_object(claims=False)` leaves this out
    of the list payload because it is 48% of it and one drawer needs one project's
    worth; this is the other half of that trade.
    """
    from sqlalchemy import select

    project = session.scalar(
        select(Project).options(selectinload(Project.sources)).where(Project.id == project_id)
    )
    return None if project is None else _claims_json(project)
