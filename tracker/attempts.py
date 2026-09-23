"""Fields we have already gone looking for and not found, so we stop paying to re-ask.

`enrich --agent` costs ~77,000 tokens a row. Some of the fields it chases will never
be filled, because nobody published them — `expected_online` on a campus whose
operator has announced nothing beyond a groundbreaking is the ordinary case, not the
exception. Without a memory of the attempt, every run asks again, at full price, and
gets the same silence.

**The record is prose in `project.notes`, with no marker.** Not a new table. That is
this codebase's settled answer to "where does a decision about a row live", stated at
:func:`tracker.audit.settled_codes`: a column "would need a migration, would have to
be kept in step with merges, and would say less than the sentence does".
`upsert._merge_notes` is what makes it durable — operator prose carries no marker and
is the one class of line re-ingesting never regenerates or deletes, where a
`[tracker]` line would be wiped by the next crawl.

**An attempt expires when the evidence changes, and that is the whole design.**
:func:`tracker.audit.settled_codes` records what goes wrong when a decision never
expires: on Hyperion (#10) a settled code muzzled the detector on the row where it
had most recently been right. The same trap is here — "nobody published `mw_built`"
is true until an article does, and a permanent skip would never look again. So every
attempt stores the citation count at the time, and a field reopens the moment the row
gains evidence it did not have. Attempts are capped, not final.

Placeholder and derived citations are excluded from that count deliberately. A Census
row confirming a county is not new testimony about a campus, and a placeholder seed
URL is not testimony at all — counting either would reopen a field on the strength of
nothing, which is the reverse of what the count is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

#: Attempts at one field before it is left alone. Two rather than one: the first may
#: have run before the row had the citations that would have answered it, and the
#: second is cheap insurance against a single bad search. Three buys nothing — by
#: then the answer is that nobody has published it.
DEFAULT_MAX_ATTEMPTS: Final[int] = 2

#: The line this module writes and reads. Shaped after the `resolved \`code\`:` line
#: `logic.record_decision` writes, so the notes block reads as one register of
#: decisions rather than two competing formats.
_ATTEMPT: Final[re.Pattern[str]] = re.compile(
    r"found nothing for `([a-z][a-z0-9_]*)`: attempt (\d+), (\d+) citation"
)


@dataclass(frozen=True)
class Attempt:
    """What one field cost last time, and what the row knew when it was asked."""

    field: str
    attempts: int
    #: Citations the row held when the attempt was made. The expiry test.
    citations: int


def evidence_count(project: Any) -> int:
    """Citations that could actually answer a question about this project.

    The same exclusions `confidence.compute` applies, and for its reasons: a
    `derived:` row cites a real checkable document but is not testimony about the
    campus, and a `PLACEHOLDER` URL is a seed the operator has not replaced. Neither
    is evidence that a previously unanswerable field has become answerable.
    """
    from tracker.confidence import PLACEHOLDER_MARKER

    return sum(
        1
        for source in getattr(project, "sources", ())
        if not (source.extractor or "").startswith("derived:")
        and PLACEHOLDER_MARKER not in (source.url or "")
    )


def attempts(project: Any) -> dict[str, Attempt]:
    """Every field this row has been asked about and the state of that asking."""
    out: dict[str, Attempt] = {}
    for field, count, citations in _ATTEMPT.findall(getattr(project, "notes", "") or ""):
        out[field] = Attempt(field, int(count), int(citations))
    return out


def exhausted(project: Any, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> set[str]:
    """Fields not worth spending another call on *yet*.

    Both conditions must hold: the field has been asked about enough times, and the
    row has gained no citation since. A row that has grown new evidence reopens every
    field, which is the behaviour that keeps this a cap on waste rather than a
    permanent blind spot.
    """
    if max_attempts <= 0:
        return set()
    now = evidence_count(project)
    return {
        field
        for field, attempt in attempts(project).items()
        if attempt.attempts >= max_attempts and now <= attempt.citations
    }


def record(
    project: Any, fields: list[str] | tuple[str, ...] | set[str], *, by: str = "agent"
) -> None:
    """Note that these fields were looked for and not found.

    One line per field, rewritten rather than appended, so a field asked three times
    leaves one line saying "attempt 3" instead of three lines a reader has to count.
    The citation figure is re-stamped each time: it records what was known at the
    *latest* attempt, which is what the expiry test compares against.
    """
    from tracker.models import utcnow

    if not fields:
        return
    seen = attempts(project)
    citations = evidence_count(project)
    stamp = utcnow().date()

    kept = [
        line
        for line in (getattr(project, "notes", "") or "").splitlines()
        if line.strip() and _line_field(line) not in fields
    ]
    for field in sorted(fields):
        count = seen[field].attempts + 1 if field in seen else 1
        kept.append(
            f"{stamp} {by} found nothing for `{field}`: "
            f"attempt {count}, {citations} citation(s) at the time"
        )
    project.notes = "\n".join(kept)


def _line_field(line: str) -> str | None:
    """The field an attempt line is about, or None for any other note."""
    found = _ATTEMPT.search(line)
    return found.group(1) if found else None


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "Attempt",
    "attempts",
    "evidence_count",
    "exhausted",
    "record",
]
