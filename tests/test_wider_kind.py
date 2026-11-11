"""A figure for more than the campus is never the campus's value.

On 2026-11-11 three of the six wrong investment figures left in the answer key were
real figures for a bigger object — an energy infrastructure campus, a power station
converted with its data center, two campuses summed into a county's total — and the
reader's own scope label said "this site" for all of them.
"""

from __future__ import annotations

import json

import pytest

from tracker.ingest.crawl import wider_kind


@pytest.mark.parametrize(
    ("quote", "value", "field", "expected"),
    [
        # Several campuses, or a county's total.
        (
            "The original $10 billion commitment plus an additional $11 billion brings tracked "
            "Madison County investment to $21 billion.",
            21e9,
            "investment_usd",
            "covers_more",
        ),
        (
            "The new owners plan to invest $1 billion to create new data centers across "
            "multiple sites in Loudoun County.",
            1e9,
            "investment_usd",
            "covers_more",
        ),
        (
            "The proposal called for three campuses across 200 acres with up to 500 MW of "
            "capacity supplied by Constellation Energy.",
            500,
            "mw_planned",
            "covers_more",
        ),
        # The power plant is part of the object.
        (
            "Project Star, a $22.3 billion energy infrastructure campus in Encinal, Texas",
            22.3e9,
            "investment_usd",
            "covers_more",
        ),
        (
            "a $10 billion plan to convert the former Homer City Generating Station into a "
            "natural gas-powered energy and data center campus.",
            10e9,
            "investment_usd",
            "covers_more",
        ),
        (
            "Project Star, a $22 billion data center and power campus in Encinal",
            22e9,
            "investment_usd",
            "covers_more",
        ),
        (
            "Plans for a 96-megawatt data center, a 180-megawatt on-site power plant, and an "
            "estimated $1.113 billion in investment.",
            1.113e9,
            "investment_usd",
            "covers_more",
        ),
        (
            "the number nobody would give residents at a hearing: an 800 megawatt power plant",
            800,
            "mw_planned",
            "covers_more",
        ),
        (
            "plans to develop a scalable clean energy campus in Pike County with capacity of up "
            "to 1.2 gigawatts",
            1200,
            "mw_planned",
            "covers_more",
        ),
        # The campus's own total, however long the horizon or the phases.
        (
            "Equinix expects to invest at least $1 billion on its North Ashburn campus over the "
            "next 10 years.",
            1e9,
            "investment_usd",
            None,
        ),
        (
            "Total campus target: 320 MW, ~$5B investment across all phases.",
            5e9,
            "investment_usd",
            None,
        ),
        (
            "the total combined investment is estimated to exceed $2 billion for EdgeCore and its "
            "customers",
            2e9,
            "investment_usd",
            None,
        ),
        (
            "Construction costs for the two sites could reach $10 billion.",
            10e9,
            "investment_usd",
            None,
        ),
        # A plant that supplies or neighbours the campus is not part of its figure.
        (
            "Set to cost $1.5 billion, the data centers will be powered by the Freestone Energy "
            "Campus 1GW natural gas-fired combined-cycle facility",
            1.5e9,
            "investment_usd",
            None,
        ),
        (
            "a proposed $3.6 billion, 600 MW data center that would have consumed 15% of Tucson "
            "Electric Power's current generation capacity",
            3.6e9,
            "investment_usd",
            None,
        ),
        (
            "The proposed development would be right near an existing power plant and demand "
            "approximately 6 gigawatts at full build-out.",
            6000,
            "mw_planned",
            None,
        ),
        (
            "will deliver 3,000 MW across 30 hyperscale data center buildings — with on-site "
            "behind-the-meter power generation",
            3000,
            "mw_planned",
            None,
        ),
        (
            "300 megawatts of critical IT load with direct access to energy infrastructure",
            300,
            "mw_planned",
            None,
        ),
        # One campus in a list keeps its own figure.
        (
            "including the 3 new campuses: a 480MW campus in south Dallas, a 192MW campus in "
            "Culpeper",
            192,
            "mw_planned",
            None,
        ),
        (
            "a 10 GW data center campus powered by 10 GW of new generation",
            10000,
            "mw_planned",
            None,
        ),
        # The wider figure is another number than the one claimed.
        (
            "Microsoft will invest $2 billion in the campus, part of $80 billion across multiple "
            "sites",
            2e9,
            "investment_usd",
            None,
        ),
    ],
)
def test_a_figure_for_more_than_the_campus_is_named(quote, value, field, expected):
    assert wider_kind(quote, value, field) == expected


def test_the_nightly_regate_marks_planned_capacity_and_the_merge_leaves_it_out(session):
    from tracker.backfill import regate_scope
    from tracker.models import Project, Source
    from tracker.upsert import recompute_from_sources

    project = Project(
        name="900 Conshohocken Road",
        company="MLP Ventures",
        city="Conshohocken",
        state="PA",
        dedup_key="mlp|city:conshohocken|PA",
        phase="announced",
        confidence=1,
        mw_planned=800.0,
    )
    session.add(project)
    session.flush()
    source = Source(
        project_id=project.id,
        url="https://e.test/plant",
        source_type="trade_press",
        claims=json.dumps({"mw_planned": 800.0}),
        quotes=json.dumps({"mw_planned": "Buried in that request: an 800 megawatt power plant."}),
        fields="mw_planned",
    )
    session.add(source)
    session.flush()
    session.refresh(project)

    report = regate_scope(session, apply=True)
    assert report.money_marked == {"covers_more": 1}
    assert json.loads(source.unconfirmed_reasons) == {"mw_planned": "covers_more"}
    recompute_from_sources(session, project)
    assert project.mw_planned is None, "a power plant's megawatts are not the campus's"
