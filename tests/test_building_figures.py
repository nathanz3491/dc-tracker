"""One building's figure never stands as the campus's while the campus's own is known.

A capacity or investment sentence that gives its figure to one building or phase —
"the 36MW Hillsboro 3 data center", "initially offering 75MW", "VA-2, a $225 million
two-story data center" — is read out of the sentence, never asked of the model, and
labelled scope `building`. The merge then lets it fill the campus column only when
nothing describes the whole site. What is pinned here is the reading's positional
rules, taken from sentences in the corpus, and the three paths that apply it.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker.ingest.crawl import axis_gate, site_names
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.models import Project, Source
from tracker.upsert import upsert_record
from tracker.vocab import part_from_quote

ROW = site_names({"name": "Flexential Hillsboro Campus", "city": "Hillsboro"})


@pytest.mark.parametrize(
    ("quote", "value"),
    [
        ("The 36MW Hillsboro 3 data center now offers customers 358,000 square feet.", 36),
        ("initially offering 75MW of grid-connected capacity from 2028.", 75),
        (
            "VA-2, a $225 million two-story data center built in 2020 with 36 megawatts.",
            225_000_000,
        ),
        ("the first 12MW phase of CHI-2 in Chicago.", 12),
        ("NTT Phoenix PH1 will add 36MW and is the first of seven on the 240MW campus.", 36),
        ("Building K will have 19.2 MW of critical load.", 19.2),
        ("capacity of 1.5GW in Phase 1, rising to 2.5GW in future phases.", 1500),
    ],
)
def test_a_figure_its_sentence_gives_to_one_part_is_that_parts(quote, value):
    assert part_from_quote(quote, value, ROW)


@pytest.mark.parametrize(
    ("quote", "value"),
    [
        # the campus total beside a building's name
        ("NTT Phoenix PH1 will add 36MW and is the first of seven on the 240MW campus.", 240),
        ("the 54MW POR03F, which will bring the campus up to a total capacity of 200MW.", 200),
        ("capacity of 1.5GW in Phase 1, rising to 2.5GW in future phases.", 2500),
        ("its $1.5 billion, 176 MW AZ1 campus in Goodyear", 1_500_000_000),
        ("the first of five buildings and 142 megawatts planned for the campus.", 142),
        ("support the first phase of its 300MW Cinco data center campus", 300),
        # "initial" a few words away names something else
        ("is in the initial stages of developing a 100MW data center complex", 100),
        ("$19 billion in revenue over its initial term.", 19_000_000_000),
        # a number that is not a building's
        ("526 MW Committed Critical IT Load1 15 Years Contract Term", 526),
        ("Microsoft plans a 300 MW data center 30 miles east of the city.", 300),
        # the same figure twice, once for each of two things
        ("a site initially at 25 MW and potentially 75 MW, and one initially at 75 MW", 75),
        # an earlier figure's qualifier does not reach across it
        (
            "initially announced at $1 billion, it is now expected to cost $3 billion.",
            3_000_000_000,
        ),
    ],
)
def test_a_figure_for_the_whole_site_is_left_alone(quote, value):
    assert part_from_quote(quote, value, ROW) == ""


def test_a_building_the_row_is_named_after_is_the_site():
    """ "COL4" on a row called "Cologix COL4" is the whole of what the row describes."""
    quote = "IAD4 will add 20MW of IT load"
    assert part_from_quote(quote, 20, site_names({"name": "DataBank IAD3"})) == "iad4"
    assert part_from_quote(quote, 20, site_names({"name": "DataBank IAD4"})) == ""


def test_the_gate_reads_it_and_refuses_it_from_the_model():
    quote = "The 36MW Hillsboro 3 data center now offers customers 358,000 square feet."
    got = axis_gate({"scope": "this_site"}, quote, site_names=ROW, field="mw_planned", value=36)
    assert got["scope"] == "building"
    # A model volunteering the label earns nothing the sentence does not say: it
    # falls back to the neutral default, as any unlicensed scope does.
    plain = "The campus will offer 36MW of capacity."
    got = axis_gate({"scope": "building"}, plain, site_names=ROW, field="mw_planned", value=36)
    assert got["scope"] == "unnamed"
    # Only campus totals take it: a building's energised megawatts are a floor.
    got = axis_gate({"scope": "unnamed"}, quote, site_names=ROW, field="mw_built", value=36)
    assert got["scope"] == "unnamed"


def _campus(session, *sources) -> Project:
    result = upsert_record(
        session,
        IngestRecord(
            project={
                "company": "Flexential",
                "name": "Flexential Hillsboro Campus",
                "city": "Hillsboro",
                "state": "OR",
            },
            sources=list(sources),
        ),
    )
    session.commit()
    return session.get(Project, result.project_id)


def _cite(url, when, field, value, quote, scope=None):
    return SourceRecord(
        url=url,
        source_type="trade_press",
        excerpt="excerpt",
        claims={field: value},
        quotes={field: quote},
        claim_meta={field: {"scope": scope}} if scope else {},
        fetched_at=when,
    )


BUILDING = "The 36MW Hillsboro 3 data center now offers customers 358,000 square feet."


def test_one_buildings_figure_loses_to_the_campus_figure(session):
    """Newer, same weight, quoted — and still not the campus's 200 MW."""
    project = _campus(
        session,
        _cite(
            "https://a.example/campus",
            dt.datetime(2025, 1, 1),
            "mw_planned",
            200.0,
            "the Hillsboro campus will total 200MW",
        ),
        _cite(
            "https://b.example/h3",
            dt.datetime(2026, 1, 1),
            "mw_planned",
            36.0,
            BUILDING,
            scope="building",
        ),
    )
    assert project.mw_planned == 200.0


def test_one_buildings_figure_fills_a_campus_nothing_else_describes(session):
    project = _campus(
        session,
        _cite(
            "https://b.example/h3",
            dt.datetime(2026, 1, 1),
            "mw_planned",
            36.0,
            BUILDING,
            scope="building",
        ),
    )
    assert project.mw_planned == 36.0


def test_the_reason_names_the_rule(session):
    from tracker.logic import check_collisions

    project = _campus(
        session,
        _cite(
            "https://a.example/campus",
            dt.datetime(2025, 1, 1),
            "mw_planned",
            200.0,
            "the Hillsboro campus will total 200MW",
        ),
        _cite(
            "https://b.example/h3",
            dt.datetime(2026, 1, 1),
            "mw_planned",
            36.0,
            BUILDING,
            scope="building",
        ),
    )
    (collision,) = [c for c in check_collisions(project) if c.field == "mw_planned"]
    assert collision.decided_by == "whole site"
    assert "one building" in collision.why


def test_backfill_reads_stored_claims_and_undoes_a_stale_reading(session):
    """Claims written before the reading existed carry no envelope, or `unnamed`."""
    from tracker.backfill import regate_scope

    project = Project(
        name="Flexential Hillsboro Campus",
        company="Flexential",
        city="Hillsboro",
        state="OR",
        dedup_key="flexential|city:hillsboro|OR",
        phase="construction",
    )
    session.add(project)
    session.flush()
    bare = Source(
        project_id=project.id,
        url="https://b.example/h3",
        source_type="trade_press",
        fetched_at=dt.datetime(2026, 1, 1),
        claims=json.dumps({"mw_planned": 36.0}),
        quotes=json.dumps({"mw_planned": BUILDING}),
    )
    stale = Source(
        project_id=project.id,
        url="https://a.example/campus",
        source_type="trade_press",
        fetched_at=dt.datetime(2025, 1, 1),
        claims=json.dumps({"mw_planned": 200.0}),
        quotes=json.dumps({"mw_planned": "the Hillsboro campus will total 200MW"}),
        claim_meta=json.dumps({"mw_planned": {"scope": "building"}}),
    )
    session.add_all([bare, stale])
    session.flush()

    report = regate_scope(session, apply=True)

    assert report.changed == 2
    assert json.loads(bare.claim_meta) == {"mw_planned": {"scope": "building"}}
    assert json.loads(stale.claim_meta)["mw_planned"]["scope"] == "this_site"
    assert regate_scope(session, apply=True).changed == 0, "a second pass moved something"


def test_the_agent_path_labels_its_facts_the_same_way():
    from tracker.gapfill import _fact_axes

    got = _fact_axes({"mw_planned": (36.0, BUILDING), "phase": ("operational", BUILDING)}, ROW)
    assert got == {"mw_planned": {"scope": "building"}}
