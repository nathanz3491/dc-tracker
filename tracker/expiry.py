"""When a decision stops holding: a later report that the figure changed.

Two kinds of decision used to hold forever. A ruling (`superseded`, `misread`) took a
citation's claim out of the merge with no end, and a verified answer in the answer key
was cited as a source with no date, so the rule that retires a source a year older
than its rivals (`upsert.STALE_AFTER`) never reached it and it outranked every later
news article for good.

**What ends one, measured on 2026-11-13.** Not a timer and not a repeat: of the 627
rulings whose figure was printed again after the ruling, the forty read by hand were
echoes — Hyperion's 2024 "$10 billion" in a November 2026 article, "announced plans in
spring 2024 to build a $3.3 billion data center", a neighbouring building, a stale
directory. Lapsing on either would have put them back. What does mean the world moved
is a sentence saying the figure *changed* — "increasing its investment from $12
billion to $18 billion", "raised the investment to more than $50 billion", "delayed to
2028" (`crawl.reports_change`), or for a stage, a later stage. So:

* **An answer reopens** when a live, first-hand, whole-campus claim dated after the
  answer was recorded reports a change to a different value (for `phase`, a later
  stage on the build path). The answer is then not scored or applied
  (`AnswerKey.reopened_on`), and its own citation is set aside as `superseded` — true
  when it was verified, restated since — so the ordinary merge decides between what
  is left. `answers load --replace` records it again.
* **A `superseded` ruling lifts** when such a claim, dated after the ruling, reports a
  change *to the ruled figure*: the campus went back to it, so "restated since" is no
  longer true of it. The field is decided again by the merge and the nightly checks.
* **A `misread` never lifts.** It is a fact about a sentence, which cannot change; it
  ends only when a re-read of that sentence claims something else
  (`upsert._carried_reasons`).

A ruling made before rulings were dated counts as made on `conflicts.DATED_FROM`, the
latest it can have been made. First-announced dates never reopen: they are history.

Free (no model, no network). `tracker logic expire` previews; the night runs it with
`--apply` in its free step, before re-deriving.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import AnswerKey, Project, Source

#: Fields whose change a sentence can report (`crawl.reports_change`).
FIGURES: tuple[str, ...] = ("investment_usd", "mw_planned", "expected_online")
#: Fields an answer can be reopened on: the figures, and the stage by a later stage.
ANSWER_FIELDS: tuple[str, ...] = (*FIGURES, "phase")

#: Who a reopening is recorded as: a rule of the code, from a sentence it read.
BY = "change report"


@dataclass(frozen=True)
class Report:
    """One sentence, dated, saying a field of this campus now has `value`."""

    source_id: int
    url: str
    field: str
    value: Any
    day: dt.date
    precision: str | None
    quote: str


@dataclass
class Reopened:
    """A decision a later report has reopened."""

    #: "answer" or "ruling".
    kind: str
    project_id: int
    name: str
    field: str
    #: The answer, or the ruled claim's value.
    was: Any
    #: The day the answer was recorded, or the ruling made.
    since: dt.date
    report: Report
    #: The ruled citation (a ruling), or the answer's id (an answer).
    source_id: int | None = None
    answer_id: int | None = None
    #: Citations set aside with the answer (its own sentence).
    set_aside: list[int] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "project_id": self.project_id,
            "name": self.name,
            "field": self.field,
            "was": self.was,
            "since": self.since.isoformat(),
            "source_id": self.source_id,
            "answer_id": self.answer_id,
            "set_aside": self.set_aside,
            "report": {
                "source_id": self.report.source_id,
                "url": self.report.url,
                "value": self.report.value,
                "day": self.report.day.isoformat(),
                "quote": self.report.quote,
            },
        }


@dataclass
class ExpiryReport:
    answers: list[Reopened] = field(default_factory=list)
    rulings: list[Reopened] = field(default_factory=list)
    #: Answer-key claims given the day their answer was recorded (`date_answer_claims`).
    dated: int = 0
    #: Rows whose values moved once applied: (id, field, before, after).
    moved: list[tuple[int, str, Any, Any]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "answers_reopened": [r.as_json() for r in self.answers],
            "rulings_lifted": [r.as_json() for r in self.rulings],
            "answer_claims_dated": self.dated,
            "moved": [
                {"project_id": p, "field": f, "before": b, "after": a} for p, f, b, a in self.moved
            ],
        }


def _claims(source: Source) -> dict:
    try:
        out = json.loads(source.claims or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def change_reports(project: Project) -> dict[str, list[Report]]:
    """Per field, the live claims on this row that could reopen a decision, oldest first.

    Live means what could decide the field: quoted, not ruled out or refused, about the
    whole campus, first-hand (not a directory). Dated, by the sentence's own date or
    the article's — an undated claim says only when the crawler called. For the
    figures, the sentence must report a change (`crawl.reports_change`); for `phase`
    every live stage is offered and `reopen_answers` asks for a later one.
    """
    from tracker.ingest.crawl import reports_change
    from tracker.upsert import _published_day, claims_by_field

    sources = list(project.sources)
    by_url = {s.url: s for s in sources}
    out: dict[str, list[Report]] = {}
    for name, claims in claims_by_field(sources).items():
        if name not in ANSWER_FIELDS:
            continue
        for claim in claims:
            if claim.decided_against or not claim.confirmed or claim.part or claim.tertiary:
                continue
            day = _published_day(claim)
            source = by_url.get(claim.url)
            if day is None or source is None or not claim.quote:
                continue
            if name != "phase" and not reports_change(claim.quote, claim.value, name, day.year):
                continue
            out.setdefault(name, []).append(
                Report(source.id, claim.url, name, claim.value, day, claim.precision, claim.quote)
            )
    for reports in out.values():
        reports.sort(key=lambda r: (r.day, r.source_id))
    return out


def _later_stage(new: Any, old: Any) -> bool:
    """Whether `new` is further along the build path than every stage `old` names.

    Only along the path (announced → permitting → construction → operational). A stop
    — paused, cancelled, denied — is read wrong often enough (a rezoning "denied" for
    another site, a phase "cancelled") that it is left to the nightly checks.
    """
    from tracker.vocab import PHASE_PROGRESSION

    rank = {name: i for i, name in enumerate(PHASE_PROGRESSION)}
    olds = [o.strip() for o in str(old).split("|")]
    if new not in rank or not all(o in rank for o in olds):
        return False
    return all(rank[new] > rank[o] for o in olds)


def _answer_citations(project: Project, answer: AnswerKey) -> list[Source]:
    """The citations that carry this answer: its page, claiming what it says."""
    from tracker import answer_key
    from tracker.normalize import canonical_url

    if not answer.source_url:
        return []
    url = canonical_url(answer.source_url)
    out = []
    for source in project.sources:
        if source.url != url:
            continue
        claim = _claims(source).get(answer.field)
        if claim is None:
            continue
        try:
            verdict = answer_key.judge(answer, claim, None)
        except (TypeError, ValueError):
            continue
        if verdict in ("right", "imprecise"):
            out.append(source)
    return out


def reopen_answers(session: Session, project_ids: list[int] | None = None) -> list[Reopened]:
    """Every verified answer a report published after it says has changed."""
    from tracker import answer_key
    from tracker.conflicts import ruling_of

    out = []
    reports_of: dict[int, dict[str, list[Report]]] = {}
    for answer in answer_key.answers(session):
        if answer.field not in ANSWER_FIELDS or answer.value is None:
            continue
        if project_ids and answer.project_id not in project_ids:
            continue
        project = session.get(Project, answer.project_id)
        if project is None:
            continue
        if project.id not in reports_of:
            reports_of[project.id] = change_reports(project)
        recorded = answer.recorded_at.date()
        for report in reports_of[project.id].get(answer.field, []):
            if report.day <= recorded:
                continue
            if answer.field == "phase":
                if not _later_stage(report.value, answer.value):
                    continue
            else:
                try:
                    verdict = answer_key.judge(answer, report.value, report.precision)
                except (TypeError, ValueError):
                    continue
                if verdict != "wrong":
                    continue
            out.append(
                Reopened(
                    "answer",
                    project.id,
                    project.name,
                    answer.field,
                    answer.value,
                    recorded,
                    report,
                    answer_id=answer.id,
                    set_aside=[
                        s.id
                        for s in _answer_citations(project, answer)
                        if ruling_of(s, answer.field) is None
                    ],
                )
            )
            break
    return out


def _same_value(field_name: str, a: Any, b: Any, precision: str | None) -> bool:
    if field_name == "expected_online":
        width = 4 if precision in ("year", "half") else 7
        return str(a)[:width] == str(b)[:width]
    from tracker.upsert import _same_figure

    return _same_figure(a, b)


def reopen_rulings(session: Session, project_ids: list[int] | None = None) -> list[Reopened]:
    """Every `superseded` ruling a later report has gone back to."""
    from tracker.conflicts import SUPERSEDED, ruling_of

    query = select(Source.project_id).where(Source.unconfirmed_reasons.like("%superseded%"))
    if project_ids:
        query = query.where(Source.project_id.in_(project_ids))
    out = []
    for project_id in sorted(set(session.scalars(query))):
        project = session.get(Project, project_id)
        if project is None:
            continue
        reports = None
        for source in project.sources:
            for name in FIGURES:
                ruling = ruling_of(source, name)
                if ruling is None or ruling.reason != SUPERSEDED:
                    continue
                value = _claims(source).get(name)
                if value is None:
                    continue
                if reports is None:
                    reports = change_reports(project)
                for report in reports.get(name, []):
                    if report.source_id == source.id or report.day <= ruling.made:
                        continue
                    if not _same_value(name, report.value, value, report.precision):
                        continue
                    out.append(
                        Reopened(
                            "ruling",
                            project.id,
                            project.name,
                            name,
                            value,
                            ruling.made,
                            report,
                            source_id=source.id,
                        )
                    )
                    break
    return out


def date_answer_claims(session: Session, *, apply: bool) -> int:
    """Give every claim an answer wrote the day the answer was recorded (`as_of`).

    An answer is true as of the day it was verified. Its citation used to carry no
    date when the answer's page was new to the row, and an undated claim is never
    stale (`upsert.stale_claims`), so it outranked lighter sources forever. Dated,
    it ages like any other claim. Idempotent; returns the claims it dated.
    """
    from tracker import answer_key

    dated = 0
    for answer in answer_key.answers(session, reopened=True):
        if answer.value is None:
            continue
        project = session.get(Project, answer.project_id)
        if project is None:
            continue
        day = answer.recorded_at.date().isoformat()
        for source in _answer_citations(project, answer):
            try:
                meta = json.loads(source.claim_meta or "{}")
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            entry = meta.get(answer.field)
            entry = dict(entry) if isinstance(entry, dict) else {}
            if entry.get("as_of"):
                continue
            dated += 1
            if apply:
                entry["as_of"] = day
                meta[answer.field] = entry
                source.claim_meta = json.dumps(meta, sort_keys=True, ensure_ascii=False)
    return dated


def expire(session: Session, *, apply: bool, project_ids: list[int] | None = None) -> ExpiryReport:
    """Find, and with `apply` write, every decision a later report has reopened.

    Answers first, so a ruling the answer's own citation now carries is not lifted in
    the same pass. Each reopening goes into the row's notes as the rule's decision,
    naming the report. The caller commits.
    """
    from tracker import logic as logic_mod
    from tracker.audit import fmt_value
    from tracker.conflicts import lift_ruling, supersede
    from tracker.models import utcnow
    from tracker.upsert import recompute_from_sources

    report = ExpiryReport()
    report.dated = date_answer_claims(session, apply=apply)
    report.answers = reopen_answers(session, project_ids)
    if apply:
        today = utcnow().date()
        for item in report.answers:
            answer = session.get(AnswerKey, item.answer_id)
            answer.reopened_on = today
            answer.reopened_by = item.report.source_id
            for source_id in item.set_aside:
                source = session.get(Source, source_id)
                if source is not None:
                    supersede(source, item.field, by=BY)
            logic_mod.record_decision(
                session.get(Project, item.project_id),
                "answer_reopened",
                f"{item.field} answer {item.was} (recorded {item.since}) reopened: "
                f"s{item.report.source_id} ({item.report.day}) reports "
                f"{fmt_value(item.report.value)}; {len(item.set_aside)} citation(s) set aside",
                by="rule",
                detail=item.report.url,
            )
        session.flush()
    report.rulings = reopen_rulings(session, project_ids)
    if apply:
        for item in report.rulings:
            source = session.get(Source, item.source_id)
            if source is None or not lift_ruling(source, item.field):
                continue
            logic_mod.record_decision(
                session.get(Project, item.project_id),
                "ruling_reopened",
                f"{item.field} {fmt_value(item.was)} on s{item.source_id} "
                f"(superseded {item.since}) back in: s{item.report.source_id} "
                f"({item.report.day}) reports a change to it",
                by="rule",
                detail=item.report.url,
            )
        session.flush()
        for project_id in sorted({i.project_id for i in report.answers + report.rulings}):
            project = session.get(Project, project_id)
            before = {f: getattr(project, f) for f in ANSWER_FIELDS}
            recompute_from_sources(session, project)
            for name, was in before.items():
                if getattr(project, name) != was:
                    report.moved.append((project_id, name, was, getattr(project, name)))
        session.flush()
    return report


__all__ = [
    "ANSWER_FIELDS",
    "BY",
    "FIGURES",
    "ExpiryReport",
    "Reopened",
    "Report",
    "change_reports",
    "date_answer_claims",
    "expire",
    "reopen_answers",
    "reopen_rulings",
]
