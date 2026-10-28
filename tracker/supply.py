"""A company's next three years of supply, and how much of it could slip.

The client's request (2026-10-21): the company page should read in megawatts
rather than project counts, and it should say when one large project carries a
big share of the company's near-term supply and is at risk of delay — "this one
campus is 30% of the next three years, and it is at serious risk of slipping".

Two readings, both made from rows already stored. Nothing here writes.

**Supply by year.** Each campus the company is credited with adds the capacity it
is credited with that is not yet running, filed under the year its expected online
date falls in. A date whose stated period is over with nothing running is
*overdue*: the capacity is still expected and nobody has said when, so it is filed
under this year and marked, not dropped. A campus with no date, or an operating
campus whose date has passed, is *undated*: there is capacity still to come and
nothing says when. The window is this year and the two after it.

**Delay risk**, one level per campus — high, elevated or low — with the reasons,
each one something a reader can check on the project page:

* *high* — the stated online date has passed with no news of the campus running
  (`delays`); the date has slipped twice or more; an open obstacle a source says
  has stopped work;
* *elevated* — one slip; an open obstacle a source says moved the schedule or the
  scope; due within a year with no construction reported.

An operating campus is read the same way while it still has planned capacity
to deliver — Abilene runs 421 of its 1,200 MW, and the rest is what an investor is
asking about. Once nothing is left to deliver, and for a paused or cancelled
campus, the level is low: there is no date left to miss.

Only quoted obstacles and dated milestones count. An unquoted obstacle is a
model's guess that a sentence would not support, and a slip that may only be a
more precise restatement of the same year is not a slip. The levels are a reading
of evidence, not a forecast, and the page says which evidence each one rests on.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from tracker import delays
from tracker.vocab import OPEN_RISK_STATUS

#: Years in the supply window: this year and the two after it.
WINDOW_YEARS: Final[int] = 3

#: A campus at or above this share of the window is called out by name.
CONCENTRATION_SHARE: Final[float] = 0.2

#: Phases that mean no construction has been reported.
_BEFORE_CONSTRUCTION: Final[frozenset[str]] = frozenset({"announced", "permitting"})

#: How far ahead "due soon" looks, for a campus with no construction reported.
_SOON: Final[dt.timedelta] = dt.timedelta(days=365)

HIGH: Final = "high"
ELEVATED: Final = "elevated"
LOW: Final = "low"
_RANK: Final[dict[str, int]] = {LOW: 0, ELEVATED: 1, HIGH: 2}

#: A risk category in a reader's words — the console's `RISK_LABEL`, lower-cased.
_CATEGORY_WORDS: Final[dict[str, str]] = {
    "grid_capacity": "grid capacity",
    "transmission": "transmission",
    "permitting": "permitting",
    "environmental": "environmental review",
    "equipment_supply": "equipment supply",
    "chip_supply": "chip supply",
    "financing": "financing",
    "offtake": "no committed buyer",
    "community_opposition": "local opposition",
    "water": "water",
    "unclassified": "an unclassified obstacle",
}


@dataclass(frozen=True)
class DelayRisk:
    """One campus's level, and the evidence it rests on, in a reader's words."""

    level: str
    reasons: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {"level": self.level, "reasons": list(self.reasons)}


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    return value if isinstance(value, dt.date) else None


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def slips(events: Iterable[Any]) -> list[Any]:
    """The recorded slips of a campus's online date, oldest first.

    A `delayed` milestone, quoted or derived, unless it is a forecast or only
    narrowed the date inside the period it already promised (`delays.restates`) —
    the same test the logic check removes those by, so this reader and the page
    cannot disagree about which moves were late.
    """
    out = [
        e
        for e in events
        if getattr(e, "event_type", None) == "delayed"
        and getattr(e, "unconfirmed", None) != "forecast"
        and not delays.restates(e)
    ]
    return sorted(out, key=lambda e: _as_date(getattr(e, "event_date", None)) or dt.date.min)


def _categories(risks: list[Any]) -> str:
    names = sorted({_CATEGORY_WORDS.get(r.category, r.category.replace("_", " ")) for r in risks})
    return ", ".join(names)


def running_unknown(project: Any) -> bool:
    """An operating campus no source gives a running figure for.

    Its planned capacity less "nothing" is not what is still to come: something runs,
    and nobody has said how much. Englewood (#72) read "18 MW still to come" beside
    "in service since 2022", and 34 such campuses put 3,647 MW into their companies'
    future supply on the 10-22 copy of production. What is left there is unknown, and
    every reading below says so rather than counting the whole plan as future.
    """
    return (getattr(project, "phase", None) or "").lower() == "operational" and getattr(
        project, "mw_built", None
    ) is None


def _capacity_to_come(project: Any) -> bool:
    """Whether a campus still has planned capacity it does not yet run."""
    planned = getattr(project, "mw_planned", None)
    if planned is None or running_unknown(project):
        return False
    return (getattr(project, "mw_built", None) or 0.0) < planned


def delay_risk(
    project: Any,
    *,
    today: dt.date,
    suspected: delays.SuspectedDelay | None = None,
) -> DelayRisk:
    """How likely this campus is to miss its date, from evidence on the page.

    `suspected` is the campus's suspected delay when the caller already has it (the
    company page reads the whole fleet's at once); otherwise it is worked out here.
    """
    phase = (getattr(project, "phase", None) or "").lower()
    if phase in ("paused", "cancelled") or (
        phase == "operational" and not _capacity_to_come(project)
    ):
        return DelayRisk(LOW)
    if suspected is None:
        suspected = delays.suspected_delay(project, today=today)

    high: list[str] = []
    elevated: list[str] = []
    if suspected is not None:
        high.append(
            f"its stated online date ({suspected.stated}) passed "
            f"{_plural(suspected.days_late, 'day')} ago with no news of it running"
        )

    slipped = slips(getattr(project, "events", ()) or ())
    if phase == "operational":
        # Part of it runs. A slip to a date that has since gone by is the history of
        # what was built — Lake Mariner's "to 2020", IREN Childress's "to 2022" —
        # not a reading of what is still to come.
        slipped = [
            e
            for e in slipped
            if (when := _as_date(getattr(e, "event_date", None))) is not None
            and when.year >= today.year
        ]
    if len(slipped) >= 2:
        high.append(f"its online date has slipped {len(slipped)} times")
    elif slipped:
        when = _as_date(getattr(slipped[-1], "event_date", None))
        elevated.append(
            "its online date has slipped once" + (f", to {when.isoformat()}" if when else "")
        )

    open_risks = [
        r
        for r in getattr(project, "risks", ()) or ()
        if getattr(r, "status", None) == OPEN_RISK_STATUS
        and getattr(r, "unconfirmed", None) is None
    ]
    blocking = [r for r in open_risks if r.severity == "blocking"]
    material = [r for r in open_risks if r.severity == "material"]
    if blocking:
        high.append(
            f"{_plural(len(blocking), 'open obstacle')} a source says stopped work "
            f"({_categories(blocking)})"
        )
    if material:
        elevated.append(
            f"{_plural(len(material), 'open obstacle')} a source says moved the schedule "
            f"or scope ({_categories(material)})"
        )

    expected = _as_date(getattr(project, "expected_online", None))
    if (
        suspected is None
        and phase in _BEFORE_CONSTRUCTION
        and expected is not None
        and today
        <= delays.period_end(expected, getattr(project, "expected_online_precision", None))
        and expected <= today + _SOON
    ):
        elevated.append(
            f"due {delays.stated(expected, getattr(project, 'expected_online_precision', None))} "
            "with no construction reported"
        )

    if high:
        return DelayRisk(HIGH, tuple(high + elevated))
    if elevated:
        return DelayRisk(ELEVATED, tuple(elevated))
    return DelayRisk(LOW)


def still_to_come(
    project: Any,
    *,
    today: dt.date,
    suspected: delays.SuspectedDelay | None = None,
) -> dict[str, Any] | None:
    """The capacity one campus has still to deliver, and when it is expected.

    The project page's summary, read the way the supply table above reads a whole
    company: planned less running, filed under the year `_year_of` gives it — the
    campus's own date while the period it names is still open, *overdue* when the
    campus is a suspected delay, otherwise the next tranche still to come, and no
    year at all when nothing says. `when` is that date as the source stated it
    ("Q3 2026"), or the tranche's year. None when no planned figure is cited, and
    `mw` None with `unknown` set for an operating campus with no running figure
    (`running_unknown`).
    """
    planned = getattr(project, "mw_planned", None)
    if planned is None:
        return None
    if running_unknown(project):
        return {
            "mw": None,
            "year": None,
            "overdue": False,
            "when": None,
            "from": None,
            "unknown": True,
        }
    remaining = max(float(planned) - float(getattr(project, "mw_built", None) or 0.0), 0.0)
    if suspected is None:
        suspected = delays.suspected_delay(project, today=today)
    year, overdue = _year_of(project, today, suspected) if remaining > 0 else (None, False)
    expected = _as_date(getattr(project, "expected_online", None))
    precision = getattr(project, "expected_online_precision", None)
    own = expected is not None and (overdue or delays.period_end(expected, precision) >= today)
    when = None if year is None else delays.stated(expected, precision) if own else str(year)
    return {
        "mw": remaining,
        "year": year,
        "overdue": overdue,
        "when": when,
        "from": None if year is None else "campus" if own else "tranche",
    }


@dataclass(frozen=True)
class Credited:
    """One campus as the company page counts it: the project and its credited MW."""

    project: Any
    mw_planned: float
    mw_built: float


def _year_of(
    project: Any, today: dt.date, late: delays.SuspectedDelay | None
) -> tuple[int | None, bool]:
    """The year a campus's remaining capacity is expected, and whether it is overdue.

    Overdue means a suspected delay (`delays`) and nothing else, so this table and
    the suspected-delay list can never disagree about a campus. A date that has
    passed without making the campus a suspected delay — something there runs, or a
    tranche is still to come — files the capacity under that tranche's year, and
    with neither it has no date at all.
    """
    expected = _as_date(getattr(project, "expected_online", None))
    if expected is None:
        return None, False
    precision = getattr(project, "expected_online_precision", None)
    if delays.period_end(expected, precision) >= today:
        return expected.year, False
    if late is not None:
        return today.year, True
    coming = [
        when
        for block in getattr(project, "blocks", ()) or ()
        if delays.tranche_state(block, today) == delays.COMING
        and (
            when := _as_date(getattr(block, "energized_on", None))
            or _as_date(getattr(block, "expected_online", None))
        )
        is not None
    ]
    return (max(min(coming).year, today.year), False) if coming else (None, False)


def supply(
    campuses: Iterable[Credited],
    *,
    today: dt.date,
    suspected: dict[int, delays.SuspectedDelay] | None = None,
) -> dict[str, Any]:
    """The company page's supply section: by year, by campus, and what is concentrated.

    `suspected` maps project id to its suspected delay, from `delays.suspected`, so
    a company with forty campuses is not forty separate questions.
    """
    suspected = suspected or {}
    years = list(range(today.year, today.year + WINDOW_YEARS))
    by_year: dict[int, float] = dict.fromkeys(years, 0.0)
    overdue_mw = 0.0
    later_mw = 0.0
    undated_mw = 0.0
    running_unknown_mw = 0.0
    rows: list[dict[str, Any]] = []

    for credited in campuses:
        project = credited.project
        if running_unknown(project):
            # Running, and nobody says how much: not future supply.
            running_unknown_mw += credited.mw_planned
            continue
        remaining = max(credited.mw_planned - credited.mw_built, 0.0)
        if remaining <= 0:
            continue
        late = suspected.get(project.id)
        if late is None and project.id not in suspected:
            late = delays.suspected_delay(project, today=today)
        year, overdue = _year_of(project, today, late)
        risk = delay_risk(project, today=today, suspected=late)
        row = {
            "id": project.id,
            "name": project.name,
            "phase": project.phase,
            "mw": remaining,
            "year": year,
            "overdue": overdue,
            "risk": risk.as_json(),
        }
        if year is None:
            undated_mw += remaining
            continue
        if year > years[-1]:
            later_mw += remaining
            continue
        by_year[max(year, years[0])] += remaining
        if overdue:
            overdue_mw += remaining
        rows.append(row)

    total = sum(by_year.values())
    for row in rows:
        row["share"] = row["mw"] / total if total else 0.0
    rows.sort(key=lambda r: (-r["mw"], r["id"]))

    flagged = [
        r
        for r in rows
        if r["share"] >= CONCENTRATION_SHARE and r["risk"]["level"] in (HIGH, ELEVATED)
    ]
    flagged.sort(key=lambda r: (-_RANK[r["risk"]["level"]], -r["share"], r["id"]))
    at_risk = sum(r["mw"] for r in rows if r["risk"]["level"] == HIGH)

    return {
        "as_of": today.isoformat(),
        "years": [{"year": y, "mw": by_year[y]} for y in years],
        "total_mw": total,
        "overdue_mw": overdue_mw,
        "later_mw": later_mw,
        "undated_mw": undated_mw,
        "running_unknown_mw": running_unknown_mw,
        "high_risk_mw": at_risk,
        "campuses": rows,
        "concentrated": [r["id"] for r in flagged],
    }


__all__ = [
    "CONCENTRATION_SHARE",
    "ELEVATED",
    "HIGH",
    "LOW",
    "WINDOW_YEARS",
    "Credited",
    "DelayRisk",
    "delay_risk",
    "running_unknown",
    "slips",
    "still_to_come",
    "supply",
]
