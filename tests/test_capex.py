"""Attribution: who is actually buying the capacity.

The rollup answers a question the schema does not key on, so most of what is
worth testing is the attribution rule rather than the arithmetic.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker import capex
from tracker.dedup import customer_key, is_undisclosed
from tracker.models import Event, Project, Risk

# --- Customer identity -------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Meta", "Facebook"),
        ("Meta Platforms", "Facebook"),
        ("Amazon Web Services", "Amazon"),
        ("Amazon Web Services, Inc.", "Amazon Data Services"),
        ("Alphabet", "Google Cloud"),
        ("Microsoft Corporation", "Microsoft"),
    ],
)
def test_the_same_buyer_folds_to_one_key(a: str, b: str):
    """A rollup that splits Meta from Facebook halves the number it exists to give."""
    assert customer_key(a) == customer_key(b) != ""


@pytest.mark.parametrize(
    "value",
    [
        "Fortune 100 technology company",
        "Fortune 100 customer",
        "Publicly-traded global enterprise (technology company based in the Bay Area)",
        "an undisclosed hyperscale customer",
        "a leading technology company",
        "confidential",
    ],
)
def test_a_hedge_is_not_an_identity(value: str):
    """Four of the twelve populated values on the live database were of this shape.

    Left alone each becomes its own one-project "customer", so the table shows
    several tiny tenants that are probably one real one — and probably one
    already named in a row above.
    """
    assert is_undisclosed(value)
    assert customer_key(value) == ""


@pytest.mark.parametrize("value", ["OpenAI", "eBay", "Oracle", "AMD", "CoreWeave"])
def test_a_real_name_survives(value: str):
    assert not is_undisclosed(value)
    assert customer_key(value)


# --- Attribution -------------------------------------------------------------


def _project(session, **kwargs) -> Project:
    defaults = {
        "name": "Campus",
        "company": "Someone",
        "state": "TX",
        "city": "Abilene",
        "dedup_key": f"k{kwargs.get('name', 'x')}{kwargs.get('company', 'y')}",
        "phase": "construction",
        "confidence": 2,
    }
    defaults.update(kwargs)
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


def test_a_named_tenant_is_the_buyer(session):
    project = _project(session, company="Crusoe", customer="OpenAI")
    name, key, self_built = capex.attribute(project)
    assert (key, self_built) == ("openai", False)
    assert name == "OpenAI"


def test_a_self_built_campus_is_attributed_to_its_operator(session):
    """`customer` is null on a hyperscaler's own campus and that is correct.

    Grouping on the column alone would drop these entirely, which is most of the
    capacity the question is actually about.
    """
    project = _project(session, company="Meta", customer=None)
    name, key, self_built = capex.attribute(project)
    assert (key, self_built) == ("meta", True)
    assert name == "Meta"


def test_a_hedged_tenant_falls_through_to_the_operator_rule(session):
    project = _project(session, company="Meta", customer="a Fortune 100 technology company")
    _name, key, self_built = capex.attribute(project)
    assert (key, self_built) == ("meta", True)


def test_a_landlord_with_no_tenant_is_unattributed_not_guessed(session):
    """Naming the developer as the buyer would invent the fact the table is for."""
    project = _project(session, company="STACK Infrastructure", customer=None)
    name, key, self_built = capex.attribute(project)
    assert (name, key, self_built) == (capex.UNATTRIBUTED, "", False)


def test_private_end_users_count_even_though_they_file_nothing(session):
    """The company list answers "whose filings to read", not "who buys capacity".

    Using it alone excluded OpenAI and xAI, the second and third largest
    unattributed operators on the live database.
    """
    for company in ("OpenAI", "xAI", "Anthropic"):
        _name, key, self_built = capex.attribute(_project(session, company=company, name=company))
        assert self_built is True, company
        assert key


# --- Rollup ------------------------------------------------------------------


def test_rollup_sums_across_operators_for_one_buyer(session):
    _project(session, name="A", company="Crusoe", customer="OpenAI", mw_planned=1000)
    _project(session, name="B", company="Vantage", customer="OpenAI", mw_planned=500)
    _project(session, name="C", company="Meta", mw_planned=200)

    positions = {p.key: p for p in capex.rollup(session)}
    assert positions["openai"].mw_planned == 1500
    assert positions["openai"].projects == 2
    assert positions["meta"].mw_planned == 200


def test_cancelled_projects_are_out_of_the_forward_pipeline(session):
    # Distinct cities: one company twice in one locality is a suspected-duplicate
    # signal the rollup now acts on, and that is not what this test is about.
    _project(session, name="Live", company="Meta", mw_planned=100)
    _project(session, name="Dead", company="Meta", city="Austin", phase="cancelled", mw_planned=900)

    live = {p.key: p for p in capex.rollup(session)}["meta"]
    assert live.mw_planned == 100

    both = {p.key: p for p in capex.rollup(session, include_terminal=True)}["meta"]
    assert both.mw_planned == 1000


def test_unattributed_sorts_last_however_large(session):
    """It is a residual, not a buyer, and it is usually the biggest row."""
    _project(session, name="Small", company="Meta", mw_planned=1)
    _project(session, name="Huge", company="STACK Infrastructure", mw_planned=9999)

    positions = capex.rollup(session)
    assert positions[-1].name == capex.UNATTRIBUTED
    assert positions[-1].mw_planned == 9999


def test_capacity_lands_in_the_year_it_is_expected_online(session):
    # Distinct cities, so the duplicate guard (same company, one locality) stays
    # out of a test about year bucketing.
    _project(
        session,
        name="A",
        company="Meta",
        mw_planned=300,
        expected_online=dt.date(2028, 1, 1),
    )
    _project(
        session,
        name="B",
        company="Meta",
        city="Dallas",
        mw_planned=700,
        expected_online=dt.date(2028, 6, 1),
    )
    _project(session, name="C", company="Meta", city="Austin", mw_planned=50)  # no date

    meta = {p.key: p for p in capex.rollup(session)}["meta"]
    assert meta.mw_by_year == {2028: 1000}
    assert meta.mw_planned == 1050
    # The same capacity, split by quarter. "Whose pipeline lands next quarter" is
    # the question the year column cannot answer.
    assert meta.mw_by_quarter == {"2028Q1": 300, "2028Q2": 700}
    assert capex.quarters([meta]) == ["2028Q1", "2028Q2"]


@pytest.mark.parametrize(
    ("month", "quarter"),
    [(1, "Q1"), (3, "Q1"), (4, "Q2"), (6, "Q2"), (7, "Q3"), (9, "Q3"), (10, "Q4"), (12, "Q4")],
)
def test_every_month_lands_in_the_right_quarter(session, month: int, quarter: str):
    """Off-by-one here would move capacity a quarter, which is the whole point."""
    _project(
        session, name="A", company="Meta", mw_planned=100, expected_online=dt.date(2027, month, 15)
    )
    meta = {p.key: p for p in capex.rollup(session)}["meta"]
    assert meta.mw_by_quarter == {f"2027{quarter}": 100}


def test_quarter_precision_is_measured_rather_than_assumed(session):
    """A source that said only "2027" normalises to 1 January and looks like Q1.

    The quarter columns are worth having and worth distrusting, and the only
    honest way to say how much is to count how many dates are suspiciously round.
    Without this the view would pile a year of vagueness into Q1 and present it
    as a schedule.
    """
    _project(session, name="A", company="Meta", mw_planned=100, expected_online=dt.date(2027, 1, 1))
    _project(session, name="B", company="Meta", mw_planned=100, expected_online=dt.date(2027, 5, 1))
    _project(
        session, name="C", company="Meta", mw_planned=100, expected_online=dt.date(2027, 5, 14)
    )
    _project(session, name="D", company="Meta", mw_planned=100)  # undated, not counted

    precision = capex.date_precision(session)
    assert precision["dated"] == 3
    assert round(precision["year_only_pct"]) == 33  # only the 1 January one
    assert round(precision["month_start_pct"]) == 67  # 1 Jan and 1 May


def test_risk_and_slippage_attach_to_the_buyer(session):
    project = _project(session, name="A", company="Meta", mw_planned=400)
    session.add(
        Risk(project_id=project.id, category="transmission", severity="material", summary="s")
    )
    session.add(
        Event(
            project_id=project.id,
            event_date=dt.date(2028, 1, 1),
            event_type="delayed",
            description="moved",
        )
    )
    session.flush()

    meta = {p.key: p for p in capex.rollup(session)}["meta"]
    assert meta.mw_at_risk == 400
    assert meta.slipped == 1
    assert meta.at_risk_unconfirmed == 0
    assert capex.blocking_risk(session, "meta") == "transmission/material"


def test_an_unconfirmed_obstacle_counts_toward_exposure_and_is_disclosed(session):
    """Counted, and said so.

    Leaving a 待确认 obstacle out would understate exposure, which is the worse
    direction to be wrong in — a reported obstacle is information before it is
    evidenced. Disclosing the count is what keeps that from being a total whose
    composition a reader cannot see.
    """
    project = _project(session, name="A", company="Meta", mw_planned=400)
    session.add(
        Risk(
            project_id=project.id,
            category="transmission",
            severity="material",
            summary="s",
            unconfirmed="no_quote",
        )
    )
    session.flush()

    meta = {p.key: p for p in capex.rollup(session)}["meta"]
    assert meta.mw_at_risk == 400
    assert meta.at_risk_projects == 1
    assert meta.at_risk_unconfirmed == 1


def test_one_quoted_obstacle_takes_a_project_out_of_the_disclosure(session):
    """A project with one cited obstacle is not in doubt, whatever else it has."""
    project = _project(session, name="A", company="Meta", mw_planned=400)
    session.add(
        Risk(project_id=project.id, category="water", severity="watch", summary="cited", quote="q")
    )
    session.add(
        Risk(
            project_id=project.id,
            category="transmission",
            severity="material",
            summary="vague",
            unconfirmed="no_quote",
        )
    )
    session.flush()

    meta = {p.key: p for p in capex.rollup(session)}["meta"]
    assert meta.at_risk_projects == 1
    assert meta.at_risk_unconfirmed == 0


# --- Honesty about what the view covers --------------------------------------


def test_coverage_reports_how_much_of_the_database_is_attributed(session):
    _project(session, name="A", company="Crusoe", customer="OpenAI")
    _project(session, name="B", company="Meta")
    _project(session, name="C", company="STACK Infrastructure")
    _project(session, name="D", company="Flexential")

    cover = capex.coverage(session)
    assert cover["projects"] == 4
    assert cover["named_tenant_pct"] == 25.0
    assert cover["self_built_pct"] == 25.0
    assert cover["attributed_pct"] == 50.0


def test_an_operator_named_as_a_customer_is_flagged_not_corrected(session):
    """Observed live: one project listed a wholesale developer as its tenant.

    Surfaced rather than fixed, for the same reason `dedup` refuses to auto-merge
    across granularity: a landlord genuinely can lease from another landlord, so
    it is a question for a human.
    """
    _project(session, name="Their own", company="Aligned Data Centers")
    flagged = _project(
        session, name="Frederick", company="Quantum Loophole", customer="Aligned Data Centers"
    )

    suspects = capex.suspect_attributions(session)
    assert [s[0] for s in suspects] == [flagged.id]

    # Still attributed, because we do not silently discard the claim.
    assert {p.key for p in capex.rollup(session)} >= {"aligned"}


# --- Duplicates, which the customer axis makes expensive ---------------------


def test_one_campus_stored_per_party_is_flagged_with_its_cost(session):
    """A duplicate is a nuisance in a site listing and a wrong number here.

    The Abilene campus is stored once per company attached to it, so grouping by
    end customer counts the same 1.2 GW four times.
    """
    for company, name in [
        ("Crusoe", "Stargate Abilene"),
        ("OpenAI/Oracle", "Stargate"),
        ("OpenAI", "Stargate"),
        ("Oracle", "Stargate - Abilene"),
    ]:
        _project(session, name=name, company=company, city="Abilene", mw_planned=1200)

    pairs = capex.suspected_duplicates(session)
    assert len(pairs) == 6, "four rows make six pairs"
    # Three redundant rows, not six: a row appearing in several pairs counts once.
    assert capex.double_counted_mw(pairs) == 3600


def test_six_pairs_for_one_campus_become_one_decision(session):
    """An operator merges a group, not a pair.

    Six pairwise flags for four rows would be six prompts to answer the same
    question, and `tracker merge` takes one survivor with any number of rows to
    fold in — so the grouping is what makes the finding actionable at all.
    """
    for company, name in [
        ("Crusoe", "Stargate Abilene"),
        ("OpenAI/Oracle", "Stargate"),
        ("OpenAI", "Stargate"),
        ("Oracle", "Stargate - Abilene"),
    ]:
        _project(session, name=name, company=company, city="Abilene", mw_planned=1200)
    # A second campus elsewhere must not be pulled into the first group.
    for company in ("SoftBank", "OpenAI"):
        _project(session, name="Stargate Milam", company=company, city="Milam", mw_planned=700)

    groups = capex.duplicate_groups(capex.suspected_duplicates(session))
    assert [len(g) for g in groups] == [4, 2], "largest group first"
    assert all(g == sorted(g) for g in groups), "ids ascending, so the default survivor is stable"
    assert not set(groups[0]) & set(groups[1])


def test_grouping_is_transitive_across_pairs():
    """A links to B and B links to C, with no A-C pair: one campus, not two.

    Built from pairs directly rather than from the detector, because the detector
    is transitive on every real example to hand and the fixture would prove
    nothing — it would pass against an implementation that only ever merged pairs
    sharing a *first* id. The chain is what matters: `looks_like_the_same_site`
    compares names, and "Stargate Abilene" can match "Stargate" and "Stargate -
    Abilene" while those two do not match each other. Offering that as two merges
    would leave a duplicate behind whichever one the operator ran.
    """

    def pair(a: int, b: int) -> capex.DuplicatePair:
        return capex.DuplicatePair(
            a_id=a,
            a_company="A",
            a_name="n",
            b_id=b,
            b_company="B",
            b_name="n",
            locality="abilene",
            state="TX",
            b_mw=1200.0,
        )

    groups = capex.duplicate_groups([pair(1, 2), pair(2, 3)])
    assert groups == [[1, 2, 3]]

    # Order of arrival must not matter: the second pair can arrive first, and a
    # later pair can be the one that welds two existing groups together.
    assert capex.duplicate_groups([pair(2, 3), pair(1, 2)]) == [[1, 2, 3]]
    assert capex.duplicate_groups([pair(1, 2), pair(3, 4), pair(2, 3)]) == [[1, 2, 3, 4]]
    # Unrelated pairs stay apart.
    assert capex.duplicate_groups([pair(1, 2), pair(3, 4)]) == [[1, 2], [3, 4]]


def test_a_busy_locality_produces_no_false_duplicates(session):
    for company, name in [
        ("Digital Realty", "Ashburn Campus"),
        ("Equinix", "North Ashburn Campus"),
        ("QTS Data Centers", "Shellhorn DC-1"),
        ("Vizsla Ventures", "Dulles Berry"),
    ]:
        _project(session, name=name, company=company, city="Ashburn", state="VA", mw_planned=100)

    assert capex.suspected_duplicates(session) == []


def test_duplicate_pairs_survive_the_session_closing(session):
    """Every caller renders the report after the session is gone."""
    _project(session, name="Stargate", company="Crusoe", city="Abilene", mw_planned=1200)
    _project(session, name="Stargate", company="Oracle", city="Abilene", mw_planned=1200)
    pairs = capex.suspected_duplicates(session)
    session.expunge_all()
    assert pairs[0].a_company and pairs[0].locality  # no DetachedInstanceError


# --- Per-tranche attribution -------------------------------------------------
#
# The reason `customer` sits on a capacity block at all. Lake Mariner is 378 MW
# being built for Fluidstack beside 60 MW already serving Core42; putting all of it
# against whichever name reached `project.customer` first is simply wrong.


def _block(session, project, **kwargs):
    from tracker.models import CapacityBlock

    defaults = {
        "block_key": kwargs.get("label", "b").lower().replace(" ", "-"),
        "label": "Block",
        "status": "planned",
        "generic": False,
    }
    defaults.update(kwargs)
    block = CapacityBlock(project_id=project.id, **defaults)
    session.add(block)
    session.flush()
    return block


def test_a_campus_with_two_tenants_splits_between_them(session):
    project = _project(session, company="TeraWulf", customer="Fluidstack", mw_planned=750.0)
    _block(
        session,
        project,
        label="Akela",
        mw=378.0,
        customer="Fluidstack",
        status="under_construction",
    )
    _block(session, project, label="La Lupa", mw=60.0, customer="Core42", status="serving")
    session.refresh(project)

    shares, leftover = capex.block_shares(project)
    by_key = {s.key: s for s in shares}
    assert by_key["fluidstack"].mw_planned == 378.0
    assert by_key["core42"].mw_planned == 60.0
    # Serving means delivering, so it counts as built; the other does not.
    assert by_key["core42"].mw_built == 60.0
    assert by_key["fluidstack"].mw_built == 0.0
    # The rest of the cited campus total stays with the campus rather than being
    # invented into somebody's position.
    assert leftover == 750.0 - 438.0


def test_the_campus_total_is_conserved_not_replaced(session):
    """Splitting must not create or destroy megawatts."""
    project = _project(session, company="TeraWulf", customer="Fluidstack", mw_planned=750.0)
    _block(
        session,
        project,
        label="Akela",
        mw=378.0,
        customer="Fluidstack",
        status="under_construction",
    )
    _block(session, project, label="La Lupa", mw=60.0, customer="Core42", status="serving")
    session.refresh(project)

    total = sum(p.mw_planned for p in capex.rollup(session))
    assert total == 750.0


def test_an_unconfirmed_tranche_capacity_is_never_attributed(session):
    """Same rule as the rollup: a figure nobody stated is not a buyer's position."""
    project = _project(session, company="Someone", customer=None, mw_planned=100.0)
    _block(session, project, label="Phase 1", mw=9000.0, customer="Meta", unconfirmed_fields="mw")
    session.refresh(project)

    shares, leftover = capex.block_shares(project)
    assert shares == []
    assert leftover == 100.0


def test_a_cancelled_tranche_is_out_of_its_buyer_s_pipeline(session):
    project = _project(session, company="Someone", customer=None, mw_planned=200.0)
    _block(session, project, label="Phase 1", mw=50.0, customer="Meta", status="cancelled")
    _block(session, project, label="Phase 2", mw=50.0, customer="Meta", status="planned")
    session.refresh(project)

    shares, _ = capex.block_shares(project)
    assert [s.mw_planned for s in shares] == [50.0]


def test_a_tranche_books_capacity_on_its_own_date_not_the_campus_s(session):
    """The campus has one `expected_online`; its tranches do not land together."""
    import datetime as dt

    project = _project(
        session,
        company="TeraWulf",
        customer=None,
        mw_planned=438.0,
        expected_online=dt.date(2027, 6, 1),
    )
    _block(
        session,
        project,
        label="La Lupa",
        mw=60.0,
        customer="Core42",
        status="serving",
        energized_on=dt.date(2025, 3, 1),
    )
    _block(
        session,
        project,
        label="Akela",
        mw=378.0,
        customer="Fluidstack",
        status="under_construction",
        expected_online=dt.date(2027, 1, 1),
    )
    session.refresh(project)

    positions = {p.key: p for p in capex.rollup(session)}
    assert positions["core42"].mw_by_year == {2025: 60.0}
    assert positions["fluidstack"].mw_by_year == {2027: 378.0}


def test_a_project_with_no_blocks_is_attributed_exactly_as_before(session):
    project = _project(session, company="Meta", customer=None, mw_planned=120.0, mw_built=40.0)
    session.refresh(project)

    shares, leftover = capex.block_shares(project)
    assert shares == [] and leftover == 120.0
    position = next(p for p in capex.rollup(session) if p.key == "meta")
    assert (position.mw_planned, position.mw_built, position.projects) == (120.0, 40.0, 1)


def test_two_rows_holding_one_tranche_are_flagged_even_if_named_differently(session):
    """Three rows in Andrews, TX each held the same 70 MW AWS tranche.

    `block_key` is derived, so two rows carrying `stingray` are two readings of one
    building. That is harder evidence than a name resemblance, and it catches pairs
    the name test misses.
    """
    a = _project(session, name="Stingray Facility", company="Vantage", city="Andrews")
    b = _project(session, name="Project Bluebird", company="Vantage", city="Andrews")
    _block(session, a, block_key="stingray", label="Stingray", mw=70.0)
    _block(session, b, block_key="stingray", label="Stingray", mw=70.0)
    session.refresh(a)
    session.refresh(b)

    pairs = capex.suspected_duplicates(session)
    found = [p for p in pairs if {p.a_id, p.b_id} == {a.id, b.id}]
    assert found, "a shared tranche did not raise the pair"
    assert found[0].shared_blocks == ("stingray",)


def test_a_generic_tranche_shared_by_two_rows_proves_nothing(session):
    """Half the database has a `phase-1`. Pairing on it would pair everything."""
    a = _project(session, name="Alpha Campus", company="Vantage", city="Mesa")
    b = _project(session, name="Beta Campus", company="Aligned", city="Mesa")
    _block(session, a, block_key="phase-1", label="Phase 1", mw=50.0, generic=True)
    _block(session, b, block_key="phase-1", label="Phase 1", mw=50.0, generic=True)
    session.refresh(a)
    session.refresh(b)

    pairs = capex.suspected_duplicates(session)
    assert not [p for p in pairs if {p.a_id, p.b_id} == {a.id, b.id}]


# --- One row per suspected campus ---------------------------------------------
#
# Flagging a duplicate was not enough: until somebody merged, every extra row
# added its full capacity to a buyer again. The rollup now counts one
# representative per suspected group and discloses the rest — skipped, never
# merged, so `tracker merge` stays the only real repair.


def test_a_suspected_duplicate_group_is_counted_once_in_the_rollup(session):
    """Abilene was stored four times and 1.2 GW counted four times against OpenAI."""
    for company in ("Crusoe", "OpenAI", "Oracle", "OpenAI/Oracle"):
        _project(
            session,
            name="Stargate",
            company=company,
            customer="OpenAI",
            mw_planned=1200,
            investment_usd=500,
        )

    openai = {p.key: p for p in capex.rollup(session)}["openai"]
    assert openai.projects == 1
    assert openai.mw_planned == 1200
    assert openai.investment_usd == 500
    assert openai.duplicate_rows_skipped == 3
    assert openai.mw_duplicate_skipped == 3600
    assert openai.investment_duplicate_skipped_usd == 1500
    # The ids behind the numbers, so a reader can click the aggregate open.
    assert len(openai.project_ids) == 1
    assert len(openai.duplicate_skipped_ids) == 3
    assert not set(openai.project_ids) & set(openai.duplicate_skipped_ids)


def test_the_representative_is_the_row_a_merge_would_keep(session):
    """A named tenant beats a larger reading, and the skip lands on its own buyer.

    The Oracle row's megawatts must show up under Crusoe's disclosure, not
    OpenAI's — an Oracle reader should see what was set aside on their side.
    """
    _project(session, name="Stargate Abilene", company="Crusoe", mw_planned=900)
    _project(session, name="Stargate", company="OpenAI", customer="OpenAI", mw_planned=500)

    positions = {p.key: p for p in capex.rollup(session)}
    assert positions["openai"].projects == 1
    assert positions["openai"].mw_planned == 500
    crusoe = positions["crusoe"]
    assert crusoe.projects == 0
    assert crusoe.mw_planned == 0
    assert crusoe.duplicate_rows_skipped == 1
    assert crusoe.mw_duplicate_skipped == 900


def test_a_terminal_member_never_represents_its_group(session):
    """A cancelled row is not in the default table, so it cannot displace a live one."""
    _project(session, name="Stargate", company="Crusoe", mw_planned=100)
    _project(session, name="Stargate Campus", company="Oracle", phase="cancelled", mw_planned=900)

    crusoe = {p.key: p for p in capex.rollup(session)}["crusoe"]
    assert crusoe.projects == 1
    assert crusoe.mw_planned == 100
    assert crusoe.duplicate_rows_skipped == 0

    # Asked to see terminal rows too, the larger cancelled reading may represent.
    both = {p.key: p for p in capex.rollup(session, include_terminal=True)}
    assert both["crusoe"].duplicate_rows_skipped == 1


# --- Only confirmed dollars are summed ----------------------------------------


def _source(session, project, *, fields=None, unconfirmed_fields=None, reasons=None):
    import json

    from tracker.models import Source, utcnow

    source = Source(
        project_id=project.id,
        url=f"https://example.com/{project.id}/{fields or 'none'}/{unconfirmed_fields or 'none'}",
        source_type="trade_press",
        fetched_at=utcnow(),
        fields=fields,
        unconfirmed_fields=unconfirmed_fields,
        unconfirmed_reasons=json.dumps(reasons) if reasons else None,
    )
    session.add(source)
    session.flush()
    return source


def test_an_investment_no_source_confirms_is_excluded_and_disclosed(session):
    """A programme-wide total demoted at ingest must not sum into a campus column."""
    project = _project(session, company="Meta", mw_planned=100, investment_usd=500_000_000_000)
    _source(session, project, unconfirmed_fields="investment_usd")

    meta = {p.key: p for p in capex.rollup(session)}["meta"]
    assert meta.investment_usd == 0
    assert meta.investment_excluded_usd == 500_000_000_000
    assert meta.mw_planned == 100  # only the dollars are excluded


def test_one_confirming_source_keeps_the_investment_counted(session):
    project = _project(session, company="Meta", investment_usd=1_000_000)
    _source(session, project, unconfirmed_fields="investment_usd")
    _source(session, project, fields="mw_planned,investment_usd")

    meta = {p.key: p for p in capex.rollup(session)}["meta"]
    assert meta.investment_usd == 1_000_000
    assert meta.investment_excluded_usd == 0


def test_a_hand_written_investment_with_no_claims_still_counts(session):
    """No source mentions the field, so there is no ingest decision to read back.

    This is the boundary that keeps manual seeds and fixtures counting: the
    exclusion reads back a demotion, it does not demand a quote from rows whose
    provenance never went through the gate.
    """
    bare = _project(session, company="Meta", investment_usd=2_000_000)
    other = _project(session, name="Annex", company="Meta", city="Austin", investment_usd=3_000_000)
    _source(session, other, fields="mw_planned")  # names other fields, says nothing about money

    meta = {p.key: p for p in capex.rollup(session)}["meta"]
    assert meta.investment_usd == 5_000_000
    assert meta.investment_excluded_usd == 0
    assert bare.investment_usd == 2_000_000


def test_only_an_implausible_figure_is_excluded_not_a_merely_unquoted_one(session):
    """The reason is what stops the exclusion over-reaching.

    Both figures are 待确认 and one bit cannot tell them apart, so both used to be
    dropped from the sum. Only the first genuinely is not this site's money; the
    second is very likely correct and simply nobody quoted it, and excluding it
    understates the one number the table exists to state.
    """
    programme = _project(session, name="Abilene", company="Meta", mw_planned=100)
    programme.investment_usd = 500_000_000_000
    _source(
        session,
        programme,
        unconfirmed_fields="investment_usd",
        reasons={"investment_usd": "out_of_scale"},
    )
    unquoted = _project(
        session, name="Annex", company="Meta", city="Austin", investment_usd=2_000_000_000
    )
    _source(
        session,
        unquoted,
        unconfirmed_fields="investment_usd",
        reasons={"investment_usd": "no_quote"},
    )
    session.flush()

    meta = {p.key: p for p in capex.rollup(session)}["meta"]
    assert meta.investment_excluded_usd == 500_000_000_000
    assert meta.investment_usd == 2_000_000_000
    assert meta.investment_unquoted_usd == 2_000_000_000, "counted, and disclosed"


def test_a_source_older_than_the_reason_column_is_still_excluded(session):
    """No reason recorded means a source written before migration 0013.

    Read as the conservative case, which is exactly the behaviour this split
    replaced — so no number that was already being reported can move.
    """
    project = _project(session, company="Meta", mw_planned=100, investment_usd=500_000_000_000)
    _source(session, project, unconfirmed_fields="investment_usd")  # no reasons

    meta = {p.key: p for p in capex.rollup(session)}["meta"]
    assert meta.investment_usd == 0
    assert meta.investment_excluded_usd == 500_000_000_000


def test_a_skipped_duplicate_is_not_disclosed_twice(session):
    """A skipped row's unconfirmed $500B lands in one disclosure, not two."""
    _project(session, name="Stargate", company="OpenAI", customer="OpenAI", mw_planned=1200)
    dupe = _project(
        session,
        name="Stargate Campus",
        company="Oracle",
        customer="OpenAI",
        mw_planned=800,
        investment_usd=500,
    )
    _source(session, dupe, unconfirmed_fields="investment_usd")

    openai = {p.key: p for p in capex.rollup(session)}["openai"]
    assert openai.investment_duplicate_skipped_usd == 500
    assert openai.investment_excluded_usd == 0


# --- The year grid -------------------------------------------------------------


def test_year_columns_are_continuous_across_a_gap():
    """2028 and 2030 with data must render 2029 as an empty column, not skip it."""
    position = capex.Position(name="Meta", key="meta", mw_by_year={2028: 100.0, 2030: 50.0})
    assert capex.year_columns([position], start=2026) == [2028, 2029, 2030]


def test_year_columns_drop_the_past_and_cap_the_future():
    stale = capex.Position(name="a", key="a", mw_by_year={2024: 5.0, 2027: 1.0})
    assert capex.year_columns([stale], start=2026) == [2027]

    sprawl = capex.Position(name="b", key="b", mw_by_year={2026: 1.0, 2040: 1.0})
    assert len(capex.year_columns([sprawl], start=2026)) == capex.MAX_YEAR_COLUMNS

    assert capex.year_columns([], start=2026) == []


def test_quarter_columns_stay_data_only():
    """A continuous quarter grid would spend the whole width on empty quarters."""
    position = capex.Position(name="m", key="m", mw_by_quarter={"2026Q1": 1.0, "2027Q3": 2.0})
    assert capex.quarter_columns([position], start="2026Q1") == ["2026Q1", "2027Q3"]
    assert capex.quarter_columns([position], start="2026Q2") == ["2027Q3"]


# --- Which tranche keys may pair two rows ------------------------------------


def test_a_tranche_key_seen_in_two_towns_identifies_nothing(session):
    """`existing` paired Element Critical's Houston One with Switch's Houston
    campus: two unrelated operators, one shared word, and a false pair above the
    two real ones. `generic` is decided from the label's own words, so it cannot
    catch a real word that names a kind of tranche and no particular one."""
    from tracker.models import CapacityBlock

    for company, city in [("Element Critical", "Houston"), ("Switch", "Houston")]:
        row = _project(session, name=f"{city} site", company=company, city=city, state="TX")
        row.blocks.append(
            CapacityBlock(block_key="existing", label="Existing", mw=10.0, status="serving")
        )
    # The same key on a third row in another town is what proves it is vocabulary.
    other = _project(session, name="Austin site", company="Other", city="Austin", state="TX")
    other.blocks.append(
        CapacityBlock(block_key="existing", label="Existing", mw=10.0, status="serving")
    )
    session.flush()

    assert capex.suspected_duplicates(session) == []


def test_a_tranche_key_confined_to_one_town_still_pairs(session):
    """The Abilene case must survive: a campus stored four times has four rows
    holding `building-1`, so a count-based rarity rule would throw the flagship
    duplicate away. All four are in one town, which is what keeps it."""
    from tracker.models import CapacityBlock

    for company in ("IREN Limited", "Iris Energy"):
        row = _project(session, name="Childress", company=company, city="Childress", state="TX")
        row.blocks.append(
            CapacityBlock(block_key="horizon-1", label="Horizon 1", mw=50.0, status="planned")
        )
    session.flush()

    (pair,) = capex.suspected_duplicates(session)
    assert pair.shared_blocks == ("horizon-1",)
    assert "tranche" in pair.kinds


def test_a_pair_says_what_raised_it(session):
    """ "These two look similar" is not something a reader can check."""
    _project(session, name="Stargate Abilene", company="Crusoe", city="Abilene", mw_planned=1200)
    _project(session, name="Stargate", company="Crusoe/Oracle", city="Abilene", mw_planned=1200)

    (pair,) = capex.suspected_duplicates(session)
    assert "party" in pair.kinds
    assert "crusoe" in pair.why


def test_the_strongest_evidence_is_offered_first(session):
    """A shared tranche is two readings of one building; a shared word is a word."""
    from tracker.models import CapacityBlock

    for company in ("A Corp", "B Corp"):
        row = _project(session, name="Sweetwater", company=company, city="Sweetwater", state="TX")
        row.blocks.append(
            CapacityBlock(block_key="sweetwater-2", label="Sweetwater 2", mw=50.0, status="planned")
        )
    _project(session, name="Stargate Milam", company="SoftBank", city="Milam", mw_planned=700)
    _project(session, name="Stargate", company="OpenAI", city="Milam", mw_planned=700)
    session.flush()

    pairs = capex.suspected_duplicates(session)
    assert pairs[0].kinds[0] == "tranche"
    assert pairs[-1].kinds[0] == "name"


def test_cross_granularity_duplicates_are_found_across_localities(session):
    """The pairs the locality bucket structurally cannot see.

    `suspected_duplicates` buckets by `(city or county, state)` and compares only
    within a bucket — which is exactly what a cross-granularity duplicate is not.
    Hyperion was stored four times, as `richland parish`, `holly ridge`,
    `richland` and `richmond parish`, so the rows sat in four different buckets
    and no name or tranche evidence was ever consulted. `tracker duplicates`
    reported none of them, while the INGEST path had already written "possible
    duplicate of project #284" into one row's notes: two surfaces disagreeing,
    not a missing algorithm.
    """
    from tracker.capex import duplicate_groups, suspected_duplicates
    from tracker.models import Project

    rows = [
        Project(
            name="Hyperion",
            company="Meta",
            city="Richland Parish",
            county="Richland Parish",
            state="LA",
            dedup_key="meta|county:richland|LA",
        ),
        Project(
            name="Hyperion Data Center",
            company="Meta Platforms",
            city="Holly Ridge",
            county="Richland Parish",
            state="LA",
            dedup_key="meta|city:holly ridge|LA",
        ),
        Project(
            name="Meta Richland",
            company="Meta Platforms, Inc.",
            city="Richland",
            state="LA",
            dedup_key="meta|city:richland|LA",
        ),
    ]
    for row in rows:
        session.add(row)
    session.flush()

    pairs = suspected_duplicates(session)
    ids = {(p.a_id, p.b_id) for p in pairs}
    assert ids, "the group must be findable at all"

    # All three are one campus, so one decision rather than three.
    groups = duplicate_groups(pairs)
    assert any(len(g) == 3 for g in groups), groups

    # `identity` says the two rows describe one *place* at two granularities. It
    # used to lead `EVIDENCE_ORDER` on the strength of being structural rather than
    # textual, and that reading was wrong twice over: it is a weak statement about
    # which *building*, and no automated path can settle it — `dupresolve` refuses
    # granularity alone, so the report opened with 31 of its 49 pairs in the one
    # class nothing could act on. It now sorts below the signals that name a
    # building.
    identity = [p for p in pairs if "identity" in p.kinds]
    assert identity
    assert identity[0].rank > capex.EVIDENCE_ORDER.index("tranche")
    assert "granularity" in identity[0].why

    # And granularity no longer arrives alone. The second pass used to record the
    # key match and discard everything else it knew, so a pair like this one — three
    # spellings of Hyperion — carried one evidence class while plainly sharing a
    # name. Both are recorded now, which is what lets the rails tell "granularity
    # and nothing else" from "granularity, and they are also the same building".
    assert "hyperion" in identity[0].shared_tokens
    assert "name" in identity[0].kinds


def test_the_locality_signals_still_fire(session):
    """The union must not cost what the original pass found.

    Measured when this was written: the structural pass ALONE finds pairs the
    locality pass misses and loses 225 of the 230 it finds — it cannot see the
    same-locality/different-company case, which is the one `capex` needed this
    for. Abilene was stored four times, once per company attached to it.
    """
    from tracker.capex import suspected_duplicates
    from tracker.models import Project

    for company in ("Crusoe", "Oracle"):
        session.add(
            Project(
                name=f"Stargate Abilene ({company})",
                company=company,
                city="Abilene",
                state="TX",
                dedup_key=f"{company.lower()}|city:abilene|TX",
            )
        )
    session.flush()

    pairs = suspected_duplicates(session)
    assert any("name" in p.kinds or "party" in p.kinds for p in pairs), pairs


# --- detection: the cases the report could not see, and the ones it must not ---
#
# Every fixture below is a pair measured on the live database. The positives were
# invisible to the report; the negatives were false pairs it produced, or ones the
# change that found the positives would otherwise have introduced.


def _blocked(session, project, label, key, mw=70.0) -> None:
    from tracker.models import CapacityBlock

    session.add(
        CapacityBlock(project_id=project.id, label=label, block_key=key, mw=mw, status="planned")
    )
    session.flush()


def test_a_tranche_shared_across_two_granularities_is_found(session):
    """The flagship case, and it was invisible.

    Stargate is stored as Crusoe's `abilene` row and as Oracle's `shackelford
    county` row, both holding `county.shackelford`. `identifying_block_keys` used to
    discard any key appearing in more than one locality as vocabulary — and a
    cross-granularity duplicate is two localities by construction, so the evidence
    that would settle it was thrown away for being evidence.
    """
    a = _project(
        session,
        name="Stargate Abilene",
        company="Crusoe",
        city="Abilene",
        state="TX",
        dedup_key="crusoe|city:abilene|TX",
    )
    b = _project(
        session,
        name="Stargate - Shackelford County",
        company="Oracle",
        city=None,
        county="Shackelford",
        state="TX",
        dedup_key="oracle|county:shackelford|TX",
    )
    _blocked(session, a, "Shackelford County", "county.shackelford")
    _blocked(session, b, "Shackelford County", "county.shackelford")

    found = [p for p in capex.suspected_duplicates(session) if {a.id, b.id} == {p.a_id, p.b_id}]
    assert found, "a shared tranche across two granularities must raise a pair"
    assert "tranche" in found[0].kinds
    assert "county.shackelford" in found[0].shared_blocks


def test_the_same_company_and_name_twice_is_the_strongest_class(session):
    """Six pairs on the live database, every one reported under the weakest class.

    `distinctive_name_tokens` strips generic words and the locality, so two
    byte-identical names produced no name evidence at all and the pair rested on
    granularity — which `dupresolve` refuses to merge on.
    """
    a = _project(
        session,
        name="Stafford Technology Campus",
        company="STACK Infrastructure",
        city="Stafford",
        county="Stafford",
        state="VA",
        dedup_key="stack|city:stafford|VA",
    )
    b = _project(
        session,
        name="Stafford Technology Campus",
        company="STACK Infrastructure",
        city=None,
        county="Stafford",
        state="VA",
        dedup_key="stack|county:stafford|VA",
    )

    found = [p for p in capex.suspected_duplicates(session) if {a.id, b.id} == {p.a_id, p.b_id}]
    assert found and found[0].exact
    assert found[0].kinds[0] == "exact"
    assert found[0].rank == 0
    assert "same company and the same name" in found[0].why


def test_a_facility_number_identifies_a_building_inside_one_market(session):
    """`va-4` on two Ashburn rows is one building. This is the largest group on the
    live database — RagingWire and NTT under four names sharing `va-4`, `va-5` and
    `va-6` — and a rule that treated the code as vocabulary everywhere lost it."""
    a = _project(session, name="VA2 Data Center", company="RagingWire", city="Ashburn", state="VA")
    b = _project(session, name="Ashburn Campus", company="NTT", city="Ashburn", state="VA")
    _blocked(session, a, "VA4", "va-4")
    _blocked(session, b, "VA4", "va-4")

    found = [p for p in capex.suspected_duplicates(session) if {a.id, b.id} == {p.a_id, p.b_id}]
    assert found and "tranche" in found[0].kinds


def test_a_facility_number_across_two_markets_is_an_airport(session):
    """DataBank's `IAD3` in Ashburn and Aligned's Sterling campus share a key and an
    airport. Two operators, two buildings, sixty kilometres of Loudoun County."""
    a = _project(session, name="IAD3", company="DataBank", city="Ashburn", state="VA")
    b = _project(session, name="Aligned Sterling", company="Aligned", city="Sterling", state="VA")
    _blocked(session, a, "IAD3", "iad-3")
    _blocked(session, b, "IAD3", "iad-3")

    assert not [p for p in capex.suspected_duplicates(session) if {a.id, b.id} == {p.a_id, p.b_id}]


def test_a_tranche_named_after_the_town_pairs_nothing(session):
    """`austin` is a tranche label on Switch's Austin campus and on Sabey's in Round
    Rock. One metro, two buildings, and the locality is never distinctive."""
    a = _project(
        session, name="Austin Data Center Campus", company="Switch", city="Austin", state="TX"
    )
    b = _project(session, name="Round Rock Campus", company="Sabey", city="Round Rock", state="TX")
    _blocked(session, a, "Austin", "austin")
    _blocked(session, b, "Austin", "austin")

    assert not [p for p in capex.suspected_duplicates(session) if {a.id, b.id} == {p.a_id, p.b_id}]


def test_a_tranche_made_of_type_words_pairs_nothing(session):
    """`capacity-1` names a kind of tranche. `blocks.generic` cannot catch it,
    because it reads the label's own words and "Capacity 1" looks specific."""
    a = _project(
        session, name="Project Merlin", company="Galaxy Digital", city="McGregor", state="TX"
    )
    b = _project(
        session, name="Helios II", company="Galaxy Digital", city=None, county="Dickens", state="TX"
    )
    _blocked(session, a, "Capacity 1", "capacity-1")
    _blocked(session, b, "Capacity 1", "capacity-1")

    found = [p for p in capex.suspected_duplicates(session) if {a.id, b.id} == {p.a_id, p.b_id}]
    assert not [p for p in found if "tranche" in p.kinds]


def test_two_spellings_of_one_company_are_not_party_evidence(session):
    """`party` carries an unattended merge, so it has to mean something.

    The second pass buckets rows by company, so `shares_a_party` is true of every
    pair it produces. Recorded as evidence, that would have offered to fold NTT's
    Itasca campus into NTT's Chicago one, 31.7 km away.
    """
    a = _project(
        session,
        name="Chicago Data Center",
        company="NTT",
        city="Itasca",
        county="DuPage",
        state="IL",
    )
    b = _project(
        session,
        name="Chicago Campus",
        company="NTT",
        city="Chicago",
        county="DuPage",
        state="IL",
    )

    for pair in capex.suspected_duplicates(session):
        if {a.id, b.id} == {pair.a_id, pair.b_id}:
            assert "party" not in pair.kinds
            assert not pair.shared_parties
