"""Values that moved the wrong way on 2026-10-19, and the rules that stop them.

The night read 600 articles, many of them old: the builders' archives reach back a
decade and re-reads replay articles under a newer model. Every rule below is about
evidence that is real but coarser, older or second-hand displacing evidence that is
finer, newer or first-hand.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker import confidence as conf
from tracker.dedup import customer_key, is_undisclosed
from tracker.upsert import Policy, _Claim, resolve


def _date(value, *, precision=None, weight=2, url="https://a.test/1"):
    return _Claim(value, weight, dt.datetime(2026, 1, 1), "trade_press", url, precision=precision)


# --- a coarse date never displaces a finer one inside it ---------------------------


def test_the_earliest_date_keeps_its_day_against_the_same_years_bare_year():
    """Fairwater: announced 2023-03-30, and an archive post saying "2023" read as
    2023-01-01 took the row's day away."""
    claims = [
        _date("2023-03-30", url="https://a.test/precise"),
        _date("2023-01-01", precision="year", url="https://a.test/year"),
    ]
    assert resolve(Policy.MIN, claims, None, ratchet=False) == "2023-03-30"


def test_a_genuinely_earlier_year_still_wins():
    claims = [_date("2023-03-30"), _date("2022-01-01", precision="year", url="https://b.test")]
    assert resolve(Policy.MIN, claims, None, ratchet=False) == "2022-01-01"


def test_refinement_takes_the_earliest_finer_date_and_stays_inside_the_span():
    claims = [
        _date("2023-01-01", precision="year", url="https://a.test/y"),
        _date("2023-11-02", url="https://a.test/d"),
        _date("2023-03-01", precision="month", url="https://a.test/m"),
        _date("2024-02-01", url="https://a.test/next-year"),
    ]
    assert resolve(Policy.MIN, claims, None, ratchet=False) == "2023-03-01"


def test_the_strongest_forecast_is_narrowed_not_replaced():
    """Stargate Abilene: "September 2025" became "2025" when a heavier source said
    2025. The two agree; one says when within the year."""
    claims = [
        _date("2025-01-01", precision="year", weight=3, url="https://filing.test"),
        _date("2025-09-01", precision="month", weight=2, url="https://news.test"),
    ]
    claims.sort(key=lambda c: c.weight, reverse=True)
    assert resolve(Policy.PREFER_WEIGHT, claims, None, ratchet=False) == "2025-09-01"


def test_a_forecast_outside_the_winners_span_does_not_move_it():
    claims = [
        _date("2025-01-01", precision="year", weight=3, url="https://filing.test"),
        _date("2026-03-01", precision="month", weight=2, url="https://news.test"),
    ]
    claims.sort(key=lambda c: c.weight, reverse=True)
    assert resolve(Policy.PREFER_WEIGHT, claims, None, ratchet=False) == "2025-01-01"


def test_a_quarter_is_three_months_and_a_half_six():
    q3 = [
        _date("2027-07-01", precision="quarter", weight=3, url="https://q.test"),
        _date("2027-10-15", weight=2, url="https://outside.test"),
        _date("2027-08-20", weight=1, url="https://inside.test"),
    ]
    q3.sort(key=lambda c: c.weight, reverse=True)
    assert resolve(Policy.PREFER_WEIGHT, q3, None, ratchet=False) == "2027-08-20"


# --- what counts as corroboration --------------------------------------------------


def _view(url, fields, source_type="trade_press"):
    return conf.SourceView(source_type=source_type, url=url, fields=fields)


def test_a_citation_that_only_named_the_campus_corroborates_nothing():
    """818 of 4,782 citations listed only identity fields — which skip the gate —
    and lifted rows from 2 to 3 for confirming where a campus is."""
    quoted = _view("https://one.test/a", "company,city,state,mw_planned")
    named = _view("https://two.test/b", "company,city,state")
    score = conf.compute([quoted, named], populated_tracked_fields=12)
    assert not any("independent domains" in r for r in score.reasons)

    second = _view("https://two.test/b", "company,state,investment_usd")
    score = conf.compute([quoted, second], populated_tracked_fields=12)
    assert any("2 independent domains" in r for r in score.reasons)


@pytest.mark.parametrize(
    "url",
    [
        "https://datacenterindex.ai/projects?developer=CoreWeave",
        "https://www.kadoa.com/datacenter/operator/compass-datacenters",
        "https://savrn.com/data-center-capital-atlas/mississippi",
        "https://usdatacenterprojects.com/virginia/tract-files-900-mw",
    ],
)
def test_the_directories_the_night_reached_are_not_first_hand(url):
    assert conf.is_tertiary(_view(url, "mw_built"))


@pytest.mark.parametrize(
    "customer", ["Fortune 50 company", "AA-rated hyperscaler", "a leading global tech company"]
)
def test_a_hedged_tenant_is_not_a_named_one(customer):
    assert is_undisclosed(customer)
    assert customer_key(customer) == ""


def test_a_named_tenant_still_is():
    assert customer_key("Microsoft") == "microsoft"
    assert not is_undisclosed("Meta Platforms")


# --- a derived delay on a running campus --------------------------------------------


def test_a_derived_delay_on_an_operating_campus_is_found_and_cleared_for_free(session):
    from tracker import logic
    from tracker.models import Event, Project

    project = Project(
        name="Council Bluffs",
        company="Google",
        city="Council Bluffs",
        state="IA",
        dedup_key="google|city:council bluffs|IA",
        phase="operational",
    )
    session.add(project)
    session.flush()
    session.add(
        Event(
            project_id=project.id,
            event_date=dt.date(2030, 1, 1),
            event_type="delayed",
            description="expected_online moved from 2025-01-01 to 2030-01-01 (+1826 days)",
        )
    )
    session.flush()
    session.refresh(project)

    finding = next(
        f for f in logic.check_rules(project) if f.code == "delay_on_an_operating_campus"
    )
    assert finding.code in logic.FREE_CODES
    key, _why = logic.free_answer(project, finding)
    action = next(a for a in logic.ACTIONS[finding.code] if a.key == key)
    assert "removed 1" in action.apply(session, project, finding)
    assert not [e for e in project.events if e.event_type == "delayed"]
