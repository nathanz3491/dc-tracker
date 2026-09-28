"""What a campus's phase and progress may rest on, and how the page says so.

Found on Switch's The Rock (#44), which read `operational` — marked "quoted", over
the sentence "Switch has unveiled plans" — while every quoted citation said
`announced`. Three faults stacked:

* two unnamed blocks ("Phase 1", "Phase 2", status `serving`, no quote) lifted the
  campus, although the row already left their megawatts out as unplaceable — a
  city page about several operators had put Sabey's finished building on Switch;
* a forecast ("expected to begin operations", late 2024) was filed as `energized`,
  and counted as reached once its date passed;
* the panel printed another claim's sentence under the stored value, and called
  claims that disagree "different scopes".
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker import blocks, export, gaps, tracks
from tracker.ingest import crawl
from tracker.models import Event, Project, Source
from tracker.upsert import recompute_from_sources


def _campus(session, *, blocks_=()):
    project = Project(
        name="The Rock",
        company="Switch",
        city="Round Rock",
        state="TX",
        dedup_key="switch|rock",
        phase="announced",
    )
    session.add(project)
    session.flush()
    for i, url in enumerate(("https://a.example/1", "https://b.example/2")):
        session.add(
            Source(
                project_id=project.id,
                url=url,
                source_type="trade_press",
                fields="phase",
                claims=json.dumps({"phase": "announced"}),
                quotes=json.dumps({"phase": f"Switch has unveiled plans ({i})"}),
            )
        )
    session.add(
        Source(
            project_id=project.id,
            url="https://c.example/3",
            source_type="trade_press",
            claims=json.dumps({"phase": "operational"}),
            unconfirmed_fields="phase",
            unconfirmed_reasons=json.dumps({"phase": "no_quote"}),
        )
    )
    if blocks_:
        # Blocks are rebuilt from the citations on every recompute, so they are put
        # where real ones come from: a source's own record of the tranches it named.
        session.add(
            Source(
                project_id=project.id,
                url="https://city.example/data-centers",
                source_type="government_doc",
                blocks=json.dumps(
                    [{"label": label, "status": status} for label, status in blocks_]
                ),
            )
        )
    session.flush()
    session.refresh(project)
    return project


# --- a campus is lifted only by buildings that name themselves ---------------------


def test_an_unnamed_serving_block_does_not_lift_the_campus(session):
    project = _campus(
        session,
        blocks_=[("Phase 1", "serving"), ("Phase 2", "serving")],
    )
    recompute_from_sources(session, project)
    assert project.phase == "announced", "four quotes say announced; two unnamed phases do not"


def test_a_named_serving_building_still_lifts_it(session):
    project = _campus(session, blocks_=[("TX-1", "serving")])
    recompute_from_sources(session, project)
    assert project.phase == "operational"


def test_the_drift_check_expects_what_a_recompute_stores(session):
    """Write path and read path share one rule, or the nightly check "repairs" rows
    straight back to what the recompute raises them to."""
    project = _campus(session, blocks_=[("Phase 1", "serving")])
    recompute_from_sources(session, project)
    assert blocks.phase_after_blocks("announced", list(project.blocks)) == project.phase


# --- the panel says what set a value, and never borrows another claim's sentence --


def test_a_lifted_phase_is_derived_from_its_buildings_not_quoted(session):
    project = _campus(session, blocks_=[("TX-1", "serving")])
    recompute_from_sources(session, project)
    prov = gaps.provenance(project, "phase")
    assert prov.tier == gaps.DERIVED
    assert prov.quote is None, "no block status carries a quote"
    assert "TX-1" in prov.via


def test_a_value_no_claim_holds_borrows_no_sentence(session):
    project = _campus(session)
    project.phase = "construction"  # nothing claims it and no building explains it
    prov = gaps.provenance(project, "phase")
    assert prov.tier == gaps.UNCONFIRMED
    assert prov.quote is None, "'Switch has unveiled plans' is not evidence for construction"


def test_the_claims_panel_says_the_stored_value_is_not_the_claims_answer(session):
    project = _campus(session, blocks_=[("TX-1", "serving")])
    recompute_from_sources(session, project)
    env = export._claims_json(project)["phase"]
    assert env["stored_differs"] is True and env["claims_say"] == "announced"


def test_unquoted_disagreement_is_not_called_different_scopes(session):
    project = _campus(session)
    recompute_from_sources(session, project)
    env = export._claims_json(project)["phase"]
    assert "why" not in env, "no quoted claim disagrees"
    assert env["unquoted_rivals"] == 1, "the unquoted 'operational' does disagree"


# --- a forecast is not a milestone ---------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "description", "forecast"),
    [
        ("energized", "Switch Round Rock expected to begin operations", True),
        ("groundbreaking", "Expected groundbreaking", True),
        ("energized", "Project expected to be operational", True),
        ("energized", "Initial phase expected online in second half of 2022", True),
        ("energized", "Target delivery H2 2026", True),
        ("groundbreaking", "Construction on the AI buildings scheduled to begin late 2025", True),
        ("site_work", "Site preparation expected to begin summer 2021", True),
        ("permit_approved", "Rezoning expected to be approved next month", True),
        ("groundbreaking", "Construction began on Phase I, with 200MW planned for 2026", False),
        ("groundbreaking", "Ground broken on SMF02, the second data center on the campus", False),
        ("energized", "COL4-S data center completed and announced", False),
        ("energized", "36MW Chicago data center (CHI1) opened", False),
        ("groundbreaking", "The campus is already under construction with 2,300 workers", False),
        ("permit_approved", "Council approved the rezoning", False),
        ("announced", "Switch unveiled plans for the campus", False),
    ],
)
def test_a_forecast_is_told_from_a_milestone(event_type, description, forecast):
    assert crawl.description_is_forecast(event_type, description) is forecast


def test_the_ingest_gate_files_a_forecast_as_one():
    article = (
        "Estimates for Switch in Round Rock to begin operations range from late 2024 into 2025."
    )
    events = crawl._events(
        {
            "events": [
                {
                    "event_date": "2024-10-01",
                    "event_type": "energized",
                    "description": "Switch Round Rock expected to begin operations",
                    "quote": article,
                }
            ]
        },
        article,
        "https://a.example/1",
    )
    assert [e.unconfirmed for e in events] == ["forecast"], "the gate demotes, never deletes"


def test_a_track_does_not_count_a_forecast_whose_date_has_passed():
    class E:
        def __init__(self, kind, unconfirmed=None):
            self.event_type, self.event_date, self.unconfirmed = kind, "2024-10-01", unconfirmed

    reached = tracks.standing(1, [E("energized")], []).track("power").reached
    forecast = tracks.standing(1, [E("energized", "forecast")], []).track("power").reached
    assert reached and not forecast


def test_the_backfill_reports_first_and_writes_only_when_asked(session):
    from tracker.backfill import demote_forecast_events

    project = _campus(session)
    rows = [
        Event(
            project_id=project.id,
            event_date=dt.date(2024, 10, 1),
            event_type="energized",
            description="Switch Round Rock expected to begin operations",
        ),
        Event(
            project_id=project.id,
            event_date=dt.date(2023, 1, 1),
            event_type="groundbreaking",
            description="Crews broke ground",
            unconfirmed="quote_off_target",
        ),
        Event(
            project_id=project.id,
            event_date=dt.date(2021, 6, 2),
            event_type="announced",
            description="Switch unveiled plans for the campus",
        ),
    ]
    session.add_all(rows)
    session.flush()

    preview = demote_forecast_events(session)
    assert preview.total == 2 and all(r.unconfirmed != "forecast" for r in rows)

    done = demote_forecast_events(session, apply=True)
    assert done.total == 2
    assert [r.unconfirmed for r in rows] == ["forecast", "forecast", None]
    assert demote_forecast_events(session).total == 0, "run twice, changes nothing"
