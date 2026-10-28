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
attempt stores the citation count and the day, and a field reopens when the row gains
a citation **published on or after the day it was last looked for**. Attempts are
capped, not final.

**Published since, not merely attached since.** Until 2026-10-28 any citation the row
gained reopened every field, and the nightly news watch attaches articles every night
— on its first look at a campus, a year of them. On the 10-28 copy of production 85
searched-out fields stood reopened: 46 by articles published before the search that
gave up on them, 13 by citations fetched no later than the day of that search, 17 by
an article published since and 9 by undated pages fetched since. An article that was
already out there when the agent searched, and that the extractor has since read
without finding the figure, is not evidence the figure became answerable. Reopening on
it cost twice over: the row fell out of T2, and the next night's T2 pass picked it
first — a field or two short — to pay for the same search again: 18 of the 50 rows it
would have chosen that night.

An undated citation counts by the day it was fetched, so a page nobody can date still
reopens a field rather than being ignored; so does any line written without a day.

Placeholder and derived citations are excluded deliberately. A Census row confirming a
county is not new testimony about a campus, and a placeholder seed URL is not
testimony at all — counting either would reopen a field on the strength of nothing,
which is the reverse of what the count is for.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

#: Attempts at one field before it is left alone. Two rather than one: the first may
#: have run before the row had the citations that would have answered it, and the
#: second is cheap insurance against a single bad search. Three buys nothing — by
#: then the answer is that nobody has published it.
DEFAULT_MAX_ATTEMPTS: Final[int] = 2

#: The line this module writes and reads. Shaped after the `resolved \`code\`:` line
#: `logic.record_decision` writes, so the notes block reads as one register of
#: decisions rather than two competing formats. The leading day is optional because
#: the line is prose a person may also write; without it the field reopens on any
#: citation gained, the rule before the day was read.
_ATTEMPT: Final[re.Pattern[str]] = re.compile(
    r"(?:(?P<day>\d{4}-\d{2}-\d{2})[ \t]+\S+[ \t]+)?"
    r"found nothing for `(?P<field>[a-z][a-z0-9_]*)`: "
    r"attempt (?P<count>\d+), (?P<citations>\d+) citation"
)


@dataclass(frozen=True)
class Attempt:
    """What one field cost last time, and what the row knew when it was asked."""

    field: str
    attempts: int
    #: Citations the row held when the attempt was made. Nothing gained, nothing new.
    citations: int
    #: The day of the latest attempt, or None for a line that does not say. A citation
    #: published on or after it is what reopens the field (`exhausted`).
    day: dt.date | None = None


def _evidence(project: Any) -> Iterator[Any]:
    """The citations that could actually answer a question about this project.

    The same exclusions `confidence.compute` applies, and for its reasons: a
    `derived:` row cites a real checkable document but is not testimony about the
    campus, and a `PLACEHOLDER` URL is a seed the operator has not replaced. Neither
    is evidence that a previously unanswerable field has become answerable.
    """
    from tracker.confidence import PLACEHOLDER_MARKER

    for source in getattr(project, "sources", ()):
        if not (source.extractor or "").startswith("derived:") and PLACEHOLDER_MARKER not in (
            source.url or ""
        ):
            yield source


def evidence_count(project: Any) -> int:
    """How many citations could answer a question about this project (`_evidence`)."""
    return sum(1 for _source in _evidence(project))


def _day(value: Any) -> dt.date | None:
    """The calendar day of a stored timestamp, whatever shape it arrived in."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def published_since(project: Any, day: dt.date | None) -> bool:
    """Whether the row cites anything that may be news since `day`.

    An article published on or after that day counts — the same day too, since a
    search may not have indexed it yet. An undated page counts when it was fetched
    *after* that day, so a page nobody can date is still treated as possibly new
    rather than ignored; not on the day itself, because the harvest that re-reads a
    row's own pages runs minutes before the agent's search and stamps them with it.
    `None` — a line that never said when it was written — is answered yes, which is
    the rule from before the day was read.
    """
    if day is None:
        return True
    for source in _evidence(project):
        published = _day(getattr(source, "published_at", None))
        if published is not None:
            if published >= day:
                return True
            continue
        fetched = _day(getattr(source, "fetched_at", None))
        if fetched is not None and fetched > day:
            return True
    return False


def attempts(project: Any) -> dict[str, Attempt]:
    """Every field this row has been asked about and the state of that asking."""
    out: dict[str, Attempt] = {}
    for found in _ATTEMPT.finditer(getattr(project, "notes", "") or ""):
        field = found["field"]
        out[field] = Attempt(
            field, int(found["count"]), int(found["citations"]), _day(found["day"])
        )
    return out


def exhausted(project: Any, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> set[str]:
    """Fields not worth spending another call on *yet*.

    A field is exhausted once it has been asked about `max_attempts` times, and it
    stays so until the row gains a citation **published on or after** the day of the
    last attempt. That keeps this a cap on waste rather than a permanent blind spot,
    without letting an old article — attached later by the news watch, read by the
    extractor and found silent on the field — undo a search that already looked.
    """
    if max_attempts <= 0:
        return set()
    now = evidence_count(project)
    return {
        field
        for field, attempt in attempts(project).items()
        if attempt.attempts >= max_attempts
        and (now <= attempt.citations or not published_since(project, attempt.day))
    }


def record(
    project: Any, fields: list[str] | tuple[str, ...] | set[str], *, by: str = "agent"
) -> None:
    """Note that these fields were looked for and not found.

    One line per field, rewritten rather than appended, so a field asked three times
    leaves one line saying "attempt 3" instead of three lines a reader has to count.
    The day and the citation figure are re-stamped each time: they record what was
    known at the *latest* attempt, which is what the expiry test compares against.
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
    return found["field"] if found else None


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "Attempt",
    "attempts",
    "evidence_count",
    "exhausted",
    "published_since",
    "record",
]
