"""`tracker backfill derive`: the free repair pass, and its one load-bearing property.

**Running it twice must change nothing.** Every other test here is detail; that one
is the reason the command can be trusted at all. A value on a project is a function
of its citations, so a second pass over unchanged citations must produce unchanged
values. If it does not, the derivation is not a pure function of what is stored and
every number in the database is whichever pass ran last.

The same obligation is already carried by `test_confidence_cache_is_consistent`,
`test_blocks_cache_is_consistent` and `test_h200_cache_is_consistent`. This extends
it from the three caches to the whole row.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from tracker import derive
from tracker.ingest.records import IngestRecord, RiskRecord, SourceRecord
from tracker.models import Project
from tracker.upsert import upsert_record

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)


def source(url="https://example.com/a", source_type="trade_press", **claims):
    return SourceRecord(
        url=url,
        source_type=source_type,
        excerpt="excerpt",
        claims=claims,
        quotes={k: f"the campus is {v}" for k, v in claims.items()},
        fetched_at=T0,
    )


def record(**project):
    base = {"company": "Meta", "name": "Hyperion", "city": "Richland", "state": "LA"}
    return IngestRecord(project={**base, **project}, sources=[source(**{})])


def ingest(session, rec):
    result = upsert_record(session, rec)
    session.commit()
    return session.get(Project, result.project_id)


def test_a_second_pass_changes_nothing(session):
    """The property the command rests on."""
    ingest(
        session,
        IngestRecord(
            project={"company": "Meta", "name": "Hyperion", "city": "Richland", "state": "LA"},
            sources=[source(mw_planned=5000, phase="construction")],
        ),
    )

    first = derive.run(session)
    session.commit()
    second = derive.run(session)
    session.commit()

    assert second.changed == 0, [c.render() for c in second.changes]
    assert second.blocks_touched == 0
    assert first.projects == second.projects == 1


def test_it_moves_a_row_that_drifted_from_its_sources(session):
    """A hand-edited value is put back to what the citations imply.

    This is the whole point: nothing else reaches a stored project's own fields.
    `tracker init` recomputes confidence, accelerators and blocks, and stops there.
    """
    project = ingest(
        session,
        IngestRecord(
            project={"company": "Meta", "name": "Hyperion", "city": "Richland", "state": "LA"},
            sources=[source(mw_planned=5000)],
        ),
    )
    project.mw_planned = 14_462.0
    session.commit()

    report = derive.run(session)
    session.commit()

    assert project.mw_planned == 5000
    assert report.changed == 1
    assert report.by_field == {"mw_planned": 1}
    assert "mw_planned" in report.changes[0].render()


def test_a_project_with_no_citations_is_left_alone(session):
    """Never emptied. The row may be an operator's own work.

    Reading "no claims" as "no facts" would delete it on the way past, and a repair
    pass that can destroy data is not free however cheap it is to run.
    """
    project = Project(
        name="Entered by hand",
        company="Someone",
        state="TX",
        city="Abilene",
        dedup_key="someone|abilene|-|tx",
        phase="announced",
        confidence=0,
        mw_planned=250.0,
    )
    session.add(project)
    session.commit()

    report = derive.run(session)
    session.commit()

    assert report.unsourced == 1
    assert report.changed == 0
    assert project.mw_planned == 250.0


def test_one_project_at_a_time(session):
    ingest(
        session,
        IngestRecord(
            project={"company": "Meta", "name": "Hyperion", "city": "Richland", "state": "LA"},
            sources=[source(url="https://example.com/a", mw_planned=5000)],
        ),
    )
    other = ingest(
        session,
        IngestRecord(
            project={"company": "Vantage", "name": "Frontier", "city": "Shackelford", "state": "TX"},
            sources=[source(url="https://example.com/b", mw_planned=1400)],
        ),
    )
    other.mw_planned = 99.0
    session.commit()

    report = derive.run(session, project_id=1)
    session.commit()

    assert report.projects == 1
    assert other.mw_planned == 99.0


def test_it_reports_the_blocker_and_the_fields_the_sources_still_dispute(session):
    """A repair that cannot say what it changed is indistinguishable from one that did nothing."""
    project = ingest(
        session,
        IngestRecord(
            project={"company": "Meta", "name": "Hyperion", "city": "Richland", "state": "LA"},
            sources=[
                source(url="https://a.example/x", source_type="trade_press", mw_planned=5000),
                source(url="https://b.example/y", source_type="general_media", mw_planned=2000),
            ],
            risks=[
                RiskRecord(
                    category="grid_capacity",
                    severity="blocking",
                    summary="no interconnection agreement",
                    quote="Entergy has not signed an interconnection agreement.",
                    source_url="https://a.example/x",
                )
            ],
        ),
    )
    project.blocker = None
    session.commit()

    report = derive.run(session)
    session.commit()

    assert project.blocker == "no interconnection agreement"
    assert "blocker" in report.by_field
    assert report.conflicts.get(project.id) == ["mw_planned"]


def test_clearing_a_blocker_settles_the_confidence_in_the_same_pass(session):
    """The ordering bug `backfill derive` found on its first live run.

    `blocker` is one of the twelve tracked fields, so it is part of the `populated`
    count confidence reads — and it used to be derived *after* the score. A pass
    that cleared a resolved obstacle therefore scored the row against the blocker it
    arrived with, and the *next* pass scored it against the one it left with. On the
    live database that was #79 moving from 2 to 1 on a second run, which is exactly
    the property this command cannot be allowed to break.
    """
    from tracker.models import Risk

    project = ingest(
        session,
        IngestRecord(
            project={"company": "Meta", "name": "Hyperion", "city": "Richland", "state": "LA"},
            sources=[source(mw_planned=5000)],
            risks=[
                RiskRecord(
                    category="permitting",
                    severity="blocking",
                    summary="Rezoning refused.",
                    quote="the campus is 5000",
                )
            ],
        ),
    )
    assert project.blocker == "Rezoning refused."

    # An operator resolves it. Nothing has re-derived the row yet.
    session.scalars(select(Risk)).one().status = "resolved"
    session.commit()

    first = derive.run(session)
    session.commit()
    second = derive.run(session)
    session.commit()

    assert project.blocker is None
    assert first.changed == 1
    assert second.changed == 0, [c.render() for c in second.changes]
