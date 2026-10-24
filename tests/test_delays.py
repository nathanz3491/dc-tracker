"""Suspected delays: one rule, and every surface reading the same answer from it.

Two ways this can be worse than useless, as with every check in this project. It can
miss a late campus, which is the ordinary failure. Or it can call a campus late that
is not — still inside the period its source promised, already running, or stopped on
purpose — and that is the costly one, because a reader who finds the flag wrong
twice stops reading it. Most of this file is the second kind.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tracker import delays, logic
from tracker.models import CapacityBlock, Event, Project, Source

TODAY = dt.date(2026, 10, 20)


def _stating(expected, *, quoted=True, url="https://news.test/campus"):
    """A citation giving `expected` as the online date: quoted, or 待确认."""
    return SimpleNamespace(
        id=1,
        url=url,
        source_type="trade_press",
        extractor=None,
        claims=json.dumps({"expected_online": expected.isoformat()}),
        fields=None if not quoted else "expected_online",
        unconfirmed_fields=None if quoted else "expected_online",
        unconfirmed_reasons=None if quoted else json.dumps({"expected_online": "no_quote"}),
    )


def _cite(session, project, *, quoted=True, url="https://news.test/campus"):
    """Store `_stating` for a saved campus, under its own URL."""
    said = _stating(project.expected_online, quoted=quoted, url=f"{url}/{project.id}")
    session.add(
        Source(
            project_id=project.id,
            url=said.url,
            source_type="trade_press",
            claims=said.claims,
            fields=said.fields,
            unconfirmed_fields=said.unconfirmed_fields,
            unconfirmed_reasons=said.unconfirmed_reasons,
        )
    )


def _block(status="planned", expected=None, energized=None, label="Phase 1", parent=None):
    return SimpleNamespace(
        status=status,
        expected_online=expected,
        energized_on=energized,
        label=label,
        parent=parent,
    )


def _event(kind, when, unconfirmed=None):
    return SimpleNamespace(event_type=kind, event_date=when, unconfirmed=unconfirmed)


def _assess(
    expected,
    precision=None,
    *,
    phase="construction",
    built=None,
    blocks=(),
    events=(),
    today=TODAY,
):
    return delays.assess(
        expected_online=expected,
        precision=precision,
        phase=phase,
        mw_built=built,
        blocks=blocks,
        events=events,
        today=today,
    )


# --- a stated period is not late until it is over --------------------------------


@pytest.mark.parametrize(
    ("stored", "precision", "due", "shown"),
    [
        (dt.date(2025, 1, 1), "year", dt.date(2025, 12, 31), "2025"),
        (dt.date(2025, 7, 1), "half", dt.date(2025, 12, 31), "H2 2025"),
        (dt.date(2025, 1, 1), "half", dt.date(2025, 6, 30), "H1 2025"),
        (dt.date(2025, 7, 1), "quarter", dt.date(2025, 9, 30), "Q3 2025"),
        (dt.date(2025, 10, 1), "quarter", dt.date(2025, 12, 31), "Q4 2025"),
        (dt.date(2025, 2, 1), "month", dt.date(2025, 2, 28), "Feb 2025"),
        (dt.date(2025, 7, 15), "day", dt.date(2025, 7, 15), "15 Jul 2025"),
        (dt.date(2025, 7, 15), None, dt.date(2025, 7, 15), "15 Jul 2025"),
        # Nothing recorded on the first of a month: the wider period, because the
        # narrower reading calls a campus late while its promise is still running.
        (dt.date(2025, 7, 1), None, dt.date(2025, 7, 31), "Jul 2025"),
        (dt.date(2025, 1, 1), None, dt.date(2025, 12, 31), "2025"),
    ],
)
def test_a_date_is_due_on_the_last_day_of_the_period_it_names(stored, precision, due, shown):
    assert delays.period_end(stored, precision) == due
    assert delays.stated(stored, precision) == shown

    # On the last day of the period it is not late yet; the day after, it is.
    assert _assess(stored, precision, today=due) is None
    late = _assess(stored, precision, today=due + dt.timedelta(days=1))
    assert late is not None
    assert late.due == due
    assert late.days_late == 1
    assert late.stated == shown


def test_the_delay_says_what_it_read_and_why_in_one_sentence():
    late = _assess(dt.date(2025, 7, 1), "quarter")
    assert late is not None
    assert late.read_as == "quarter" and late.precision == "quarter"
    assert late.days_late == (TODAY - dt.date(2025, 9, 30)).days == 385
    assert late.reason == (
        "Expected online Q3 2025, a quarter that ended 385 days ago; the phase is "
        "still construction and nothing reported since says it is running."
    )
    assert late.as_json() == {
        "expected_online": "2025-07-01",
        "precision": "quarter",
        "read_as": "quarter",
        "stated": "Q3 2025",
        "due": "2025-09-30",
        "days_late": 385,
        "as_of": "2026-10-20",
        "reason": late.reason,
    }


def test_an_unrecorded_precision_is_named_as_such_in_the_json():
    """`precision` is what the source recorded; `read_as` is how the rule read it."""
    late = _assess(dt.date(2025, 1, 1))
    assert late is not None
    assert late.precision is None and late.read_as == "year"


def test_no_date_is_never_late():
    assert _assess(None) is None


# --- the phase -------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["announced", "permitting", "construction"])
def test_a_campus_not_yet_running_is_late_once_its_period_ends(phase):
    assert _assess(dt.date(2024, 1, 1), "year", phase=phase) is not None


@pytest.mark.parametrize("phase", ["operational", "paused", "cancelled"])
def test_running_paused_and_cancelled_campuses_are_not_delays(phase):
    """Operational is not late; a stopped campus has a status of its own."""
    assert _assess(dt.date(2024, 1, 1), "year", phase=phase) is None


# --- what is already running -----------------------------------------------------


def test_running_capacity_is_news_of_going_live():
    """Red Oak read "360 MW running of 360" beside a 2020 date on a production copy.

    Flagging it would put "no news of going live" next to a running figure on the
    same row — the phase lags, the campus does not.
    """
    assert _assess(dt.date(2020, 4, 1), "quarter", built=360.0) is None


def test_a_cited_zero_is_not_running():
    assert _assess(dt.date(2020, 4, 1), "quarter", built=0.0) is not None


# --- the tranches ----------------------------------------------------------------


@pytest.mark.parametrize("status", ["energized", "serving"])
def test_a_running_tranche_is_news_of_going_live(status):
    assert _assess(dt.date(2024, 1, 1), "year", blocks=[_block(status=status)]) is None


def test_a_tranche_energised_by_its_own_date_is_running():
    tranche = _block(status="under_construction", energized=dt.date(2026, 3, 1))
    assert _assess(dt.date(2024, 1, 1), "year", blocks=[tranche]) is None


def test_a_tranche_still_to_come_explains_a_passed_campus_date():
    """Phase 1's date is on the row and phase 2 runs to its own schedule."""
    coming = _block(status="planned", expected=dt.date(2027, 6, 1), label="Phase 2")
    assert _assess(dt.date(2024, 1, 1), "year", blocks=[coming]) is None


def test_a_tranche_whose_year_is_still_running_is_still_to_come():
    """Dated "2026" and asked in October 2026: not late, so still to come."""
    this_year = _block(status="planned", expected=dt.date(2026, 1, 1), label="Phase 2")
    assert delays.tranche_state(this_year, TODAY) == delays.COMING
    assert _assess(dt.date(2024, 1, 1), "year", blocks=[this_year]) is None


@pytest.mark.parametrize("status", ["paused", "cancelled"])
def test_a_stopped_tranche_explains_nothing(status):
    """A cancelled phase 2 dated 2027 does not put the campus back on schedule."""
    stopped = _block(status=status, expected=dt.date(2027, 6, 1), label="Phase 2")
    assert delays.tranche_state(stopped, TODAY) == delays.STOPPED
    assert _assess(dt.date(2024, 1, 1), "year", blocks=[stopped]) is not None


def test_a_slipped_or_undated_tranche_leaves_the_campus_late():
    slipped = _block(status="under_construction", expected=dt.date(2024, 3, 1))
    undated = _block(status="planned", label="Phase 2")
    assert delays.tranche_state(slipped, TODAY) == delays.LATE
    assert delays.tranche_state(undated, TODAY) == delays.UNDATED
    assert _assess(dt.date(2024, 1, 1), "year", blocks=[slipped, undated]) is not None


def test_the_utilitys_plant_is_not_the_campus():
    """Gas units filed as tranches: running or scheduled, they say nothing about the halls."""
    plant = [
        _block(status="energized", label="Entergy gas plant"),
        _block(status="planned", expected=dt.date(2028, 1, 1), label="Solar farm"),
    ]
    assert _assess(dt.date(2024, 1, 1), "year", blocks=plant) is not None


# --- the milestones --------------------------------------------------------------


def test_a_confirmed_energisation_since_the_date_is_news_of_going_live():
    events = [_event("energized", dt.date(2025, 3, 1))]
    assert _assess(dt.date(2025, 1, 1), "year", events=events) is None


def test_an_energisation_with_an_imperfect_quote_still_counts():
    """`quote_off_target` is a milestone somebody reported; only a forecast is not."""
    events = [_event("energized", dt.date(2025, 3, 1), unconfirmed="quote_off_target")]
    assert _assess(dt.date(2025, 1, 1), "year", events=events) is None


def test_a_forecast_energisation_is_not_news_of_going_live():
    """ "Expected to begin operations in late 2025", read in 2026, is still a schedule."""
    events = [_event("energized", dt.date(2025, 10, 1), unconfirmed="forecast")]
    assert _assess(dt.date(2025, 1, 1), "year", events=events) is not None


def test_an_energisation_before_the_date_cannot_show_the_date_was_met():
    """Usually an earlier phase: the 2023 hall does not deliver the 2025 expansion."""
    events = [_event("energized", dt.date(2023, 6, 1))]
    assert _assess(dt.date(2025, 1, 1), "year", events=events) is not None


def test_an_energisation_dated_after_today_has_not_happened():
    events = [_event("energized", dt.date(2027, 1, 1))]
    assert _assess(dt.date(2025, 1, 1), "year", events=events) is not None


def test_other_milestones_are_not_news_of_going_live():
    """A signed tenant or a delivered transformer is progress, not a running campus."""
    events = [
        _event("first_customer", dt.date(2025, 6, 1)),
        _event("equipment_install", dt.date(2025, 6, 1)),
    ]
    assert _assess(dt.date(2025, 1, 1), "year", events=events) is not None


# --- every surface reads the same answer ------------------------------------------


def _row(session, name, *, quoted=True, **kwargs) -> Project:
    """A campus, with a citation stating its online date when it has one."""
    project = Project(
        name=name,
        company="Someone",
        state="TX",
        city=name,
        dedup_key=f"someone|{name.lower()}|tx",
        confidence=2,
        **{
            "phase": "construction",
            "golive_checked_at": dt.datetime.combine(TODAY, dt.time.min),
            **kwargs,
        },
    )
    session.add(project)
    session.flush()
    if project.expected_online is not None:
        _cite(session, project, quoted=quoted)
        session.flush()
    return project


@pytest.fixture
def fleet(session, monkeypatch):
    """Rows covering every branch, on a fixed day for both clocks."""
    monkeypatch.setattr(logic, "_today", lambda: TODAY)
    monkeypatch.setattr(delays, "as_of", lambda: TODAY)

    late = _row(
        session,
        "Late",
        expected_online=dt.date(2025, 7, 1),
        expected_online_precision="quarter",
        mw_planned=300.0,
    )
    later = _row(session, "Later", expected_online=dt.date(2023, 1, 1), mw_planned=100.0)
    tranche_late = _row(session, "Tranche Late", expected_online=dt.date(2024, 5, 1))
    session.add(
        CapacityBlock(
            project_id=tranche_late.id,
            block_key="phase-1",
            label="Phase 1",
            status="under_construction",
            expected_online=dt.date(2024, 5, 1),
        )
    )
    _row(
        session, "This Year", expected_online=dt.date(2026, 1, 1), expected_online_precision="year"
    )
    _row(session, "Running", phase="operational", expected_online=dt.date(2024, 1, 1))
    _row(session, "Built", expected_online=dt.date(2020, 4, 1), mw_built=360.0, mw_planned=360.0)
    _row(session, "Paused", phase="paused", expected_online=dt.date(2024, 1, 1))
    _row(session, "Undated")
    energised = _row(session, "Energised", expected_online=dt.date(2025, 1, 1))
    session.add(
        Event(
            project_id=energised.id,
            event_type="energized",
            event_date=dt.date(2025, 4, 1),
            description="power on",
        )
    )
    forecast = _row(session, "Forecast", expected_online=dt.date(2025, 1, 1))
    session.add(
        Event(
            project_id=forecast.id,
            event_type="energized",
            event_date=dt.date(2025, 4, 1),
            description="expected to energise",
            unconfirmed="forecast",
        )
    )
    phase_two = _row(session, "Phase Two", expected_online=dt.date(2024, 1, 1))
    session.add(
        CapacityBlock(
            project_id=phase_two.id,
            block_key="phase-2",
            label="Phase 2",
            status="planned",
            expected_online=dt.date(2027, 1, 1),
        )
    )
    session.flush()
    for project in session.scalars(select(Project)):
        session.refresh(project)
    return {
        "late": {late.id, later.id, tranche_late.id, forecast.id},
        # 1,024, 872, 385 and 293 days late on 2026-10-20.
        "by_days": [later.id, tranche_late.id, late.id, forecast.id],
    }


def test_the_command_line_and_every_payload_agree(session, fleet):
    """`logic check`, the export, the console's index and table, and the batch reader.

    Five readers, one function. If any of them grew its own idea of "late", a reader
    would find a campus flagged on one page and not on another, and nothing on screen
    would say which was wrong.
    """
    from tracker.export import fetch_projects, to_json_object
    from tracker.webui import dataset, query

    projects = fetch_projects(session)
    by_cli = {
        p.id for p in projects if any(f.code == "past_its_own_date" for f in logic.check_rules(p))
    }
    by_export = {p.id for p in projects if to_json_object(p, claims=False)["suspected_delay"]}
    by_index = {r["id"] for r in dataset.index_rows(session) if r["suspected_delay"]}
    by_batch = set(delays.suspected(session))
    by_table = set(query.ids(session, query.Filters(delayed=True)))

    assert by_cli == fleet["late"]
    assert by_export == by_cli
    assert by_index == by_cli
    assert by_batch == by_cli
    assert by_table == by_cli

    one = next(p for p in projects if p.name == "Late")
    shipped = to_json_object(one, claims=False)["suspected_delay"]
    assert shipped == delays.suspected(session)[one.id].as_json()
    assert shipped["stated"] == "Q3 2025" and shipped["days_late"] == 385


def test_the_table_sorts_most_overdue_first_and_the_rest_after(session, fleet):
    from tracker.webui import query

    found = query.ids(session, query.Filters(sort="days_late", direction="desc"))
    assert found[: len(fleet["by_days"])] == fleet["by_days"]
    assert set(found[len(fleet["by_days"]) :]).isdisjoint(fleet["late"])

    ascending = query.ids(session, query.Filters(sort="days_late", direction="asc"))
    assert ascending[: len(fleet["by_days"])] == list(reversed(fleet["by_days"]))


def test_the_table_reads_the_filter_and_the_sort_from_the_address():
    from tracker.webui import query

    filters, _offset, _limit = query.parse({"delayed": ["1"], "sort": ["days_late"]})
    assert filters.delayed and filters.sort == "days_late"
    assert not query.parse({})[0].delayed


def test_a_company_page_counts_its_late_campuses(session, fleet):
    from tracker import company

    profile = company.profile(session, "someone")
    assert profile is not None
    assert profile["suspected_delays"]["count"] == len(fleet["late"])
    assert profile["suspected_delays"]["project_ids"] == fleet["by_days"]
    assert profile["suspected_delays"]["mw_planned"] == 400.0
    flagged = {r["id"] for r in profile["projects"] if r["suspected_delay"]}
    assert flagged == fleet["late"]


def test_a_section_is_overdue_only_when_its_period_is_over(session, monkeypatch):
    """The tranche "was due" label, computed where the CLI computes it.

    The page used to work it out in the browser from the stored day, so "2026" was
    overdue on 2 January, and a paused or cancelled tranche was "due" forever.
    """
    from tracker.export import _sections_json

    monkeypatch.setattr(delays, "as_of", lambda: TODAY)
    project = _row(session, "Sections")
    for number, status, expected in (
        (1, "under_construction", dt.date(2024, 3, 1)),
        (2, "planned", dt.date(2026, 1, 1)),
        (3, "cancelled", dt.date(2024, 3, 1)),
        (4, "paused", dt.date(2024, 3, 1)),
        (5, "serving", dt.date(2024, 3, 1)),
    ):
        session.add(
            CapacityBlock(
                project_id=project.id,
                block_key=f"building-{number}",
                label=f"Building {number}",
                status=status,
                expected_online=expected,
            )
        )
    session.flush()
    session.refresh(project)
    overdue = {s["label"]: s["overdue"] for s in _sections_json(project)}
    assert overdue == {
        "Building 1": True,
        "Building 2": False,
        "Building 3": False,
        "Building 4": False,
        "Building 5": False,
    }


# --- a search, not only what we hold (client rule, 2026-10-21) --------------------


def test_a_late_campus_is_not_flagged_until_a_search_after_its_date_found_nothing():
    """Savvis MP2 (2014), CH3 (2018) and NTT Mesa (2022) read "suspected delay" on
    10-22 because nothing we held said they ran — and nothing had searched."""
    stub = SimpleNamespace(
        expected_online=dt.date(2022, 1, 1),
        expected_online_precision="year",
        phase="construction",
        mw_built=None,
        blocks=[],
        events=[],
        golive_checked_at=None,
        sources=[_stating(dt.date(2022, 1, 1))],
    )
    assert delays.suspected_delay(stub, today=TODAY) is None
    assert delays.suspected_delay(stub, today=TODAY, require_check=False) is not None
    stub.golive_checked_at = dt.datetime(2022, 12, 31, 23, 0)  # before the year ended
    assert delays.suspected_delay(stub, today=TODAY) is None
    stub.golive_checked_at = dt.datetime(2026, 10, 19)
    assert delays.suspected_delay(stub, today=TODAY) is not None


def test_the_watch_lists_the_late_campuses_still_waiting_for_their_search(session):
    waiting = _row(
        session,
        "Waiting",
        expected_online=dt.date(2024, 4, 1),
        expected_online_precision="month",
        golive_checked_at=None,
    )
    _row(
        session, "Searched", expected_online=dt.date(2024, 4, 1), expected_online_precision="month"
    )
    stale = _row(
        session,
        "Stale",
        expected_online=dt.date(2025, 1, 1),
        expected_online_precision="year",
        golive_checked_at=dt.datetime(2025, 6, 1),
    )
    session.commit()
    got = [pid for pid, _late in delays.awaiting_search(session, today=TODAY)]
    assert got == [waiting.id, stale.id]
    assert set(delays.suspected(session, today=TODAY)) == {
        p.id for p in session.query(Project) if p.name == "Searched"
    }


def test_a_flagged_campus_is_searched_again_after_a_month_and_stays_flagged_meanwhile(session):
    """The client's rule is that a search *still* finds nothing. Searched once, Tesla's
    Dojo stayed flagged on a query that could not find its 2025 shutdown."""
    recent = _row(
        session,
        "Recent",
        expected_online=dt.date(2024, 4, 1),
        expected_online_precision="month",
        golive_checked_at=dt.datetime.combine(TODAY - dt.timedelta(days=29), dt.time.min),
    )
    month = _row(
        session,
        "Month",
        expected_online=dt.date(2024, 4, 1),
        expected_online_precision="month",
        golive_checked_at=dt.datetime.combine(TODAY - dt.timedelta(days=30), dt.time.min),
    )
    oldest = _row(
        session,
        "Oldest",
        expected_online=dt.date(2024, 4, 1),
        expected_online_precision="month",
        golive_checked_at=dt.datetime(2025, 1, 1),
    )
    never = _row(
        session,
        "Never",
        expected_online=dt.date(2025, 4, 1),
        expected_online_precision="month",
        golive_checked_at=None,
    )
    session.commit()
    got = [pid for pid, _late in delays.awaiting_search(session, today=TODAY)]
    assert got == [never.id, oldest.id, month.id], "never searched first, then stalest"
    flagged = set(delays.suspected(session, today=TODAY))
    assert flagged == {recent.id, month.id, oldest.id}, "waiting for a repeat is not unflagged"


def test_only_a_date_a_source_states_makes_a_campus_late(session, monkeypatch):
    """On 10-24 five of ten campuses past their date rested on a date nobody had
    verified: a directory page's guess at Carter's Store, a month an article never gave
    at Compass Hoffman Estates (it said "2026"), a misread a directory copied at NTT
    Gainesville."""
    monkeypatch.setattr(delays, "as_of", lambda: TODAY)
    quoted = _row(session, "Quoted", expected_online=dt.date(2024, 4, 1))
    doubted = _row(session, "Doubted", quoted=False, expected_online=dt.date(2024, 4, 1))
    listed = _row(session, "Listed", quoted=False, expected_online=dt.date(2024, 4, 1))
    _cite(session, listed, url="https://www.dcatlas.io/campus")  # a directory, quoted
    other = _row(session, "Other", expected_online=dt.date(2024, 4, 1))
    other.expected_online = dt.date(2024, 7, 1)  # the row holds a date nobody quoted
    session.commit()
    for project in (quoted, doubted, listed, other):
        session.refresh(project)

    assert set(delays.suspected(session, today=TODAY)) == {quoted.id}
    one_by_one = {
        p.id for p in (quoted, doubted, listed, other) if delays.suspected_delay(p, today=TODAY)
    }
    assert one_by_one == {quoted.id}
    assert [pid for pid, _ in delays.awaiting_search(session, today=TODAY)] == []


def _listing(phase, *, url, quoted=True, read=dt.datetime(2026, 9, 1)):
    """A citation giving the campus's phase, read on `read`."""
    return SimpleNamespace(
        id=2,
        url=url,
        source_type="trade_press",
        extractor=None,
        claims=json.dumps({"phase": phase}),
        fields="phase" if quoted else None,
        unconfirmed_fields=None if quoted else "phase",
        unconfirmed_reasons=None if quoted else json.dumps({"phase": "quote_off_target"}),
        published_at=None,
        fetched_at=read,
    )


def test_a_directory_listing_the_campus_as_running_is_news_of_going_live():
    """Aligned's Sterling building (IAD-03), due fall 2023, was listed as operational by
    three directories and flagged for three years, because no press release said so."""
    due = dt.date(2023, 10, 1)
    stub = SimpleNamespace(
        expected_online=due,
        expected_online_precision="month",
        phase="construction",
        mw_built=None,
        blocks=[],
        events=[],
        golive_checked_at=dt.datetime(2026, 10, 1),
        sources=[_stating(due)],
    )
    assert delays.suspected_delay(stub, today=TODAY) is not None

    listed = _listing("operational", url="https://baxtel.com/data-center/aligned-iad03")
    stub.sources = [_stating(due), listed]
    assert delays.listed_running(stub.sources, due)
    assert delays.suspected_delay(stub, today=TODAY) is None

    # A listing read before the date cannot show the date being met.
    early = _listing("operational", url="https://baxtel.com/x", read=dt.datetime(2023, 1, 5))
    # A model's 待确认 reading, with no quote behind it, is not a report.
    guessed = _listing("operational", url="https://news.test/guess", quoted=False)
    # Nor a phase short of running.
    building = _listing("construction", url="https://baxtel.com/y")
    for weak in (early, guessed, building):
        stub.sources = [_stating(due), weak]
        assert delays.suspected_delay(stub, today=TODAY) is not None, weak.url
