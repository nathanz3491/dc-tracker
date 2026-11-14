"""What the 2026-11-13 night got wrong.

Five readings, each from a real sentence: a company's operations in a town read as
one campus ("its Southaven operations could amount to an over $45 billion
investment"), Epoch AI's modelled capital cost, a fuel
cell plant's capacity read as the campus's, the whole campus's completion read as its
online date, and another project's 2024 withdrawal read as this campus's stop.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker.ingest.crawl import money_kind, online_kind, wider_kind

# --- readings -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "value", "expected"),
    [
        (
            "It hosts an estimated 243k H100-equivalents of AI compute, supported by 238 MW "
            "of IT power at a capital cost of $9.0 B.",
            9e9,
            "speculative",
        ),
        ("Meta will build a $10 billion data center.", 10e9, None),
    ],
)
def test_epoch_ais_modelled_cost_is_an_estimate(quote, value, expected):
    assert money_kind(quote, value) == expected


@pytest.mark.parametrize(
    ("quote", "value", "expected"),
    [
        (
            "The company has said its Southaven operations could amount to an over $45 "
            "billion investment.",
            45e9,
            "covers_more",
        ),
        # The facility's own operations are the campus.
        (
            "expanding the facility's operations from cryptocurrency mining to an AI data "
            "center that will cost $1 billion",
            1e9,
            None,
        ),
        # A hedged budget of the campus's own stays its budget.
        ("Total investment in the project could reach up to $900 million.", 9e8, None),
        ("The $2 billion campus will start operations in 2027.", 2e9, None),
    ],
)
def test_a_companys_operations_in_a_place_are_more_than_the_campus(quote, value, expected):
    assert wider_kind(quote, value, "investment_usd") == expected


@pytest.mark.parametrize(
    ("quote", "value", "expected"),
    [
        (
            "As part of the expanded partnership, the data center will be supported by up "
            "to 2.45 GW of installed Bloom fuel cell capacity.",
            2450.0,
            "covers_more",
        ),
        (
            "it would be powered by up to 2.45 GW of gas-fuelled Bloom Energy fuel cells in "
            "an on-site microgrid.",
            2450.0,
            "covers_more",
        ),
        # What the campus draws is its own demand, wherever the power comes from.
        (
            "Jupiter is designed to draw up to 2.45 gigawatts from a private microgrid of "
            "Bloom Energy fuel cells running on natural gas.",
            2450.0,
            None,
        ),
        ("a 1 GW data center campus in Texas", 1000.0, None),
    ],
)
def test_a_fuel_cell_plants_capacity_is_not_the_campus(quote, value, expected):
    assert wider_kind(quote, value, "mw_planned") == expected


@pytest.mark.parametrize(
    ("quote", "value", "expected"),
    [
        (
            "The entire campus is currently scheduled for completion by June 2036, meaning "
            "construction will continue for nearly another decade.",
            "2036-06-01",
            "build_out",
        ),
        ("Full build-out is expected by 2032.", "2032-01-01", "build_out"),
        (
            "Serverfarm said the first building will begin operations in the summer of 2027, "
            "with the entire facility operational by fall 2028.",
            "2028-09-01",
            "build_out",
        ),
        # The first building's year in the same sentence is the online date.
        (
            "Serverfarm said the first building will begin operations in the summer of 2027, "
            "with the entire facility operational by fall 2028.",
            "2027-07-01",
            None,
        ),
        (
            "DataBank expects to deliver the first of the four initial buildings in the "
            "third quarter of 2026 and the last by the end of 2027.",
            "2027-10-01",
            "build_out",
        ),
        ("Macrohardrr is expected to be online in 2027.", "2027-01-01", None),
        # "The last quarter" is a quarter, not the last building.
        (
            "The first phase of the project is scheduled to be online in the last quarter of "
            "2028, with construction slated for completion in 2029, Allen said.",
            "2028-10-01",
            None,
        ),
        # A build-out that sizes the campus, then the first phase's year.
        (
            "The site will span ~600 acres and deliver 1.2GW of capacity upon full "
            "buildout, with an initial phase of 260MW expected to be operational by "
            "October 2027.",
            "2027-10-01",
            None,
        ),
        ("Completion of the full campus is expected by mid-2026.", "2026-07-01", "build_out"),
        # The build-out words belong to the year they come before.
        (
            "Full buildout is not projected to complete until around 2038, with the first "
            "building targeted for 2028.",
            "2028-01-01",
            None,
        ),
    ],
)
def test_the_whole_campus_finished_is_not_its_online_date(quote, value, expected):
    assert online_kind(quote, value) == expected


def test_a_stop_dated_before_the_campus_was_announced_is_another_projects(session):
    """Tract Buckeye (#1619): announced 2024-08, "withdrawn in May 2024" was another site."""
    from tracker.models import Project, Source
    from tracker.upsert import recompute_from_sources

    project = Project(
        name="Tract Buckeye",
        company="Tract",
        state="AZ",
        city="Buckeye",
        dedup_key="tract|city:buckeye|AZ",
        confidence=1,
        phase="announced",
    )
    session.add(project)
    session.flush()

    def cite(url, claims, quotes, meta):
        session.add(
            Source(
                project_id=project.id,
                url=url,
                source_type="general_media",
                claims=json.dumps(claims),
                quotes=json.dumps(quotes),
                claim_meta=json.dumps(meta),
                fields=",".join(claims),
                fetched_at=dt.datetime(2026, 11, 13),
            )
        )
        session.flush()

    cite(
        "https://t.test/announce",
        {"phase": "announced", "first_announced": "2024-08-28"},
        {"phase": "Tract announced its acquisition", "first_announced": "Aug. 28, 2024"},
        {},
    )
    cite(
        "https://t.test/report",
        {"phase": "cancelled"},
        {"phase": "Tract's $14 billion project in Arizona was withdrawn in May 2024"},
        {"phase": {"as_of": "2024-05-01"}},
    )
    session.refresh(project)
    recompute_from_sources(session, project)
    assert project.phase == "announced"

    # A stop after the announcement still stops it.
    cite(
        "https://t.test/later",
        {"phase": "cancelled"},
        {"phase": "Tract withdrew the Buckeye project in March 2026"},
        {"phase": {"as_of": "2026-03-01"}},
    )
    session.refresh(project)
    recompute_from_sources(session, project)
    assert project.phase == "cancelled"
