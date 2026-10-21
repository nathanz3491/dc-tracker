"""The relationships every figure must keep (勾稽关系), and the register that checks them.

The client asked for the data's relationships to be defined clearly (2026-10-21, A4).
These pin that the *held* ones hold on data built the way the write path builds it,
that each *reported* one names the campus and the size of the break, and that the
register says so in a reader's words.
"""

from __future__ import annotations

import datetime as dt

from tracker import invariants
from tracker.models import CapacityBlock, Project


def _campus(session, name, **kwargs) -> Project:
    defaults = {
        "name": name,
        "company": "Someone",
        "state": "TX",
        "city": "Abilene",
        "dedup_key": f"k-{name}",
        "phase": "construction",
        "confidence": 2,
    }
    defaults.update(kwargs)
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


def _tranche(session, project, label, mw, **kwargs) -> None:
    session.add(
        CapacityBlock(
            project_id=project.id,
            block_key=label.lower().replace(" ", "-"),
            label=label,
            mw=mw,
            status=kwargs.pop("status", "planned"),
            generic=False,
            **kwargs,
        )
    )
    session.flush()


def _findings(session) -> dict[str, invariants.Finding]:
    session.expire_all()
    return {f.invariant.key: f for f in invariants.check(session)}


def test_every_invariant_is_stated_in_both_languages_and_says_what_fixes_it():
    keys = [i.key for i in invariants.INVARIANTS]
    assert len(keys) == len(set(keys))
    for inv in invariants.INVARIANTS:
        assert inv.kind in (invariants.HELD, invariants.REPORTED)
        assert inv.level in ("campus", "tranche", "company")
        assert inv.statement.endswith(".") and inv.zh.endswith("。")
        assert inv.remedy


def test_the_held_relationships_hold_on_a_campus_built_the_ordinary_way(session):
    campus = _campus(session, "Ordinary", mw_planned=300.0, mw_built=100.0, customer="Meta")
    _tranche(session, campus, "Building 1", 100.0, status="serving", customer="Meta")
    _tranche(session, campus, "Building 2", 200.0, status="under_construction")
    found = _findings(session)
    for key, finding in found.items():
        if finding.invariant.kind == invariants.HELD:
            assert finding.holds, (key, finding.violations)
    assert invariants.report_json(list(found.values()))["holds"] is True


def test_tranches_that_describe_a_campus_twice_are_reported_with_the_megawatts(session):
    """Hyperion: 20,124 MW of tranches against a 5,000 MW campus."""
    campus = _campus(session, "Twice", mw_planned=1200.0)
    _tranche(session, campus, "Campus", 1200.0)
    _tranche(session, campus, "Building 1", 600.0)
    (violation,) = _findings(session)["tranches_once"].violations
    assert violation.project_id == campus.id and violation.mw == 600.0
    assert "counted twice" in violation.detail


def test_a_tranche_bigger_than_its_campus_is_reported_as_out_of_scale(session):
    campus = _campus(session, "Kilowatts", mw_planned=18.0)
    _tranche(session, campus, "2022 Expansion", 4500.0)
    (violation,) = _findings(session)["tranche_le_campus"].violations
    assert violation.mw == 4500.0 and "2022 Expansion" in violation.detail


def test_a_phase_behind_its_running_tranches_is_reported(session):
    campus = _campus(session, "Behind", phase="announced", mw_planned=100.0)
    _tranche(session, campus, "Phase 1", 50.0, status="serving")
    (violation,) = _findings(session)["phase_not_behind_tranches"].violations
    assert "Phase 1" in violation.detail


def test_an_online_date_before_the_announcement_is_reported(session):
    _campus(
        session,
        "Early",
        first_announced=dt.date(2025, 1, 21),
        expected_online=dt.date(2024, 12, 31),
    )
    (violation,) = _findings(session)["announced_before_online"].violations
    assert "2024-12-31" in violation.detail


def test_a_company_is_the_sum_of_its_campuses_even_when_tranches_overlap(session):
    """Before the fix in `capex.block_shares` this broke on nine campuses."""
    campus = _campus(session, "Overlap", mw_planned=400.0, customer="CoreWeave")
    _tranche(session, campus, "Campus", 400.0, customer="CoreWeave")
    _tranche(session, campus, "Building 1", 400.0, customer="CoreWeave")
    assert _findings(session)["company_sums_campuses"].holds
