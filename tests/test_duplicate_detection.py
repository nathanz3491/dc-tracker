"""Duplicates the three place-and-tranche passes could not see.

Measured on a production copy: 22 pairs that were never reported, every one a real
duplicate on inspection — Stargate Michigan stored six times with two rows spelling
Saline as "Salien", Project Jupiter under "Doña Ana" and "Doña Ana County", xAI's
Colossus under `Memphis` and `孟菲斯`, Yondr under "Loudoun and Prince William" and
"... counties".
"""

from __future__ import annotations

import json

from tracker.capex import _locality_word, _one_edit_apart, suspected_duplicates
from tracker.models import Project, Source


def _row(session, **kw) -> Project:
    defaults = {"phase": "construction", "confidence": 1}
    project = Project(**{**defaults, **kw})
    session.add(project)
    session.flush()
    return project


def _ids(session) -> set[tuple[int, int]]:
    return {(p.a_id, p.b_id) for p in suspected_duplicates(session)}


def test_one_swapped_letter_is_still_the_town():
    assert _one_edit_apart("salien", "saline")
    assert _one_edit_apart("abilene", "abilen")
    assert _one_edit_apart("memphis", "memphix")
    assert not _one_edit_apart("saline", "salem")
    assert not _one_edit_apart("austin", "boston")


def test_kind_words_and_accents_do_not_make_a_second_place():
    fold = [
        _locality_word(Project(county="Doña Ana")),
        _locality_word(Project(county="Doña Ana County")),
    ]
    assert fold == ["dona ana", "dona ana"]
    assert _locality_word(Project(county="Loudoun and Prince William counties")) == (
        "loudoun and prince william"
    )
    assert _locality_word(Project(city="Saline Township")) == "saline"


def test_a_misspelt_town_is_compared_with_the_real_one(session):
    """#122 and #237: one company, one name, "Salien" against "Saline"."""
    a = _row(
        session,
        company="OpenAI, Oracle",
        name="OpenAI Oracle Michigan Data Center",
        city="Salien",
        state="MI",
        dedup_key="a",
    )
    b = _row(
        session,
        company="OpenAI, Oracle",
        name="OpenAI-Oracle Michigan Data Center",
        city="Saline",
        state="MI",
        dedup_key="b",
    )
    assert (a.id, b.id) in _ids(session)


def test_a_county_spelt_two_ways_is_one_bucket(session):
    a = _row(
        session,
        company="Yondr Group",
        name="Yondr Northern Virginia",
        county="Loudoun and Prince William",
        state="VA",
        dedup_key="a",
    )
    b = _row(
        session,
        company="Yondr Group",
        name="Yondr Northern Virginia",
        county="Loudoun and Prince William counties",
        state="VA",
        dedup_key="b",
    )
    assert (a.id, b.id) in _ids(session)


def test_one_name_in_one_state_is_raised_whatever_the_place_says(session):
    """Colossus as `Memphis` and as `孟菲斯`: no place in common, no tranche."""
    a = _row(session, company="xAI", name="Colossus", city="Memphis", state="TN", dedup_key="a")
    b = _row(session, company="xAI", name="Colossus", city="孟菲斯", state="TN", dedup_key="b")
    pairs = {(p.a_id, p.b_id): p for p in suspected_duplicates(session)}
    assert (a.id, b.id) in pairs
    assert pairs[(a.id, b.id)].kinds[0] == "exact"


def test_a_name_that_is_only_its_town_raises_nothing(session):
    """Two operators' "Santa Clara Data Center" in Santa Clara name no site at all, and
    a false pair would hold a real campus out of the buyer table."""
    _row(
        session,
        company="Digital Realty",
        name="Santa Clara Data Center",
        city="Santa Clara",
        state="CA",
        dedup_key="a",
    )
    _row(
        session,
        company="STACK Infrastructure",
        name="Santa Clara Data Center",
        city="Santa Clara",
        state="CA",
        dedup_key="b",
    )
    _row(
        session, company="Meta", name="Texas Data Center", city="Temple", state="TX", dedup_key="c"
    )
    _row(
        session,
        company="Google",
        name="Texas Data Center",
        city="Midlothian",
        state="TX",
        dedup_key="d",
    )
    assert _ids(session) == set()


def test_the_same_name_in_another_state_is_another_campus(session):
    _row(
        session,
        company="Applied Digital",
        name="Polaris Forge 1",
        city="Ellendale",
        state="ND",
        dedup_key="a",
    )
    _row(
        session,
        company="Applied Digital",
        name="Polaris Forge 1",
        city="Harwood",
        state="SD",
        dedup_key="b",
    )
    assert _ids(session) == set()


def test_a_parked_pair_stays_parked(session):
    from tracker import pairs as pairs_mod

    a = _row(session, company="xAI", name="Colossus", city="Memphis", state="TN", dedup_key="a")
    b = _row(session, company="xAI", name="Colossus", city="Southaven", state="TN", dedup_key="b")
    session.add(
        Source(
            project_id=a.id,
            url="https://example.test/a",
            source_type="trade_press",
            claims=json.dumps({"mw_planned": 150.0}),
        )
    )
    session.flush()
    pairs_mod.park(session, [a.id, b.id], reason="two halls, ruled apart", by="operator")
    assert _ids(session) == set()


# --- one question per new row, wherever its twin is filed ----------------------
#
# On 2026-10-05 the night's new row "SoftBank / SB Energy — PORTS Technology Campus"
# in Piketon sat beside "SB Energy — PORTS-Pike Technology Campus" in Pike County,
# the same 10 GW site. Neither pass compares a town with another company's county.
#
# Since 2026-10-21 "SoftBank / SB Energy" is an alias of SB Energy's company, so that
# exact pair is one company and the regular passes catch it. The twin pass is still
# what pairs a joint name with one of its parties, so the fixture keeps one.


def _ports(session, *, joint="Mitsui / SB Energy") -> tuple[Project, Project]:
    old = _row(
        session,
        name="PORTS-Pike Technology Campus",
        company="SB Energy",
        county="Pike County",
        state="OH",
        dedup_key="sb energy|county:pike|OH",
    )
    new = _row(
        session,
        name="PORTS Technology Campus (Piketon)",
        company=joint,
        city="Piketon",
        county="Pike County",
        state="OH",
        dedup_key=f"{joint.lower().replace(' / ', ' ')}|city:piketon|OH",
    )
    return old, new


def test_a_new_row_is_paired_with_its_twin_under_another_town_and_company(session):
    """Since 2026-10-30 the county pass raises this pair too — one county, a shared
    name word — and with it the party the two companies share."""
    from tracker.capex import twin_pairs

    old, new = _ports(session)
    regular = _pair(session, old, new)
    assert {"county", "party"} <= set(regular.kinds)
    assert regular.holds_rows_out, "a shared party is evidence of its own"

    (pair,) = twin_pairs(session, [new.id])
    assert (pair.a_id, pair.b_id) == (old.id, new.id)
    assert "party" in pair.kinds, "a shared operator is evidence a merge may rest on"


def test_the_original_ports_pair_is_now_one_company_to_the_regular_passes(session):
    old, new = _ports(session, joint="SoftBank / SB Energy")
    assert (old.id, new.id) in _ids(session)


def test_a_shared_place_alone_pairs_nothing(session):
    """Two developers in one borough are two sites until something else says so."""
    from tracker.capex import twin_pairs

    _row(
        session,
        name="Red Hook Hub",
        company="Charter",
        city="Brooklyn",
        state="NY",
        dedup_key="charter|city:brooklyn|NY",
    )
    new = _row(
        session,
        name="Industry City Expansion",
        company="DataVerge",
        city="Brooklyn",
        state="NY",
        dedup_key="dataverge|city:brooklyn|NY",
    )
    assert twin_pairs(session, [new.id]) == []


def test_a_pair_already_ruled_out_is_not_asked_again(session):
    from tracker import pairs
    from tracker.capex import twin_pairs

    old, new = _ports(session)
    pairs.park(session, [old.id, new.id], reason="different sites", by="operator")
    assert twin_pairs(session, [new.id]) == []


# --- one county, two towns, two companies --------------------------------------------


def _jupiter(session):
    """Project Jupiter on 2026-10-30: Oracle's row under Las Cruces, BorderPlex's under
    Santa Teresa, the same 2,450 MW and $165B in Doña Ana County, never compared."""
    oracle = _row(
        session,
        company="Oracle",
        name="Stargate - Doña Ana County (Project Jupiter)",
        city="Las Cruces",
        county="Doña Ana County",
        state="NM",
        dedup_key="oracle|city:las cruces|NM",
        mw_planned=2450.0,
        investment_usd=165_000_000_000,
    )
    borderplex = _row(
        session,
        company="BorderPlex Digital Assets",
        name="Project Jupiter",
        city="Santa Teresa",
        county="Doña Ana County",
        state="NM",
        dedup_key="borderplex|city:santa teresa|NM",
        mw_planned=2450.0,
        investment_usd=165_000_000_000,
    )
    return oracle, borderplex


def _pair(session, a, b):
    return next(p for p in suspected_duplicates(session) if {p.a_id, p.b_id} == {a.id, b.id})


def test_one_county_two_towns_two_companies_and_the_same_figures(session):
    oracle, borderplex = _jupiter(session)
    pair = _pair(session, oracle, borderplex)
    assert "county" in pair.kinds
    assert "Doña Ana County" in pair.why
    assert "$165.0B" in pair.why and "2,450 MW" in pair.why


def test_it_holds_no_row_out_of_the_totals_until_judged(session):
    """A county is a large place: 50 of the 55 pairs this pass raised on the 10-30
    copy rested on it and a figure or a word, and a false pair hides a real campus
    from capex."""
    from tracker.capex import rollup

    oracle, borderplex = _jupiter(session)
    assert not _pair(session, oracle, borderplex).holds_rows_out
    counted = {pid for position in rollup(session) for pid in position.project_ids}
    assert {oracle.id, borderplex.id} <= counted


def test_a_county_text_naming_two_counties_counts_as_each(session):
    """Berry Hill: one row's county read "Danville and Pittsylvania"."""
    a = _row(
        session,
        company="Stack Infrastructure",
        name="Berry Hill campus",
        county="Danville and Pittsylvania",
        state="VA",
        dedup_key="stack|county:danville and pittsylvania|VA",
        investment_usd=100_000_000_000,
    )
    b = _row(
        session,
        company="Stack Infrastructure",
        name="Berry Hill",
        city="Danville",
        county="Pittsylvania County",
        state="VA",
        dedup_key="stack|city:danville|VA",
        investment_usd=100_000_000_000,
        mw_planned=3000.0,
    )
    assert "county" in _pair(session, a, b).kinds


def test_one_county_and_nothing_else_is_not_a_pair(session):
    _row(
        session,
        company="Google",
        name="Google Council Bluffs",
        city="Council Bluffs",
        county="Pottawattamie County",
        state="IA",
        dedup_key="google|city:council bluffs|IA",
        mw_planned=300.0,
        investment_usd=1_000_000_000,
    )
    _row(
        session,
        company="Meta",
        name="Meta Carter Lake",
        city="Carter Lake",
        county="Pottawattamie County",
        state="IA",
        dedup_key="meta|city:carter lake|IA",
        mw_planned=200.0,
        investment_usd=800_000_000,
    )
    assert _ids(session) == set()


def test_only_a_judge_that_read_the_articles_may_merge_on_a_county(session):
    from tracker.dupresolve import evidence_blocks_merge

    oracle, borderplex = _jupiter(session)
    pair = _pair(session, oracle, borderplex)
    if set(pair.kinds) & {"exact", "tranche", "party"}:
        return  # hard evidence of its own; the rail under test does not arise
    refused = evidence_blocks_merge(pair, oracle, borderplex, judge_read_the_sources=False)
    assert refused and "county" in refused
    assert evidence_blocks_merge(pair, oracle, borderplex, judge_read_the_sources=True) is None
