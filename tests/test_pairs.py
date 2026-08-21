"""Recording that two rows are *not* one campus.

`tracker duplicates` proposes and never merges, which left the report with only
one possible answer. A pair that was simply wrong came back on every run — and,
because `capex.rollup` reads the same pairs and sets one row of each group aside,
it also kept a real campus's capacity out of the buyer table. These tests pin both
halves: the report forgets a parked pair, and so does the rollup.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker import capex, pairs
from tracker.models import NotDuplicate, Project


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


def _two_that_pair(session) -> tuple[Project, Project]:
    a = _project(session, name="Stargate Abilene", company="Crusoe", mw_planned=1200)
    b = _project(session, name="Stargate", company="Oracle", mw_planned=1200)
    assert capex.suspected_duplicates(session), "fixture must actually pair"
    return a, b


def test_parking_a_pair_removes_it_from_the_report(session):
    a, b = _two_that_pair(session)
    pairs.park(session, [a.id, b.id], reason="different buildings on one street")
    assert capex.suspected_duplicates(session) == []


def test_parking_reaches_the_capex_rollup_too(session):
    """The reason parking exists rather than a `--hide` flag on the report.

    `rollup` counts one row per suspected group and discloses the rest, so a false
    pair silently removes a real campus from the buyer table. Parking has to put it
    back, and it does because both read `suspected_duplicates`.
    """
    a, b = _two_that_pair(session)
    before = {p.key: p.projects for p in capex.rollup(session)}
    pairs.park(session, [a.id, b.id])
    after = {p.key: p.projects for p in capex.rollup(session)}
    assert sum(after.values()) > sum(before.values())


def test_a_parked_pair_is_still_visible_when_asked_for(session):
    a, b = _two_that_pair(session)
    pairs.park(session, [a.id, b.id])
    assert capex.suspected_duplicates(session, include_parked=True)


def test_parking_reaches_the_ingest_path_too(session):
    """The third reader, and the one that was overruling the decision.

    Only the *reports* consulted `not_duplicate`. `upsert._find_duplicate_candidate`
    did not, so the next crawl of either row rewrote the derived "possible duplicate
    of project #N" warning and re-capped confidence at 1 — the operator's recorded
    decision quietly reversed, for as long as the row kept being read.
    """
    from tracker.ingest.records import IngestRecord, SourceRecord
    from tracker.upsert import upsert_record

    a, b = _two_that_pair(session)
    pairs.park(session, [a.id, b.id], reason="different buildings on one street")
    session.commit()

    result = upsert_record(
        session,
        IngestRecord(
            project={
                "company": a.company,
                "name": a.name,
                "city": a.city,
                "state": a.state,
                "mw_planned": 1200.0,
            },
            sources=[
                SourceRecord(
                    url="https://a.test/recrawl",
                    source_type="trade_press",
                    excerpt="excerpt",
                    claims={"mw_planned": 1200.0},
                    quotes={"mw_planned": "1200 MW"},
                    fetched_at=dt.datetime(2026, 1, 1),
                )
            ],
        ),
    )
    assert result.duplicate_of is None, "a rejected pair must not be re-proposed"
    row = session.get(Project, result.project_id)
    assert "possible duplicate" not in (row.notes or "")


def test_parking_is_pairwise_so_a_new_row_is_still_asked_about(session):
    """Three rows parked as distinct says nothing about a fourth.

    A group is a closure computed at read time. Storing "these three are separate"
    as a group would suppress a pairing with a row that did not exist yet, which is
    a decision nobody made.
    """
    a = _project(session, name="Stargate Abilene", company="Crusoe", mw_planned=1200)
    b = _project(session, name="Stargate", company="Oracle", mw_planned=1200)
    pairs.park(session, [a.id, b.id])
    c = _project(session, name="Stargate Abilene", company="OpenAI", mw_planned=1200)

    reported = {(p.a_id, p.b_id) for p in capex.suspected_duplicates(session)}
    assert (a.id, b.id) not in reported
    assert (a.id, c.id) in reported or (b.id, c.id) in reported


def test_parking_three_ids_records_every_pair_among_them(session):
    a = _project(session, name="One", company="A")
    b = _project(session, name="Two", company="B")
    c = _project(session, name="Three", company="C")
    written = pairs.park(session, [c.id, a.id, b.id])
    assert len(written) == 3
    assert all(x < y for x, y in written), "stored in one canonical order"


def test_re_parking_keeps_the_first_decision(session):
    """An operator's judgement is not overwritten by a model re-running later."""
    a, b = _two_that_pair(session)
    pairs.park(session, [a.id, b.id], reason="checked the address", by="operator")
    again = pairs.park(session, [a.id, b.id], reason="looks distinct", by="model (0.7)")
    assert again == []
    row = session.query(NotDuplicate).one()
    assert (row.decided_by, row.reason) == ("operator", "checked the address")


def test_unparking_puts_the_question_back(session):
    a, b = _two_that_pair(session)
    pairs.park(session, [a.id, b.id])
    assert pairs.unpark(session, [b.id, a.id]) == [(min(a.id, b.id), max(a.id, b.id))]
    assert capex.suspected_duplicates(session)


def test_parking_an_unknown_id_says_which_one(session):
    a = _project(session, name="One", company="A")
    with pytest.raises(pairs.UnknownProject, match="9999"):
        pairs.park(session, [a.id, 9999])


def test_parking_needs_two_ids(session):
    a = _project(session, name="One", company="A")
    with pytest.raises(ValueError, match="at least two"):
        pairs.park(session, [a.id])


def test_the_listing_names_both_rows_and_who_decided(session):
    a, b = _two_that_pair(session)
    pairs.park(session, [a.id, b.id], reason="two operators, two buildings")
    (entry,) = pairs.listing(session)
    assert "Crusoe" in entry.a_label and "Oracle" in entry.b_label
    assert entry.decided_by == "operator"
    assert entry.reason == "two operators, two buildings"


def test_deleting_a_project_takes_its_parked_pairs_with_it(session):
    """A merge makes the question moot, and a dangling pair would suppress a
    future pairing involving a recycled id."""
    a, b = _two_that_pair(session)
    pairs.park(session, [a.id, b.id])
    session.delete(b)
    session.flush()
    assert session.query(NotDuplicate).count() == 0
