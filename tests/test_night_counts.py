"""The nightly loop's three numbers, pinned to their definitions.

`scripts/overnight.sh` decides whether a round made progress by comparing these
three numbers, so `quality.night_counts` must give exactly what the loop's inline
computation gave: a faster count that drifts from the definition would stop the
night early, or never. The reference below is that computation, written out
independently of the function under test.
"""

from __future__ import annotations

import datetime as dt

from tracker import clean, logic
from tracker.audit import settled_codes
from tracker.capex import duplicate_groups, suspected_duplicates
from tracker.models import Project
from tracker.quality import NightCounts, night_counts


def _reference(session) -> tuple[int, int, int]:
    """The definitions, as the loop's heredoc computed them."""
    settled: dict[int, set[str]] = {}
    todo = 0
    for f in logic.review(session).findings:
        if f.project_id not in settled:
            row = session.get(Project, f.project_id)
            settled[f.project_id] = settled_codes(row) if row else set()
        if f.code not in settled[f.project_id]:
            todo += 1
    pairs = [p for p in suspected_duplicates(session) if set(p.kinds) - {"name"}]
    below = sum(1 for c in clean.scan(session).cards if c.tier < 2)
    return todo, len(duplicate_groups(pairs)), below


def _project(session, **kwargs) -> Project:
    defaults = {
        "name": "Campus",
        "company": "Someone",
        "state": "TX",
        "city": "Abilene",
        "dedup_key": f"n|{kwargs.get('name', 'x')}|{kwargs.get('company', 'y')}",
        "phase": "construction",
        "confidence": 2,
    }
    defaults.update(kwargs)
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


def _fixture(session) -> None:
    # One campus stored once per party: a duplicate group on more than the name.
    for company, name in [
        ("Crusoe", "Stargate Abilene"),
        ("OpenAI/Oracle", "Stargate"),
        ("OpenAI", "Stargate"),
        ("Oracle", "Stargate - Abilene"),
    ]:
        _project(session, name=name, company=company, mw_planned=1200)
    # Rows the free logic checks object to: more built than planned, and online
    # before it was announced.
    _project(
        session,
        name="Overbuilt",
        company="Builder",
        city="Midland",
        mw_planned=100,
        mw_built=300,
        phase="operational",
        first_announced=dt.date(2025, 1, 1),
        expected_online=dt.date(2023, 1, 1),
    )
    _project(session, name="Quiet", company="Nobody", city="Odessa")


def test_night_counts_matches_the_loop_definition(session):
    _fixture(session)

    got = night_counts(session)

    assert isinstance(got, NightCounts)
    assert tuple(got) == _reference(session)
    # Every slot is exercised, so a definition that drops one cannot pass by zeros.
    assert got.findings > 0 and got.groups > 0 and got.below_t2 > 0
    assert got.groups == 1, "four rows for one campus are one group, not six pairs"
    assert got.below_t2 == 6, "no row here has a citation, so none reaches T2"


def test_an_answered_finding_stops_counting(session):
    """Settled on its row means out of the count — the number a round can lower."""
    _fixture(session)
    before = night_counts(session)

    finding = next(f for f in logic.review(session).findings if f.code not in logic.FREE_CODES)
    row = session.get(Project, finding.project_id)
    logic.record_decision(row, finding.code, "dismissed: the figure is right", by="operator")
    session.flush()
    same_code = sum(
        1
        for f in logic.review(session).findings
        if f.project_id == row.id and f.code == finding.code
    )

    after = night_counts(session)
    assert tuple(after) == _reference(session)
    assert after.findings == before.findings - same_code
    assert (after.groups, after.below_t2) == (before.groups, before.below_t2)
