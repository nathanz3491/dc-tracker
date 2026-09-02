"""Agent-backed triage: the repair has to survive a recompute.

The test that matters most here is
`test_a_ruled_out_claim_survives_a_recompute`, and its counterpart
`test_assigning_the_column_does_not_survive` which demonstrates the bug the
existing `logic.py` actions still have. Measured on the live database before this
module existed: a run resolved `built_exceeds_planned` 18 times and
`no_inversions` stayed at exactly 30 failures, because every one of those edits
was undone by the next `backfill derive` — and `tracker init` runs one on every
deploy.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker import triage
from tracker.llm import LLMReply, ToolCall
from tracker.models import Project, Source

_ARTICLE = (
    "Digital Realty said the Ashburn campus expansion adds one building. "
    "The company reported 230 MW of capacity across the entire Ashburn campus, "
    "a figure that covers all eight existing buildings and not this expansion alone. "
    "The new building itself is rated at 19.2 MW."
)


@pytest.fixture
def row(session):
    """A project whose two citations disagree about mw_built, one of them by scope."""
    project = Project(
        name="Digital Ashburn Campus",
        company="Digital Realty",
        city="Ashburn",
        state="VA",
        dedup_key="digital realty|city:ashburn|VA",
        phase="construction",
        mw_planned=19.2,
        mw_built=230.0,
    )
    session.add(project)
    session.flush()
    campus = Source(
        project_id=project.id,
        url="https://example.test/campus-total",
        source_type="trade_press",
        excerpt="230 MW across the entire Ashburn campus.",
        fields="mw_built",
        claims=json.dumps({"mw_built": 230.0}),
        published_at=dt.datetime(2026, 1, 1),
    )
    building = Source(
        project_id=project.id,
        url="https://example.test/this-building",
        source_type="trade_press",
        excerpt="The new building is rated at 19.2 MW.",
        fields="mw_built",
        claims=json.dumps({"mw_built": 19.2}),
        published_at=dt.datetime(2026, 2, 1),
    )
    session.add_all([campus, building])
    session.flush()
    return project, campus, building


class _ScriptedAgent:
    """A provider that reads one article, then rules on it."""

    def __init__(self, *, source_url: str, source_id: int, quote: str, confidence: float = 0.95):
        self.source_url, self.source_id = source_url, source_id
        self.quote, self.confidence = quote, confidence
        self.turn = 0

    def converse(self, *, system, messages, tools=None, max_tokens=None):
        self.turn += 1
        if self.turn == 1:
            return LLMReply(
                text="",
                tool_calls=(
                    ToolCall(
                        id="a",
                        name="read_article",
                        arguments={"url": self.source_url},
                        raw_arguments=json.dumps({"url": self.source_url}),
                    ),
                ),
            )
        payload = {
            "field": "mw_built",
            "source_ids": [self.source_id],
            "reason": "the 230 MW figure is the whole campus, not this expansion",
            "quote": self.quote,
            "confidence": self.confidence,
        }
        return LLMReply(
            text="",
            tool_calls=(
                ToolCall(
                    id="b",
                    name="rule_out_claims",
                    arguments=payload,
                    raw_arguments=json.dumps(payload),
                ),
            ),
        )


@pytest.fixture
def cached(tmp_path):
    """The campus article, in a cache dir `read_article` will hit rather than fetch."""
    from tracker.ingest.fetch import cache_path

    url = "https://example.test/campus-total"
    path = cache_path(url, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ARTICLE, encoding="utf-8")
    return tmp_path


# --- the property this module exists for -------------------------------------


def test_a_ruled_out_claim_survives_a_recompute(session, row):
    """The whole point. A superseded claim leaves the merge permanently."""
    from tracker.upsert import recompute_from_sources

    project, campus, _building = row
    acted, sentence, refusal = triage.apply_rule_out(
        session,
        project,
        {
            "field": "mw_built",
            "source_ids": [campus.id],
            "reason": "campus total, not this expansion",
            "confidence": 0.95,
        },
        articles={},
        require_quote=False,
    )
    assert acted, refusal
    assert project.mw_built == 19.2, sentence

    recompute_from_sources(session, project)
    assert project.mw_built == 19.2, "a superseded claim came back — the repair is not durable"


def test_assigning_the_column_does_not_survive(session, row):
    """The bug the existing `logic.py` actions still have, pinned so it cannot be
    reintroduced here. `_clear_built` does exactly this."""
    from tracker.upsert import recompute_from_sources

    project, _campus, _building = row
    project.mw_built = None
    session.flush()

    recompute_from_sources(session, project)
    assert project.mw_built == 230.0, "the column assignment survived; this test is now wrong"


def test_ruling_out_every_claim_leaves_the_field_empty_without_inventing_one(session, row):
    project, campus, building = row
    acted, _sentence, refusal = triage.apply_rule_out(
        session,
        project,
        {
            "field": "mw_built",
            "source_ids": [campus.id, building.id],
            "reason": "neither figure describes this row",
            "confidence": 0.95,
        },
        articles={},
        require_quote=False,
    )
    assert acted, refusal
    assert project.mw_built is None


# --- the refusals ------------------------------------------------------------


def test_a_field_no_citation_claims_is_refused_rather_than_reported_as_repaired(session, row):
    """Superseding a claim that does not exist reports a repair that changed
    nothing — the exact failure this module was written to stop."""
    project, campus, _building = row
    acted, _sentence, refusal = triage.apply_rule_out(
        session,
        project,
        {
            "field": "investment_usd",
            "source_ids": [campus.id],
            "reason": "made up",
            "confidence": 0.99,
        },
        articles={},
        require_quote=False,
    )
    assert not acted
    assert "claims investment_usd" in refusal


def test_an_identity_field_cannot_be_ruled_on(session, row):
    project, campus, _ = row
    acted, _s, refusal = triage.apply_rule_out(
        session,
        project,
        {"field": "company", "source_ids": [campus.id], "confidence": 1.0},
        articles={},
        require_quote=False,
    )
    assert not acted
    assert "not a field" in refusal


def test_a_citation_on_another_row_cannot_be_ruled_out(session, row):
    project, _campus, _building = row
    acted, _s, refusal = triage.apply_rule_out(
        session,
        project,
        {"field": "mw_built", "source_ids": [99999], "confidence": 1.0},
        articles={},
        require_quote=False,
    )
    assert not acted
    assert "is a citation on this row" in refusal


def test_confidence_below_the_floor_is_refused(session, row):
    project, campus, _ = row
    acted, _s, refusal = triage.apply_rule_out(
        session,
        project,
        {"field": "mw_built", "source_ids": [campus.id], "confidence": 0.5},
        articles={},
        min_confidence=0.9,
        require_quote=False,
    )
    assert not acted
    assert "below 0.90" in refusal


def test_a_quote_that_is_not_in_any_article_read_is_refused(session, row):
    """Without this the edit stores as `inferred`, and `capex` does not sum it."""
    project, campus, _ = row
    acted, _s, refusal = triage.apply_rule_out(
        session,
        project,
        {
            "field": "mw_built",
            "source_ids": [campus.id],
            "quote": "the operator confirmed the expansion alone draws 19.2 megawatts",
            "confidence": 0.95,
        },
        articles={"https://example.test/campus-total": _ARTICLE},
        require_quote=True,
    )
    assert not acted
    assert "not in any article" in refusal


def test_a_real_sentence_from_an_article_the_run_read_is_accepted(session, row):
    project, campus, _ = row
    acted, sentence, refusal = triage.apply_rule_out(
        session,
        project,
        {
            "field": "mw_built",
            "source_ids": [campus.id],
            "quote": ("The company reported 230 MW of capacity across the entire Ashburn campus"),
            "confidence": 0.95,
        },
        articles={"https://example.test/campus-total": _ARTICLE},
        require_quote=True,
    )
    assert acted, refusal
    assert "superseded" in sentence


# --- end to end through the loop --------------------------------------------


def test_triage_reads_an_article_then_rules_on_it(session, row, cached, monkeypatch):
    project, campus, _building = row
    monkeypatch.setattr("tracker.config.install_root", lambda: cached.parent, raising=False)

    from tracker import agent

    # Point the toolkit's cache at the fixture rather than the install root.
    real_toolkit = agent.evidence_toolkit
    monkeypatch.setattr(
        agent,
        "evidence_toolkit",
        lambda s, **kw: real_toolkit(
            s, cache_dir=cached, **{k: v for k, v in kw.items() if k != "cache_dir"}
        ),
    )

    model = _ScriptedAgent(
        source_url=campus.url,
        source_id=campus.id,
        quote="The company reported 230 MW of capacity across the entire Ashburn campus",
    )

    outcome = triage.triage(
        session,
        project,
        question="mw_built 230 exceeds mw_planned 19.2",
        extractor=model,
        min_confidence=0.9,
    )

    assert outcome.verdict == "ruled", outcome.note
    assert outcome.acted
    assert project.mw_built == 19.2
    assert "read_article" in outcome.steps


def test_leave_alone_is_recorded_as_a_real_answer(session, row):
    project, _campus, _building = row

    class _Declining:
        def converse(self, *, system, messages, tools=None, max_tokens=None):
            payload = {"reason": "both figures are plausible and neither article says which"}
            return LLMReply(
                text="",
                tool_calls=(
                    ToolCall(
                        id="x",
                        name="leave_alone",
                        arguments=payload,
                        raw_arguments=json.dumps(payload),
                    ),
                ),
            )

    outcome = triage.triage(session, project, question="which is right?", extractor=_Declining())

    assert outcome.verdict == "left"
    assert not outcome.acted
    assert "neither article says which" in outcome.note
    assert project.mw_built == 230.0  # nothing touched


def test_a_provider_without_tools_is_an_error_not_a_silent_decline(session, row):
    project, _campus, _building = row

    class Old:
        def complete(self, *, system, user, max_tokens=None):
            return LLMReply(text="{}")

    outcome = triage.triage(session, project, question="q", extractor=Old())

    assert outcome.verdict == "error"
    assert "cannot use tools" in outcome.note


# --- the NOT NULL column, which cost three rounds of an overnight run --------


def test_ruling_out_a_phase_claim_does_not_violate_not_null(session, row):
    """`phase` is the one NOT NULL field in RULEABLE_FIELDS.

    Blanking it before the recompute raised IntegrityError on the flush, and
    because the exception escaped mid-batch it killed the whole logic phase of
    rounds 1, 2 and 3 of the first overnight run — three of five rounds did no
    logic work at all.
    """
    import json as _json

    project, campus, _building = row
    campus.claims = _json.dumps({"mw_built": 230.0, "phase": "operational"})
    campus.fields = "mw_built,phase"
    session.flush()

    acted, sentence, refusal = triage.apply_rule_out(
        session,
        project,
        {
            "field": "phase",
            "source_ids": [campus.id],
            "reason": "that article describes a different building",
            "confidence": 0.95,
        },
        articles={},
        require_quote=False,
    )

    assert acted, refusal
    assert project.phase is not None, sentence
    session.flush()  # the flush that used to raise
