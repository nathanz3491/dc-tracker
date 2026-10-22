"""A campus's phase is never below a milestone it has already reached.

On 2026-10-21 the merge's filters removed every claim that held three campuses up:
Google's Clydesdale fell to `announced` beside its October 2025 groundbreaking and
Niagara to `announced` beside two approved permits. `blocks.phase_after_milestones` lifts the
phase back to what a quoted, dated, past milestone means.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tracker.blocks import phase_after_milestones, settled_phase

TODAY = dt.date(2026, 10, 22)


def _event(kind: str, when: dt.date | None, unconfirmed: str | None = None):
    return SimpleNamespace(event_type=kind, event_date=when, unconfirmed=unconfirmed)


@pytest.mark.parametrize(
    ("phase", "event", "expected"),
    [
        ("announced", _event("groundbreaking", dt.date(2025, 10, 1)), "construction"),
        # Power on is as often a construction feed as a hall in service.
        ("construction", _event("energized", dt.date(2026, 6, 4)), "construction"),
        ("announced", _event("permit_approved", dt.date(2026, 6, 1)), "permitting"),
        # Already further along: nothing lowers.
        ("operational", _event("groundbreaking", dt.date(2025, 10, 1)), "operational"),
        # A schedule is not a milestone reached.
        ("announced", _event("groundbreaking", dt.date(2027, 3, 1)), "announced"),
        ("announced", _event("groundbreaking", dt.date(2025, 10, 1), "forecast"), "announced"),
        ("announced", _event("groundbreaking", None), "announced"),
        # Milestones that say nothing about the phase.
        ("announced", _event("land_acquired", dt.date(2025, 1, 1)), "announced"),
        # A paused campus that once broke ground is still paused.
        ("paused", _event("groundbreaking", dt.date(2025, 10, 1)), "paused"),
    ],
)
def test_a_reached_milestone_is_a_floor(phase, event, expected):
    assert phase_after_milestones(phase, [event], today=TODAY) == expected


def test_the_write_and_the_drift_check_agree():
    """`settled_phase` is what `logic.check_collisions` expects a recompute to store."""
    project = SimpleNamespace(
        events=[_event("groundbreaking", dt.date(2025, 10, 1))], blocks=[], nesting=[]
    )
    assert settled_phase("announced", project) == "construction"


def test_the_recompute_stores_it(session):
    import json

    from tracker.models import Event, Project, Source
    from tracker.upsert import recompute_from_sources

    project = Project(
        name="Clydesdale",
        company="Google",
        city="Owasso",
        state="OK",
        dedup_key="google|city:owasso|OK",
        phase="construction",
        confidence=1,
    )
    session.add(project)
    session.flush()
    source = Source(
        project_id=project.id,
        url="https://datacenters.google/oklahoma",
        source_type="company_filing",
        fetched_at=dt.datetime(2026, 10, 21),
        claims=json.dumps({"phase": "announced"}),
        quotes=json.dumps({"phase": "Google announces the development of a new campus."}),
        fields="phase",
    )
    session.add(source)
    session.flush()
    session.add(
        Event(
            project_id=project.id,
            source_id=source.id,
            event_type="groundbreaking",
            event_date=dt.date(2025, 10, 1),
            description="Beale broke ground in October 2025",
        )
    )
    session.flush()
    session.refresh(project)
    recompute_from_sources(session, project)
    assert project.phase == "construction"
