"""Why a stored value is wrong, and what a proposed check would change.

The answer key (`tracker.answer_key`) says how many stored values are right; this
says why the rest are not, and which step wrote them, from the statements behind
each value (`fact`, where `held` marks the ones a stored value rests on). Measured
on 2026-11-11 it found what no count could: 10 of the 13 wrong investment figures
should have been empty — real figures for two campuses, a power plant, a company's
pledge to a state, or an estimate — and the right figure was never in any article
read for 12 of them. Choosing among sources was not the problem; letting the wrong
kind of number in was.

`trial` is for the checks that follow from that: given a test on a value's sentence,
it says on the answer key which wrong values it would fix and which right ones it
would lose, and across the whole database how many stored values it would empty.
The first blunt test — refuse any sentence with a hedge word — fixed 7 of 13 and lost
none of the 32 right in the key, and would have emptied 86 of 628 investment figures
across the database, half of them good. The second number is the one that stopped it.

Reads only.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker import answer_key
from tracker.models import AnswerKey, Fact, Project, Source

#: Statement tiers a stored value can rest on; `ruled_out` is a ruling against it.
STANDING: Final = ("confirmed", "directory", "derived", "inferred", "unconfirmed")

# --- candidate checks on a value's sentence ---------------------------------------

#: Words of a guess, a condition or a ceiling. The blunt first candidate.
HEDGE: Final = re.compile(
    r"\b(could|would|might|may|estimat\w*|project(?:ed|ions?)|potential\w*|up to|as much as|"
    r"rang(?:e|es|ing)|model|assum\w*|over time|over the next|over \d+ years|expandable|"
    r"expansion path|full build-?out|all phases|total capital needs)\b",
    re.I,
)

#: Words of a figure that covers more than the campus.
WIDE: Final = re.compile(
    r"\b(two|three|four|several|multiple)\s+(?:data center\s+)?campuses\b|\bcampuses\b|"
    r"\b(power|energy|generation|gas-fired|power plant|site readiness|infrastructure)\b",
    re.I,
)

#: The checks `tracker answers try` knows, by name: a test on a sentence that says
#: whether it may set the value.
CHECKS: Final[dict[str, Callable[[str], bool]]] = {
    "hedge": lambda q: bool(HEDGE.search(q)),
    "wide": lambda q: bool(WIDE.search(q)),
    "either": lambda q: bool(HEDGE.search(q) or WIDE.search(q)),
}


# --- which step wrote it ---------------------------------------------------------


def step_of(extractor: str | None) -> str:
    """The step that wrote a citation, in a reader's words."""
    e = (extractor or "").lower()
    if e.startswith("crawl:"):
        return "article reader"
    if e.startswith("gapfill-agent") or "agent" in e:
        return "research agent"
    if "answer" in e:
        return "answer key"
    if e.startswith("derived"):
        return "derived"
    return e.split(":")[0] or "unknown"


@dataclass(frozen=True)
class Statement:
    """One citation's statement of the field, as the diagnosis needs it."""

    value: str | None
    held: bool
    tier: str
    verdict: str
    quote: str
    step: str
    url: str | None


def statements(session: Session, answer: AnswerKey) -> list[Statement]:
    """Every campus statement of the answer's field, judged against it."""
    out = []
    for f in session.scalars(
        select(Fact).where(
            Fact.project_id == answer.project_id,
            Fact.entity == "campus",
            Fact.field == answer.field,
        )
    ):
        src = session.get(Source, f.source_id) if f.source_id else None
        try:
            verdict = answer_key.judge(answer, f.value)
        except (TypeError, ValueError):
            verdict = "?"
        out.append(
            Statement(
                f.value,
                bool(f.held),
                f.tier,
                verdict,
                f.quote or "",
                step_of(src.extractor if src else None),
                src.url if src else None,
            )
        )
    return out


# --- why -----------------------------------------------------------------------

#: Causes, in the order a report lists them.
CAUSES: Final = (
    "the figure's sentence is a guess, a condition or a ceiling",
    "the figure's sentence covers more than the campus",
    "another figure, the right one never read",
    "the right value was read and ruled out",
    "the right value was read, another kept",
    "nothing found",
    "found, not kept",
    "a date stored less precisely than it was given",
    "no citation behind it",
)


@dataclass(frozen=True)
class Diagnosis:
    """Why one verified answer is not right."""

    project_id: int
    name: str
    field: str
    stored: str | None
    answer: str | None
    outcome: str
    cause: str
    #: The steps that wrote the citations the stored value rests on.
    steps: tuple[str, ...]
    #: The first of those citations' sentences, and its address.
    quote: str
    url: str | None
    #: The answer says the field should be empty.
    should_be_empty: bool


def _cause(outcome: str, standing: list[Statement], all_: list[Statement]) -> str:
    right = [s for s in all_ if s.verdict == "right"]
    if outcome == "empty":
        return "found, not kept" if right else "nothing found"
    if outcome == "imprecise":
        return "a date stored less precisely than it was given"
    if not standing:
        return "no citation behind it"
    if right:
        if all(s.tier == "ruled_out" for s in right):
            return "the right value was read and ruled out"
        return "the right value was read, another kept"
    quotes = " ".join(s.quote for s in standing)
    if HEDGE.search(quotes):
        return "the figure's sentence is a guess, a condition or a ceiling"
    if WIDE.search(quotes):
        return "the figure's sentence covers more than the campus"
    return "another figure, the right one never read"


def diagnose(session: Session, *, fields: tuple[str, ...] | None = None) -> list[Diagnosis]:
    """Every verified answer the database does not get right, and why."""
    out = []
    for answer in answer_key.answers(session, ("verified",)):
        if fields and answer.field not in fields:
            continue
        project = session.get(Project, answer.project_id)
        if project is None:
            continue
        value, precision = answer_key.held_values(project)[answer.field]
        outcome = answer_key.judge(answer, value, precision)
        if outcome == "right":
            continue
        all_ = statements(session, answer)
        standing = [s for s in all_ if s.held and s.tier in STANDING]
        truth = (
            answer.value
            if answer.low is None and answer.high is None
            else f"{answer.low}..{answer.high}"
        )
        out.append(
            Diagnosis(
                project.id,
                f"{project.company} — {project.name}",
                answer.field,
                None if value is None else str(value),
                truth,
                outcome,
                _cause(outcome, standing, all_),
                tuple(sorted({s.step for s in standing})),
                standing[0].quote if standing else "",
                standing[0].url if standing else None,
                answer.value is None and answer.low is None and answer.high is None,
            )
        )
    return out


@dataclass
class StepScore:
    """Right and judged stored values whose citations one step alone wrote."""

    right: int = 0
    judged: int = 0


def by_step(session: Session) -> dict[str, StepScore]:
    """How often a stored value is right, by the step that wrote its citations.

    Only values whose standing citations all came from one step are counted, so a
    value two steps agree on is credited to neither. On 2026-11-11: the article
    reader 122 of 153, the research agent 3 of 9 (investment 0 of 5).
    """
    out: dict[str, StepScore] = {}
    for answer in answer_key.answers(session, ("verified",)):
        project = session.get(Project, answer.project_id)
        if project is None:
            continue
        value, precision = answer_key.held_values(project)[answer.field]
        outcome = answer_key.judge(answer, value, precision)
        if outcome == "empty":
            continue
        standing = [s for s in statements(session, answer) if s.held and s.tier in STANDING]
        steps = {s.step for s in standing} or ({"no citation"} if value is not None else set())
        if len(steps) != 1:
            continue
        score = out.setdefault(next(iter(steps)), StepScore())
        score.judged += 1
        score.right += outcome == "right"
    return out


# --- what a check would change --------------------------------------------------


@dataclass
class Trial:
    """What refusing the sentences a check names would do to one field."""

    check: str
    field: str
    #: On the answer key.
    fixed: int = 0
    lost: int = 0
    still_wrong: int = 0
    #: Across the database: values held, and the ones it would empty or change.
    held: int = 0
    emptied: list[tuple[int, str, str]] = field(default_factory=list)
    changed: list[tuple[int, str, str]] = field(default_factory=list)


def _after(standing: list[Fact], others: list[Fact], refuse: Callable[[str], bool]) -> str:
    """'same', 'empty' or 'other': where a value would land with the check applied."""
    if any(not refuse(f.quote or "") for f in standing):
        return "same"
    if any(not refuse(f.quote or "") for f in others):
        return "other"
    return "empty"


def trial(session: Session, check: str, *, field_name: str = "investment_usd") -> Trial:
    """Refuse the sentences `check` names for `field_name`; count what moves.

    A value whose every standing sentence is refused would be empty, or would fall
    to another standing statement if one survives ("changed" — the merge decides
    which, so it is listed for a person to read, not scored).
    """
    refuse = CHECKS[check]
    out = Trial(check, field_name)
    keyed = {
        (a.project_id): a
        for a in answer_key.answers(session, ("verified",))
        if a.field == field_name
    }
    for project in session.scalars(
        select(Project).where(getattr(Project, field_name).is_not(None))
    ):
        facts = list(
            session.scalars(
                select(Fact).where(
                    Fact.project_id == project.id,
                    Fact.entity == "campus",
                    Fact.field == field_name,
                    Fact.tier.in_(STANDING),
                )
            )
        )
        standing = [f for f in facts if f.held]
        if not standing:
            continue
        out.held += 1
        where = _after(standing, [f for f in facts if not f.held], refuse)
        line = (project.id, str(getattr(project, field_name)), (standing[0].quote or "")[:200])
        if where == "empty":
            out.emptied.append(line)
        elif where == "other":
            out.changed.append(line)
        answer = keyed.get(project.id)
        if answer is None:
            continue
        value, precision = answer_key.held_values(project)[field_name]
        now = answer_key.judge(answer, value, precision)
        after = now if where != "empty" else answer_key.judge(answer, None)
        if now != "right" and after == "right":
            out.fixed += 1
        elif now == "right" and after != "right":
            out.lost += 1
        elif now != "right":
            out.still_wrong += 1
    return out


def summary(diagnoses: list[Diagnosis]) -> dict[str, Counter]:
    """field -> Counter of causes."""
    out: dict[str, Counter] = {}
    for d in diagnoses:
        out.setdefault(d.field, Counter())[d.cause] += 1
    return out


def as_json(d: Diagnosis) -> dict[str, object]:
    return json.loads(
        json.dumps(
            {
                "project_id": d.project_id,
                "name": d.name,
                "field": d.field,
                "stored": d.stored,
                "answer": d.answer,
                "outcome": d.outcome,
                "cause": d.cause,
                "steps": list(d.steps),
                "quote": d.quote,
                "url": d.url,
                "should_be_empty": d.should_be_empty,
            }
        )
    )


__all__ = [
    "CAUSES",
    "CHECKS",
    "HEDGE",
    "STANDING",
    "WIDE",
    "Diagnosis",
    "StepScore",
    "Trial",
    "as_json",
    "by_step",
    "diagnose",
    "statements",
    "step_of",
    "summary",
    "trial",
]
