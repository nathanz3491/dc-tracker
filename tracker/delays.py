"""Suspected delays: a campus whose own online date has passed with nothing saying it runs.

The client's request: many projects carry an expected online date, and once that
date has passed with no news of the campus going live, the project should be flagged
as a suspected delay. Until this module nothing on the console said so — the one
place the condition was computed was a finding in `tracker logic check`, which no
reader of the console sees.

**One rule, one function.** :func:`assess` is the only definition. `tracker logic
check` raises `past_its_own_date` from it (`logic.check_rules`), the export ships it
on every project (`export.to_json_object`), the console's table filters and sorts on
it (`webui/query.py`), the company page counts it (`company.profile`) and the API
carries it. Two copies of "is this campus late" would part company on the first date
that sits on a boundary — a period not yet over, a tranche still to come — and a
reader who sees a chip the command line does not report, or the reverse, has caught
the product contradicting itself.

What it asks, in order:

1. **A stated online date whose period is over.** "Q3 2025" is stored as
   2025-07-01 and "2026" as 2026-01-01; a source promising a period has not missed
   it until the period ends (:func:`period_end`). *Stated* means a citation says it
   in words the evidence gate verified (`facts.stated`): a 待确认 date, or a
   directory's copy, does not make a campus late. On 2026-10-24 five of the ten
   campuses past their date rested on such a date. Four were records to fix: a
   directory page's guess (Carter's Store), a month an article never gave (Compass
   Hoffman Estates, where the article said "2026"), a misread copied by a directory
   (NTT Gainesville), a 2013 announcement (Savvis MP2). The fifth, DC Blox Conyers,
   was a real slip whose date had been taken without a quote; a re-read that quotes
   it flags it again.
2. **A phase that does not already settle it.** An operational campus is not late,
   and a paused or cancelled one has a status of its own: calling a stopped campus
   "delayed" would bury the more important fact under a softer one.
3. **No news of going live.** No capacity a source says is running (`mw_built`), no
   tranche running, and no energisation milestone dated on or after the expected
   date that is more than a forecast. Running capacity is asked first because the
   row shows it: on a production copy on 2026-10-20, Red Oak read "360 MW running of
   360" beside a 2020 date and would have carried "no news of going live" next to it.
   An energisation from *before* the date is not that news: it is usually an earlier
   phase, and it cannot show the date being met. **Nor a source listing it as
   operational**, read on or after the date (:func:`listed_running`) -- a directory
   included, though a directory's word does not make the row's phase. The flag says
   "nothing reported says it runs"; Aligned's Sterling building was listed as
   operational since 2023 by three directories and flagged anyway, because no press
   release announced the opening.
4. **No tranche still to come.** A campus whose date passed while a later tranche
   runs to its own schedule is not late: the date on the row was the first phase's.

A utility's plant filed as tranches of the campus (`blocks.is_generation`) is not the
campus, so it is neither running evidence nor a tranche still to come.

**It is a suspicion, and nothing here writes.** The two usual causes are a slip
nobody reported and a campus that went live without an article we read. Both call
for a crawl, not an edit, which is why the finding's one action in `logic` is the
narrow "this date is stale" and the console shows a chip rather than changing a
value.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.blocks import is_generation
from tracker.vocab import BLOCK_LIVE, BLOCK_TERMINAL, PHASE_TERMINAL

#: Phases that already say the campus is running.
LIVE_PHASES: Final[frozenset[str]] = frozenset({"operational"})

#: Months a date stated at each precision spans, from the day it is stored as.
PRECISION_MONTHS: Final[dict[str, int]] = {"month": 1, "quarter": 3, "half": 6, "year": 12}

#: What each precision is called when a period of that length "ended".
_PERIOD_NOUN: Final[dict[str, str]] = {
    "month": "month",
    "quarter": "quarter",
    "half": "half-year",
    "year": "year",
}

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: How a tranche stands against its own date. See :func:`tranche_state`.
LIVE: Final = "live"
STOPPED: Final = "stopped"
COMING: Final = "coming"
LATE: Final = "late"
UNDATED: Final = "undated"


def as_of() -> dt.date:
    """The day every "has it passed" question is asked on: UTC, like `logic._today`."""
    from tracker.models import utcnow

    return utcnow().date()


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    return value if isinstance(value, dt.date) else None


def read_as(value: dt.date, precision: str | None) -> str:
    """The precision a stored date is read at: the recorded one, or its shape.

    NULL precision means "day, or nothing recorded" (`upsert.apply_date_precision`),
    and most of the unrecorded ones are a model writing a bare year as `YYYY-01-01`
    or a month as `YYYY-MM-01`. On the first of a month this reads the wider period,
    because the cost is asymmetric: the wider reading delays a warning, the narrower
    one calls a campus late — and offers to clear its date — while the period it was
    promised for is still running.
    """
    if precision in PRECISION_MONTHS or precision == "day":
        return precision
    if precision is None and value.day == 1:
        return "year" if value.month == 1 else "month"
    return "day"


def period_end(value: dt.date, precision: str | None) -> dt.date:
    """The last day a date stated at `precision` still covers.

    "The second half of 2026" is stored as 2026-07-01 and "2026" as 2026-01-01, so
    comparing the stored day with today calls both overdue the morning they start.
    On 2026-10-13 that is what `past_its_own_date` did to Vantage Frontier and Novva
    Mesa, and the resolver then cleared two correct dates. A source promising a
    period has not missed it until the period is over.

    **The calendar period the date falls in**, not a span counted from the stored day.
    A hedge is stored inside its year — "late 2028" as 2028-10-01, "summer of 2025" as
    2025-06-01 — and read as the year (`normalize.precision_in_quote`). Counted from
    the day, that year would have run to 30 September 2029.
    """
    how = read_as(value, precision)
    months = PRECISION_MONTHS.get(how)
    if not months:
        return value
    last = ((value.month - 1) // months + 1) * months
    return dt.date(value.year + last // 12, last % 12 + 1, 1) - dt.timedelta(days=1)


_MOVED: Final = re.compile(r"moved from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})")


def restates(event: Any) -> bool:
    """Whether a derived `delayed` milestone only narrowed the date it moved.

    `upsert._record_slippage` once wrote one whenever the stored date moved later,
    even inside the period the old date promised — "2028" becoming "June 2028" —
    worded "this may be a more precise restatement rather than a delay". Every reader
    but one showed it as a delay. A milestone a source wrote (`source_id`) is never
    read this way: only the derived ones carry the "moved from … to …" sentence, and
    the old date is read the way an unrecorded one is (:func:`read_as`).
    """
    if getattr(event, "event_type", None) != "delayed" or getattr(event, "source_id", None):
        return False
    found = _MOVED.search(getattr(event, "description", "") or "")
    if not found:
        return False
    before, after = (dt.date.fromisoformat(day) for day in found.groups())
    return after <= period_end(before, None)


def stated(value: dt.date, precision: str | None) -> str:
    """The date as the source gave it: `Q3 2025`, `H2 2026`, `2026`, `Jul 2026`."""
    how = read_as(value, precision)
    if how == "year":
        return str(value.year)
    if how == "half":
        return f"H{1 if value.month <= 6 else 2} {value.year}"
    if how == "quarter":
        return f"Q{(value.month - 1) // 3 + 1} {value.year}"
    if how == "month":
        return f"{_MONTHS[value.month - 1]} {value.year}"
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


@dataclass(frozen=True)
class SuspectedDelay:
    """Why one campus reads as late, in the parts every surface needs."""

    #: The date as stored on the row, and the precision its source recorded — None
    #: for a day, or for a source that recorded none.
    expected_online: dt.date
    precision: str | None
    #: The precision the rule read it at, which is `precision` where one was
    #: recorded and the stored day's shape where not (:func:`read_as`).
    read_as: str
    #: The date the way a reader would write it: "Q3 2025".
    stated: str
    #: The last day the stated period covered. The campus became late the day after.
    due: dt.date
    #: Days from `due` to `as_of`.
    days_late: int
    #: The day this was decided on, so a figure read later says what it was relative to.
    as_of: dt.date
    #: The phase the row holds, which is what "nothing says it runs" rests on.
    phase: str
    #: One sentence, in the reader's terms.
    reason: str

    def as_json(self) -> dict[str, Any]:
        return {
            "expected_online": self.expected_online.isoformat(),
            "precision": self.precision,
            "read_as": self.read_as,
            "stated": self.stated,
            "due": self.due.isoformat(),
            "days_late": self.days_late,
            "as_of": self.as_of.isoformat(),
            "reason": self.reason,
        }


def tranche_state(block: Any, today: dt.date) -> str:
    """Where one tranche stands against its own date.

    `live` when it is running, or a source dates its energisation on or before
    today; `stopped` when paused or cancelled; `coming` when a source gives it an
    energisation date still ahead or its stated period is not over; `late` when that
    period is over and none of the above; `undated` when it carries no date at all.

    A tranche records no precision of its own, so its date is read the way an
    unrecorded project date is (:func:`read_as`): the first of a month as that month,
    1 January as the whole year.
    """
    status = getattr(block, "status", "") or ""
    if status in BLOCK_TERMINAL:
        return STOPPED
    energized = _as_date(getattr(block, "energized_on", None))
    if status in BLOCK_LIVE or (energized is not None and energized <= today):
        return LIVE
    if energized is not None:
        return COMING
    when = _as_date(getattr(block, "expected_online", None))
    if when is None:
        return UNDATED
    return LATE if period_end(when, None) < today else COMING


def _is_generation(block: Any) -> bool:
    return is_generation(getattr(block, "label", None), getattr(block, "parent", None))


def _went_live_since(event: Any, expected: dt.date, today: dt.date) -> bool:
    """An energisation dated on or after the expected date, and more than a forecast.

    Read the way `tracks.standing` reads a milestone: a forecast is not history even
    once its date has passed, and one dated after today has not happened.
    """
    if getattr(event, "event_type", None) != "energized":
        return False
    if getattr(event, "unconfirmed", None) == "forecast":
        return False
    when = _as_date(getattr(event, "event_date", None))
    return when is not None and expected <= when <= today


def _reason(read: str, shown: str, days: int, phase: str) -> str:
    ago = f"{days} day{'' if days == 1 else 's'} ago"
    noun = _PERIOD_NOUN.get(read)
    when = f"a {noun} that ended {ago}" if noun else ago
    return (
        f"Expected online {shown}, {when}; the phase is still {phase or 'unset'} "
        "and nothing reported since says it is running."
    )


def assess(
    *,
    expected_online: Any,
    precision: str | None,
    phase: str | None,
    mw_built: float | None,
    blocks: Iterable[Any],
    events: Iterable[Any],
    today: dt.date,
    checked_at: Any = None,
    require_check: bool = False,
    sources: Iterable[Any] | None = None,
) -> SuspectedDelay | None:
    """The one definition of a suspected delay. See the module docstring for the rule.

    Structural on purpose: `blocks`, `events` and `sources` are anything carrying the
    attributes the ORM rows have, so the batch reader below can hand in rows it
    loaded in four queries and get exactly the answer `suspected_delay` gives for
    one project.

    `sources` are the campus's citations, which rule 1's "stated" (`facts.stated`)
    and rule 3's listing (:func:`listed_running`) read; None skips both, for a caller
    asking about a date alone. `require_check` adds the rule's fifth condition
    (:func:`searched_since_due`): the project readers pass it, with the campus's
    `golive_checked_at`; the news watch leaves it off to find the campuses that still
    need their search.
    """
    from tracker.facts import stated as facts_stated

    expected = _as_date(expected_online)
    if expected is None:
        return None
    cited = None if sources is None else list(sources)
    if cited is not None and not facts_stated(expected, cited, "expected_online"):
        return None
    due = period_end(expected, precision)
    if due >= today:
        return None
    stage = (phase or "").lower()
    if stage in LIVE_PHASES or stage in PHASE_TERMINAL:
        return None
    if mw_built and mw_built > 0:
        return None
    campus = [b for b in blocks if not _is_generation(b)]
    states = {tranche_state(b, today) for b in campus}
    if LIVE in states or COMING in states:
        return None
    if any(_went_live_since(e, expected, today) for e in events):
        return None
    if cited is not None and listed_running(cited, expected):
        return None
    if require_check and not searched_since_due(checked_at, due):
        return None

    read = read_as(expected, precision)
    shown = stated(expected, precision)
    days = (today - due).days
    return SuspectedDelay(
        expected_online=expected,
        precision=precision,
        read_as=read,
        stated=shown,
        due=due,
        days_late=days,
        as_of=today,
        phase=stage,
        reason=_reason(read, shown, days, stage),
    )


def listed_running(sources: Iterable[Any], expected: dt.date) -> bool:
    """Whether a citation read on or after `expected` lists the campus as operational.

    A directory counts (`facts.stated` at the `directory` tier as well as
    `confirmed`): its word does not set the row's phase, which a source of the top
    tier decides, but it is a report that the campus runs, and the flag says that
    nothing reported does. Aligned's Sterling building (IAD-03) was due in fall 2023
    and listed as operational by three directories, with no press release; it was
    flagged for three years. A 待确认 "operational" -- a model's reading no quote
    bore out -- does not count, and nor does a listing read before the date, which
    cannot show the date being met.
    """
    from tracker.facts import stated as facts_stated

    since = [s for s in sources if (_read_on(s) or dt.date.min) >= expected]
    return facts_stated("operational", since, "phase", tiers=("confirmed", "directory"))


def _read_on(source: Any) -> dt.date | None:
    """When a citation was published, else read."""
    return _as_date(getattr(source, "published_at", None)) or _as_date(
        getattr(source, "fetched_at", None)
    )


def searched_since_due(checked_at: Any, due: dt.date) -> bool:
    """Whether a go-live search ran after the stated period ended (rule 5).

    The client's rule is "past its date, and a search still finds no news of it
    going live" — not "past its date, and nothing we happen to hold says so". A
    campus that went live years ago without an article we read is not late; it is
    missing a fact, and the search is how the fact is found (`ingest.watch`). A
    search made before the period ended does not count: it could not have seen the
    campus miss it. Whatever the search found is queued and read before anything else
    in the queue (`ingest.discover.deciding`), so a campus it shows running leaves the
    list that night, through its phase.
    """
    when = _as_date(checked_at)
    return when is not None and when > due


def suspected_delay(
    project: Any, *, today: dt.date | None = None, require_check: bool = True
) -> SuspectedDelay | None:
    """Whether one project is a suspected delay, and why. Reads its tranches,
    milestones and citations."""
    return assess(
        expected_online=getattr(project, "expected_online", None),
        precision=getattr(project, "expected_online_precision", None),
        phase=getattr(project, "phase", None),
        mw_built=getattr(project, "mw_built", None),
        blocks=getattr(project, "blocks", ()) or (),
        events=getattr(project, "events", ()) or (),
        today=today if today is not None else as_of(),
        checked_at=getattr(project, "golive_checked_at", None),
        require_check=require_check,
        sources=getattr(project, "sources", ()) or (),
    )


def suspected(
    session: Session, *, today: dt.date | None = None, require_check: bool = True
) -> dict[int, SuspectedDelay]:
    """Every suspected delay in the database, by project id.

    For the readers that ask about the whole fleet at once — the console's light
    index, the table's filter and sort — and must not hydrate every project to do
    it. Four queries: the rows whose stored date has passed and whose phase leaves
    the question open (a period cannot end before the day it starts on, so nothing
    else can qualify), then the tranches, energisations and citations of only those
    rows. Every answer still comes from :func:`assess`.
    """
    from tracker.models import CapacityBlock, Event, Project, Source

    when = today if today is not None else as_of()
    open_phases = LIVE_PHASES | set(PHASE_TERMINAL)
    candidates = (
        select(Project.id)
        .where(Project.expected_online.is_not(None))
        .where(Project.expected_online < when)
        .where(Project.phase.not_in(sorted(open_phases)))
    )
    rows = session.execute(
        select(
            Project.id,
            Project.expected_online,
            Project.expected_online_precision,
            Project.phase,
            Project.mw_built,
            Project.golive_checked_at,
        ).where(Project.id.in_(candidates))
    ).all()
    if not rows:
        return {}

    blocks: dict[int, list[Any]] = {}
    for block in session.scalars(
        select(CapacityBlock).where(CapacityBlock.project_id.in_(candidates))
    ):
        blocks.setdefault(block.project_id, []).append(block)
    events: dict[int, list[Any]] = {}
    for event in session.scalars(
        select(Event).where(Event.project_id.in_(candidates), Event.event_type == "energized")
    ):
        events.setdefault(event.project_id, []).append(event)
    cited: dict[int, list[Any]] = {}
    for source in session.execute(
        select(
            Source.id,
            Source.project_id,
            Source.url,
            Source.source_type,
            Source.extractor,
            Source.claims,
            Source.fields,
            Source.unconfirmed_fields,
            Source.unconfirmed_reasons,
            Source.published_at,
            Source.fetched_at,
        ).where(Source.project_id.in_(candidates))
    ):
        cited.setdefault(source.project_id, []).append(source)

    out: dict[int, SuspectedDelay] = {}
    for pid, expected, precision, phase, built, checked in rows:
        found = assess(
            expected_online=expected,
            precision=precision,
            phase=phase,
            mw_built=built,
            blocks=blocks.get(pid, ()),
            events=events.get(pid, ()),
            today=when,
            checked_at=checked,
            require_check=require_check,
            sources=cited.get(pid, ()),
        )
        if found is not None:
            out[pid] = found
    return out


#: How long a flagged campus goes before its go-live search is made again. The
#: client's rule is that a search *still* finds no news: searched once, Tesla's Dojo
#: stayed flagged with a query that could not find its 2025 shutdown, and nothing
#: would ever have asked again. A month keeps the cost to about one search a day.
GOLIVE_RECHECK: Final = dt.timedelta(days=30)


def awaiting_search(
    session: Session, *, today: dt.date | None = None
) -> list[tuple[int, SuspectedDelay]]:
    """Campuses whose go-live search is due, never-searched first, most overdue first.

    What the news watch's go-live check works through (`ingest.watch.run`): each is
    late by the record, and either has not been searched since its period ended or
    -- flagged, then -- was last searched more than :data:`GOLIVE_RECHECK` ago. The
    search a flag rests on stands meanwhile: a campus due its repeat stays flagged
    until the repeat finds something, not hidden while it waits.
    """
    from tracker.models import Project

    when = today if today is not None else as_of()
    unchecked = suspected(session, today=when, require_check=False)
    if not unchecked:
        return []
    checked = dict(
        session.execute(
            select(Project.id, Project.golive_checked_at).where(Project.id.in_(list(unchecked)))
        ).all()
    )
    stale = when - GOLIVE_RECHECK
    first, again = [], []
    for pid, late in unchecked.items():
        last = _as_date(checked.get(pid))
        if not searched_since_due(last, late.due):
            first.append((pid, late))
        elif last <= stale:
            again.append((pid, late))
    first.sort(key=lambda item: (-item[1].days_late, item[0]))
    again.sort(key=lambda item: (_as_date(checked.get(item[0])), item[0]))
    return first + again


__all__ = [
    "COMING",
    "GOLIVE_RECHECK",
    "LATE",
    "LIVE",
    "LIVE_PHASES",
    "PRECISION_MONTHS",
    "STOPPED",
    "UNDATED",
    "SuspectedDelay",
    "as_of",
    "assess",
    "awaiting_search",
    "listed_running",
    "period_end",
    "read_as",
    "restates",
    "searched_since_due",
    "stated",
    "suspected",
    "suspected_delay",
    "tranche_state",
]
