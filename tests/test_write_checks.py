"""The relationships every write must keep, and the morning report of the rest.

Step 4.6 of the client plan (2026-10-21). A held relationship is guaranteed by the
write path, so a write that would break one is a bug: it is refused, and the crawl
rolls back that record alone. The reported ones are shown on the campus's own page,
and the nightly loop records how many break each and how that moved.
"""

from __future__ import annotations

import pytest

from tracker import invariants
from tracker.models import CapacityBlock, InvariantTally, Project


def _campus(session, **kwargs) -> Project:
    defaults = {
        "name": "Abilene",
        "company": "Crusoe",
        "state": "TX",
        "city": "Abilene",
        "dedup_key": "crusoe|city:abilene|TX",
        "phase": "construction",
        "confidence": 2,
        "mw_planned": 1200.0,
    }
    defaults.update(kwargs)
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


def test_a_campus_running_past_its_plan_is_refused(session):
    project = _campus(session, mw_built=1500.0)
    with pytest.raises(invariants.HeldBroken, match="running_le_planned"):
        invariants.enforce(project)


def test_a_campus_that_keeps_every_held_relationship_passes(session):
    project = _campus(session, mw_built=400.0)
    invariants.enforce(project)
    assert invariants.campus_breaks(project, kind=invariants.HELD) == []


def test_the_reported_relationships_a_campus_breaks_are_listed_for_its_page(session):
    project = _campus(session)
    for key, label, mw in (("phase-1", "Phase 1", 1000.0), ("phase-2", "Phase 2", 800.0)):
        session.add(
            CapacityBlock(project_id=project.id, block_key=key, label=label, mw=mw, generic=False)
        )
    session.flush()
    session.refresh(project)
    keys = {inv.key for inv, _v in invariants.campus_breaks(project, kind=invariants.REPORTED)}
    assert keys == {"tranches_once"}


def test_the_project_payload_carries_them(session):
    from tracker.webui.dataset import project_payload

    project = _campus(session)
    session.add(CapacityBlock(project_id=project.id, block_key="a", label="A", mw=1000.0))
    session.add(CapacityBlock(project_id=project.id, block_key="b", label="B", mw=800.0))
    session.flush()
    session.refresh(project)
    (relation,) = project_payload(project)["relations"]
    assert relation["key"] == "tranches_once" and "counted twice" in relation["detail"]


def test_the_nightly_record_says_what_moved_since_the_last(session):
    project = _campus(session)
    first = {c.invariant.key: c for c in invariants.record(session, invariants.check(session))}
    assert first["tranches_once"].before is None
    assert "first count" in first["tranches_once"].line

    session.add(CapacityBlock(project_id=project.id, block_key="a", label="A", mw=1000.0))
    session.add(CapacityBlock(project_id=project.id, block_key="b", label="B", mw=800.0))
    session.flush()
    session.expire_all()
    second = {c.invariant.key: c for c in invariants.record(session, invariants.check(session))}
    assert second["tranches_once"].before == (0, 0.0)
    assert "+1, +600 MW" in second["tranches_once"].line
    assert "unchanged" in second["running_le_planned"].line
    assert session.query(InvariantTally).count() == 2 * len(invariants.INVARIANTS)
