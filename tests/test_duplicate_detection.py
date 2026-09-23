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
