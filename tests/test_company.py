"""One name per company, and one total per company.

The client's complaint was that one company read as several — "META / META Inc" —
so per-company figures did not add up. Two things are pinned here: every surface
prints a company under the one name `company.display_names` gives it, and the
company page's megawatts are the Capex page's, campus by campus.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter, defaultdict

import pytest

from tracker import capex, company, feed, watchfor
from tracker.dedup import company_slug
from tracker.models import CapacityBlock, Project
from tracker.webui import assets, dataset, mapsummary
from tracker.webui import query as table

NTT_SPELLINGS = (
    "NTT DATA",
    "NTT Data Group",
    "NTT Global Data Centers",
    "NTT Global Data Centers (RagingWire)",
    "NTT Global Data Centers Americas",
    "NTT Ltd.",
    "RagingWire",
    "RagingWire Data Centers",
)

AMAZON_SPELLINGS = (
    "AWS",
    "Amazon",
    "Amazon (AWS)",
    "Amazon Data Services",
    "Amazon Data Services Inc.",
    "Amazon Web Services",
)


def _project(session, **kwargs) -> Project:
    defaults = {
        "name": "Campus",
        "company": "Someone",
        "state": "TX",
        "city": "Abilene",
        "phase": "construction",
        "confidence": 2,
    }
    defaults.update(kwargs)
    defaults.setdefault(
        "dedup_key", f"{defaults['company']}|{defaults['name']}|{defaults['city']}".lower()
    )
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


def _block(session, project, **kwargs) -> CapacityBlock:
    defaults = {"block_key": kwargs.get("label", "b").lower(), "label": "B", "status": "planned"}
    defaults.update(kwargs)
    block = CapacityBlock(project_id=project.id, generic=False, **defaults)
    session.add(block)
    session.flush()
    return block


# --- the one name ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spellings", "expected"), [(NTT_SPELLINGS, "NTT"), (AMAZON_SPELLINGS, "Amazon")]
)
def test_a_spelling_family_prints_as_the_roster_name(session, spellings, expected):
    """NTT sat under eight spellings and Amazon under six on a copy of production."""
    for i, spelling in enumerate(spellings):
        _project(session, name=f"Campus {i}", company=spelling, city=f"Town {i}")

    names = company.display_names(session)
    assert {names.company(s) for s in spellings} == {expected}
    # The key is unchanged: the name is how it reads, not what it is.
    assert len({capex.company_key(s) for s in spellings}) == 1


def test_off_the_roster_the_spelling_most_rows_use_wins():
    spellings = Counter({"Rowan Digital Infrastructure": 2, "Rowan Data Centers": 1})
    assert company.display_name("rowan", spellings, roster={}) == "Rowan Digital Infrastructure"


def test_a_tie_never_depends_on_the_order_rows_were_read_in():
    one = Counter({"AVAIO Digital Partners": 1, "AVAIO Digital": 1})
    other = Counter({"AVAIO Digital": 1, "AVAIO Digital Partners": 1})
    assert company.display_name("avaio digital", one, roster={}) == "AVAIO Digital"
    assert company.display_name("avaio digital", other, roster={}) == "AVAIO Digital"


def test_the_roster_name_wins_over_any_count():
    spellings = Counter({"Amazon Web Services": 30, "Amazon": 13})
    assert company.display_name("amazon", spellings, roster={"amazon": "Amazon"}) == "Amazon"


def test_one_campus_listed_building_by_building_does_not_outvote_the_rest(session):
    big = _project(session, name="Big", company="Somebody Else", city="Big Town")
    for n in range(3):
        _block(session, big, label=f"Hall {n}", customer="Foo Cloud Inc.", mw=10.0)
    _project(session, name="A", company="Foo Cloud", city="A Town")
    _project(session, name="B", company="Foo Cloud", city="B Town")

    assert company.display_names(session).company("Foo Cloud Inc.") == "Foo Cloud"


def test_a_hedge_is_printed_as_written_and_names_nobody(session):
    hedge = "a Fortune 100 technology company"
    _project(session, company="QTS Data Centers", customer=hedge)
    names = company.display_names(session)
    assert names.customer(hedge) == hedge
    assert names.customer("Amazon Web Services") == "Amazon"


# --- the payloads carry it ------------------------------------------------------------


def test_every_row_shape_carries_the_one_name(session):
    """The light index, a table row and a project's page all print "Amazon"."""
    aws = _project(session, name="North", company="AWS", customer=None, city="Boardman")
    _project(session, name="South", company="Amazon Web Services", city="Hermiston")
    tenant = _project(
        session, name="Leased", company="QTS Data Centers", customer="Amazon (AWS)", city="Rome"
    )
    session.expire_all()

    index = {row["id"]: row for row in dataset.index_rows(session)}
    assert index[aws.id]["company_name"] == "Amazon"
    assert index[aws.id]["company"] == "AWS", "the source's own spelling stays"
    assert index[aws.id]["company_slug"] == "amazon"
    assert index[tenant.id]["company_name"] == "QTS"
    assert index[tenant.id]["customer_name"] == "Amazon"
    assert index[tenant.id]["customer_slug"] == "amazon"

    project = session.get(Project, aws.id)
    assert dataset.project_payload(project)["company_name"] == "Amazon"
    assert dataset.table_row(project)["company_name"] == "Amazon"
    _total, rows = table.page(session, table.Filters(sort="id", direction="asc"))
    assert [r["company_name"] for r in rows] == ["Amazon", "Amazon", "QTS"]


def test_a_search_finds_a_row_by_the_name_it_is_printed_under(session):
    """A row stored as "AWS" reads "Amazon" on the screen, so "amazon" must find it."""
    aws = _project(session, name="North", company="AWS", city="Boardman")
    _project(session, name="Elsewhere", company="Meta", city="Prineville")

    assert table.ids(session, table.Filters(q="amazon")) == [aws.id]
    assert table.ids(session, table.Filters(q="aws")) == [aws.id], "the stored spelling too"


def test_the_watch_picker_groups_by_company_not_by_spelling(session):
    """It grouped the stored strings, so NTT was offered eight times."""
    ids = [
        _project(session, name=f"Campus {i}", company=s, city=f"Town {i}").id
        for i, s in enumerate(NTT_SPELLINGS)
    ]
    groups: dict[str, set[int]] = defaultdict(set)
    labels: dict[str, set[str]] = defaultdict(set)
    for row in dataset.index_rows(session):
        groups[row["company_slug"]].add(row["id"])
        labels[row["company_slug"]].add(row["company_name"])
    assert groups == {"ntt": set(ids)}
    assert labels == {"ntt": {"NTT"}}

    # And the picker keys on that, not on the row's own text.
    source = (assets.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    body = re.search(r"function watchCandidates\(projects\) \{.*?\n\}\n", source, re.S)
    assert body, "app.js no longer declares watchCandidates"
    picker = body.group(0)
    assert "const companyId = p.company_slug ||" in picker
    assert "companies.get(companyId)" in picker
    assert "tenants.get(p.customer_slug)" in picker
    assert "companies.get(company.toLowerCase())" not in picker, "grouped by spelling again"


def test_capex_names_each_buyer_once(session):
    """The buyer table printed whichever spelling reached its row first."""
    _project(session, name="North", company="AWS", city="Boardman", mw_planned=100.0)
    _project(
        session, name="South", company="Amazon Web Services", city="Hermiston", mw_planned=50.0
    )
    names = {p.key: p.name for p in capex.rollup(session)}
    assert names["amazon"] == "Amazon"


def test_updates_and_monitor_print_the_one_name(session):
    project = _project(session, name="North", company="AWS", city="Boardman")
    project.created_at = dt.datetime.now()
    session.flush()

    digest = feed.digest(session, account_id=None)
    assert {s.company for s in digest.signals} == {"Amazon"}
    report = watchfor.report(session, everything=True)
    assert [p.company for p in report.projects] == ["Amazon"]
    assert report.projects[0].as_json()["company_slug"] == "amazon"


# --- one total ------------------------------------------------------------------------


@pytest.fixture
def fleet(session):
    """Meta, counted three ways that used to inflate its total, and its landlord.

    * Prineville is stored twice — once as Meta, once as Facebook — 1,000 MW and
      800 MW. Capex counts one row of a suspected duplicate.
    * Austin is cancelled. Capex leaves it out.
    * Meta leases one 200 MW building of QTS's 600 MW Atlanta campus. Capex credits
      Meta with that building, not the campus.
    """
    prineville = _project(
        session,
        name="Prineville Data Center",
        company="Meta",
        city="Prineville",
        state="OR",
        mw_planned=1000.0,
        mw_built=300.0,
    )
    twin = _project(
        session,
        name="Prineville Data Center",
        company="Facebook",
        city="Prineville",
        state="OR",
        mw_planned=800.0,
        mw_built=300.0,
    )
    austin = _project(
        session, name="Austin", company="Meta", city="Austin", phase="cancelled", mw_planned=900.0
    )
    atlanta = _project(
        session,
        name="Atlanta Campus",
        company="QTS Data Centers",
        city="Atlanta",
        state="GA",
        mw_planned=600.0,
        mw_built=250.0,
    )
    _block(session, atlanta, label="Building A", mw=200.0, customer="Meta", status="serving")
    session.expire_all()
    return {
        "prineville": prineville.id,
        "twin": twin.id,
        "austin": austin.id,
        "atlanta": atlanta.id,
    }


def test_a_companys_total_is_its_capex_row(session, fleet):
    """Twin once, cancelled out, the tenant credited with its building — as on Capex."""
    pairs = capex.suspected_duplicates(session)
    assert capex.duplicate_groups(pairs) == [sorted([fleet["prineville"], fleet["twin"]])]
    meta = {p.key: p for p in capex.rollup(session, pairs=pairs)}["meta"]

    page = company.profile(session, "meta")
    totals = page["totals"]
    assert totals["mw_planned"] == pytest.approx(meta.mw_planned) == 1200.0
    assert totals["mw_built"] == pytest.approx(meta.mw_built) == 500.0
    assert totals["counting"]["capex_row"] == {"mw_planned": 1200.0, "mw_built": 500.0}
    assert totals["counting"]["no_named_buyer"] == {"mw_planned": 0.0, "mw_built": 0.0}
    assert (totals["counting"]["duplicates"], totals["counting"]["stopped"]) == (1, 1)

    # Every campus is still listed, each saying what it adds and why.
    counted = {row["id"]: row["counted"] for row in page["projects"]}
    assert counted[fleet["prineville"]]["status"] == "counted"
    assert counted[fleet["twin"]] == {
        "status": "duplicate",
        "of": fleet["prineville"],
        "mw_planned": 0.0,
        "mw_built": 0.0,
    }
    assert counted[fleet["austin"]]["status"] == "stopped"
    assert counted[fleet["atlanta"]]["status"] == "part"
    assert counted[fleet["atlanta"]]["mw_planned"] == 200.0
    assert counted[fleet["atlanta"]]["for"] == ["QTS"]
    assert page["name"] == "Meta"


def test_a_landlord_is_credited_with_what_no_named_buyer_holds(session, fleet):
    """QTS has no Capex row: Capex files its campus under "(no named customer)".

    The company page credits that remainder to the company building it, so the
    landlord's page is not empty and no megawatt is on two companies' pages.
    """
    qts = company.profile(session, "qts")
    assert qts["name"] == "QTS"
    assert qts["totals"]["mw_planned"] == 400.0
    assert qts["totals"]["mw_built"] == 50.0
    assert qts["totals"]["counting"]["capex_row"]["mw_planned"] == 0.0
    assert qts["totals"]["counting"]["no_named_buyer"]["mw_planned"] == 400.0
    (row,) = qts["projects"]
    assert row["counted"]["status"] == "part" and row["counted"]["for"] == ["Meta"]

    # Every megawatt Capex counts is on exactly one company's page.
    positions = capex.rollup(session)
    pages = sum(company.profile(session, s)["totals"]["mw_planned"] for s in ("meta", "qts"))
    assert pages == pytest.approx(sum(p.mw_planned for p in positions)) == 1600.0


def test_the_cached_rollup_gives_the_same_page(session, fleet):
    """The console passes its cached rollup; the answer must not depend on that."""
    tally = capex.counting(session)
    assert company.profile(session, "meta", tally=tally) == company.profile(session, "meta")
    assert tally.set_aside == {fleet["twin"]: fleet["prineville"]}


def test_the_map_counts_the_same_rows_at_the_campus_figure(session, fleet):
    """Same rows as Capex; each at the campus's own capacity."""
    summary = mapsummary.build(session, tally=capex.counting(session))
    assert summary["skipped"] == [fleet["twin"]]
    assert summary["totals"]["campuses"] == 2
    assert summary["totals"]["planned_mw"] == 1600.0


def test_a_buyer_only_a_party_names_still_has_a_page(session):
    """Its megawatts are on Capex, so they have to be on some company's page."""
    from tracker.models import ProjectParty

    site = _project(session, name="Fermi", company="Fermi America", city="Amarillo", mw_planned=500)
    session.add(
        ProjectParty(project_id=site.id, name="TensorWave", party_key="tensorwave", role="customer")
    )
    session.flush()
    session.expire_all()

    page = company.profile(session, company_slug("tensorwave"))
    assert page is not None
    assert [r["id"] for r in page["projects"]] == [site.id]
    assert page["projects"][0]["via"] == "customer"
    assert page["totals"]["mw_planned"] == 500.0
    # And the builder's page lists it as counted for the tenant.
    fermi = company.profile(session, "fermi-america")
    assert fermi["projects"][0]["counted"] == {
        "status": "elsewhere",
        "for": ["TensorWave"],
        "mw_planned": 0.0,
        "mw_built": 0.0,
    }
