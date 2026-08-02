"""Matching a typed name to a campus, and the asymmetry that shapes it.

A wrong "no match" makes a duplicate row, which `tracker duplicates` finds and
`tracker merge` fixes. A wrong "match" folds a real campus into another project's
history, and nothing detects that. So every uncertain path here leans to "no".
"""

from __future__ import annotations

import pytest

from tracker import point
from tracker.models import Project


def _project(session, name: str, company: str, city: str = "Abilene", state: str = "TX") -> Project:
    row = Project(
        name=name,
        company=company,
        city=city,
        state=state,
        dedup_key=f"{company}|{name}|{city}",
        phase="construction",
        confidence=2,
    )
    session.add(row)
    session.flush()
    return row


class _Answer:
    def __init__(self, body: str) -> None:
        self._body = body

    def complete(self, *, system, user, max_tokens):
        self.user = user

        class R:
            text = self._body
            model = "test"

        return R()


# --- the prefilter ---------------------------------------------------------------


def test_generic_words_carry_no_identity():
    """Every one of them is a "data center"; matching on that matches everything."""
    assert point.tokens("Stargate Abilene Data Center Campus") == {"stargate", "abilene"}
    assert point.tokens("The Data Center Project") == frozenset()
    # Digits are kept — COL4 and COL5 are different buildings.
    assert "col4" in point.tokens("Cologix COL4")


def test_the_shortlist_finds_the_row_however_it_is_spelled(session):
    wanted = _project(session, "Stargate Abilene", "Crusoe")
    _project(session, "Prometheus", "Meta", city="New Albany", state="OH")

    found = point.shortlist(session, "Stargate Abilene")
    assert found[0].project_id == wanted.id
    assert [c.project_id for c in found] == [wanted.id], "an unrelated campus must not appear"


def test_a_name_sharing_nothing_shortlists_nothing(session):
    _project(session, "Prometheus", "Meta", city="New Albany", state="OH")
    assert point.shortlist(session, "Nautilus Stockton Floating") == []


# --- the model's answer ------------------------------------------------------------


def test_a_confident_match_from_the_shortlist_is_taken(session):
    row = _project(session, "Stargate Abilene", "Crusoe")
    candidates = point.shortlist(session, "Stargate Abilene")
    answer = _Answer(f'{{"project_id": {row.id}, "confidence": 0.97, "reason": "same town"}}')

    match = point.identify("Stargate Abilene", candidates, extractor=answer)
    assert match.matched and match.project_id == row.id


def test_an_id_that_was_never_offered_is_refused(session):
    """The one failure that would silently attach a name to an unrelated row."""
    _project(session, "Stargate Abilene", "Crusoe")
    candidates = point.shortlist(session, "Stargate Abilene")
    match = point.identify(
        "Stargate Abilene",
        candidates,
        extractor=_Answer('{"project_id": 9999, "confidence": 0.99, "reason": "x"}'),
    )
    assert not match.matched
    assert "not on the shortlist" in match.rejected


def test_a_hedged_match_becomes_no_match(session):
    """Below the floor it routes to building a new row — the recoverable mistake."""
    row = _project(session, "Stargate Abilene", "Crusoe")
    candidates = point.shortlist(session, "Stargate Abilene")
    match = point.identify(
        "Stargate Abilene",
        candidates,
        extractor=_Answer(f'{{"project_id": {row.id}, "confidence": 0.5, "reason": "maybe"}}'),
    )
    assert not match.matched
    assert "below the" in match.rejected


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        '{"project_id": "banana", "confidence": 0.9, "reason": "x"}',
        '{"project_id": null, "confidence": 0.9, "reason": "different town"}',
    ],
)
def test_every_uncertain_answer_leans_to_building_a_new_row(session, body):
    _project(session, "Stargate Abilene", "Crusoe")
    candidates = point.shortlist(session, "Stargate Abilene")
    assert not point.identify("x", candidates, extractor=_Answer(body)).matched


def test_no_shortlist_means_no_call_at_all(session):
    """Nothing shares a word, so there is nothing to ask about. Saves a call."""

    class Explode:
        def complete(self, **_kw):
            raise AssertionError("must not call the model with an empty shortlist")

    match = point.identify("Nautilus Stockton", [], extractor=Explode())
    assert not match.matched and match.confidence == 1.0


def test_the_model_sees_every_candidate_and_their_locations(session):
    """Place is what separates two campuses of one operator, so it must be shown."""
    a = _project(session, "Stargate Milam County", "OpenAI", city="Milam", state="TX")
    b = _project(session, "Stargate Lordstown", "OpenAI", city="Lordstown", state="OH")
    candidates = point.shortlist(session, "Stargate")
    answer = _Answer('{"project_id": null, "confidence": 0.9, "reason": "x"}')
    point.identify("Stargate", candidates, extractor=answer)

    assert f"  {a.id}  " in answer.user and "Milam, TX" in answer.user
    assert f"  {b.id}  " in answer.user and "Lordstown, OH" in answer.user


# --- the search side ----------------------------------------------------------------


def test_the_queries_name_the_campus_rather_than_the_sector():
    queries = point.queries_for("Nautilus Stockton")
    assert all('"Nautilus Stockton"' in q for q in queries), "quoted, or it matches anything"
    assert any("MW" in q or "megawatt" in q for q in queries)
    assert any("permit" in q or "interconnection" in q for q in queries)
