"""A company's next three years of supply, and the campuses that could slip.

The client asked for the company page to read in megawatts and to say when one
large campus is a big share of the near-term supply and at risk of delay. These
pin what counts as supply in which year, and what each delay-risk level rests on.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tracker import supply

TODAY = dt.date(2026, 10, 21)


def _campus(
    pid=1,
    *,
    phase="construction",
    expected=None,
    precision=None,
    events=(),
    risks=(),
    built=None,
    blocks=(),
):
    return SimpleNamespace(
        id=pid,
        name=f"Campus {pid}",
        phase=phase,
        expected_online=expected,
        expected_online_precision=precision,
        mw_built=built,
        events=list(events),
        risks=list(risks),
        blocks=list(blocks),
    )


def _risk(severity, category="grid_capacity", *, status="open", unconfirmed=None):
    return SimpleNamespace(
        severity=severity, category=category, status=status, unconfirmed=unconfirmed
    )


def _slip(when, description="expected_online moved from 2026-01-01 to 2027-01-01 (+365 days)"):
    return SimpleNamespace(
        event_type="delayed", event_date=when, unconfirmed=None, description=description
    )


# --- delay risk --------------------------------------------------------------------


def test_a_passed_date_with_nothing_running_is_high_risk():
    campus = _campus(expected=dt.date(2025, 7, 1), precision="quarter")
    got = supply.delay_risk(campus, today=TODAY)
    assert got.level == supply.HIGH
    assert got.reasons[0].startswith("its stated online date (Q3 2025) passed")


def test_two_slips_or_a_work_stopping_obstacle_is_high_risk():
    two = _campus(
        expected=dt.date(2027, 6, 1),
        events=[_slip(dt.date(2026, 6, 1)), _slip(dt.date(2027, 6, 1))],
    )
    assert supply.delay_risk(two, today=TODAY).level == supply.HIGH
    stopped = _campus(expected=dt.date(2027, 6, 1), risks=[_risk("blocking", "permitting")])
    got = supply.delay_risk(stopped, today=TODAY)
    assert got.level == supply.HIGH
    assert got.reasons == ("1 open obstacle a source says stopped work (permitting)",)


def test_one_slip_a_material_obstacle_or_no_construction_close_to_the_date_is_elevated():
    one = _campus(expected=dt.date(2027, 6, 1), events=[_slip(dt.date(2027, 6, 1))])
    assert supply.delay_risk(one, today=TODAY).reasons == (
        "its online date has slipped once, to 2027-06-01",
    )
    material = _campus(expected=dt.date(2028, 1, 1), risks=[_risk("material", "transmission")])
    assert supply.delay_risk(material, today=TODAY).level == supply.ELEVATED
    soon = _campus(phase="announced", expected=dt.date(2027, 3, 1), precision="month")
    got = supply.delay_risk(soon, today=TODAY)
    assert got.level == supply.ELEVATED
    assert got.reasons == ("due Mar 2027 with no construction reported",)


def test_what_does_not_count_as_evidence_of_delay():
    """An unquoted or resolved obstacle, a within-year restatement, a forecast."""
    campus = _campus(
        expected=dt.date(2028, 1, 1),
        risks=[
            _risk("blocking", unconfirmed="quote_missing"),
            _risk("blocking", status="resolved"),
        ],
        events=[
            _slip(
                dt.date(2027, 9, 1),
                "expected_online moved from 2027-01-01 to 2027-09-01 "
                "within 2027; this may be a more precise restatement rather than a delay",
            ),
            SimpleNamespace(
                event_type="delayed",
                event_date=dt.date(2028, 1, 1),
                unconfirmed="forecast",
                description="",
            ),
        ],
    )
    assert supply.delay_risk(campus, today=TODAY) == supply.DelayRisk(supply.LOW)


def test_an_operating_or_stopped_campus_carries_no_delay_risk():
    for phase in ("operational", "paused", "cancelled"):
        campus = _campus(phase=phase, expected=dt.date(2024, 1, 1), risks=[_risk("blocking")])
        assert supply.delay_risk(campus, today=TODAY).level == supply.LOW


# --- supply by year ----------------------------------------------------------------


def test_supply_is_what_is_not_yet_running_in_the_year_it_is_due():
    rows = [
        supply.Credited(_campus(1, expected=dt.date(2026, 12, 1)), 300.0, 100.0),
        supply.Credited(_campus(2, expected=dt.date(2027, 1, 1), precision="year"), 1000.0, 0.0),
        supply.Credited(_campus(3, expected=dt.date(2030, 1, 1)), 500.0, 0.0),
        supply.Credited(_campus(4), 250.0, 0.0),
        supply.Credited(_campus(5, expected=dt.date(2028, 6, 1)), 400.0, 400.0),
    ]
    got = supply.supply(rows, today=TODAY)
    assert got["years"] == [
        {"year": 2026, "mw": 200.0},
        {"year": 2027, "mw": 1000.0},
        {"year": 2028, "mw": 0.0},
    ]
    assert (got["total_mw"], got["later_mw"], got["undated_mw"]) == (1200.0, 500.0, 250.0)
    assert [r["id"] for r in got["campuses"]] == [2, 1]


def test_overdue_capacity_stays_in_the_window_marked_as_overdue():
    late = _campus(1, expected=dt.date(2025, 7, 1), precision="quarter")
    running_late = _campus(2, phase="operational", expected=dt.date(2025, 1, 1))
    got = supply.supply(
        [supply.Credited(late, 600.0, 0.0), supply.Credited(running_late, 200.0, 100.0)],
        today=TODAY,
    )
    assert got["years"][0] == {"year": 2026, "mw": 600.0}
    assert got["overdue_mw"] == 600.0
    assert got["undated_mw"] == 100.0  # an operating campus's rest has no date
    (row,) = got["campuses"]
    assert row["overdue"] and row["risk"]["level"] == supply.HIGH


def test_a_large_campus_at_risk_is_called_out_by_its_share():
    """The client's example: one campus a third of the next three years, slipping."""
    big = _campus(1, expected=dt.date(2025, 7, 1), precision="quarter")
    safe = _campus(2, expected=dt.date(2027, 6, 1))
    small = _campus(3, expected=dt.date(2026, 12, 1), risks=[_risk("material")])
    got = supply.supply(
        [
            supply.Credited(big, 400.0, 0.0),
            supply.Credited(safe, 700.0, 0.0),
            supply.Credited(small, 100.0, 0.0),
        ],
        today=TODAY,
    )
    shares = {r["id"]: round(r["share"], 3) for r in got["campuses"]}
    assert shares == {1: 0.333, 2: 0.583, 3: 0.083}
    assert got["concentrated"] == [1]  # the safe one is large but not at risk
    assert got["high_risk_mw"] == 400.0


def test_a_passed_date_with_a_tranche_still_to_come_is_that_tranches_year_not_overdue():
    """Overdue means a suspected delay and nothing else, so the two lists agree."""
    tranche = SimpleNamespace(
        status="under_construction",
        energized_on=None,
        expected_online=dt.date(2027, 9, 1),
        label="Phase 2",
        parent=None,
    )
    campus = _campus(1, expected=dt.date(2025, 7, 1), precision="quarter", blocks=[tranche])
    got = supply.supply([supply.Credited(campus, 300.0, 0.0)], today=TODAY)
    (row,) = got["campuses"]
    assert (row["year"], row["overdue"]) == (2027, False)
    assert got["overdue_mw"] == 0.0
