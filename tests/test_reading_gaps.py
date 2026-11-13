"""Four readings the 2026-11-11 night got wrong, each from a real sentence.

A loan total ("financing to $2.65 billion"), a utility's cost for the power it builds
for a campus, a later scaling ceiling ("potential scaling to 4 GW"), and an
approximation ("nearly 1 gigawatt") that beat five exact figures because it was read
last.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker.ingest.crawl import money_kind, stated_roughly, wider_kind


@pytest.mark.parametrize(
    ("quote", "value", "expected"),
    [
        ("This brings the total Red Oak campus financing to $2.65 billion.", 2.65e9, "financing"),
        ("The loans to $500 million were refinanced last year.", 500e6, "financing"),
        # "Financing to" a company is not a figure.
        ("Blackstone provided financing to DataBank for a $1 billion campus.", 1e9, None),
    ],
)
def test_a_running_financing_total_is_financing(quote, value, expected):
    assert money_kind(quote, value) == expected


@pytest.mark.parametrize(
    ("quote", "value", "expected"),
    [
        (
            "According to the Oct. 7 order , AES will acquire or build new generation, storage "
            "and grid infrastructure for a 390-MW data center planned for Monrovia, a town "
            "Southwest of Indianapolis, at an estimated project cost of about $737 million.",
            737e6,
            "covers_more",
        ),
        (
            "Entergy will build $1.2 billion of transmission infrastructure to serve the campus.",
            1.2e9,
            "covers_more",
        ),
        # A campus budget that includes grid work is the campus's.
        (
            "The $2 billion data center campus includes grid infrastructure upgrades.",
            2e9,
            None,
        ),
        ("The developer will spend $3 billion on the campus, with a new substation.", 3e9, None),
    ],
)
def test_the_utilitys_infrastructure_for_a_campus_is_not_its_investment(quote, value, expected):
    assert wider_kind(quote, value, "investment_usd") == expected


@pytest.mark.parametrize(
    ("quote", "value", "expected"),
    [
        (
            "The AI data center campus is projected to require 100-200 MW of power at launch , "
            "with potential scaling to 4 GW .",
            4000,
            "covers_more",
        ),
        ("A 300 MW first phase, with the potential to scale to 1 GW.", 1000, "covers_more"),
        ("The site could scale to 2 gigawatts over a decade.", 2000, "covers_more"),
        # A plan stated as a scale is the plan.
        ("The complex is designed to scale to 1 gigawatt.", 1000, None),
        # The launch figure beside a ceiling is the plan.
        ("100 MW at launch, with potential scaling to 4 GW.", 100, None),
    ],
)
def test_a_possible_scaling_is_a_ceiling_not_the_plan(quote, value, expected):
    assert wider_kind(quote, value, "mw_planned") == expected


@pytest.mark.parametrize(
    ("quote", "value", "field", "expected"),
    [
        (
            "four state-of-the-art data centers with computing capacity of nearly 1 gigawatt",
            1000,
            "mw_planned",
            True,
        ),
        ("an investment of about $2 billion", 2e9, "investment_usd", True),
        ("roughly 45 MW of IT load", 45, "mw_planned", True),
        ("around 1,200 megawatts", 1200, "mw_planned", True),
        ("a 1,300 MW campus", 1300, "mw_planned", False),
        # A precise figure with a hedge on it is still precise.
        ("construction costs of approximately $1.234 billion", 1.234e9, "investment_usd", False),
        ("about $205 million", 205e6, "investment_usd", False),
        # Bounds are other statements: "up to" is how plans are stated, and a floor
        # is as often the better figure as the worse.
        ("a campus valued at more than $1 billion", 1e9, "investment_usd", False),
        ("up to 700 MW", 700, "mw_planned", False),
        # The approximation must be on the figure claimed.
        ("about 300 jobs and a 1,300 MW campus", 1300, "mw_planned", False),
        ("nearly $1 billion, of which $400 million in phase one", 400e6, "investment_usd", False),
        (None, 1000, "mw_planned", False),
    ],
)
def test_a_figure_given_roughly_is_named(quote, value, field, expected):
    assert stated_roughly(quote, value, field) is expected


def _row(session):
    from tracker.models import Project

    project = Project(
        name="Lighthouse",
        company="Vantage",
        city="Port Washington",
        state="WI",
        dedup_key="vantage|city:port washington|WI",
        confidence=1,
        phase="construction",
    )
    session.add(project)
    session.flush()
    return project


def _cite(session, project, url, mw, quote, *, source_type="general_media", fetched):
    from tracker.models import Source

    session.add(
        Source(
            project_id=project.id,
            url=url,
            source_type=source_type,
            claims=json.dumps({"mw_planned": mw}),
            quotes=json.dumps({"mw_planned": quote}),
            fields="mw_planned",
            fetched_at=fetched,
        )
    )
    session.flush()
    session.refresh(project)


def test_an_exact_figure_outranks_a_rough_one_read_later(session):
    from tracker.upsert import recompute_from_sources

    project = _row(session)
    day = dt.datetime(2026, 11, 1)
    _cite(session, project, "https://e.test/a", 1300, "a 1,300 MW campus", fetched=day)
    _cite(
        session,
        project,
        "https://e.test/b",
        1000,
        "four data centers with computing capacity of nearly 1 gigawatt",
        fetched=day + dt.timedelta(days=10),
    )
    recompute_from_sources(session, project)
    assert project.mw_planned == 1300


def test_an_approximation_others_state_exactly_keeps_its_place(session):
    """Metrobloks Southfield: the newest of four $1.5B articles said "roughly"."""
    from tracker.upsert import recompute_from_sources

    project = _row(session)
    day = dt.datetime(2026, 11, 1)
    _cite(session, project, "https://e.test/a", 1500, "a 1,500 MW campus", fetched=day)
    _cite(
        session,
        project,
        "https://e.test/b",
        500,
        "a 500 MW first phase is all it has funded",
        fetched=day + dt.timedelta(days=5),
    )
    _cite(
        session,
        project,
        "https://e.test/c",
        1500,
        "roughly 1.5 GW in two phases",
        fetched=day + dt.timedelta(days=10),
    )
    recompute_from_sources(session, project)
    assert project.mw_planned == 1500


def test_a_heavier_sources_approximation_still_wins(session):
    from tracker.upsert import recompute_from_sources

    project = _row(session)
    day = dt.datetime(2026, 11, 1)
    _cite(session, project, "https://e.test/a", 1300, "a 1,300 MW campus", fetched=day)
    _cite(
        session,
        project,
        "https://e.test/filing",
        1000,
        "a campus of about 1 GW",
        source_type="company_filing",
        fetched=day,
    )
    recompute_from_sources(session, project)
    assert project.mw_planned == 1000
