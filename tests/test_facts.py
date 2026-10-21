"""Every statement every source makes, in one table (migration 0045).

Step 4.5 of the client plan (2026-10-21, A4). A cache rebuilt from the sources, so
these pin what a cache owes: it says what the sources say, classifies each statement
the way the rest of the tracker does, marks the one the row holds, and a second
rebuild changes nothing.
"""

from __future__ import annotations

import datetime as dt
import json

from tracker import facts
from tracker.models import Event, Fact, Project, Risk, Source


def _campus(session) -> Project:
    project = Project(
        name="Abilene",
        company="Crusoe",
        state="TX",
        city="Abilene",
        dedup_key="crusoe|city:abilene|TX",
        phase="construction",
        confidence=2,
        mw_planned=1200.0,
    )
    session.add(project)
    session.flush()
    return project


def _source(session, project, url, **kwargs) -> Source:
    source = Source(
        project_id=project.id,
        url=url,
        source_type=kwargs.pop("source_type", "trade_press"),
        fetched_at=dt.datetime(2026, 1, 2),
        published_at=kwargs.pop("published_at", dt.datetime(2026, 1, 1)),
        **kwargs,
    )
    session.add(source)
    session.flush()
    return source


def _rows(session, project):
    session.refresh(project)
    facts.rebuild(session, project)
    return session.query(Fact).filter(Fact.project_id == project.id).all()


def test_each_campus_statement_carries_its_tier_quote_and_whether_it_is_held(session):
    project = _campus(session)
    _source(
        session,
        project,
        "https://example.test/a",
        claims=json.dumps({"mw_planned": 1200.0, "company": "Crusoe"}),
        fields="company,mw_planned",
        quotes=json.dumps({"mw_planned": "The campus will total 1.2 GW."}),
        claim_meta=json.dumps({"mw_planned": {"scope": "this_site", "as_of": "2025-12-01"}}),
    )
    _source(
        session,
        project,
        "https://example.test/b",
        claims=json.dumps({"mw_planned": 1000.0}),
        fields="",
        unconfirmed_fields="mw_planned",
        unconfirmed_reasons=json.dumps({"mw_planned": "superseded"}),
    )
    rows = {(r.field, r.value): r for r in _rows(session, project) if r.entity == "campus"}
    quoted = rows[("mw_planned", "1200")]
    assert (quoted.tier, quoted.held, quoted.unit, quoted.number) == ("confirmed", 1, "MW", 1200.0)
    assert quoted.quote == "The campus will total 1.2 GW."
    assert quoted.as_of == dt.date(2025, 12, 1)
    assert json.loads(quoted.axes)["scope"] == "this_site"
    ruled = rows[("mw_planned", "1000")]
    assert (ruled.tier, ruled.reason, ruled.held) == ("ruled_out", "superseded", 0)


def test_tranches_parties_milestones_and_obstacles_are_statements_too(session):
    project = _campus(session)
    source = _source(
        session,
        project,
        "https://example.test/c",
        claims=json.dumps({}),
        blocks=json.dumps(
            [
                {
                    "label": "Building 1",
                    "mw": 200.0,
                    "status": "serving",
                    "quotes": {"mw": "Building 1, 200 MW, is serving."},
                }
            ]
        ),
        parties=json.dumps(
            [
                {"name": "Oracle", "role": "customer", "quote": "Oracle leases it."},
                {"name": "Lancium", "role": "owner", "unconfirmed": "no_quote"},
            ]
        ),
    )
    session.add(
        Event(
            project_id=project.id,
            event_type="energized",
            event_date=dt.date(2025, 9, 30),
            description="Energised",
            quote="Energised.",
            source_id=source.id,
        )
    )
    session.add(
        Risk(
            project_id=project.id,
            category="grid_capacity",
            severity="watch",
            summary="Grid",
            first_seen=dt.date(2025, 1, 1),
            source_id=source.id,
            quote="ERCOT flagged it.",
        )
    )
    session.flush()
    rows = _rows(session, project)
    by = {(r.entity, r.entity_key, r.field): r for r in rows}
    assert by[("tranche", "building-1", "mw")].tier == "confirmed"
    assert by[("party", "oracle", "customer")].quote == "Oracle leases it."
    assert by[("party", "lancium", "owner")].tier == "unconfirmed"
    assert by[("milestone", "energized", "event_date")].value == "2025-09-30"
    assert by[("obstacle", "grid_capacity", "severity")].value == "watch"


def test_a_directory_and_a_derived_source_are_told_apart(session):
    project = _campus(session)
    _source(
        session,
        project,
        "https://www.datacentermap.com/usa/texas/abilene/",
        claims=json.dumps({"mw_planned": 1200.0}),
        fields="mw_planned",
    )
    _source(
        session,
        project,
        "census:place/4801000",
        extractor="derived:census-place-2020",
        claims=json.dumps({"county": "Taylor County"}),
        fields="county",
    )
    tiers = {r.field: r.tier for r in _rows(session, project) if r.entity == "campus"}
    assert tiers == {"mw_planned": "directory", "county": "derived"}


def test_a_second_rebuild_changes_nothing(session):
    project = _campus(session)
    _source(
        session,
        project,
        "https://example.test/a",
        claims=json.dumps({"mw_planned": 1200.0}),
        fields="mw_planned",
    )
    first = [(r.entity, r.field, r.value, r.tier) for r in _rows(session, project)]
    second = [(r.entity, r.field, r.value, r.tier) for r in _rows(session, project)]
    assert first == second and len(first) == 1


def test_the_download_spells_out_each_statement(session):
    project = _campus(session)
    _source(
        session,
        project,
        "https://example.test/a",
        claims=json.dumps({"mw_planned": 1200.0}),
        fields="mw_planned",
    )
    _rows(session, project)
    text = facts.render_csv(facts.rows_for(session, [project.id]))
    header, line = text.splitlines()
    assert header.split(",") == list(facts.CSV_COLUMNS)
    assert ",Crusoe,Abilene,campus,,mw_planned,1200,1200.0,MW,confirmed,,True," in line
