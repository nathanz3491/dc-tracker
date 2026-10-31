"""A tranche may raise a campus's phase only while its own article still stands there.

Found on Digital Gateway (#79): a 2018 article about QTS's separate Manassas campus had
its `phase` ruled a misread on the row, but the "under construction" powered shell the
same article named kept the campus at construction — over seven articles saying QTS had
cancelled it. `blocks.lifting_blocks` leaves such a tranche out, and lets a campus the
claims call cancelled or paused be lifted only by a running building or by evidence
newer than the latest report of the stop.
"""

from __future__ import annotations

import datetime as dt
import json

from tracker import blocks
from tracker.models import Project, Source
from tracker.upsert import recompute_from_sources

STOP = dt.datetime(2026, 7, 3)


def _stopped_campus(
    session,
    *,
    tranche_status="under_construction",
    tranche_published=None,
    ruled=False,
    stop="cancelled",
):
    project = Project(
        name="Digital Gateway",
        company="QTS",
        city="Gainesville",
        state="VA",
        dedup_key="qts|city:gainesville|VA",
        phase="construction",
    )
    session.add(project)
    session.flush()
    for i in range(2):
        session.add(
            Source(
                project_id=project.id,
                url=f"https://news.example/{i}",
                source_type="trade_press",
                published_at=STOP + dt.timedelta(days=i),
                fields="phase",
                claims=json.dumps({"phase": stop}),
                quotes=json.dumps({"phase": f"QTS withdrew its final appeal ({i})"}),
            )
        )
    session.add(
        Source(
            project_id=project.id,
            url="https://old.example/manassas",
            source_type="trade_press",
            published_at=tranche_published,
            fields="phase",
            claims=json.dumps({"phase": "construction"}),
            quotes=json.dumps({"phase": "QTS is building a new campus in Manassas"}),
            unconfirmed_reasons=json.dumps({"phase": "misread"}) if ruled else None,
            blocks=json.dumps([{"label": "Manassas powered shell", "status": tranche_status}]),
        )
    )
    session.flush()
    session.refresh(project)
    return project


# --- a ruled-out citation's tranche lifts nothing -----------------------------------


def test_the_tranche_of_a_misread_article_does_not_lift_a_cancelled_campus(session):
    project = _stopped_campus(session, ruled=True, tranche_published=dt.datetime(2018, 10, 3))
    recompute_from_sources(session, project)
    assert project.phase == "cancelled"


def test_a_superseded_stage_claim_leaves_its_running_building_standing(session):
    """Superseded is "outdated", not "about somewhere else": ATL3's finished expansion
    still serves although the article that named it is no longer the latest word."""
    project = Project(
        name="ATL3",
        company="DataBank",
        city="Atlanta",
        state="GA",
        dedup_key="databank|city:atlanta|GA",
        phase="construction",
    )
    session.add(project)
    session.flush()
    session.add(
        Source(
            project_id=project.id,
            url="https://a.example/1",
            source_type="trade_press",
            fields="phase",
            claims=json.dumps({"phase": "construction"}),
            quotes=json.dumps({"phase": "DataBank is building the expansion"}),
        )
    )
    session.add(
        Source(
            project_id=project.id,
            url="https://b.example/2",
            source_type="trade_press",
            fields="phase",
            claims=json.dumps({"phase": "announced"}),
            quotes=json.dumps({"phase": "DataBank announced the expansion"}),
            unconfirmed_reasons=json.dumps({"phase": "superseded"}),
            blocks=json.dumps([{"label": "ATL3 expansion", "status": "serving"}]),
        )
    )
    session.flush()
    session.refresh(project)
    recompute_from_sources(session, project)
    assert project.phase == "operational"


def test_nor_a_campus_that_is_still_only_announced(session):
    project = Project(
        name="Beumeade",
        company="Example",
        city="Ashburn",
        state="VA",
        dedup_key="example|city:ashburn|VA",
        phase="announced",
    )
    session.add(project)
    session.flush()
    session.add(
        Source(
            project_id=project.id,
            url="https://a.example/1",
            source_type="trade_press",
            fields="phase",
            claims=json.dumps({"phase": "announced"}),
            quotes=json.dumps({"phase": "The developer has filed plans"}),
        )
    )
    session.add(
        Source(
            project_id=project.id,
            url="https://b.example/2",
            source_type="trade_press",
            fields="phase",
            claims=json.dumps({"phase": "construction"}),
            quotes=json.dumps({"phase": "Construction is under way on the building"}),
            unconfirmed_reasons=json.dumps({"phase": "misread"}),
            blocks=json.dumps([{"label": "Building A", "status": "under_construction"}]),
        )
    )
    session.flush()
    session.refresh(project)
    recompute_from_sources(session, project)
    assert project.phase == "announced"


# --- a stopped campus: only a running building or newer evidence lifts it ------------


def test_an_old_planned_building_does_not_lift_a_cancelled_campus(session):
    project = _stopped_campus(session, tranche_published=dt.datetime(2018, 10, 3))
    recompute_from_sources(session, project)
    assert project.phase == "cancelled"


def test_a_tranche_on_an_undated_page_cannot_show_it_is_newer(session):
    project = _stopped_campus(session, tranche_published=None)
    recompute_from_sources(session, project)
    assert project.phase == "cancelled"


def test_a_tranche_reported_after_the_stop_still_lifts(session):
    project = _stopped_campus(session, tranche_published=dt.datetime(2026, 9, 1))
    recompute_from_sources(session, project)
    assert project.phase == "construction"


def test_a_tranche_between_two_reports_of_the_stop_does_not_lift(session):
    """Sulphur Springs (#1449): reported halted, then a roundup still listed its first
    building "under construction", then the halt was reported again. Measured against
    the first report of the stop the roundup was newer and lifted the campus back."""
    project = _stopped_campus(session, tranche_published=STOP + dt.timedelta(days=9), stop="paused")
    session.add(
        Source(
            project_id=project.id,
            url="https://news.example/halted-again",
            source_type="trade_press",
            published_at=STOP + dt.timedelta(days=70),
            fields="phase",
            claims=json.dumps({"phase": "paused"}),
            quotes=json.dumps({"phase": "construction on the project is on hold"}),
        )
    )
    session.flush()
    session.refresh(project)
    recompute_from_sources(session, project)
    assert project.phase == "paused"


def test_a_running_building_still_lifts_a_stopped_campus(session):
    """The protection `raise_phase` was written for: a serving building is not cancelled."""
    project = _stopped_campus(
        session, tranche_status="serving", tranche_published=dt.datetime(2018, 10, 3), stop="paused"
    )
    recompute_from_sources(session, project)
    assert project.phase == "operational"


# --- one rule for the write and the drift check --------------------------------------


def test_the_drift_check_expects_what_the_recompute_stores(session):
    project = _stopped_campus(session, ruled=True, tranche_published=dt.datetime(2018, 10, 3))
    recompute_from_sources(session, project)
    assert blocks.settled_phase("cancelled", project) == project.phase


def test_without_the_claims_answer_only_the_ruling_is_applied(session):
    project = _stopped_campus(session, ruled=True, tranche_published=dt.datetime(2018, 10, 3))
    recompute_from_sources(session, project)
    assert blocks.lifting_blocks(project, list(project.blocks)) == []
