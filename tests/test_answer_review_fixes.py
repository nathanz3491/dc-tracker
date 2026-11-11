"""The rules the 2026-11-11 answer-key review asked for.

Each wrong answer there was a real sentence about something else: a power plant's
construction, construction not yet started, a later expansion's ceiling, a directory's
own estimate, a halted campus's old online date, or an unquoted copy of a figure whose
quoted sentences had already been refused.
"""

from __future__ import annotations

import json

import pytest

from tracker.ingest.crawl import construction_not_of_the_campus, wider_kind


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        # A power plant is what is being built.
        (
            "Tallgrass is developing an associated natural gas power hub, which Laramie County "
            "said was already under construction in July.",
            True,
        ),
        ("At least 27 of those permanent turbines were under construction as of July.", True),
        # Construction that has not started.
        (
            "Initial plans of the developer call for construction of the first phase to commence "
            "in late 2026.",
            True,
        ),
        ("Construction is slated to begin this spring.", True),
        (
            "The company is still working through the processes, one of the final steps before "
            "construction can begin.",
            True,
        ),
        # The campus being built, whatever else the sentence names.
        ("Construction is already underway, powered by a 400MW Oncor substation.", False),
        ("The recapitalization has allowed Cologix to begin construction on its project.", False),
        (
            "CyrusOne has broken ground on two of the three facilities, with construction of the "
            "third due to commence next year.",
            False,
        ),
        ("The data center campus is under construction next to the gas plant.", False),
        ("Construction began in March on the first two buildings.", False),
    ],
)
def test_construction_that_is_not_the_campus_being_built(sentence, expected):
    assert construction_not_of_the_campus(sentence.lower()) is expected


@pytest.mark.parametrize(
    ("quote", "value", "expected"),
    [
        (
            "Monarch is designed as a multi-phase campus, with a clear expansion path to 8GW+ as "
            "workload demand grows.",
            8000,
            "covers_more",
        ),
        (
            "its October 5 commissioner packet sets a 1,000-megawatt base case with potential "
            "expansion to 1,500 megawatts.",
            1500,
            "covers_more",
        ),
        ("Power: 8 gigawatts from on-site microgrid (natural gas)", 8000, "covers_more"),
        # The base figure is the plan.
        ("a 1,000-megawatt base case with potential expansion to 1,500 megawatts.", 1000, None),
        # "Scale to" is how a plan is often stated.
        ("The complex is designed to scale to 1 gigawatt.", 1000, None),
    ],
)
def test_a_later_expansion_is_not_the_plan(quote, value, expected):
    assert wider_kind(quote, value, "mw_planned") == expected


def _row(session, **fields):
    from tracker.models import Project

    project = Project(
        name="Campus",
        company="Someone",
        city="Town",
        state="TX",
        dedup_key=f"someone|city:town|TX|{len(fields)}",
        confidence=1,
        **fields,
    )
    session.add(project)
    session.flush()
    return project


def _cite(session, project, url, claims, quotes=None, **columns):
    from tracker.models import Source

    session.add(
        Source(
            project_id=project.id,
            url=url,
            source_type="general_media",
            claims=json.dumps(claims),
            quotes=json.dumps(quotes or {}),
            **columns,
        )
    )
    session.flush()
    session.refresh(project)


def test_a_halted_campus_holds_no_online_date(session):
    from tracker.upsert import recompute_from_sources

    project = _row(session, phase="paused")
    _cite(
        session,
        project,
        "https://e.test/kit",
        {"expected_online": "2026-10-01", "phase": "paused"},
        {"expected_online": "Q4 2026: 400MW (Buildings 1-4)", "phase": "The campus is on hold."},
        fields="expected_online,phase",
    )
    recompute_from_sources(session, project)
    assert project.phase == "paused"
    assert project.expected_online is None
    assert "left empty: the campus is paused" in (project.notes or "")


def test_an_unquoted_copy_of_a_refused_figure_is_not_the_fallback(session):
    """Project Star: both quoted $22B sentences were a power campus's; an unquoted $22B stood."""
    from tracker.upsert import recompute_from_sources

    project = _row(session, phase="announced", investment_usd=22_000_000_000)
    _cite(
        session,
        project,
        "https://e.test/power",
        {"investment_usd": 22_000_000_000},
        {"investment_usd": "Project Star, a $22 billion data center and power campus in Encinal"},
        unconfirmed_fields="investment_usd",
        unconfirmed_reasons=json.dumps({"investment_usd": "covers_more"}),
    )
    _cite(
        session,
        project,
        "https://e.test/unquoted",
        {"investment_usd": 22_000_000_000},
        unconfirmed_fields="investment_usd",
        unconfirmed_reasons=json.dumps({"investment_usd": "quote_off_target"}),
    )
    recompute_from_sources(session, project)
    assert project.investment_usd is None


def test_a_directory_estimate_does_not_displace_a_quoted_figure(session):
    from tracker.confidence import TERTIARY_DOMAINS
    from tracker.upsert import recompute_from_sources

    assert {"everydatacenter.com", "wvencyclopedia.org"} <= TERTIARY_DOMAINS
    project = _row(session, phase="announced")
    _cite(
        session,
        project,
        "https://everydatacenter.com/google-project-spade.html",
        {"mw_planned": 960.0},
        {"mw_planned": "960 MW planned IT capacity at full build-out (our estimate)."},
        fields="mw_planned",
    )
    _cite(
        session,
        project,
        "https://www.related-digital.com/missouri",
        {"mw_planned": 1200.0},
        {"mw_planned": "a campus that will deliver 1.2 GW of utility capacity"},
        fields="mw_planned",
    )
    recompute_from_sources(session, project)
    assert project.mw_planned == 1200.0
