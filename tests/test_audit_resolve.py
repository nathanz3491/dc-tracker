"""Settling an implausible figure, and how far down the ladder it has to go.

`tracker audit` was only ever half a tool: it reported the same 11,250 MW
colocation expansion on every run since it was written, because the only repair it
could offer was a sentence telling somebody to go and read an article.

The ladder is five rungs and each one runs only because the one above declined —
arithmetic, the operator, a model on the row, a web search, the model again. These
tests pin the *order*, because the order is the cost control: rungs one and two are
free, and a run that skipped to rung three would quietly spend a call on every
finding a label already answers.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker import audit
from tracker.models import Project, Source


def _project(session, **kwargs) -> Project:
    defaults = {
        "name": "Campus",
        "company": "Someone",
        "state": "CO",
        "city": "Englewood",
        "dedup_key": f"k{kwargs.get('name', 'x')}",
        "phase": "construction",
        "confidence": 2,
    }
    defaults.update(kwargs)
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


def _block(session, project, label, mw, status="planned"):
    from tracker.blocks import block_key
    from tracker.models import CapacityBlock

    key = block_key(label)
    session.add(
        CapacityBlock(
            project_id=project.id,
            label=label,
            block_key=key.value,
            generic=key.generic,
            mw=mw,
            status=status,
        )
    )
    session.flush()


def _out_of_scale(session, *, label: str, mw: float, campus: float = 18.0):
    """A project whose tranche is larger than the whole site, and its finding."""
    project = _project(session, name=f"Site {label}", mw_planned=campus)
    _block(session, project, label, mw)
    session.refresh(project)
    findings = [f for f in audit.check_project(project) if f.code == "block_out_of_scale"]
    assert findings, "fixture must produce the finding under test"
    return project, findings[0]


class _Model:
    """A reasoning model that answers with whatever it was constructed holding."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, *, system, user, max_tokens):
        self.prompts.append(user)
        reply = self.replies.pop(0) if self.replies else '{"key": "s", "confidence": 0}'

        class R:
            text = reply
            model = "test-model"

        return R()


# --- rung 1: the answers that are arithmetic ---------------------------------


def test_a_tranche_whose_label_states_its_own_capacity_is_settled_for_free(session):
    """The one shape where the right figure is written down beside the wrong one.

    "2.4 MW Lease" carrying 2400 on a 15 MW campus is 2.4 MW recorded in
    kilowatts. Nothing is being judged — the label says the number — so this rung
    costs nothing and needs no model.
    """
    project, finding = _out_of_scale(session, label="2.4 MW Lease", mw=2400.0, campus=15.0)
    got = audit.resolve_one(session, project, finding)

    assert got.stage == "arithmetic"
    assert got.acted
    assert project.blocks[0].mw == pytest.approx(2.4)
    assert "thousand times" in got.reason


def test_a_tranche_the_label_does_not_explain_is_left_to_the_ladder(session):
    """No arithmetic without a stated figure to check the stored one against."""
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    assert audit.free_answer(project, finding) is None


# --- rung 2: the person at the keyboard --------------------------------------


def test_the_operator_is_asked_before_any_model_is(session):
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    model = _Model()
    got = audit.resolve_one(session, project, finding, extractor=model, ask=lambda *_: "c")

    assert got.stage == "operator"
    assert project.blocks[0].mw is None
    assert model.prompts == [], "the model must not be asked what a person answered"


def test_a_shrug_at_the_keyboard_hands_the_question_down(session):
    """Not knowing is the commonest honest answer and must cost one keystroke."""
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    model = _Model('{"key": "c", "confidence": 0.8, "reason": "no quote anywhere"}')
    got = audit.resolve_one(session, project, finding, extractor=model, ask=lambda *_: None)

    assert got.stage == "model"
    assert model.prompts, "the model was asked"


def test_a_skip_at_the_keyboard_stops_there(session):
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    model = _Model()
    got = audit.resolve_one(session, project, finding, extractor=model, ask=lambda *_: "s")

    assert not got.acted
    assert model.prompts == []


# --- rung 3: a model, and the things it may not do ---------------------------


def test_a_model_below_the_confidence_floor_changes_nothing(session):
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    model = _Model('{"key": "c", "confidence": 0.4, "reason": "probably"}')
    got = audit.resolve_one(session, project, finding, extractor=model)

    assert not got.acted
    assert "0.40" in got.note
    assert project.blocks[0].mw == 2250.0


def test_an_option_that_was_not_offered_is_refused(session):
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    model = _Model('{"key": "z", "confidence": 0.99, "reason": "trust me"}')
    got = audit.resolve_one(session, project, finding, extractor=model)

    assert not got.acted and "not offered" in got.note


def test_an_answer_with_no_reason_is_unusable(session):
    """The reason is stored on the project and read by people later."""
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    model = _Model('{"key": "c", "confidence": 0.9}')
    got = audit.resolve_one(session, project, finding, extractor=model)

    assert not got.acted and "no reason" in got.note


# --- rungs 4 and 5: going and looking ----------------------------------------


def test_needing_more_evidence_sends_the_question_to_the_web(session, monkeypatch):
    """ "The row does not contain the answer" is a useful reply, not a failure.

    It is the whole reason this is a ladder rather than one call — the alternative
    is forcing a guess out of a row that has nothing in it.
    """
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    searched = audit.Searched(
        queries=["q"],
        urls=["https://example.test/a"],
        passages=["FROM https://example.test/a\nThe Phase 1 expansion adds 2.25 MW."],
    )
    monkeypatch.setattr(audit, "find_online", lambda *a, **k: searched)

    model = _Model(
        '{"key": "m", "confidence": 0.2, "reason": "nothing here states it"}',
        '{"key": "k", "confidence": 0.9, "reason": "the article says 2.25 MW"}',
    )
    got = audit.resolve_one(session, project, finding, extractor=model)

    assert got.stage == "model-after-search"
    assert project.blocks[0].mw == pytest.approx(2.25)
    assert "2.25 MW" in model.prompts[1], "what the search found reaches the second call"


def test_a_search_that_finds_nothing_leaves_the_row_alone(session, monkeypatch):
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    monkeypatch.setattr(
        audit, "find_online", lambda *a, **k: audit.Searched(error="no usable search results")
    )
    model = _Model('{"key": "m", "confidence": 0.2, "reason": "not in the row"}')
    got = audit.resolve_one(session, project, finding, extractor=model)

    assert not got.acted
    assert "no usable search results" in got.note
    assert project.blocks[0].mw == 2250.0


def test_searching_is_skipped_when_it_is_turned_off(session, monkeypatch):
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    monkeypatch.setattr(audit, "find_online", lambda *a, **k: pytest.fail("must not search"))
    model = _Model('{"key": "m", "confidence": 0.2, "reason": "not in the row"}')

    assert not audit.resolve_one(
        session, project, finding, extractor=model, allow_search=False
    ).acted


# --- what the record says afterwards -----------------------------------------


def test_every_edit_names_who_made_it(session):
    """A row edited by a model and one edited by a person who read the sources are
    different things, and six months later the note is the only place that
    difference survives."""
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    model = _Model('{"key": "c", "confidence": 0.9, "reason": "nothing cites it"}')
    audit.resolve_one(session, project, finding, extractor=model)

    assert "model (0.90) resolved `block_out_of_scale`" in project.notes
    assert "operator resolved" not in project.notes


def test_a_settled_finding_is_remembered_across_runs(session):
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    audit.resolve_one(session, project, finding, ask=lambda *_: "d")
    assert "block_out_of_scale" in audit.settled_codes(project)


def test_a_reverted_repair_re_opens_the_question(session):
    """The defect that hid the worst row in the database behind its own repair.

    Every action here writes a project scalar or a block row, and both are caches
    that `recompute_from_sources` and `recompute_blocks` re-derive from the claim
    set. So an answered question can come undone — and answering by code alone then
    muzzles the detector on exactly the row where it was most recently right.

    Observed on Hyperion (#10): a model cleared `mw_planned` 13,620 as uncited,
    `blocks.reconcile` raised it back from the tranche sum, and
    `campus_exceeds_worlds_largest` was skipped from then on.
    """
    project = _project(session, name="Reverted", mw_planned=13620.0)
    finding = next(
        f for f in audit.check_project(project) if f.code == "campus_exceeds_worlds_largest"
    )
    audit.resolve_one(session, project, finding, ask=lambda *_: "c")

    assert project.mw_planned is None, "the action clears the figure"
    assert "campus_exceeds_worlds_largest" in audit.settled_codes(project)

    # Whatever put it back — a rollup, a recompute, a hand edit — the question is
    # open again, because the row no longer holds the value the decision recorded.
    project.mw_planned = 14462.0
    settled = audit.settled_codes(project)
    assert "campus_exceeds_worlds_largest" not in settled
    open_now = [f.code for f in audit.check_project(project) if f.code not in settled]
    assert "campus_exceeds_worlds_largest" in open_now


def test_a_dismissal_survives_a_value_changing_under_it(session):
    """A dismissal is a judgement, not an edit, so nothing can revert it.

    The counterpart to the test above, and the reason the check is not simply "does
    the finding still fire": an operator who has said the figure is right is
    entitled to stop being asked, even as the figure moves.
    """
    project = _project(session, name="Genuinely huge", mw_planned=9000.0)
    finding = next(
        f for f in audit.check_project(project) if f.code == "campus_exceeds_worlds_largest"
    )
    audit.resolve_one(session, project, finding, ask=lambda *_: "d")
    assert "campus_exceeds_worlds_largest" in audit.settled_codes(project)

    project.mw_planned = 9500.0
    assert "campus_exceeds_worlds_largest" in audit.settled_codes(project)


def test_a_code_carrying_a_digit_round_trips(session):
    """The regex used to be `[a-z_]+`, so such a code would never mark settled.

    No code contains a digit today, which is exactly why this was free to fix and
    would have been undetectable later — the failure is silent and only in the skip
    path.
    """
    project = _project(session, name="Digits")
    audit.record(project, "rule_42-b", "left as it stands", by="operator")
    assert "rule_42-b" in audit.settled_codes(project)


def test_dismissing_writes_nothing_but_the_decision(session):
    """Some projects genuinely are enormous. Saying so has to be an answer."""
    project = _project(session, name="Huge", mw_planned=9000.0)
    finding = next(
        f for f in audit.check_project(project) if f.code == "campus_exceeds_worlds_largest"
    )
    audit.resolve_one(session, project, finding, ask=lambda *_: "d")

    assert project.mw_planned == 9000.0
    assert "campus_exceeds_worlds_largest" in audit.settled_codes(project)


def test_with_no_model_and_no_keyboard_nothing_is_invented(session):
    project, finding = _out_of_scale(session, label="Phase 1", mw=2250.0)
    got = audit.resolve_one(session, project, finding)

    assert not got.acted
    assert "no model was configured" in got.note


# --- what the model is shown --------------------------------------------------


def test_the_evidence_block_shows_a_claim_with_no_quote_as_such(session):
    """The commonest thing that decides one of these is which figure is quoted."""
    project = _project(session, name="Englewood", mw_planned=11250.0)
    session.add(
        Source(
            project_id=project.id,
            url="https://example.test/a",
            source_type="trade_press",
            fetched_at=dt.datetime(2026, 1, 1),
            claims=json.dumps({"mw_planned": 11250.0}),
        )
    )
    session.flush()
    session.refresh(project)

    text = audit.evidence_block(project, audit.check_project(project)[0])
    assert "no quote" in text
    assert "11250" in text


def test_a_passage_is_kept_only_when_it_carries_a_figure_and_names_the_site(session):
    """Four fetched pages of industry prose would crowd out the sentence that matters."""
    project = _project(session, name="Englewood", company="Flexential", city="Englewood")
    article = (
        "The data center industry grew again this year.\n"
        "Flexential said the Englewood expansion will add 11.25 MW of capacity.\n"
        "Analysts expect more growth in the coming quarters and beyond.\n"
    )
    kept = audit.relevant_passage(article, project)

    assert "11.25 MW" in kept
    assert "Analysts expect" not in kept
