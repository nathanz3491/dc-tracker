"""Companies as entities: one row per company, its spellings, kind and parent.

Step 4.3 of the client plan (2026-10-21). The key stays the identity — these pin that
the registry agrees with it, names companies as the console always has, follows a
person's alias wherever keys are stored, and takes one back cleanly.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from tracker import capex, companies, dedup
from tracker.company import display_names
from tracker.models import Company, CompanyAlias, Project, ProjectParty, Watch


def _campus(session, name, company, *, customer=None) -> Project:
    project = Project(
        name=name,
        company=company,
        customer=customer,
        state="TX",
        city="Abilene",
        dedup_key=f"{dedup.company_key(company)}|city:{name.lower()}|TX",
        phase="construction",
        confidence=2,
    )
    session.add(project)
    session.flush()
    return project


def _party(session, project, name, role="customer") -> ProjectParty:
    party = ProjectParty(
        project_id=project.id, name=name, party_key=dedup.company_key(name), role=role
    )
    session.add(party)
    session.flush()
    return party


def test_the_registry_holds_every_company_under_the_name_the_console_prints(session):
    _campus(session, "A", "Meta Platforms, Inc.")
    _campus(session, "B", "Facebook")
    _campus(session, "C", "Northwind Data Centers", customer="Contoso AI")
    _campus(session, "D", "Northwind Data Centers LLC")
    report = companies.sync(session)
    assert report.created > 0
    names = companies.names(session)
    assert names["meta"] == "Meta"  # the roster's name
    assert names["northwind"] == "Northwind Data Centers"  # the spelling most campuses use
    assert names["contoso ai"] == "Contoso AI"
    assert display_names(session).of_key("northwind") == "Northwind Data Centers"
    rows = session.scalars(select(Project).order_by(Project.id)).all()
    meta = session.scalar(select(Company).where(Company.key == "meta"))
    assert rows[0].company_id == rows[1].company_id == meta.id
    assert meta.kind == "hyperscaler"
    assert "facebook" in {a.spelling for a in meta.aliases}


def test_a_second_sync_changes_nothing(session):
    _campus(session, "A", "Meta")
    companies.sync(session)
    again = companies.sync(session)
    assert (again.created, again.renamed, again.aliases_added, again.attached) == (0, [], 0, 0)


def test_a_company_only_a_party_names_is_named_by_the_party(session):
    project = _campus(session, "A", "Crusoe")
    party = _party(session, project, "Zephyr Compute")
    companies.sync(session)
    assert companies.names(session)["zephyr compute"] == "Zephyr Compute"
    assert party.company_id is not None


def test_a_persons_alias_moves_every_key_it_touches(session):
    project = _campus(session, "A", "Northwind Data Centers")
    _campus(session, "B", "Fabrikam Compute")
    from tracker import accounts

    reader = accounts.create(session, "reader@example.com", "correct horse battery")
    session.add(
        Watch(account_id=reader.id, entry="Northwind", company_key="northwind", project_key="")
    )
    companies.sync(session)
    fabrikam = companies.find(session, "Fabrikam Compute")
    cached = capex.company_key("Northwind Data Centers")  # warm the cache the alias must drop

    report = companies.add_alias(session, "Northwind Data Centers", fabrikam, by="nathan")

    assert cached == "northwind"
    assert dedup.company_key("Northwind Data Centers") == "fabrikam compute"
    assert capex.company_key("Northwind Data Centers") == "fabrikam compute"
    assert (report.old_key, report.new_key) == ("northwind", "fabrikam compute")
    assert project.company_id == fabrikam.id
    assert session.scalar(select(Watch)).company_key == "fabrikam compute"
    alias = session.scalar(select(CompanyAlias).where(CompanyAlias.decided_by == "nathan"))
    assert alias.spelling == "northwind data centers"


def test_taking_an_alias_back_restores_the_company(session):
    project = _campus(session, "A", "Northwind Data Centers")
    _campus(session, "B", "Fabrikam Compute")
    companies.sync(session)
    fabrikam = companies.find(session, "Fabrikam Compute")
    companies.add_alias(session, "Northwind Data Centers", fabrikam, by="nathan")
    companies.remove_alias(session, "Northwind Data Centers")
    assert dedup.company_key("Northwind Data Centers") == "northwind"
    assert project.company_id == companies.find(session, "northwind").id


def test_the_codes_own_spellings_cannot_be_taken_back_here(session):
    _campus(session, "A", "Meta")
    companies.sync(session)
    with pytest.raises(companies.CompanyError, match="own list"):
        companies.remove_alias(session, "Facebook")


def test_an_alias_needs_a_person_and_a_real_change(session):
    _campus(session, "A", "Meta")
    companies.sync(session)
    meta = companies.find(session, "Meta")
    with pytest.raises(companies.CompanyError, match="already reads as"):
        companies.add_alias(session, "Facebook", meta, by="nathan")
    with pytest.raises(companies.CompanyError, match="person's name"):
        companies.add_alias(session, "Zuck Co", meta, by="")


def test_a_persons_aliases_are_read_from_the_database_opened(engine):
    from tracker.db import session_scope

    with session_scope(engine) as session:
        _campus(session, "A", "Northwind Data Centers")
        _campus(session, "B", "Fabrikam Compute")
        companies.sync(session)
        companies.add_alias(
            session, "Northwind Data Centers", companies.find(session, "Fabrikam Compute"), by="n"
        )
    dedup.use_person_aliases({})
    assert dedup.company_key("Northwind Data Centers") == "northwind"
    with engine.connect() as conn:
        assert companies.load_aliases(conn.connection.driver_connection) is True
    assert dedup.company_key("Northwind Data Centers") == "fabrikam compute"


def test_a_database_without_the_registry_has_no_persons_aliases():
    import sqlite3

    dedup.use_person_aliases({"northwind data centers": "fabrikam compute"})
    companies.load_aliases(sqlite3.connect(":memory:"))
    assert dedup.company_key("Northwind Data Centers") == "northwind"


def test_a_name_a_person_chose_survives_the_nightly_sync(session):
    _campus(session, "A", "Northwind Data Centers")
    companies.sync(session)
    row = companies.find(session, "Northwind Data Centers")
    companies.rename(session, row, "Northwind (NWD)", by="nathan")
    companies.sync(session)
    assert row.name == "Northwind (NWD)" and row.name_by == "nathan"
    assert display_names(session).of_key("northwind") == "Northwind (NWD)"


def test_a_parent_is_recorded_and_a_loop_refused(session):
    _campus(session, "A", "Northwind Data Centers")
    _campus(session, "B", "Fabrikam Compute")
    companies.sync(session)
    child = companies.find(session, "Northwind Data Centers")
    parent = companies.find(session, "Fabrikam Compute")
    companies.set_parent(session, child, parent, by="nathan")
    family = companies.family(session, "fabrikam compute")
    assert family["subsidiaries"] == [{"name": "Northwind Data Centers", "slug": "northwind"}]
    assert companies.family(session, "northwind")["parent"]["slug"] == "fabrikam-compute"
    with pytest.raises(companies.CompanyError, match="already under"):
        companies.set_parent(session, parent, child, by="nathan")


def test_a_new_campus_is_filed_under_its_company_as_it_is_written(session):
    from tracker.ingest.records import IngestRecord, SourceRecord
    from tracker.upsert import upsert_record

    claims = {
        "name": "Waco Data Center",
        "company": "Northwind Data Centers LLC",
        "city": "Waco",
        "state": "TX",
    }
    record = IngestRecord(
        project={**claims, "country": "US"},
        sources=[
            SourceRecord(
                url="https://example.test/n",
                source_type="trade_press",
                fetched_at=dt.datetime(2026, 1, 1),
                claims=claims,
            )
        ],
    )
    project = session.get(Project, upsert_record(session, record).project_id)
    company = session.get(Company, project.company_id)
    assert (company.key, company.name) == ("northwind", "Northwind Data Centers LLC")
