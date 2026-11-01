"""Correct the stored values the answer key proved wrong, through the claims.

The answer key (`tracker.answer_key`) holds researched, quote-checked values for the
largest campuses, and on 2026-11-01 it judged 86 stored values wrong. Each verified
answer carries the page and the exact sentence that states it, so most of those can
be corrected without reading anything again.

**Through the claims, never the column.** A project scalar is a cache of its claims:
typing a value onto the row would be undone by the next recompute. So a correction
is the same two moves a person makes by hand:

1. **Cite the answer.** The answer's page and sentence become a citation on that row
   (`upsert_record(route_to=…)`, source type `manual`), its claim the answer's value,
   with the answer's date precision. A page the row already cites gets the claim
   folded into its citation rather than replacing it, so nothing it said is lost.
2. **Rule out what the answer refutes.** Every live claim on the row that the key
   judges wrong for that field is marked decided against as `misread` (the sentence
   was about something else), exactly as `logic rule-out` does, and the row is
   re-derived from what stands.

An answer of "no figure" (the field should be empty) is step 2 alone. The decision
goes into the row's notes as the answer key's. Afterwards each row is judged again;
one the merge still holds wrong (a tranche raising the stage, say) is reported, not
forced. A range answer is not cited (there is no one value to state), only its
out-of-range claims are ruled out, and only when a claim inside the range stands.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from tracker import answer_key
from tracker.models import AnswerKey, Project

NUMBERS = ("mw_planned", "investment_usd")
DATES = ("expected_online", "first_announced")


@dataclass
class Fix:
    """One wrong value and what correcting it takes."""

    project_id: int
    field: str
    held: Any
    answer: str
    cite: bool
    refuted: list[int] = field(default_factory=list)  # source ids whose claim goes
    note: str = ""


@dataclass
class Outcome:
    fix: Fix
    before: Any
    after: Any
    verdict: str  # the key's verdict on the row afterwards


def _claims(source: Any) -> dict:
    try:
        return json.loads(source.claims or "{}")
    except ValueError:
        return {}


def _decided(source: Any, name: str) -> bool:
    from tracker.upsert import DECIDED_REASONS

    try:
        return json.loads(source.unconfirmed_reasons or "{}").get(name) in DECIDED_REASONS
    except ValueError:
        return False


def _shown(answer: AnswerKey) -> str:
    if answer.value is None and answer.low is None and answer.high is None:
        return "(no figure)"
    if answer.value is None:
        return f"{answer.low}..{answer.high}"
    return answer.value + (f" ({answer.precision})" if answer.precision else "")


def _claim_value(answer: AnswerKey) -> Any:
    """The value a citation of this answer claims, in the shape a reader writes it."""
    first = str(answer.value).split("|")[0]
    if answer.field in NUMBERS:
        return float(first)
    return first


def plan(session: Session, project_ids: list[int] | None = None) -> list[Fix]:
    """Every verified answer the row now holds wrong, with what correcting it takes."""
    fixes = []
    for answer in answer_key.answers(session):
        if project_ids and answer.project_id not in project_ids:
            continue
        project = session.get(Project, answer.project_id)
        if project is None:
            continue
        held, precision = answer_key.held_values(project)[answer.field]
        if answer_key.judge(answer, held, precision) != "wrong":
            continue
        refuted = []
        backing_inside = False
        for source in project.sources:
            claims = _claims(source)
            if answer.field not in claims or _decided(source, answer.field):
                continue
            verdict = answer_key.judge(answer, claims[answer.field], None)
            if verdict == "wrong":
                refuted.append(source.id)
            elif verdict in ("right", "imprecise"):
                backing_inside = True
        point = answer.value is not None
        cite = point and bool(answer.source_url and answer.quote)
        note = ""
        if answer.value is None and answer.low is not None and not backing_inside:
            refuted, note = [], "range answer and no claim inside it: left for a reader"
        elif point and not cite and not backing_inside:
            refuted, note = [], "no cited sentence to stand in for what would be ruled out"
        fixes.append(
            Fix(answer.project_id, answer.field, held, _shown(answer), cite, refuted, note)
        )
    return fixes


def _cite(session: Session, project: Project, answer: AnswerKey, now: dt.datetime) -> None:
    from tracker.ingest.records import IngestRecord, SourceRecord
    from tracker.normalize import canonical_url
    from tracker.upsert import upsert_record

    url = canonical_url(answer.source_url)
    meta = {}
    if answer.field in DATES and answer.precision:
        meta = {answer.field: {"date_precision": answer.precision}}
    if answer.field == "mw_planned" and answer.basis:
        meta = {answer.field: {"basis": answer.basis}}
    record = SourceRecord(
        url=url,
        source_type="manual",
        fetched_at=now,
        excerpt=answer.quote,
        claims={answer.field: _claim_value(answer)},
        quotes={answer.field: answer.quote},
        claim_meta=meta,
        extractor="answer key",
    )
    already = any(s.url == url for s in project.sources)
    upsert_record(
        session,
        IngestRecord(
            project={
                k: getattr(project, k)
                for k in ("company", "name", "city", "county", "state")
                if getattr(project, k, None)
            },
            sources=[record],
        ),
        route_to=project.id,
        reading={(project.id, url)} if already else None,
    )


def apply(session: Session, fixes: list[Fix], *, now: dt.datetime | None = None) -> list[Outcome]:
    """Make each fix and judge the row again. The caller commits."""
    from tracker import logic as logic_mod
    from tracker.audit import fmt_value
    from tracker.conflicts import supersede
    from tracker.models import Source
    from tracker.upsert import recompute_confidence, recompute_from_sources

    now = now or dt.datetime.now(dt.UTC).replace(tzinfo=None)
    keyed = {(a.project_id, a.field): a for a in answer_key.answers(session)}
    out = []
    for fix in fixes:
        if fix.note:
            continue
        answer = keyed[(fix.project_id, fix.field)]
        project = session.get(Project, fix.project_id)
        before = getattr(project, fix.field)
        if fix.cite:
            _cite(session, project, answer, now)
            session.flush()
        for source_id in fix.refuted:
            source = session.get(Source, source_id)
            if source is not None:
                supersede(source, fix.field, reason="misread")
        if Project.__table__.columns[fix.field].nullable:
            setattr(project, fix.field, None)
        session.flush()
        recompute_from_sources(session, project)
        after = getattr(project, fix.field)
        what = (
            f"{fix.field} {fmt_value(before)} -> {fmt_value(after)} (answer key: {fix.answer}; "
            f"{len(fix.refuted)} claim(s) ruled out as misread"
            + ("; its sentence cited" if fix.cite else "")
            + ")"
        )
        logic_mod.record_decision(
            project, "answer_key_corrected", what, by="answer key", detail=answer.source_url or ""
        )
        session.flush()
        held, precision = answer_key.held_values(project)[fix.field]
        out.append(Outcome(fix, before, after, answer_key.judge(answer, held, precision)))
    recompute_confidence(session)
    return out


__all__ = ["Fix", "Outcome", "apply", "plan"]
