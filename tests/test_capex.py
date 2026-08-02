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
    _project(session, name="Live", company="Meta", mw_planned=100)
    _project(session, name="Dead", company="Meta", phase="cancelled", mw_planned=900)

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
        mw_planned=700,
        expected_online=dt.date(2028, 6, 1),
    )
    _project(session, name="C", company="Meta", mw_planned=50)  # no date, no year bucket

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
    assert capex.blocking_risk(session, "meta") == "transmission/material"


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
