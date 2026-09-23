"""What a model was paid to look at and could not decide — so it is not asked again.

Every paid phase of the overnight loop selects its work the same way each round, and
until this module none of them remembered an undecided answer. A pair the agent left
alone, a ruling the rails refused, an audit finding the model declined, an obstacle it
judged unclear, a row whose missing fields nobody has published: each was selected
again next round, in the same order, and paid for again. `scripts/overnight.sh` stops
after two rounds in a row fail to move a count, so the waste was bounded by the stop
rule rather than by anything that knew the answer was already in.

**A decline holds while the evidence it was judged on is unchanged.** Each caller
hashes what the model was actually shown (:func:`fingerprint`) — both rows' citations
for a pair, the evidence block for an audit finding, the article behind an obstacle —
and a changed hash is a different question. That is the only condition under which a
second look could reach a different answer from the same model, so it is the only
thing worth paying for.

**And it lapses after :data:`COOLDOWN_DAYS` regardless**, because three of the five
phases let the model search the open web and the web changes while the row does not.
An enrich pass that found nothing published in September has not learned that nothing
will be published in October.

A decline is never a decision. Nothing here touches a row, and nothing reads a
decline as evidence about the data — the recorded decisions, the ones a reader should
see, stay in `project.notes` via `logic.record_decision`. `--again` on each command
ignores this store entirely.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import ModelDecline, Project, utcnow

#: How long a decline holds with nothing changed. Long enough that a nightly loop
#: asks each undecided question about once a month instead of once a round; short
#: enough that a figure first published after the look is still found.
COOLDOWN_DAYS: Final = 30

KINDS: Final = ("pair", "logic", "audit", "risk", "gapfill")


def fingerprint(*parts: Any) -> str:
    """A short, stable hash of whatever a question was asked with.

    JSON with sorted keys and `str` for anything JSON cannot spell (dates), so the
    same evidence hashes the same in every process and on every machine.
    """
    blob = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def citations(project: Project) -> list[tuple[Any, ...]]:
    """A row's citations as the parts a fingerprint needs, in a fixed order.

    The claims and the decisions recorded on them, not merely the URLs: a
    re-extraction that changes what an article is read as saying, or a ruling that
    supersedes one of its claims, is new evidence for every question about the row.
    """
    return sorted(
        (
            s.url,
            s.claims or "",
            s.unconfirmed_reasons or "",
            s.blocks or "",
            s.parties or "",
        )
        for s in (project.sources or ())
    )


def _fresh(row: ModelDecline, *, now: dt.datetime, cooldown_days: int) -> bool:
    return row.decided_at is not None and now - row.decided_at < dt.timedelta(days=cooldown_days)


def load(session: Session, kind: str) -> dict[str, ModelDecline]:
    """Every decline of one kind, by subject. One query for a whole selection."""
    rows = session.scalars(select(ModelDecline).where(ModelDecline.kind == kind)).all()
    return {row.subject: row for row in rows}


def holds(
    known: dict[str, ModelDecline],
    subject: str,
    print_: str,
    *,
    now: dt.datetime | None = None,
    cooldown_days: int = COOLDOWN_DAYS,
) -> bool:
    """Whether `subject` was declined on exactly this evidence, recently enough.

    Takes the mapping :func:`load` returns rather than a session, so a caller
    filtering hundreds of candidates reads the table once.
    """
    row = known.get(subject)
    if row is None or row.fingerprint != print_:
        return False
    return _fresh(row, now=now or utcnow(), cooldown_days=cooldown_days)


def record(
    session: Session,
    kind: str,
    subject: str,
    print_: str,
    *,
    outcome: str,
    reason: str = "",
    by: str = "agent",
) -> None:
    """Remember that `subject` was put to a model on this evidence and left undecided.

    Replaces any earlier look at the same subject: only the latest evidence matters.
    Flushed, never committed — the caller's transaction decides, exactly as it does
    for the decisions this sits beside.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown decline kind {kind!r}; expected one of {KINDS}")
    row = session.scalar(
        select(ModelDecline).where(ModelDecline.kind == kind, ModelDecline.subject == subject)
    )
    if row is None:
        row = ModelDecline(kind=kind, subject=subject)
        session.add(row)
    row.fingerprint = print_
    row.outcome = outcome[:80]
    row.reason = " ".join(str(reason or "").split())[:400] or None
    row.decided_by = by
    row.decided_at = utcnow()
    session.flush()


def forget(session: Session, kind: str, subject: str) -> None:
    """Drop a decline, when a later run did decide the question."""
    row = session.scalar(
        select(ModelDecline).where(ModelDecline.kind == kind, ModelDecline.subject == subject)
    )
    if row is not None:
        session.delete(row)
        session.flush()


def split(
    items: Iterable[Any],
    known: dict[str, ModelDecline],
    key: Any,
    *,
    again: bool = False,
) -> tuple[list[Any], int]:
    """`items` still worth asking, and how many were held back as already declined.

    `key(item)` returns `(subject, fingerprint)`. `again=True` holds nothing back —
    the escape hatch every command carries as `--again`.
    """
    kept: list[Any] = []
    held = 0
    now = utcnow()
    for item in items:
        if not again:
            subject, print_ = key(item)
            if holds(known, subject, print_, now=now):
                held += 1
                continue
        kept.append(item)
    return kept, held


__all__ = [
    "COOLDOWN_DAYS",
    "KINDS",
    "citations",
    "fingerprint",
    "forget",
    "holds",
    "load",
    "record",
    "split",
]
