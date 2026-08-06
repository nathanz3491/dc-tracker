"""Plausibility checks: is this number possible at all?

Every threshold here is a claim about the physical or economic world, so each test
names the real row that motivated it. The checks exist because `logic check` cannot
help: each of these rows was perfectly self-consistent around a figure wrong by
three orders of magnitude, and a contradiction detector only fires when two fields
disagree.

The case that prompted the command: **project 72**, Flexential's Englewood
*expansion* at 11,250 MW — larger than any campus planned anywhere, unquoted, and
implying $187k per MW against the $2-30M a real build costs. It was the largest
number in the database and nothing challenged it.
"""

from __future__ import annotations

import json

import pytest

from tracker import audit
from tracker.models import Project, Source


def _project(session, **kwargs) -> Project:
    defaults = {
        "name": "Campus",
        "company": "Someone",
        "state": "CO",
        "city": "Englewood",
        "dedup_key": f"k{kwargs.get('name', 'x')}",
        "phase": "construction",
        "confidence": 2,
    }
    defaults.update(kwargs)
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


_URLS = iter(range(1, 10_000))


def _source(session, project, claims: dict, *, unconfirmed: str | None = None) -> None:
    """One citation.

    Two columns decide the tier and they have to be set the way ingest sets them:
    `claims` holds every value, and `fields` lists only the ones a quote confirmed —
    an unconfirmed field is *left out* of `fields` rather than flagged inside it
    (`gaps._tier_of`). A double that listed it in both made every value read as
    REPORTED, so the 待确认 test passed for the wrong reason.
    """
    unconfirmed_set = {f.strip() for f in (unconfirmed or "").split(",") if f.strip()}
    session.add(
        Source(
            project_id=project.id,
            url=f"https://example.test/{next(_URLS)}",
            source_type="trade_press",
            claims=json.dumps(claims),
            fields=",".join(k for k in claims if k not in unconfirmed_set),
            unconfirmed_fields=unconfirmed,
            extractor="crawl:v1",
        )
    )
    session.flush()


def _codes(project) -> set[str]:
    return {f.code for f in audit.check_project(project)}


# --- a campus larger than any campus on earth --------------------------------


def test_the_project_72_case_is_caught(session):
    """11,250 MW for a colocation expansion, the largest figure in the database."""
    project = _project(session, name="Englewood Expansion", mw_planned=11250.0)
    session.refresh(project)
    assert "campus_exceeds_worlds_largest" in _codes(project)


def test_a_real_multi_gigawatt_campus_is_left_alone(session):
    """Stargate Abilene is genuinely 4,500 MW. The ceiling must clear it."""
    project = _project(session, name="Stargate Abilene", mw_planned=4500.0)
    session.refresh(project)
    assert "campus_exceeds_worlds_largest" not in _codes(project)


# --- the same figure quoted in two units -------------------------------------


def test_two_sources_a_thousandfold_apart_are_a_unit_error(session):
    """kW read as MW. Not a disagreement — the same number twice."""
    project = _project(session, name="Hillsboro", mw_planned=36000.0)
    _source(session, project, {"mw_planned": 36.0})
    _source(session, project, {"mw_planned": 36000.0})
    session.refresh(project)
    assert "same_figure_two_units" in _codes(project)


def test_a_hundredfold_gap_is_caught_too(session):
    """A misplaced decimal against a rounded sibling."""
    project = _project(session, name="Plano", mw_planned=2250.0)
    _source(session, project, {"mw_planned": 22.5})
    _source(session, project, {"mw_planned": 2250.0})
    session.refresh(project)
    assert "same_figure_two_units" in _codes(project)


def test_two_sources_that_merely_disagree_are_not_a_unit_error(session):
    """48 against 60 MW is two readings of one campus, and normal."""
    project = _project(session, name="AZP-2", mw_planned=60.0)
    _source(session, project, {"mw_planned": 48.0})
    _source(session, project, {"mw_planned": 60.0})
    session.refresh(project)
    assert "same_figure_two_units" not in _codes(project)


def test_a_growing_campus_is_not_a_unit_error(session):
    """A 10x expansion over time is real news, not a misread. 1000x is not."""
    project = _project(session, name="Growing", mw_planned=500.0)
    _source(session, project, {"mw_planned": 50.0})
    _source(session, project, {"mw_planned": 500.0})
    session.refresh(project)
    assert "same_figure_two_units" not in _codes(project)


# --- dollars against megawatts ------------------------------------------------


def test_a_campus_costing_almost_nothing_per_megawatt_is_flagged(session):
    """Project 72's other smell: $2.1B over 11,250 MW is $187k per MW."""
    project = _project(session, name="Englewood", mw_planned=11250.0, investment_usd=2_100_000_000)
    session.refresh(project)
    assert "usd_per_mw_out_of_band" in _codes(project)


def test_a_programme_budget_on_one_campus_is_flagged(session):
    """$300B against one 4,500 MW site — the Stargate programme, not the campus."""
    project = _project(session, name="Abilene", mw_planned=4500.0, investment_usd=300_000_000_000)
    session.refresh(project)
    assert "usd_per_mw_out_of_band" in _codes(project)


@pytest.mark.parametrize("per_mw", [2_000_000, 8_000_000, 12_000_000, 30_000_000])
def test_an_ordinary_build_cost_is_never_flagged(session, per_mw: int):
    """The band has to clear a bare shell and a liquid-cooled AI hall alike."""
    project = _project(session, name=f"Ord{per_mw}", mw_planned=100.0, investment_usd=per_mw * 100)
    session.refresh(project)
    assert "usd_per_mw_out_of_band" not in _codes(project)


def test_a_tiny_site_is_exempt_from_the_cost_band(session):
    """Below 5 MW the ratio is dominated by fit-out and says nothing."""
    project = _project(session, name="Tiny", mw_planned=2.0, investment_usd=500_000_000)
    session.refresh(project)
    assert "usd_per_mw_out_of_band" not in _codes(project)


# --- a gigawatt resting on no quote -------------------------------------------


def test_a_gigawatt_with_no_quote_behind_it_is_flagged(session):
    project = _project(session, name="MACROHARD", mw_planned=2000.0)
    _source(session, project, {"mw_planned": 2000.0}, unconfirmed="mw_planned")
    session.refresh(project)
    assert "giant_capacity_unconfirmed" in _codes(project)


def test_a_gigawatt_a_quote_confirms_is_left_alone(session):
    """Real gigawatt campuses exist. The check is about evidence, not size."""
    project = _project(session, name="Cited GW", mw_planned=2000.0)
    _source(session, project, {"mw_planned": 2000.0})
    session.refresh(project)
    assert "giant_capacity_unconfirmed" not in _codes(project)


def test_a_small_unconfirmed_figure_is_not_worth_a_finding(session):
    """Most of the database is 待确认. Only a giant one earns an interrupt."""
    project = _project(session, name="Small", mw_planned=40.0)
    _source(session, project, {"mw_planned": 40.0}, unconfirmed="mw_planned")
    session.refresh(project)
    assert "giant_capacity_unconfirmed" not in _codes(project)


# --- a derived figure that drifted from its own input -------------------------


def test_an_h200_estimate_that_no_longer_matches_its_capacity_is_flagged(session):
    """How Applied Digital was caught: 7,500 MW beside an H200 figure meaning 7 MW."""
    project = _project(session, name="Jamestown", mw_planned=7500.0, h200_equivalent=5400)
    session.refresh(project)
    assert "h200_disagrees_with_capacity" in _codes(project)


def test_an_h200_estimate_derived_from_its_own_capacity_agrees(session):
    from tracker.compute import h200_equivalent

    project = _project(
        session, name="Fine", mw_planned=100.0, h200_equivalent=h200_equivalent(100.0)
    )
    session.refresh(project)
    assert "h200_disagrees_with_capacity" not in _codes(project)


# --- shape of the command -----------------------------------------------------


def test_a_clean_row_produces_nothing(session):
    project = _project(session, name="Clean", mw_planned=100.0, investment_usd=800_000_000)
    _source(session, project, {"mw_planned": 100.0})
    session.refresh(project)
    assert audit.check_project(project) == []


def test_every_finding_says_what_to_do_about_it(session):
    """A finding with no remedy is an alarm, and alarms get ignored."""
    project = _project(session, name="Bad", mw_planned=11250.0, investment_usd=2_100_000_000)
    _source(session, project, {"mw_planned": 11250.0}, unconfirmed="mw_planned")
    session.refresh(project)
    found = audit.check_project(project)
    assert found
    assert all(f.remedy and f.summary for f in found)


def test_the_run_can_be_scoped_to_given_projects(session):
    bad = _project(session, name="Bad", mw_planned=11250.0)
    _project(session, name="AlsoBad", mw_planned=11250.0)
    session.flush()
    scoped = audit.run(session, project_ids=[bad.id])
    assert {f.project_id for f in scoped} == {bad.id}
    assert len(audit.run(session)) > len(scoped)


def test_unit_errors_sort_above_softer_smells(session):
    """`--limit`-less output is read top-down, so the poisonous ones come first."""
    _project(session, name="Cost", mw_planned=100.0, investment_usd=50_000_000_000)
    unit = _project(session, name="Unit", mw_planned=36000.0)
    session.flush()
    assert audit.run(session)[0].project_id == unit.id
