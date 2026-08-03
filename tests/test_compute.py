"""Capacity restated as accelerators.

The column is a unit conversion, and the tests are mostly about the ways a unit
conversion can quietly become a claim: inventing a number for a site nobody has
sized, dressing a rounded input as a measurement, or drifting away from the
megawatt figure it was derived from.
"""

from __future__ import annotations

import pytest

from tracker.compute import DEFAULT_KW_PER_H200, describe, h200_equivalent
from tracker.config import Settings
from tracker.models import Project


def test_the_ratio_is_the_researched_one():
    """1.06 kW per GPU of node-level IT load (DGX H200: 8.5 kW for eight), times
    a 1.2 PUE for a liquid-cooled hall. Roughly 770 accelerators per megawatt."""
    assert pytest.approx(1.3) == DEFAULT_KW_PER_H200
    assert h200_equivalent(1.0) == pytest.approx(770, abs=10)


@pytest.mark.parametrize(
    ("mw", "expected"),
    [(48, 37_000), (150, 120_000), (900, 690_000), (1200, 920_000)],
)
def test_capacity_converts(mw, expected):
    assert h200_equivalent(mw) == expected


def test_a_site_nobody_has_sized_gets_nothing_rather_than_zero():
    """A zero would be summed. Null is the honest answer and the table shows a dash.

    This is the same rule the rest of the project runs on: silence is not
    evidence, and a column that looks complete because the empties were filled
    with zeroes makes every total wrong.
    """
    assert h200_equivalent(None) is None
    assert h200_equivalent(0) is None
    assert h200_equivalent("") is None
    assert h200_equivalent("not a number") is None


def test_a_rounding_error_of_a_site_is_not_reported_as_a_cluster():
    """Below a tenth of a megawatt the answer is "a handful", not a count."""
    assert h200_equivalent(0.05) is None
    assert h200_equivalent(0.5) is not None


def test_the_output_is_rounded_to_the_precision_of_the_input():
    """The input is a figure somebody rounded before publishing it.

    `48 MW` is not 48.000, so 36,923 would dress a rounded input as a
    measurement. Two significant figures says as much as is known.
    """
    assert h200_equivalent(48) == 37_000
    assert str(h200_equivalent(48)).count("0") >= 3


def test_the_ratio_is_a_setting_so_the_column_can_be_rebased():
    """Boards get denser and PUE improves; the column must be able to follow
    without a migration."""
    tighter = Settings(kw_per_h200=1.0)
    assert h200_equivalent(1.0, settings=tighter) == 1000
    assert h200_equivalent(1.0, settings=tighter) > h200_equivalent(1.0)


@pytest.mark.parametrize(
    ("count", "text"),
    [(None, "—"), (770, "770"), (37_000, "37k"), (920_000, "920k"), (3_800_000, "3.8M")],
)
def test_counts_read_at_a_glance(count, text):
    assert describe(count) == text


# --- how it is stored -------------------------------------------------------


def _project(session, **kwargs) -> Project:
    fields = {
        "name": "Prometheus",
        "company": "Meta",
        "city": "New Albany",
        "state": "OH",
        "dedup_key": "meta|prometheus",
        "phase": "construction",
        **kwargs,
    }
    row = Project(**fields)
    session.add(row)
    session.flush()
    return row


def test_built_capacity_wins_over_planned(session):
    """Two questions, and this column answers the first: what is there *now*.

    A 1 GW campus with 150 MW energised has 150 MW of compute today. Using the
    plan would report capacity that does not exist yet as though it did.
    """
    from tracker.upsert import apply_h200_equivalent

    project = _project(session, mw_planned=1000.0, mw_built=150.0)
    apply_h200_equivalent(project)
    assert project.h200_equivalent == h200_equivalent(150.0)


def test_planned_capacity_is_the_fallback(session):
    from tracker.upsert import apply_h200_equivalent

    project = _project(session, mw_planned=1000.0)
    apply_h200_equivalent(project)
    assert project.h200_equivalent == h200_equivalent(1000.0)


def test_the_stored_value_follows_the_capacity(session):
    """It is a restatement, not an independent fact.

    A row whose megawatts moved and whose accelerator count did not would be
    self-contradictory, and `logic check` would be right to flag it.
    """
    from tracker.upsert import apply_h200_equivalent

    project = _project(session, mw_planned=100.0)
    apply_h200_equivalent(project)
    first = project.h200_equivalent

    project.mw_planned = 400.0
    apply_h200_equivalent(project)
    assert project.h200_equivalent > first
    assert project.h200_equivalent == h200_equivalent(400.0)


def test_a_cited_chip_count_beats_the_conversion(session):
    """An article saying "100,000 GPUs" has answered directly; no ratio improves it."""
    from tracker.upsert import apply_h200_equivalent

    project = _project(session, mw_planned=48.0)
    apply_h200_equivalent(project, {"h200_equivalent": [_claim(100_000)]})
    assert project.h200_equivalent == 100_000


def test_rubbish_from_a_source_falls_back_rather_than_being_stored(session):
    from tracker.upsert import apply_h200_equivalent

    project = _project(session, mw_planned=48.0)
    apply_h200_equivalent(project, {"h200_equivalent": [_claim("lots")]})
    assert project.h200_equivalent == h200_equivalent(48.0)


def _claim(value):
    """One entry shaped like `claims_by_field` produces."""
    import datetime as dt

    from tracker.upsert import _Claim

    return _Claim(
        value=value,
        weight=3,
        fetched_at=dt.datetime(2026, 1, 1),
        source_type="trade_press",
        url="https://example.test",
        confirmed=True,
    )


def test_the_cache_is_consistent_after_a_recompute(session):
    """`recompute_h200` must be a no-op on a database that is already current.

    Same contract as `recompute_confidence`, and the same reason: if running it
    twice keeps changing rows, then either the derivation is not a pure function
    of what is stored, or `tracker init` is reporting churn that is not real.
    """
    from tracker.upsert import recompute_h200

    _project(session, mw_planned=250.0)
    _project(session, mw_planned=None, dedup_key="meta|thin", name="Thin")
    session.flush()

    assert recompute_h200(session) >= 1
    assert recompute_h200(session) == 0, "a second pass should change nothing"


def test_a_row_with_no_capacity_stays_empty_through_a_recompute(session):
    from tracker.upsert import recompute_h200

    thin = _project(session, mw_planned=None)
    recompute_h200(session)
    assert thin.h200_equivalent is None
