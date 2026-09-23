"""Questions a model could not answer are not paid for twice on the same evidence.

The overnight loop selects its work the same way every round. Until `declines`, an
undecided answer left no trace, so a pair the agent left alone, a ruling the rails
refused, or an audit finding the model declined came back next round in the same
order at the same price — until two rounds in a row failed to move a count.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker import declines, triage
from tracker.capex import DuplicatePair
from tracker.llm import LLMReply, ToolCall
from tracker.models import ModelDecline, Project, Source, utcnow


def _project(session, **kw) -> Project:
    defaults = {"state": "AZ", "phase": "construction", "company": "Compass Datacenters"}
    project = Project(**{**defaults, **kw})
    session.add(project)
    session.flush()
    return project


# --- the store -----------------------------------------------------------------


def test_a_decline_holds_on_the_same_evidence_and_only_on_it(session):
    declines.record(session, "risk", "7", "abc", outcome="unclear", reason="no sentence says so")
    known = declines.load(session, "risk")

    assert declines.holds(known, "7", "abc")
    assert not declines.holds(known, "7", "abd"), "new evidence is a new question"
    assert not declines.holds(known, "8", "abc"), "another subject was never asked"


def test_a_decline_lapses_after_the_cooldown(session):
    """The agent phases can search the web, and the web changes while the row does not."""
    declines.record(session, "pair", "12-34", "f", outcome="left alone")
    row = session.query(ModelDecline).one()
    row.decided_at = utcnow() - dt.timedelta(days=declines.COOLDOWN_DAYS + 1)
    session.flush()

    assert not declines.holds(declines.load(session, "pair"), "12-34", "f")


def test_the_latest_look_replaces_the_last(session):
    declines.record(session, "audit", "3:x", "old", outcome="declined")
    declines.record(session, "audit", "3:x", "new", outcome="declined again")

    rows = session.query(ModelDecline).all()
    assert [(r.fingerprint, r.outcome) for r in rows] == [("new", "declined again")]


def test_split_holds_back_what_was_declined_and_again_asks_everything(session):
    declines.record(session, "logic", "1:code", "p1", outcome="unusable")
    items = [("1:code", "p1"), ("1:code-2", "p2"), ("2:code", "p3")]
    known = declines.load(session, "logic")

    kept, held = declines.split(items, known, key=lambda item: item)
    assert (kept, held) == (items[1:], 1)

    kept, held = declines.split(items, known, key=lambda item: item, again=True)
    assert (kept, held) == (items, 0)


def test_an_unknown_kind_is_refused_rather_than_written(session):
    with pytest.raises(ValueError):
        declines.record(session, "pairs", "1-2", "p", outcome="x")


def test_the_fingerprint_moves_when_a_citation_changes(session):
    """Claims, not just URLs: a re-extraction or a ruling on a claim is new evidence."""
    project = _project(session, name="A", city="El Mirage", dedup_key="a")
    source = Source(
        project_id=project.id,
        url="https://example.test/a",
        source_type="trade_press",
        claims=json.dumps({"mw_planned": 250.0}),
    )
    session.add(source)
    session.flush()
    session.refresh(project)
    before = declines.fingerprint(declines.citations(project))

    source.unconfirmed_reasons = json.dumps({"mw_planned": "superseded"})
    session.flush()
    assert declines.fingerprint(declines.citations(project)) != before


# --- pairs ---------------------------------------------------------------------


@pytest.fixture
def pair_rows(session):
    a = _project(session, name="Phoenix - El Mirage", city="El Mirage", dedup_key="c|city:em|AZ")
    b = _project(session, name="Compass Maricopa County", county="Maricopa", dedup_key="c|co:m|AZ")
    session.add(
        Source(
            project_id=a.id,
            url="https://example.test/el-mirage",
            source_type="trade_press",
            claims=json.dumps({"mw_planned": 250.0}),
        )
    )
    session.flush()
    pair = DuplicatePair(
        a_id=a.id,
        a_company=a.company,
        a_name=a.name,
        b_id=b.id,
        b_company=b.company,
        b_name=b.name,
        locality="Maricopa County",
        state="AZ",
        b_mw=250.0,
        shared_keys=("c|co:m|AZ",),
    )
    return a, b, pair


class _Undecided:
    """A judge that answers `leave_alone` every time, and counts how often it is asked."""

    def __init__(self, tool: str = "leave_alone", confidence: float = 0.9):
        self.tool, self.confidence, self.asked = tool, confidence, 0

    def converse(self, *, system, messages, tools=None, max_tokens=None):
        self.asked += 1
        args = {"reason": "the articles do not say", "confidence": self.confidence}
        return LLMReply(
            text="",
            tool_calls=(
                ToolCall(id="v", name=self.tool, arguments=args, raw_arguments=json.dumps(args)),
            ),
        )


@pytest.mark.parametrize(
    ("tool", "confidence", "outcome"),
    [("leave_alone", 0.9, "left alone"), ("rule_different", 0.3, "below the floor")],
)
def test_an_undecided_pair_is_recorded_with_what_it_was_shown(
    session, pair_rows, tool, confidence, outcome
):
    a, b, pair = pair_rows
    triage.pair_triage(session, pair, extractor=_Undecided(tool, confidence), allow_search=False)

    row = session.query(ModelDecline).one()
    assert (row.kind, row.subject, row.outcome) == ("pair", f"{a.id}-{b.id}", outcome)
    assert row.fingerprint == triage.pair_evidence(a, b, pair)


def test_same_but_no_merge_flag_is_not_recorded(session, pair_rows):
    """A run with `--merge` must still reach that pair — the answer can differ."""
    _a, _b, pair = pair_rows
    triage.pair_triage(
        session,
        pair,
        extractor=_Undecided("rule_same", 0.95),
        allow_merge=False,
        allow_search=False,
    )
    assert session.query(ModelDecline).count() == 0


def test_a_provider_failure_is_not_recorded(session, pair_rows):
    from tracker.llm import LLMError

    class Broken:
        def converse(self, **_):
            raise LLMError("HTTP 503")

    _a, _b, pair = pair_rows
    got = triage.pair_triage(session, pair, extractor=Broken(), allow_search=False)
    assert "503" in got.detail
    assert session.query(ModelDecline).count() == 0


def test_the_next_run_holds_a_declined_pair_back(session, pair_rows, monkeypatch):
    """The regression: nothing was recorded, so every round asked the same pairs."""
    import tracker.capex as capex

    _a, _b, pair = pair_rows
    monkeypatch.setattr(capex, "suspected_duplicates", lambda s, **kw: [pair])
    judge = _Undecided()
    held: list[int] = []

    triage.resolve_pairs(session, extractor=judge, allow_search=False, on_held=held.append)
    triage.resolve_pairs(session, extractor=judge, allow_search=False, on_held=held.append)
    assert judge.asked == 1
    assert held == [0, 1]

    triage.resolve_pairs(session, extractor=judge, allow_search=False, again=True)
    assert judge.asked == 2


def test_a_new_citation_reopens_a_declined_pair(session, pair_rows, monkeypatch):
    import tracker.capex as capex

    _a, b, pair = pair_rows
    monkeypatch.setattr(capex, "suspected_duplicates", lambda s, **kw: [pair])
    judge = _Undecided()
    triage.resolve_pairs(session, extractor=judge, allow_search=False)

    session.add(
        Source(
            project_id=b.id,
            url="https://example.test/maricopa",
            source_type="trade_press",
            claims=json.dumps({"mw_planned": 250.0}),
        )
    )
    session.flush()
    session.refresh(b)
    triage.resolve_pairs(session, extractor=judge, allow_search=False)
    assert judge.asked == 2


# --- logic ----------------------------------------------------------------------


def test_a_refused_ruling_is_held_back_on_the_next_run(session, monkeypatch):
    """`unusable` was 8 and then 12 of a round's 40 findings on the first overnight
    run, and every one was re-paid the next round."""
    from tracker.cli import logic as cli_logic
    from tracker.logic import Finding

    project = _project(session, name="A", city="Abilene", state="TX", dedup_key="a", mw_built=230.0)
    session.add(
        Source(
            project_id=project.id,
            url="https://example.test/a",
            source_type="trade_press",
            claims=json.dumps({"mw_built": 230.0}),
        )
    )
    session.flush()
    # Committed, as a real database's rows are: the loop rolls a refused ruling back.
    session.commit()
    finding = Finding(
        project_id=project.id,
        code="value_above_its_evidence_x",
        severity="warning",
        summary="230 MW built with nothing behind it",
        fields=("mw_built",),
    )
    calls: list[int] = []

    def refused(*_args, **_kw):
        calls.append(1)
        return triage.Outcome(verdict="unusable", note="the quote is not in any article")

    monkeypatch.setattr(triage, "triage", refused)
    cli_logic._triage_by_agent(session, [finding], extractor=object(), limit=5)
    cli_logic._triage_by_agent(session, [finding], extractor=object(), limit=5)
    assert len(calls) == 1

    cli_logic._triage_by_agent(session, [finding], extractor=object(), limit=5, again=True)
    assert len(calls) == 2


# --- risks ------------------------------------------------------------------------


_RISK_ARTICLE = (
    "Dominion Energy told the commission that the Loudoun interconnection cannot be "
    "energised before 2029 without new transmission. The developer said it remains "
    "confident in the schedule."
)


def _risk_row(session, *, excerpt: str = _RISK_ARTICLE):
    from tracker.models import Risk

    project = _project(session, name="Ashburn", city="Ashburn", state="VA", dedup_key="r")
    source = Source(
        project_id=project.id,
        url="https://example.test/risk",
        source_type="trade_press",
        excerpt=excerpt,
    )
    session.add(source)
    session.flush()
    risk = Risk(
        project_id=project.id,
        category="transmission",
        severity="material",
        status="open",
        summary="Interconnection cannot be energised before 2029.",
        unconfirmed="no_quote",
        source_id=source.id,
    )
    session.add(risk)
    session.flush()
    return risk, source


class _Reads:
    def __init__(self, reply: str):
        self.reply, self.asked = reply, 0

    def complete(self, *, system, user, max_tokens):
        self.asked += 1
        return LLMReply(text=self.reply)


def test_an_unclear_obstacle_is_not_read_again_on_the_same_article(session, tmp_path):
    from tracker import riskcheck

    risk, source = _risk_row(session)
    model = _Reads('{"verdict": "unclear", "confidence": 0.8, "reason": "not stated"}')
    (outcome,) = riskcheck.confirm(session, [risk], extractor=model, cache_dir=tmp_path)
    assert outcome.result == "unclear"

    unread, held = riskcheck.fresh(session, [risk], cache_dir=tmp_path)
    assert (unread, held) == ([], 1)

    # A changed article is a new question.
    source.excerpt = _RISK_ARTICLE + " Residents raised concerns about noise."
    session.flush()
    unread, held = riskcheck.fresh(session, [risk], cache_dir=tmp_path)
    assert (unread, held) == ([risk], 0)


def test_a_failed_obstacle_call_is_not_remembered(session, tmp_path):
    from tracker import riskcheck

    risk, _source = _risk_row(session)
    (outcome,) = riskcheck.confirm(
        session, [risk], extractor=_Reads("not json"), cache_dir=tmp_path
    )
    assert outcome.result == "error"
    assert session.query(ModelDecline).count() == 0


# --- audit -----------------------------------------------------------------------


def _audit_row(session):
    from tracker import audit
    from tracker.models import CapacityBlock

    project = _project(session, name="Site", city="Englewood", state="CO", dedup_key="au")
    project.mw_planned = 18.0
    session.add(
        CapacityBlock(
            project_id=project.id, block_key="phase-1", label="Phase 1", mw=2250.0, status="planned"
        )
    )
    session.flush()
    session.refresh(project)
    (finding,) = [f for f in audit.check_project(project) if f.code == "block_out_of_scale"]
    return project, finding


def test_a_model_decline_is_marked_and_a_failed_call_is_not(session):
    from tracker import audit

    class Declines:
        def complete(self, *, system, user, max_tokens):
            return LLMReply(text='{"key": "s", "confidence": 0.9, "reason": "cannot tell"}')

    class Fails:
        def complete(self, *, system, user, max_tokens):
            from tracker.llm import LLMError

            raise LLMError("HTTP 503")

    project, finding = _audit_row(session)
    assert audit.resolve_one(session, project, finding, extractor=Declines()).declined
    assert not audit.resolve_one(session, project, finding, extractor=Fails()).declined


def test_a_broken_search_is_not_an_answer_and_an_empty_one_is(session, monkeypatch):
    from tracker import audit

    class WantsMore:
        def complete(self, *, system, user, max_tokens):
            return LLMReply(text='{"key": "m", "confidence": 0.3, "reason": "not in the row"}')

    project, finding = _audit_row(session)
    monkeypatch.setattr(
        audit, "find_online", lambda *a, **k: audit.Searched(error="HTTP 500", failed=True)
    )
    assert not audit.resolve_one(session, project, finding, extractor=WantsMore()).declined

    monkeypatch.setattr(
        audit, "find_online", lambda *a, **k: audit.Searched(error="no usable search results")
    )
    assert audit.resolve_one(session, project, finding, extractor=WantsMore()).declined
