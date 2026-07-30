"""Coverage measurement, and specifically its denominators.

The point of `tracker.gaps` is not to count NULLs — SQL does that in one line. It
is to count only the NULLs that represent *missing work*, because the raw count
sent effort at rows that can never be filled: 61 announced projects were being
reported as `mw_built` misses when nothing is built on any of them.
"""

from __future__ import annotations

from itertools import count

import pytest
from sqlalchemy.exc import IntegrityError

from tracker.dedup import dedup_key
from tracker.gaps import UNMEASURABLE, FieldGap, measure, worst
from tracker.models import Project

_seq = count(1)


def add(session, **kwargs) -> Project:
    """A project row with only the columns a test cares about.

    `dedup_key` is built with the real key function rather than stubbed, so these
    rows are shaped like the ones ingest produces. The counter keeps the unique
    index happy when a test wants several projects in the same locality.
    """
    defaults = {
        "name": "Some Campus",
        "company": f"Acme {next(_seq)}",
        "state": "VA",
        "country": "US",
        "phase": "announced",
        "confidence": 1,
    }
    merged = {**defaults, **kwargs}
    merged.setdefault(
        "dedup_key",
        dedup_key(merged["company"], merged.get("city"), merged.get("county"), merged["state"]),
    )
    project = Project(**merged)
    session.add(project)
    session.flush()
    return project


def gap_for(gaps: list[FieldGap], field: str) -> FieldGap:
    return next(g for g in gaps if g.field == field)


def test_mw_built_is_measured_only_where_something_is_built(session):
    """An announced project with no `mw_built` is correct, not incomplete."""
    add(session, phase="announced", city="Ashburn")
    add(session, phase="construction", city="Reno", mw_built=None)
    add(session, phase="operational", city="Mesa", mw_built=48.0)

    built = gap_for(measure(session), "mw_built")
    assert built.applicable == 2, "the announced project must be out of the denominator"
    assert built.filled == 1
    assert built.pct == 50
    assert built.missing == 1


def test_a_county_only_project_counts_as_covered(session):
    """Regression: scoping county to `city IS NOT NULL` dropped these rows.

    A project known only by county (which is how ISO queues report location)
    already has its county. Excluding it from the denominator while it sat in the
    filled column reported 31/82 for data that was really 40/91.
    """
    add(session, city=None, county="Loudoun County")
    add(session, city="Ashburn", county=None)

    county = gap_for(measure(session), "county")
    assert county.applicable == 2
    assert county.filled == 1


def test_every_project_is_applicable_for_county_because_the_schema_says_so(session):
    """`ck_project_locality` guarantees a city or a county on every row.

    So the locality denominator always equals the project count. The predicate is
    kept anyway: it documents *why* the denominator is the total, and it stays
    correct if that constraint is ever relaxed.
    """
    add(session, city="Ashburn")
    add(session, county="Loudoun County")

    gaps = measure(session)
    assert gap_for(gaps, "county").applicable == gap_for(gaps, "name").applicable

    with pytest.raises(IntegrityError):
        add(session, city=None, county=None)
    session.rollback()


def test_fields_whose_absence_is_the_truth_report_no_percentage(session):
    """`blocker` at 2% is not a backlog; it means projects have no blockers."""
    for _ in range(5):
        add(session, city="Ashburn")

    gaps = measure(session)
    for field in UNMEASURABLE:
        gap = gap_for(gaps, field)
        assert gap.measurable is False
        assert gap.pct is None
        assert gap.note == UNMEASURABLE[field]


def test_worst_ranks_by_missing_rows_and_ignores_the_unmeasurable(session):
    for _ in range(4):
        add(session, city="Ashburn", mw_planned=100.0)

    ranked = worst(measure(session), limit=3)
    assert ranked, "there is unfilled work, so something must rank"
    assert all(g.measurable for g in ranked), "blocker/customer must never be chased"
    assert ranked == sorted(ranked, key=lambda g: -g.missing)
    assert "mw_planned" not in [g.field for g in ranked], "mw_planned is fully covered here"


def test_measure_reports_every_field_it_claims_to(session):
    add(session, city="Ashburn")
    fields = [g.field for g in measure(session)]
    assert len(fields) == len(set(fields)), "no field reported twice"
    for expected in ("mw_planned", "investment_usd", "expected_online", "lat", "lon"):
        assert expected in fields


def test_an_empty_database_yields_no_percentages(session):
    for gap in measure(session):
        assert gap.applicable == 0
        assert gap.pct is None
