"""Rows that are not data centers: taken out by `tracker exclude`, and kept out.

The cases are the five production rows that started it (2026-10-20): SpaceX's
Terafab, Ford's battery plant, Corning's cable plant, Siemens' switchgear plant and
Crusoe's factory for modular data centers — and the three rows a name rule would
most easily get wrong, which are data centers whatever they are called.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from tracker import exclude as exclude_mod
from tracker.cli import app
from tracker.db import session_scope
from tracker.dedup import dedup_key
from tracker.ingest.records import EventRecord, IngestRecord, IngestReport, SourceRecord
from tracker.models import Event, ExcludedKey, Project, ProjectAlias, Source
from tracker.upsert import upsert_record

T0 = dt.datetime(2026, 8, 29, 12, 0, 0)
ROUNDUP = "https://www.industrialinfo.com/news/article/products-for-data-centers--357873"


def _row(session, name: str, company: str, *, city=None, county=None, state="TX") -> Project:
    project = Project(
        name=name,
        company=company,
        city=city,
        county=county,
        state=state,
        dedup_key=dedup_key(company, city, county, state),
        phase="announced",
        confidence=1,
    )
    session.add(project)
    session.flush()
    return project


def _record(name: str, company: str, *, city=None, county=None, state="TX", url=ROUNDUP):
    claims = {"name": name, "company": company, "state": state, "phase": "announced"}
    if city:
        claims["city"] = city
    if county:
        claims["county"] = county
    return IngestRecord(
        project={
            "name": name,
            "company": company,
            "city": city,
            "county": county,
            "state": state,
            "country": "US",
        },
        sources=[
            SourceRecord(
                url=url, source_type="trade_press", fetched_at=T0, excerpt="A.", claims=claims
            )
        ],
        events=[
            EventRecord(
                event_date=dt.date(2026, 8, 6), event_type="announced", description="Announced."
            )
        ],
    )


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# --- the name ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "word"),
    [
        ("Terafab", "terafab"),
        ("Ford BESS Manufacturing", "manufacturing"),
        ("Corning Newton Fiber-Optic Cable Plant", "plant"),
        ("Siemens Spartanburg Plant Renovation", "plant"),
        ("Crusoe Spark Factory", "factory"),
        ("Samsung Taylor Fab", "fab"),
        ("TSMC Arizona Foundry", "foundry"),
        ("Tesla Gigafactory Texas", "gigafactory"),
        # 2026-10-21: Entergy's supply for AWS in Mississippi, stored as campuses.
        ("Delta Blues Advanced Power Station", "power station"),
        ("Vicksburg Advanced Power Station", "power station"),
        ("Delta Solar", "solar"),
        ("Palisades Reactor Restart", "reactor"),
    ],
)
def test_a_name_that_says_fab_factory_or_plant_is_not_a_data_center(name, word):
    assert exclude_mod.not_a_data_center(name) == word


@pytest.mark.parametrize(
    "name",
    [
        # The three production rows a name rule would most easily refuse.
        "Lambda Kansas City AI Factory",
        "Nebius AI Factory Birmingham",
        "Imperial Valley Computer Manufacturing",
        # A data campus inside a generating plant is a data campus.
        "TeraWulf Chesapeake Data Campus (Morgantown Generating Station)",
        "Homer City Plant Redevelopment Data Center",
        # Words that only contain one.
        "DuPont Fabros ACC9",
        "Microsoft Mount Pleasant",
        "Vantage Prefab Data Halls",
        "Stargate Abilene",
        "Battery Brooke Parkway Data Center Campus",
        "SolarEdge Hub",
        None,
        "",
    ],
)
def test_a_data_center_is_never_refused_on_its_name(name):
    assert exclude_mod.not_a_data_center(name) is None


# --- taking a row out ----------------------------------------------------------


def test_excluding_a_row_deletes_it_with_everything_hanging_off_it(session):
    plant = _row(
        session,
        "Corning Newton Fiber-Optic Cable Plant",
        "Corning",
        city="Newton",
        county="Catawba County",
        state="NC",
    )
    upsert_record(session, _record(plant.name, "Corning", city="Newton", state="NC"))
    assert _count(session, Source) == 1 and _count(session, Event) == 1

    result = exclude_mod.exclude(session, [plant.id])

    assert result.removed == [(plant.id, "Corning — Corning Newton Fiber-Optic Cable Plant")]
    assert session.get(Project, plant.id) is None
    assert _count(session, Source) == 0 and _count(session, Event) == 0


def test_every_key_the_row_could_come_back_under_is_recorded(session):
    plant = _row(
        session,
        "Siemens Spartanburg Plant Renovation",
        "Siemens",
        city="Spartanburg",
        county="Spartanburg County",
        state="SC",
    )
    session.add(
        ProjectAlias(from_dedup_key="siemens energy|city:spartanburg|SC", to_project_id=plant.id)
    )
    session.flush()

    result = exclude_mod.exclude(session, [plant.id])

    stored = set(session.scalars(select(ExcludedKey.dedup_key)))
    assert stored == set(result.keys)
    assert plant.dedup_key in stored
    # An article naming only the county computes this one.
    assert dedup_key("Siemens", None, "Spartanburg County", "SC") in stored
    assert "siemens energy|city:spartanburg|SC" in stored
    row = session.scalar(select(ExcludedKey).where(ExcludedKey.dedup_key == plant.dedup_key))
    assert (row.project_id, row.company, row.reason) == (plant.id, "Siemens", "not a data center")


def test_a_key_another_row_reaches_is_left_to_that_row(session):
    fab = _row(session, "Terafab", "SpaceX", county="Grimes County")
    other = _row(session, "SpaceX Starbase Compute", "SpaceX", city="Starbase")
    session.add(
        ProjectAlias(
            from_dedup_key=fab.dedup_key.replace("grimes", "grimes-old"), to_project_id=other.id
        )
    )
    session.flush()
    held = fab.dedup_key.replace("grimes", "grimes-old")

    result = exclude_mod.exclude(session, [fab.id])

    assert (held, other.id) not in result.left  # the alias pointed at the other row only
    assert session.scalar(select(ExcludedKey).where(ExcludedKey.dedup_key == held)) is None
    assert session.get(Project, other.id) is not None


def test_a_missing_id_or_an_empty_reason_changes_nothing(session):
    fab = _row(session, "Terafab", "SpaceX", county="Grimes County")
    with pytest.raises(exclude_mod.ExcludeError, match="#999999 does not exist"):
        exclude_mod.exclude(session, [fab.id, 999999])
    with pytest.raises(exclude_mod.ExcludeError, match="reason"):
        exclude_mod.exclude(session, [fab.id], reason="  ")
    assert session.get(Project, fab.id) is not None
    assert _count(session, ExcludedKey) == 0


# --- staying out ---------------------------------------------------------------


def test_a_reread_of_the_same_article_does_not_put_the_row_back(session):
    url = "https://www.datacenterdynamics.com/terafab/"
    fab = _row(session, "Terafab", "SpaceX", county="Grimes County")
    assert (
        upsert_record(
            session, _record("Terafab", "SpaceX", county="Grimes County", url=url)
        ).project_id
        == fab.id
    )
    exclude_mod.exclude(session, [fab.id])

    # Under a name the name rule would let through, so it is the key that refuses it.
    again = upsert_record(
        session, _record("SpaceX Grimes County site", "SpaceX", county="Grimes County", url=url)
    )

    assert again.action == "excluded" and again.project_id == 0
    assert _count(session, Project) == 0
    assert _count(session, Source) == 0  # nothing written for a row that does not exist


def test_a_new_row_named_as_a_factory_is_never_created(session):
    result = upsert_record(
        session, _record("Crusoe Spark Factory", "Crusoe", city="Brighton", state="CO")
    )
    assert result.action == "excluded"
    assert _count(session, Project) == 0


def test_a_row_that_exists_is_never_refused_by_its_name(session):
    """Only an insert is refused: a row somebody kept keeps receiving its news."""
    kept = _row(session, "Crusoe Spark Factory", "Crusoe", city="Brighton", state="CO")
    result = upsert_record(
        session, _record("Crusoe Spark Factory", "Crusoe", city="Brighton", state="CO")
    )
    assert result.action in {"update", "unchanged"} and result.project_id == kept.id


def test_a_lifted_exclusion_lets_a_later_reading_create_the_row(session):
    first = upsert_record(
        session, _record("SpaceX Grimes County Campus", "SpaceX", county="Grimes County")
    )
    exclude_mod.exclude(session, [first.project_id], reason="operator error, test")

    assert exclude_mod.lift(session, first.project_id)
    assert exclude_mod.lift(session, first.project_id) == []

    again = upsert_record(
        session, _record("SpaceX Grimes County Campus", "SpaceX", county="Grimes County")
    )
    assert again.action == "insert"


def test_the_crawl_counts_what_it_did_not_create():
    report = IngestReport(not_a_data_center=4)
    assert ("dropped: not a data center", 4) in report.as_rows()
    assert all(label != "dropped: not a data center" for label, _ in IngestReport().as_rows())


# --- the command ---------------------------------------------------------------


def _seed(engine) -> dict[str, int]:
    with session_scope(engine) as session:
        fab = _row(session, "Terafab", "SpaceX", county="Grimes County")
        dc = _row(session, "Stargate Abilene", "Crusoe", city="Abilene")
        return {"fab": fab.id, "dc": dc.id}


def _invoke(db_path, *args):
    return CliRunner().invoke(app, ["--db", str(db_path), *args])


def test_the_command_dry_run_writes_nothing(engine, db_path):
    ids = _seed(engine)
    done = _invoke(db_path, "exclude", str(ids["fab"]), "--dry-run")
    assert done.exit_code == 0, done.output
    assert "remove" in done.output and "Terafab" in done.output and "dry run" in done.output
    with session_scope(engine, commit=False) as session:
        assert session.get(Project, ids["fab"]) is not None
        assert _count(session, ExcludedKey) == 0


def test_the_command_takes_the_row_out_and_lists_it(engine, db_path):
    ids = _seed(engine)
    done = _invoke(db_path, "exclude", str(ids["fab"]), "--reason", "a chip fab")
    assert done.exit_code == 0, done.output

    # Reading only: a second *writing* command in this process would trip the
    # single-writer lock, which `atexit` releases only when the process ends.
    listed = _invoke(db_path, "exclude", "--list")
    assert listed.exit_code == 0 and "spacex|county:grimes|TX" in listed.output
    assert "a chip fab" in listed.output
    with session_scope(engine, commit=False) as session:
        assert session.get(Project, ids["fab"]) is None
        assert session.get(Project, ids["dc"]) is not None


def test_the_command_lifts_what_was_excluded(engine, db_path):
    with session_scope(engine) as session:
        session.add(
            ExcludedKey(
                dedup_key="spacex|county:grimes|TX",
                project_id=1352,
                company="SpaceX",
                name="Terafab",
                reason="not a data center",
            )
        )
    lifted = _invoke(db_path, "exclude", "--lift", "1352")
    assert lifted.exit_code == 0, lifted.output
    assert "lifted" in lifted.output and "spacex|county:grimes|TX" in lifted.output
    with session_scope(engine, commit=False) as session:
        assert _count(session, ExcludedKey) == 0


def test_the_command_wants_exactly_one_thing_to_do(engine, db_path):
    _seed(engine)
    assert _invoke(db_path, "exclude").exit_code == 2
    assert _invoke(db_path, "exclude", "1", "--list").exit_code == 2
    unknown = _invoke(db_path, "exclude", "--lift", "424242")
    assert unknown.exit_code == 1 and "nothing is excluded from #424242" in unknown.output
