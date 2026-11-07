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

**Each item says when the value now held was read** (`Item.read_at`): the read time of
the citation behind it, when that citation was read tonight. The night matches it to
the step that was running then, so the director is told which step moved an answer.
On 2026-11-06 it was told only "1 stopped being right", blamed the rulings, and ran six
logic findings all night; the answer had been moved by the news read, which no
director chooses. A value with no citation read tonight was moved by a ruling, a merge
or a re-derive.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

#: Answers lost net (stopped being right, less became right) that end a directed night.
NET_LOSS_STOPS: Final = 2
#: How old the snapshot a flip-back is measured against should be.
WEEK: Final = dt.timedelta(days=7)


@dataclass(frozen=True)
class Item:
    """One thing the check found, and when the value now held was read."""

    project_id: int
    field: str
    #: lost (an answer stopped being right), gained, flip (back to a week ago), unbacked.
    kind: str
    text: str
    #: When the citation now behind the value was read (naive UTC), if tonight.
    read_at: dt.datetime | None = None


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
    items: list[Item] = field(default_factory=list)

    def add(self, item: Item) -> None:
        self.items.append(item)
        self.detail.append(item.text)

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
            "items": [
                {**vars(i), "read_at": i.read_at.isoformat() if i.read_at else None}
                for i in self.items[:40]
            ],
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


def _read_at(
    session: Any, project_id: int, field_name: str, since: dt.datetime | None
) -> dt.datetime | None:
    """When the citation now behind a value was read, if that was at or after `since`."""
    from sqlalchemy import select

    from tracker import changes
    from tracker.models import Project, Source

    project = session.get(Project, project_id)
    if project is None or getattr(project, field_name, None) is None:
        return None
    url = changes.backing(project, field_name).url
    if not url:
        return None
    read = session.scalar(
        select(Source.fetched_at)
        .where(Source.project_id == project_id, Source.url == url)
        .order_by(Source.fetched_at.desc())
        .limit(1)
    )
    if read is None or (since is not None and read < since):
        return None
    return read


def check(
    db: Path, before: Path, week: Path | None = None, *, since: dt.datetime | None = None
) -> GuardResult:
    """Score the database against `before` (tonight's first snapshot) and `week`.

    `since` is when the night began (naive UTC): a citation read after it is tonight's.
    Without it, every citation behind a moved value counts as tonight's.
    """
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
                    kind, text = "gained", f"#{m.project_id} {m.field}: now right ({m.now_value!r})"
                elif m.was == "right":
                    out.stopped_right += 1
                    kind = "lost"
                    text = (
                        f"#{m.project_id} {m.field}: was right ({m.was_value!r}), "
                        f"now {m.now} ({m.now_value!r})"
                    )
                else:
                    continue
                read = _read_at(session, m.project_id, m.field, since)
                out.add(Item(m.project_id, m.field, kind, text, read))

            diff = changes.diff(before, session)
            for c in diff.values:
                if c.after is not None and not (c.quote or c.url or c.via or c.derived):
                    out.unbacked += 1
                    text = f"#{c.project_id} {c.field}: {c.before!r} -> {c.after!r} with nothing behind it"
                    out.add(Item(c.project_id, c.field, "unbacked", text))

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
                            text = f"#{project.id} {name}: back to {now!r} after {middle!r}"
                            read = _read_at(session, project.id, name, since)
                            out.add(Item(project.id, name, "flip", text, read))
    finally:
        engine.dispose()
    return out


__all__ = ["NET_LOSS_STOPS", "WEEK", "GuardResult", "Item", "check", "week_old"]
