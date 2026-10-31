"""The answer key: verified values for chosen campuses, and how the database scores.

Nothing else in the tracker says whether a stored value is *true*. The quote check
says a sentence exists, the tiers count open questions, and every merge rule was
justified by the incident that prompted it — so no change could be weighed on net.
On 2026-10-30 a researched draft for the 50 campuses carrying most of the money
disagreed with 83 of 259 stored answers, and nothing had said so.

The answers live in `answer_key` (migration 0050), one per campus and field. This
module loads them (`load`), judges one stored value against one answer (`judge`),
scores a database (`score`), and compares two databases against the same answers
(`compare`): "this change turned six wrong answers right and one right answer
wrong". How an answer earns `verified` is in `docs/data-quality.md`, "The answer key".

Only `verified` answers are scored by default. `unsettled` records that the evidence
did not agree and is never scored; `draft` is scored only on request, and the report
says so.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import AnswerKey, Project, utcnow

#: The fields an answer may be given for, in report order.
FIELDS: tuple[str, ...] = (
    "phase",
    "mw_planned",
    "investment_usd",
    "expected_online",
    "first_announced",
)
NUMBERS: frozenset[str] = frozenset({"mw_planned", "investment_usd"})
DATES: frozenset[str] = frozenset({"expected_online", "first_announced"})
STATUSES: tuple[str, ...] = ("verified", "unsettled", "draft")
PRECISIONS: tuple[str, ...] = ("day", "month", "quarter", "half", "year")

#: How far a stored figure may sit from the answer and still be right. Publishers
#: round — "1.2 GW" for 1,180 MW, "$10 billion" for $9.8B — and a 5% band takes the
#: rounding without accepting a different figure: the errors the key exists to catch
#: are a programme total for a campus's, IT load for facility power, a phase for the
#: whole, which differ by far more.
NUMBER_TOLERANCE = 0.05

#: What `judge` can say. `imprecise` is a date that agrees but at a coarser
#: precision than the answer ("2025" for 2025-02-25): not wrong, not the answer.
OUTCOMES: tuple[str, ...] = ("right", "imprecise", "wrong", "empty")


@dataclass(frozen=True)
class Answer:
    """One answer as it arrives, before it is stored."""

    project_id: int
    field: str
    value: str | None
    status: str
    method: str
    low: float | None = None
    high: float | None = None
    precision: str | None = None
    basis: str | None = None
    source_url: str | None = None
    quote: str | None = None
    note: str | None = None


def parse(record: Mapping[str, Any]) -> Answer:
    """One answer from a JSON-shaped record, or `ValueError` saying what is wrong.

    Checked here rather than left to the table's constraints, so a batch is refused
    with a sentence a person can act on instead of an integrity error on row 37.
    """
    try:
        project_id = int(record["project_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("every answer needs a numeric project_id") from None
    name = str(record.get("field") or "")
    if name not in FIELDS:
        raise ValueError(f"#{project_id}: field {name!r} is not one of {', '.join(FIELDS)}")
    status = str(record.get("status") or "")
    if status not in STATUSES:
        raise ValueError(
            f"#{project_id} {name}: status {status!r} is not one of {', '.join(STATUSES)}"
        )
    method = str(record.get("method") or "").strip()
    if not method:
        raise ValueError(f"#{project_id} {name}: say how the answer was reached (method)")
    raw = record.get("value")
    value = None if raw is None or str(raw).strip() == "" else str(raw).strip()
    low, high = _number(record.get("low")), _number(record.get("high"))
    precision = record.get("precision") or None
    if precision is not None and precision not in PRECISIONS:
        raise ValueError(
            f"#{project_id} {name}: precision {precision!r} is not one of {', '.join(PRECISIONS)}"
        )
    if low is not None and high is not None and low > high:
        raise ValueError(f"#{project_id} {name}: low {low} is above high {high}")
    if name in NUMBERS and value is not None and _number(value) is None:
        raise ValueError(f"#{project_id} {name}: {value!r} is not a number")
    if name in DATES and value is not None:
        try:
            dt.date.fromisoformat(value[:10])
        except ValueError:
            raise ValueError(f"#{project_id} {name}: {value!r} is not an ISO date") from None
    return Answer(
        project_id=project_id,
        field=name,
        value=value,
        status=status,
        method=method,
        low=low,
        high=high,
        precision=precision if name in DATES else None,
        basis=record.get("basis") or None,
        source_url=record.get("source_url") or None,
        quote=record.get("quote") or None,
        note=record.get("note") or None,
    )


@dataclass
class LoadReport:
    added: int = 0
    replaced: int = 0
    kept: int = 0
    missing: list[int] = field(default_factory=list)


def load(session: Session, answers: Iterable[Answer], *, replace: bool = False) -> LoadReport:
    """Store answers. An answer already held for the same campus and field is kept
    unless `replace`: a verified answer is not overwritten by accident."""
    report = LoadReport()
    for answer in answers:
        if session.get(Project, answer.project_id) is None:
            report.missing.append(answer.project_id)
            continue
        row = session.scalars(
            select(AnswerKey).where(
                AnswerKey.project_id == answer.project_id, AnswerKey.field == answer.field
            )
        ).first()
        if row is not None and not replace:
            report.kept += 1
            continue
        if row is None:
            row = AnswerKey(project_id=answer.project_id, field=answer.field)
            session.add(row)
            report.added += 1
        else:
            report.replaced += 1
        for name in (
            "value", "low", "high", "precision", "basis", "status", "method",
            "source_url", "quote", "note",
        ):  # fmt: skip
            setattr(row, name, getattr(answer, name))
        row.recorded_at = utcnow()
    session.flush()
    return report


# --- judging one value --------------------------------------------------------------


def span(day: dt.date, precision: str | None) -> tuple[dt.date, dt.date]:
    """The days a date read at `precision` covers. None reads as a day."""
    if precision in (None, "day"):
        return day, day
    if precision == "month":
        first_month, last_month = day.month, day.month
    elif precision == "quarter":
        first_month = 3 * ((day.month - 1) // 3) + 1
        last_month = first_month + 2
    elif precision == "half":
        first_month = 1 if day.month <= 6 else 7
        last_month = first_month + 5
    elif precision == "year":
        first_month, last_month = 1, 12
    else:
        raise ValueError(f"unknown precision {precision!r}")
    last_day = calendar.monthrange(day.year, last_month)[1]
    return dt.date(day.year, first_month, 1), dt.date(day.year, last_month, last_day)


def judge(answer: Answer | AnswerKey, stored: Any, stored_precision: str | None = None) -> str:
    """`right`, `imprecise`, `wrong` or `empty` for one stored value against one answer.

    An answer with no value says the field should be empty: an empty field is then
    right and any value wrong. Otherwise an empty field is `empty`, not `wrong`, so
    the report can tell a gap from an error.
    """
    if answer.value is None and answer.low is None and answer.high is None:
        return "right" if stored in (None, "") else "wrong"
    if stored in (None, ""):
        return "empty"
    if answer.field == "phase":
        return "right" if str(stored) in str(answer.value).split("|") else "wrong"
    if answer.field in NUMBERS:
        return "right" if _number_matches(answer, float(stored)) else "wrong"
    truth = span(dt.date.fromisoformat(str(answer.value)[:10]), answer.precision)
    held_day = stored if isinstance(stored, dt.date) else dt.date.fromisoformat(str(stored)[:10])
    held = span(held_day, stored_precision)
    if truth[0] <= held[0] and held[1] <= truth[1]:
        return "right"
    if held[0] <= truth[0] and truth[1] <= held[1]:
        return "imprecise"
    return "wrong"


def _number_matches(answer: Answer | AnswerKey, stored: float) -> bool:
    low, high = answer.low, answer.high
    if low is not None or high is not None:
        floor = (low if low is not None else -math.inf) * (1 - NUMBER_TOLERANCE)
        ceiling = (high if high is not None else math.inf) * (1 + NUMBER_TOLERANCE)
        return floor <= stored <= ceiling
    target = float(answer.value)
    return abs(stored - target) <= NUMBER_TOLERANCE * abs(target)


def _number(raw: Any) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(str(raw).replace(",", ""))
    except ValueError:
        return None


# --- scoring a database --------------------------------------------------------------

#: A row's stored values as `score` reads them: field -> (value, precision).
Held = dict[str, tuple[Any, str | None]]


def held_values(project: Project) -> Held:
    """What a row holds for every field the key can judge."""
    out: Held = {}
    for name in FIELDS:
        precision = getattr(project, f"{name}_precision", None) if name in DATES else None
        out[name] = (getattr(project, name), precision)
    return out


@dataclass
class FieldScore:
    outcomes: Counter = field(default_factory=Counter)

    @property
    def judged(self) -> int:
        return sum(self.outcomes[o] for o in OUTCOMES)

    @property
    def accuracy(self) -> float | None:
        return self.outcomes["right"] / self.judged if self.judged else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson(self.outcomes["right"], self.judged)


@dataclass
class Score:
    statuses: tuple[str, ...]
    fields: dict[str, FieldScore]
    absent: list[int]

    @property
    def total(self) -> FieldScore:
        out = FieldScore()
        for one in self.fields.values():
            out.outcomes.update(one.outcomes)
        return out


def answers(session: Session, statuses: tuple[str, ...] = ("verified",)) -> list[AnswerKey]:
    return list(
        session.scalars(
            select(AnswerKey)
            .where(AnswerKey.status.in_(statuses))
            .order_by(AnswerKey.project_id, AnswerKey.field)
        )
    )


def score(
    session: Session,
    *,
    statuses: tuple[str, ...] = ("verified",),
    rows: Mapping[int, Held] | None = None,
) -> Score:
    """Judge every answer of `statuses` against the rows.

    `rows` scores another database's values against this one's answers — what
    `compare` does with a snapshot. A campus missing from `rows` is reported as
    absent, not as wrong: a merge folds rows, and the score should not punish it.
    """
    out = Score(statuses=statuses, fields={name: FieldScore() for name in FIELDS}, absent=[])
    for answer in answers(session, statuses):
        if rows is None:
            project = session.get(Project, answer.project_id)
            held = held_values(project) if project is not None else None
        else:
            held = rows.get(answer.project_id)
        if held is None:
            out.absent.append(answer.project_id)
            continue
        value, precision = held[answer.field]
        out.fields[answer.field].outcomes[judge(answer, value, precision)] += 1
    return out


def snapshot_rows(path: Path) -> dict[int, Held]:
    """Every row's judgeable values in another database file, opened read-only."""
    from tracker.db import open_db, session_scope

    engine = open_db(path)
    try:
        with session_scope(engine, commit=False) as other:
            return {p.id: held_values(p) for p in other.scalars(select(Project))}
    finally:
        engine.dispose()


@dataclass(frozen=True)
class Moved:
    project_id: int
    field: str
    was: str
    now: str
    was_value: Any
    now_value: Any


def compare(
    session: Session, before: Mapping[int, Held], *, statuses: tuple[str, ...] = ("verified",)
) -> list[Moved]:
    """Every answer whose outcome differs between `before` and the database now."""
    out: list[Moved] = []
    for answer in answers(session, statuses):
        project = session.get(Project, answer.project_id)
        then = before.get(answer.project_id)
        if project is None or then is None:
            continue
        now_value, now_precision = held_values(project)[answer.field]
        was_value, was_precision = then[answer.field]
        was = judge(answer, was_value, was_precision)
        now = judge(answer, now_value, now_precision)
        if was != now:
            out.append(Moved(answer.project_id, answer.field, was, now, was_value, now_value))
    return out


def wilson(right: int, judged: int, z: float = 1.96) -> tuple[float, float] | None:
    """The 95% Wilson interval for `right` of `judged`; None when nothing was judged.

    Wilson rather than right/judged ± 1.96·se: the key is small (a few hundred
    answers, a few dozen per field) and the simple interval runs past 0 and 1 there.
    """
    if judged == 0:
        return None
    p = right / judged
    centre = (p + z * z / (2 * judged)) / (1 + z * z / judged)
    half = (
        z * math.sqrt(p * (1 - p) / judged + z * z / (4 * judged * judged)) / (1 + z * z / judged)
    )
    return max(0.0, centre - half), min(1.0, centre + half)


# --- merges --------------------------------------------------------------------------


def repoint(session: Session, old_id: int, new_id: int) -> int:
    """Carry a folded row's answers to the survivor. Returns how many moved.

    Called by `merge.merge_projects` before it deletes the folded row, whose answers
    would otherwise go with it by cascade. The two rows were one campus, so an answer
    the survivor already holds for the same field stands and the folded one is
    dropped.
    """
    held = set(session.scalars(select(AnswerKey.field).where(AnswerKey.project_id == new_id)))
    moved = 0
    for row in session.scalars(select(AnswerKey).where(AnswerKey.project_id == old_id)).all():
        if row.field in held:
            session.delete(row)
            continue
        row.project_id = new_id
        moved += 1
    session.flush()
    return moved
