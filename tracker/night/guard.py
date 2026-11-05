"""The accuracy check: did tonight make anything worse? Read-only, free.

Every count the loop moves — rows below T2, findings, duplicate groups — can be bought
with wrong values: a figure with a real quote from the wrong campus fills a field as
well as the right one does. So a night is also judged on three things counts cannot see:

* **the answer key** — of the verified answers, how many the night turned right and
  how many it turned from right to something else (`tracker.answer_key.compare`
  against the snapshot taken before round 1);
* **flip-backs** — values the night set back to what they were a week ago, undoing a
  change some night in between made. One of the two changes was wrong;
* **nothing behind it** — values the night set that no citation, obstacle or tranche
  states (`tracker.changes`, the morning report's "NOTHING" lines).

A directed night that loses two or more answers net is handed back to the fixed
schedule (`tracker.overnight`); every night's report prints the check.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

#: Answers lost net (stopped being right, less became right) that end a directed night.
NET_LOSS_STOPS: Final = 2
#: How old the snapshot a flip-back is measured against should be.
WEEK: Final = dt.timedelta(days=7)


@dataclass
class GuardResult:
    became_right: int = 0
    stopped_right: int = 0
    answers: int = 0
    flipped_back: int = 0
    unbacked: int = 0
    #: One short line per item, for the report and the director.
    detail: list[str] = field(default_factory=list)
    #: The snapshot flip-backs were measured against, or "" when there was none.
    week: str = ""

    @property
    def net_loss(self) -> int:
        return self.stopped_right - self.became_right

    @property
    def worse(self) -> bool:
        return self.net_loss >= NET_LOSS_STOPS

    def summary(self) -> str:
        week = (
            f"{self.flipped_back} value(s) set back to a week ago"
            if self.week
            else "no week-old snapshot"
        )
        return (
            f"answer key: {self.became_right} became right, {self.stopped_right} stopped being "
            f"right (of {self.answers}); {week}; {self.unbacked} value(s) with nothing behind them"
        )

    def as_json(self) -> dict:
        return {
            "became_right": self.became_right,
            "stopped_right": self.stopped_right,
            "answers": self.answers,
            "flipped_back": self.flipped_back,
            "unbacked": self.unbacked,
            "net_loss": self.net_loss,
            "week": self.week,
            "detail": self.detail[:40],
        }


def week_old(backups: Path, now: dt.datetime) -> Path | None:
    """The nightly snapshot nearest seven days before `now`, at least six days old."""
    best: tuple[float, Path] | None = None
    for path in backups.glob("tracker.backup-overnight-*.db"):
        try:
            stamp = dt.datetime.strptime(
                path.stem.removeprefix("tracker.backup-overnight-"), "%Y%m%d-%H%M%S"
            )
        except ValueError:
            continue
        age = now.replace(tzinfo=None) - stamp
        if age < dt.timedelta(days=6):
            continue
        distance = abs((age - WEEK).total_seconds())
        if best is None or distance < best[0]:
            best = (distance, path)
    return best[1] if best else None


def check(db: Path, before: Path, week: Path | None = None) -> GuardResult:
    """Score the database against `before` (tonight's first snapshot) and `week`."""
    from sqlalchemy import select

    from tracker import answer_key, changes
    from tracker.db import open_db, session_scope
    from tracker.models import Project

    out = GuardResult(week=week.name if week else "")
    engine = open_db(db)
    try:
        with session_scope(engine, commit=False) as session:
            rows = answer_key.snapshot_rows(before)
            moved = answer_key.compare(session, rows)
            out.answers = answer_key.score(session).total.judged
            for m in moved:
                if m.now == "right":
                    out.became_right += 1
                elif m.was == "right":
                    out.stopped_right += 1
                    out.detail.append(
                        f"#{m.project_id} {m.field}: was right ({m.was_value!r}), now {m.now} ({m.now_value!r})"
                    )

            diff = changes.diff(before, session)
            for c in diff.values:
                if c.after is not None and not (c.quote or c.url or c.via or c.derived):
                    out.unbacked += 1
                    out.detail.append(
                        f"#{c.project_id} {c.field}: {c.before!r} -> {c.after!r} with nothing behind it"
                    )

            if week is not None:
                then = changes._snapshot_rows(week)
                start = changes._snapshot_rows(before)
                for project in session.scalars(select(Project)):
                    old, mid = then.get(project.id), start.get(project.id)
                    if old is None or mid is None:
                        continue
                    if old.dedup_key != project.dedup_key or mid.dedup_key != project.dedup_key:
                        continue
                    for name in changes.FIELDS:
                        was = changes._norm(getattr(old, name))
                        middle = changes._norm(getattr(mid, name))
                        now = changes._norm(getattr(project, name))
                        if middle != now and now == was and was != middle:
                            out.flipped_back += 1
                            out.detail.append(
                                f"#{project.id} {name}: back to {now!r} after {middle!r}"
                            )
    finally:
        engine.dispose()
    return out


__all__ = ["NET_LOSS_STOPS", "WEEK", "GuardResult", "check", "week_old"]
