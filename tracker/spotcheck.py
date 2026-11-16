"""A random sample of each night's changed values, for a person to judge right or wrong.

**Why this exists.** Nothing else measures whether what the loop *writes* is right.
The answer key (`tracker.answer_key`) is a regression check: it says a night did not
break one of 228 values somebody already verified. Those sit on the rows people have
corrected hardest, applying the key rewrote the values it scores, and a night can
write 80 values elsewhere and still report "0 stopped being right". The quote check
says a sentence exists, not that it is about this campus. Asking a second model is
more of the loop judging itself. A person reading a fair sample is the one measure
with ground truth behind it.

**Fair means random.** Each night the loop draws `SIZE` of the night's changed
figures uniformly at random (`draw`), seeded by the night's date so a re-run draws
the same ones. Not the suspicious ones, not the big ones: a sample chosen by any rule
measures that rule. The share judged right, with its 95% interval, is then an
estimate of the share right across *all* the night's changes to these fields.

**What is judged.** One question per row: was this change right? For a value set or
changed, "is the new value right for this campus?"; for a value emptied, "was the old
value wrong for this campus, so emptying it was right?". `unsure` is an answer — the
article is gone, or the truth is not knowable from what is published — and is
counted apart, not as right or wrong.

Drawn by the nightly loop at the end of the night (`overnight.Night.draw_checks`),
answered from the console's Admin page or `tracker checks mark`, and scored by
`tracker checks` and in each morning report.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.answer_key import wilson
from tracker.changes import Changes, ValueChange
from tracker.models import SpotCheck, utcnow

#: The figures and facts a reader of the tracker acts on. Left out: `name`, `company`,
#: `city` and `state`, which a night rarely moves and a merge moves wholesale; `county`,
#: looked up from the Census; and `blocker`, a one-line summary rewritten whenever an
#: obstacle is re-read — on 2026-11-15, 15 of the 80 changes were rewordings of one,
#: and a uniform draw would spend a fifth of the checker's time on them.
SAMPLED: tuple[str, ...] = (
    "phase",
    "mw_planned",
    "mw_built",
    "investment_usd",
    "expected_online",
    "first_announced",
    "customer",
)

#: Drawn a night. About ten minutes of a person's reading; after a week, roughly a
#: hundred judged changes, which puts the interval within about ±9 points.
SIZE = 15

VERDICTS: tuple[str, ...] = ("right", "wrong", "unsure")

_SNAPSHOT_DAY = re.compile(r"overnight-(\d{8})-\d{6}")


class CheckError(ValueError):
    """A mark that cannot be recorded: no such check, or not one of `VERDICTS`."""


def night_of(snapshot: Path) -> dt.date | None:
    """The night a loop snapshot was taken on, read from its name."""
    found = _SNAPSHOT_DAY.search(snapshot.name)
    if found is None:
        return None
    return dt.datetime.strptime(found.group(1), "%Y%m%d").date()


def _text(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def _value(raw: str | None) -> Any:
    return None if raw is None else json.loads(raw)


@dataclass(frozen=True)
class Drawn:
    night: dt.date
    #: Changes to `SAMPLED` fields that night, the population the sample stands for.
    candidates: int
    #: Rows written now. Zero when the night was drawn before: `draw` never redraws.
    drawn: int
    #: Rows the night holds after this call.
    held: int


def draw(session: Session, changes: Changes, night: dt.date, *, size: int = SIZE) -> Drawn:
    """Draw `size` of the night's changes to `SAMPLED` fields at random, once.

    A night already drawn is left as it is, so the loop re-running its report, or a
    person running this by hand, cannot swap judged rows for fresh ones.
    """
    held = session.scalars(select(SpotCheck).where(SpotCheck.night == night)).all()
    candidates = [c for c in changes.values if c.field in SAMPLED]
    if held:
        return Drawn(night, len(candidates), 0, len(held))
    picked = random.Random(night.toordinal()).sample(candidates, min(size, len(candidates)))
    for c in picked:
        session.add(_row(night, c))
    session.flush()
    return Drawn(night, len(candidates), len(picked), len(picked))


def _row(night: dt.date, c: ValueChange) -> SpotCheck:
    return SpotCheck(
        night=night,
        project_id=c.project_id,
        label=c.label,
        field=c.field,
        before=_text(c.before),
        after=_text(c.after),
        url=c.url or None,
        quote=c.quote or None,
        via=c.via or None,
        why=c.why or None,
        drawn_at=utcnow(),
    )


def mark(session: Session, check_id: int, verdict: str, *, by: str, note: str = "") -> SpotCheck:
    """Record a person's answer. A second answer replaces the first."""
    if verdict not in VERDICTS:
        raise CheckError(f"verdict must be one of {', '.join(VERDICTS)}, not {verdict!r}")
    check = session.get(SpotCheck, check_id)
    if check is None:
        raise CheckError(f"no check #{check_id}")
    check.verdict = verdict
    check.note = note.strip() or None
    check.checked_by = by
    check.checked_at = utcnow()
    session.flush()
    return check


def nights(session: Session) -> list[dt.date]:
    """Every night with a sample, newest first."""
    return list(
        session.scalars(select(SpotCheck.night).distinct().order_by(SpotCheck.night.desc()))
    )


def listing(session: Session, night: dt.date) -> list[SpotCheck]:
    return list(
        session.scalars(select(SpotCheck).where(SpotCheck.night == night).order_by(SpotCheck.id))
    )


def kind(check: SpotCheck) -> str:
    """`set`, `changed` or `emptied`: which question the check asks."""
    if check.after is None:
        return "emptied"
    return "set" if check.before is None else "changed"


#: Each field as the question names it: the person answering need not know the schema.
FIELD_WORDS: dict[str, str] = {
    "phase": "stage",
    "mw_planned": "planned capacity",
    "mw_built": "capacity built",
    "investment_usd": "investment",
    "expected_online": "expected online date",
    "first_announced": "first-announced date",
    "customer": "customer",
}


def question(check: SpotCheck) -> str:
    """The one question a person answers about this change, in words."""
    what = FIELD_WORDS.get(check.field, check.field)
    if kind(check) == "emptied":
        return (
            f"Was {fmt(check.field, _value(check.before))} wrong as this campus's {what}, "
            "so emptying it was right?"
        )
    return f"Is {fmt(check.field, _value(check.after))} right as this campus's {what}?"


def fmt(field_name: str, value: Any) -> str:
    """A value as a reader writes it: `$1,000,000,000`, `1,200 MW`, a date, a stage."""
    if value is None:
        return "empty"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        if field_name == "investment_usd":
            return f"${value:,}"
        return f"{value:,} MW" if field_name.startswith("mw_") else f"{value:,}"
    return str(value)


@dataclass
class Tally:
    right: int = 0
    wrong: int = 0
    unsure: int = 0
    open: int = 0

    def add(self, verdict: str | None) -> None:
        if verdict is None:
            self.open += 1
        else:
            setattr(self, verdict, getattr(self, verdict) + 1)

    @property
    def judged(self) -> int:
        """Right or wrong. `unsure` says nothing either way, so it is not in the share."""
        return self.right + self.wrong

    @property
    def share(self) -> float | None:
        return self.right / self.judged if self.judged else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson(self.right, self.judged)

    def line(self) -> str:
        if not self.judged:
            out = "nothing judged yet"
        else:
            low, high = self.interval or (0.0, 0.0)
            out = (
                f"{self.right} of {self.judged} right ({self.share:.0%}, "
                f"95% interval {low:.0%} to {high:.0%})"
            )
        extra = [f"{self.unsure} unsure"] if self.unsure else []
        extra += [f"{self.open} not yet answered"] if self.open else []
        return out + (f"; {', '.join(extra)}" if extra else "")


@dataclass
class Score:
    total: Tally = field(default_factory=Tally)
    by_field: dict[str, Tally] = field(default_factory=dict)
    by_kind: dict[str, Tally] = field(default_factory=dict)
    by_night: dict[dt.date, Tally] = field(default_factory=dict)


def score(session: Session, *, since: dt.date | None = None) -> Score:
    """Every sample from `since` on (all of them by default), tallied."""
    query = select(SpotCheck)
    if since is not None:
        query = query.where(SpotCheck.night >= since)
    return tally(session.scalars(query))


def tally(checks: Iterable[SpotCheck]) -> Score:
    out = Score()
    for check in checks:
        out.total.add(check.verdict)
        out.by_field.setdefault(check.field, Tally()).add(check.verdict)
        out.by_kind.setdefault(kind(check), Tally()).add(check.verdict)
        out.by_night.setdefault(check.night, Tally()).add(check.verdict)
    return out


def report_lines(session: Session, drawn: Drawn | None = None, *, today: dt.date) -> list[str]:
    """The morning report's lines: tonight's draw, and the last week's score."""
    lines = []
    if drawn is not None:
        lines.append(
            f"    tonight: {drawn.held} of {drawn.candidates} changed value(s) drawn for a "
            "person to judge — Admin page in the console, or `tracker checks`"
        )
    week = score(session, since=today - dt.timedelta(days=7))
    ever = score(session)
    lines.append(f"    last 7 nights: {week.total.line()}")
    lines.append(f"    since the first sample: {ever.total.line()}")
    return lines


def as_dict(check: SpotCheck) -> dict[str, Any]:
    """One check for the console: the change, what stood behind it, and its answer."""
    return {
        "id": check.id,
        "night": check.night.isoformat(),
        "project_id": check.project_id,
        "label": check.label,
        "field": check.field,
        "kind": kind(check),
        "before": _value(check.before),
        "after": _value(check.after),
        "question": question(check),
        "what": FIELD_WORDS.get(check.field, check.field),
        "before_text": fmt(check.field, _value(check.before)),
        "after_text": fmt(check.field, _value(check.after)),
        "url": check.url,
        "quote": check.quote,
        "via": check.via,
        "why": check.why,
        "verdict": check.verdict,
        "note": check.note,
        "checked_by": check.checked_by,
        "checked_at": check.checked_at.isoformat() if check.checked_at else None,
    }


def tally_dict(t: Tally) -> dict[str, Any]:
    low, high = t.interval or (None, None)
    return {
        "right": t.right,
        "wrong": t.wrong,
        "unsure": t.unsure,
        "open": t.open,
        "share": t.share,
        "low": low,
        "high": high,
    }


__all__ = [
    "FIELD_WORDS",
    "SAMPLED",
    "SIZE",
    "VERDICTS",
    "CheckError",
    "Drawn",
    "Score",
    "Tally",
    "as_dict",
    "draw",
    "fmt",
    "kind",
    "listing",
    "mark",
    "night_of",
    "nights",
    "question",
    "report_lines",
    "score",
    "tally",
    "tally_dict",
]
