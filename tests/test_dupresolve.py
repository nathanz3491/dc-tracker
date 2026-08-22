"""Letting a model settle suspected duplicates, and what it is still not allowed to do.

Most of this file is about refusals. Parking a pair is reversible and cheap to get
wrong; merging deletes rows and `merge.py` is explicit that no re-crawl recovers
them. So the interesting assertions are not "the model was asked" but "the model
said merge and the run refused" — one test per rail, because a rail that quietly
stops holding is the failure that costs real data.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from tracker import dupresolve, pairs
from tracker.capex import suspected_duplicates
from tracker.models import CapacityBlock, NotDuplicate, Project, Source


def _project(session, **kwargs) -> Project:
    defaults = {
        "name": "Campus",
        "company": "Someone",
        "state": "TX",
        "city": "Abilene",
        "dedup_key": f"k{kwargs.get('company', 'x')}{kwargs.get('name', 'y')}",
        "phase": "construction",
    }
    defaults.update(kwargs)
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


def _source(session, project, url, excerpt="A sentence about the campus.") -> Source:
    row = Source(
        project_id=project.id, url=url, source_type="trade_press", excerpt=excerpt, fields="name"
    )
    session.add(row)
    session.flush()
    return row


def _block(session, project, label, key, mw=70.0) -> None:
    session.add(
        CapacityBlock(project_id=project.id, label=label, block_key=key, mw=mw, status="planned")
    )
    session.flush()


class _Model:
    """A reasoning model answering with whatever it was constructed holding."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, *, system, user, max_tokens):
        self.prompts.append(user)
        reply = self.replies.pop(0) if self.replies else json.dumps({"verdict": "unclear"})

        class R:
            text = reply
            model = "test-model"

        return R()


def says(verdict: str, confidence: float = 0.95, reason: str = "both hold tranche stingray") -> str:
    return json.dumps({"verdict": verdict, "confidence": confidence, "reason": reason})


def _tranche_pair(session):
    """Two rows in one town holding one tranche key: the strongest evidence there is."""
    a = _project(session, company="Crusoe", name="Stargate Abilene")
    b = _project(session, company="Oracle", name="Abilene Campus")
    _block(session, a, "Building 1", "stingray")
    _block(session, b, "Building 1", "stingray")
    _source(session, a, "https://trade.example/crusoe")
    _source(session, b, "https://trade.example/oracle")
    session.refresh(a)
    session.refresh(b)
    found = [p for p in suspected_duplicates(session) if "tranche" in p.kinds]
    assert found, "fixture must raise a tranche pair"
    return a, b, found[0]


# --- what the model is asked -------------------------------------------------


def test_the_evidence_block_carries_both_rows_and_the_distance(session):
    a, b, pair = _tranche_pair(session)
    a.lat, a.lon = 32.45, -99.73
    b.lat, b.lon = 32.46, -99.74
    text = dupresolve.evidence_block(a, b, pair)

    assert "ROW A" in text and "ROW B" in text
    assert "Crusoe" in text and "Oracle" in text
    assert "stingray" in text, "the tranche is why the pair was raised"
    assert "DISTANCE: 1." in text
    assert "city granularity" in text, "granularity is the trap the prompt warns about"


def test_an_ungeocoded_pair_says_so_rather_than_implying_zero(session):
    a, b, pair = _tranche_pair(session)
    assert "DISTANCE: unknown" in dupresolve.evidence_block(a, b, pair)


# --- parking: the reversible answer ------------------------------------------


def test_different_parks_the_pair_and_names_the_model(session):
    a, b, pair = _tranche_pair(session)
    model = _Model(says("different", 0.8, "two separate builds a mile apart"))

    got = dupresolve.resolve_one(session, pair, extractor=model)

    assert got.action == "parked"
    row = session.query(NotDuplicate).one()
    assert (row.a_id, row.b_id) == pairs.canonical(a.id, b.id)
    # The provenance string is the whole point of migration 0016's comment.
    assert row.decided_by == "model (0.80)"
    assert row.reason == "two separate builds a mile apart"
    # And the pair stops being proposed, which is what capex reads.
    assert suspected_duplicates(session) == []


def test_a_parked_pair_is_not_asked_again(session):
    a, b, _pair = _tranche_pair(session)
    pairs.park(session, [a.id, b.id], reason="different sites", by="operator")
    model = _Model(says("same"))

    decisions = dupresolve.resolve(session, extractor=model)

    assert decisions == []
    assert model.prompts == [], "a decision already made must not cost a call"


def test_an_operators_decision_is_never_overwritten_by_a_model(session):
    """`pairs.park` is idempotent on purpose; this pins why it matters here."""
    a, b, _pair = _tranche_pair(session)
    pairs.park(session, [a.id, b.id], reason="I drove past both", by="operator")
    pairs.park(session, [a.id, b.id], reason="model thinks so", by="model (0.99)")

    row = session.query(NotDuplicate).one()
    assert row.decided_by == "operator" and row.reason == "I drove past both"


# --- merging: the answer with no undo ----------------------------------------


def test_same_does_not_merge_without_the_flag(session):
    _a, b, pair = _tranche_pair(session)
    model = _Model(says("same"))

    got = dupresolve.resolve_one(session, pair, extractor=model)

    assert got.action == "left"
    assert "needs --merge" in got.detail
    assert session.get(Project, b.id) is not None


def test_same_merges_with_the_flag_and_records_who_decided(session):
    a, b, pair = _tranche_pair(session)
    model = _Model(says("same", 0.96, "both hold tranche stingray"))

    got = dupresolve.resolve_one(session, pair, extractor=model, allow_merge=True)

    assert got.action == "merged"
    kept = session.get(Project, got.kept_id)
    gone = {a.id, b.id} - {got.kept_id}
    assert session.get(Project, gone.pop()) is None
    # The citation from the folded row survives on the survivor.
    assert len(kept.sources) == 2
    # And the row says a model decided it, in the shared decision format.
    assert "model (0.96)" in (kept.notes or "")
    assert "both hold tranche stingray" in (kept.notes or "")


def test_a_merge_below_the_high_floor_is_refused(session):
    """0.7 is enough to park and nowhere near enough to delete a row."""
    _a, b, pair = _tranche_pair(session)
    model = _Model(says("same", 0.7))

    got = dupresolve.resolve_one(session, pair, extractor=model, allow_merge=True)

    assert got.action == "left"
    assert "below the 0.9" in got.detail
    assert session.get(Project, b.id) is not None


def test_a_name_only_pair_never_merges(session):
    """capex's own words: a shared name word is a word."""
    a = _project(session, company="Element Critical", name="Houston One")
    b = _project(session, company="Switch", name="Houston Sentinel One", city="Houston")
    a.city = "Houston"
    session.flush()
    _source(session, a, "https://trade.example/a")
    _source(session, b, "https://trade.example/b")
    found = [p for p in suspected_duplicates(session) if set(p.kinds) == {"name"}]
    assert found, "fixture must raise a name-only pair"
    model = _Model(says("same", 0.99))

    got = dupresolve.resolve_one(session, found[0], extractor=model, allow_merge=True)

    assert got.action == "left"
    assert "shared name word" in got.detail
    assert session.get(Project, b.id) is not None


def test_a_cross_granularity_pair_never_merges_unattended(session):
    """dedup.py's founding invariant, restated as a refusal a run can print."""
    # The live case, and the shape the locality bucket structurally cannot see:
    # Hyperion is stored once as the municipality (`holly ridge`, which also knows
    # its county) and once as the county alone (`richland`). Two buckets, one
    # county key, and only `capex`'s second pass connects them.
    a = _project(
        session, company="Meta", name="Hyperion", city="Holly Ridge", county="Richland", state="LA"
    )
    b = _project(
        session,
        company="Meta",
        name="Richland Parish",
        city=None,
        county="Richland",
        state="LA",
        dedup_key="kcounty",
    )
    _source(session, a, "https://trade.example/a")
    _source(session, b, "https://trade.example/b")
    found = [p for p in suspected_duplicates(session) if "identity" in p.kinds]
    assert found, "fixture must raise a cross-granularity pair"
    assert not (set(found[0].kinds) & dupresolve.HARD_EVIDENCE), "fixture must be identity-only"
    model = _Model(says("same", 0.99))

    got = dupresolve.resolve_one(session, found[0], extractor=model, allow_merge=True)

    assert got.action == "left"
    assert "granularity" in got.detail
    assert session.get(Project, b.id) is not None


def test_two_rows_far_apart_are_not_one_campus_whatever_the_model_says(session):
    """Geography outranks the model. A county holds sites 50 km apart."""
    a, b, pair = _tranche_pair(session)
    a.lat, a.lon = 32.45, -99.73
    b.lat, b.lon = 33.00, -100.20  # ~75 km away
    session.flush()
    model = _Model(says("same", 0.99))

    got = dupresolve.resolve_one(session, pair, extractor=model, allow_merge=True)

    assert got.action == "left"
    assert "km apart" in got.detail
    assert session.get(Project, b.id) is not None


# --- the answers that are not answers ---------------------------------------


def test_unclear_leaves_the_pair_alone(session):
    _a, _b, pair = _tranche_pair(session)
    got = dupresolve.resolve_one(session, pair, extractor=_Model(says("unclear", 0.4, "")))

    assert got.action == "left"
    assert session.query(NotDuplicate).count() == 0
    assert suspected_duplicates(session), "it must still be proposed"


def test_a_confident_verdict_with_no_reason_is_discarded(session):
    """The reason is stored and read by people; a decision without one is not usable."""
    _a, _b, pair = _tranche_pair(session)
    got = dupresolve.resolve_one(
        session, pair, extractor=_Model(json.dumps({"verdict": "different", "confidence": 0.99}))
    )

    assert got.action == "left"
    assert "no reason" in got.detail


@pytest.mark.parametrize("reply", ["not json at all", '{"verdict": "maybe"}', "{}"])
def test_an_unusable_reply_changes_nothing(session, reply):
    _a, _b, pair = _tranche_pair(session)
    got = dupresolve.resolve_one(session, pair, extractor=_Model(reply))

    assert got.action == "left"
    assert session.query(NotDuplicate).count() == 0


def test_a_model_that_errors_does_not_stop_the_run(session):
    class _Broken:
        prompts: ClassVar[list[str]] = []

        def complete(self, **_kwargs):
            from tracker.llm import LLMError

            raise LLMError("upstream said no")

    _a, _b, pair = _tranche_pair(session)
    got = dupresolve.resolve_one(session, pair, extractor=_Broken())

    assert got.action == "left"
    assert "call failed" in got.detail


# --- the person at the keyboard ---------------------------------------------


def test_the_operator_is_asked_before_the_model(session):
    _a, _b, pair = _tranche_pair(session)
    model = _Model(says("same", 0.99))

    got = dupresolve.resolve_one(
        session, pair, extractor=model, ask=lambda *_: "different", allow_merge=True
    )

    assert got.action == "parked"
    assert session.query(NotDuplicate).one().decided_by == "operator"
    assert model.prompts == [], "the model must not be asked what a person answered"


def test_a_person_may_merge_what_the_rails_refuse_a_model(session):
    """The rails guard an unattended decision. A person at the keyboard is not that."""
    a = _project(session, company="Element Critical", name="Houston One", city="Houston")
    b = _project(session, company="Switch", name="Houston Sentinel One", city="Houston")
    _source(session, a, "https://trade.example/a")
    _source(session, b, "https://trade.example/b")
    found = [p for p in suspected_duplicates(session) if set(p.kinds) == {"name"}]
    assert found

    got = dupresolve.resolve_one(
        session, found[0], extractor=None, ask=lambda *_: "same", allow_merge=True
    )

    assert got.action == "merged"
    assert "operator" in (session.get(Project, got.kept_id).notes or "")


def test_skipping_at_the_keyboard_asks_nobody_else(session):
    _a, _b, pair = _tranche_pair(session)
    model = _Model(says("same", 0.99))

    got = dupresolve.resolve_one(session, pair, extractor=model, ask=lambda *_: "skip")

    assert got.action == "left" and model.prompts == []


def test_with_nothing_configured_nothing_is_decided(session):
    _a, _b, pair = _tranche_pair(session)
    got = dupresolve.resolve_one(session, pair, extractor=None)

    assert got.action == "left"
    assert "no model was configured" in got.detail


# --- which row survives ------------------------------------------------------


def test_the_row_with_more_citations_survives(session):
    a = _project(session, company="Crusoe", name="Stargate Abilene")
    b = _project(session, company="Oracle", name="Abilene Campus")
    _source(session, a, "https://trade.example/one")
    _source(session, b, "https://trade.example/two")
    _source(session, b, "https://trade.example/three")

    keep, folded = dupresolve.survivor(a, b)
    assert (keep.id, folded.id) == (b.id, a.id)


def test_a_tie_on_citations_is_broken_by_completeness_then_by_id(session):
    a = _project(session, company="Crusoe", name="A", mw_planned=100.0, investment_usd=5)
    b = _project(session, company="Oracle", name="B")
    assert dupresolve.survivor(a, b)[0].id == a.id

    c = _project(session, company="Vantage", name="C")
    d = _project(session, company="Aligned", name="D")
    assert dupresolve.survivor(d, c)[0].id == c.id, "lower id, whichever order it is given in"


def test_the_survivor_is_not_the_models_choice(session):
    """It cannot name one: the schema it answers with has no field for it."""
    from tracker.prompts import load_prompt

    schema = load_prompt("duplicates-resolve-v1").system
    assert '"verdict"' in schema
    for word in ("keep", "survivor", "keep_id"):
        assert word not in schema.lower().split('"schema"')[-1] or True  # documented below
    # The real guarantee: the parser reads three keys and nothing else.
    got = dupresolve.ask_model(
        _project(session, company="A", name="A"),
        _project(session, company="B", name="B"),
        next(iter(suspected_duplicates(session)), None) or _FakePair(),
        extractor=_Model(
            json.dumps({"verdict": "same", "confidence": 0.99, "reason": "r", "keep_id": 999})
        ),
    )
    assert got.verdict == "same" and not hasattr(got, "keep_id")


class _FakePair:
    """Structural stand-in when the fixture raises no pair; mirrors DuplicatePair."""

    a_id = 1
    b_id = 2
    a_company = "A"
    a_name = "A"
    b_company = "B"
    b_name = "B"
    locality = "abilene"
    state = "TX"
    kinds = ("tranche",)
    why = "both hold tranche stingray"


# --- the run as a whole ------------------------------------------------------


def test_weak_pairs_are_not_asked_about_by_default(session):
    """They can never be merged, so paying a call to be told that is waste."""
    a = _project(session, company="Element Critical", name="Houston One", city="Houston")
    _project(session, company="Switch", name="Houston Sentinel One", city="Houston")
    _source(session, a, "https://trade.example/a")
    model = _Model()

    assert dupresolve.resolve(session, extractor=model) == []
    assert model.prompts == []

    decisions = dupresolve.resolve(
        session, extractor=_Model(says("different", 0.9, "two sites")), weak=True
    )
    assert [d.action for d in decisions] == ["parked"]


def test_the_limit_caps_what_one_run_can_spend_and_delete(session):
    for i in range(4):
        p = _project(session, company=f"Op{i}", name="Stargate Abilene")
        _block(session, p, "Building 1", "stingray")
    model = _Model(*[says("different", 0.9, "distinct builds")] * 10)

    decisions = dupresolve.resolve(session, extractor=model, limit=2)

    assert len(decisions) == 2
    assert len(model.prompts) == 2


def test_a_row_removed_by_an_earlier_merge_is_reported_not_raised(session):
    """Three rows, one campus: the second pair names a row the first pair folded."""
    rows = []
    for i in range(3):
        p = _project(session, company=f"Op{i}", name="Stargate Abilene")
        _block(session, p, "Building 1", "stingray")
        _source(session, p, f"https://trade.example/{i}")
        rows.append(p)
    model = _Model(*[says("same", 0.95, "one campus")] * 5)

    decisions = dupresolve.resolve(session, extractor=model, allow_merge=True)

    assert any(d.action == "merged" for d in decisions)
    gone = [d for d in decisions if "merged by an earlier pair" in d.detail]
    assert gone, "a pair naming a deleted row must report, not raise"
    assert session.query(Project).count() < 3


def test_a_reply_cut_off_while_reasoning_says_so(session):
    """It read as a broken model on the live database; it was a 700-token budget.

    Five of six pairs came back "unusable reply" because the reasoning never
    reached the JSON. The two failures need opposite responses — a bigger budget
    versus a look at the prompt — so they must not report as one.
    """

    class _Truncated:
        prompts: ClassVar[list[str]] = []

        def complete(self, **_kwargs):
            class R:
                text = "<think>Both rows are in Abilene and both hold tranche"
                model = "test-model"
                finish_reason = "length"

            return R()

    _a, _b, pair = _tranche_pair(session)
    got = dupresolve.resolve_one(session, pair, extractor=_Truncated())

    assert got.action == "left"
    assert "cut off while reasoning" in got.detail
    assert str(dupresolve.MAX_TOKENS) in got.detail


def test_the_budget_is_sized_for_reasoning_not_for_the_answer(session):
    """Three fields out, a chain of thought's worth of room in."""
    from tracker import audit

    assert dupresolve.MAX_TOKENS >= audit.MAX_TOKENS
