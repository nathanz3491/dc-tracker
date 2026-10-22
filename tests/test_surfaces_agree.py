"""Every page says the same thing about a campus (client review, 2026-10-21).

The client's first point was that one campus showed different numbers in different
places — Englewood "18 MW planned, 6,750 MW running", Abilene 240 MW running on its
cards and "1,600 MW delivering" above its tranches, one company under several names
with totals that did not add up. Each of those was fixed where it arose. This file
holds them together: a small fleet holding every case, ingested the way the crawler
writes it, and read back through every surface a reader sees — the projects table,
the project page, the company page and Capex — which must agree.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker import capex, company
from tracker.dedup import company_slug
from tracker.export import _accounting_json
from tracker.ingest.records import BlockRecord, IngestRecord, SourceRecord
from tracker.models import Project, ProjectParty
from tracker.upsert import upsert_record
from tracker.webui import dataset

T0 = dt.datetime(2026, 5, 1, 12, 0, 0)


def _ingest(session, base, claims, *, url, quotes=None, unconfirmed=(), blocks=()):
    record = IngestRecord(
        project={**base, "country": "US"},
        sources=[
            SourceRecord(
                url=url,
                source_type="trade_press",
                fetched_at=T0,
                excerpt="A.",
                claims={**base, **claims},
                quotes=quotes or {},
                unconfirmed=frozenset(unconfirmed),
                blocks=list(blocks),
            )
        ],
    )
    return upsert_record(session, record).project_id


@pytest.fixture
def fleet(session):
    englewood = {"name": "Englewood", "company": "Flexential", "city": "Englewood", "state": "CO"}
    _ingest(
        session,
        englewood,
        {"mw_planned": 18.0},
        url="https://dcd.test/englewood",
        quotes={"mw_planned": "The Englewood site will total 18 MW."},
    )
    _ingest(
        session,
        englewood,
        {"mw_planned": 11250.0, "mw_built": 6750.0},
        url="https://digest.test/flexential",
        unconfirmed={"mw_planned", "mw_built"},
    )

    abilene = {"name": "Abilene", "company": "Crusoe", "city": "Abilene", "state": "TX"}
    _ingest(
        session,
        abilene,
        {"mw_planned": 1200.0, "mw_built": 240.0, "phase": "construction"},
        url="https://dcf.test/abilene",
        quotes={
            "mw_planned": "The campus will total 1,200 MW.",
            "mw_built": "About 240 MW is running today.",
            "phase": "construction continues on the campus",
        },
        blocks=[
            BlockRecord(
                label="Abilene Campus",
                mw=1200.0,
                status="energized",
                quotes={"mw": "the 1,200 MW Abilene campus is energized"},
            ),
            BlockRecord(
                label="Building 1",
                mw=200.0,
                status="serving",
                customer="OpenAI",
                quotes={"mw": "Building 1, 200 MW, is serving OpenAI"},
            ),
        ],
    )

    prineville = {"name": "Prineville", "company": "Meta", "city": "Prineville", "state": "OR"}
    _ingest(
        session,
        prineville,
        {"mw_planned": 1000.0, "mw_built": 300.0},
        url="https://meta.test/prineville",
        quotes={"mw_planned": "1,000 MW", "mw_built": "300 MW running"},
    )
    _ingest(
        session,
        {**prineville, "company": "Facebook/Meta", "name": "Prineville Data Center"},
        {"mw_planned": 800.0},
        url="https://old.test/prineville",
        quotes={"mw_planned": "800 MW"},
    )

    late = {"name": "Late Campus", "company": "Meta", "city": "Kuna", "state": "ID"}
    _ingest(
        session,
        late,
        {"mw_planned": 400.0, "expected_online": "2025-07-01", "phase": "construction"},
        url="https://meta.test/kuna",
        quotes={
            "mw_planned": "400 MW",
            "expected_online": "online in July 2025",
            "phase": "construction began on the campus",
        },
    )

    shared = {"name": "Frontier", "company": "Vantage", "city": "Shackelford", "state": "TX"}
    pid = _ingest(
        session,
        shared,
        {"mw_planned": 1400.0},
        url="https://vantage.test/frontier",
        quotes={"mw_planned": "1.4 GW"},
    )
    for name in ("OpenAI", "Oracle"):
        session.add(
            ProjectParty(project_id=pid, name=name, party_key=name.lower(), role="customer")
        )
    # Every campus has had its go-live search, so a passed date reads late (`delays`).
    for project in session.query(Project):
        project.golive_checked_at = dt.datetime(2026, 10, 22)
    session.flush()
    session.expire_all()
    return session


def _projects(session):
    return session.query(Project).order_by(Project.id).all()


def test_running_never_exceeds_planned_and_the_headline_never_exceeds_the_campus(fleet):
    for project in _projects(fleet):
        if project.mw_built is not None and project.mw_planned is not None:
            assert project.mw_built <= project.mw_planned, project.name
        got = _accounting_json(project)
        if not got:
            continue
        if got["total"] is not None and got["delivering"] is not None:
            assert got["delivering"] <= got["total"] + 1e-6, project.name
        if project.mw_built is not None:
            assert got["delivering"] == project.mw_built, project.name

    englewood = next(p for p in _projects(fleet) if p.name == "Englewood")
    assert (englewood.mw_planned, englewood.mw_built) == (18.0, None)
    abilene = next(p for p in _projects(fleet) if p.name == "Abilene")
    assert _accounting_json(abilene)["delivering"] == 240.0


def test_the_table_row_and_the_project_page_agree(fleet):
    rows = {row["id"]: row for row in dataset.index_rows(fleet)}
    for project in _projects(fleet):
        page = dataset.project_payload(project)
        row = rows[project.id]
        for field in (
            "mw_planned",
            "mw_built",
            "phase",
            "company_name",
            "customer_name",
            "customer_shared",
            "suspected_delay",
        ):
            assert row[field] == page[field], (project.name, field)


def test_a_company_page_shows_each_campus_as_its_own_page_does(fleet):
    for slug in ("meta", "crusoe", "flexential", "vantage"):
        profile = company.profile(fleet, slug)
        assert profile is not None, slug
        for row in profile["projects"]:
            page = dataset.project_payload(fleet.get(Project, row["id"]))
            for field in ("mw_planned", "mw_built", "phase", "suspected_delay"):
                assert row[field] == page[field], (slug, row["name"], field)
            assert row["company_name"] == page["company_name"]


def test_one_company_is_one_name_and_its_pages_add_up_to_capex(fleet):
    names = {dataset.project_payload(p)["company_name"] for p in _projects(fleet)}
    assert "Facebook/Meta" not in names and "Meta" in names

    positions = capex.rollup(fleet)
    slugs = {company_slug(capex.company_key(p.company)) for p in _projects(fleet)}
    slugs |= {company_slug(p.key) for p in positions if p.key}
    pages = [company.profile(fleet, s) for s in sorted(slugs)]
    total = sum(page["totals"]["mw_planned"] for page in pages if page)
    assert total == pytest.approx(sum(p.mw_planned for p in positions))


def test_a_late_campus_reads_late_everywhere(fleet):
    late = next(p for p in _projects(fleet) if p.name == "Late Campus")
    page = dataset.project_payload(late)
    assert page["suspected_delay"] is not None
    row = next(r for r in dataset.index_rows(fleet) if r["id"] == late.id)
    assert row["suspected_delay"] == page["suspected_delay"]
    meta = company.profile(fleet, "meta")
    assert late.id in meta["suspected_delays"]["project_ids"]


def test_a_project_pages_summary_reads_the_campus_as_its_company_page_does(fleet):
    """The summary's delay risk and still-to-come are the company page's supply row.

    Client review, 2026-10-21: the project page now opens with both, and a campus
    that read high risk on its company's page and low on its own would be the
    disagreement Batch 1 was about, moved to a new place.
    """
    late = next(p for p in _projects(fleet) if p.name == "Late Campus")
    page = dataset.project_payload(late)
    rows = {row["id"]: row for row in company.profile(fleet, "meta")["supply"]["campuses"]}
    assert page["delay_risk"] == rows[late.id]["risk"]
    assert page["delay_risk"]["level"] == "high"
    assert page["still_to_come"]["mw"] == rows[late.id]["mw"] == 400.0
    assert page["still_to_come"]["overdue"] is rows[late.id]["overdue"] is True
    stated = page["suspected_delay"]["stated"]
    assert page["still_to_come"]["when"] == page["expected_online_stated"] == stated


def test_a_campus_naming_several_buyers_lists_every_one_on_its_page(fleet):
    """The summary names them all, as Capex declines to choose between them."""
    frontier = next(p for p in _projects(fleet) if p.name == "Frontier")
    page = dataset.project_payload(frontier)
    assert sorted(b["name"] for b in page["buyers"]) == ["OpenAI", "Oracle"]
    assert all(b["slug"] for b in page["buyers"])


def test_a_campus_naming_several_buyers_names_them_all_wherever_a_buyer_is_printed(fleet):
    """Stargate Abilene, 10-22: header and Buyer field said "Oracle" while the summary
    named three buyers and said Capex credits none of them."""
    frontier = next(p for p in _projects(fleet) if p.name == "Frontier")
    page = dataset.project_payload(frontier)
    row = next(r for r in dataset.index_rows(fleet) if r["id"] == frontier.id)
    assert page["customer_shared"] is row["customer_shared"] is True
    assert page["customer_name"] == row["customer_name"] == "OpenAI, Oracle"
    assert page["customer_slug"] is row["customer_slug"] is None


def test_a_builder_that_leases_out_what_it_builds_still_reads_in_megawatts(fleet):
    """xAI, 10-22: "0 MW" beside Colossus at 2 GW, every campus credited to its
    buyer. The page reads the campuses it builds as well, whole, beside Capex."""
    crusoe = company.profile(fleet, "crusoe")
    assert crusoe["totals"]["mw_planned"] < crusoe["builds"]["mw_planned"]
    assert crusoe["builds"]["mw_planned"] == 1200.0
    assert crusoe["builds"]["campuses"] == 1
    # The Capex figures are untouched: the builder reading is beside them, not in them.
    test_one_company_is_one_name_and_its_pages_add_up_to_capex(fleet)
