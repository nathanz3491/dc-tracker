"""Agent-backed triage: the repair has to survive a recompute.

The test that matters most here is
`test_a_ruled_out_claim_survives_a_recompute`, and its counterpart
`test_assigning_the_column_does_not_survive` which demonstrates the bug the
`logic.py` actions had until they were rebuilt the same way. Measured on the live database before this
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
    """The bug the `logic.py` actions had, pinned so it cannot be reintroduced here.
    `_clear_built` did exactly this until it was rebuilt around `_rule_against`."""
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
    # The row holds what its only phase claim says, as a recompute would leave it —
    # a ruling must reach the stored value to be accepted at all.
    project.phase = "operational"
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


# --- a ruling has to reach the value the row holds ---------------------------


def test_ruling_out_a_figure_the_row_does_not_hold_is_refused(session, row):
    """#72, reduced. The row held 6,750 MW built; the model said 6,750 was wrong and
    named the citation stating 18. The right figure was superseded, the wrong one
    stood, and the recorded `6750 -> 6750` settled the finding for good."""
    project, _campus, building = row
    assert project.mw_built == 230.0  # stated by `campus`, not by `building`

    acted, _sentence, refusal = triage.apply_rule_out(
        session,
        project,
        {
            "field": "mw_built",
            "source_ids": [building.id],
            "reason": "nothing supports the 230 MW figure",
            "confidence": 0.95,
        },
        articles={},
        require_quote=False,
    )

    assert not acted
    assert "states the value the row holds (230)" in refusal
    assert "superseded" not in (building.unconfirmed_reasons or "")
    assert "misread" not in (building.unconfirmed_reasons or "")


def test_a_no_op_ruling_recorded_before_the_rail_does_not_settle_its_finding(session, row):
    """The 28 rulings on the snapshot that changed nothing still read as settled.
    `settled_codes` re-opens them, so the finding is asked again — this time under
    the rail above."""
    from tracker.audit import settled_codes
    from tracker.logic import record_decision

    project, _campus, _building = row
    record_decision(
        project,
        "built_exceeds_planned",
        "mw_built 230 -> 230 (1 claim(s) superseded on citation(s) [2])",
        by="agent (0.90)",
    )
    record_decision(
        project,
        "online_before_announced",
        "first_announced 2025-01-01 -> empty (1 claim(s) superseded)",
        by="agent (0.90)",
    )
    project.first_announced = None

    assert settled_codes(project) == {"online_before_announced"}


# --- misread is not the same statement as superseded -------------------------


def test_a_ruling_is_recorded_as_misread_not_superseded(session, row):
    """The two decision reasons mean opposite things about time.

    `superseded` is documented as "correct when written, and since restated" — a
    fact about the world, which can change back. `misread` is "this sentence was
    always about another object" — a fact about the sentence, which cannot. This
    module's prompt asks the model which citations are WRONG, so its rulings are
    the second kind. Filing them as the first is what made an agent's decision look
    like it bound forever on a mutable question.
    """
    import json as _json

    from tracker.conflicts import MISREAD

    project, campus, _building = row
    acted, _sentence, refusal = triage.apply_rule_out(
        session,
        project,
        {
            "field": "mw_built",
            "source_ids": [campus.id],
            "reason": "the 230 MW figure is the campus, not this expansion",
            "confidence": 0.95,
        },
        articles={},
        require_quote=False,
    )

    assert acted, refusal
    assert _json.loads(campus.unconfirmed_reasons)["mw_built"] == MISREAD


def test_a_misread_claim_leaves_the_merge_like_a_superseded_one(session, row):
    """Both are in `DECIDED_REASONS`, so the repair is just as durable."""
    from tracker.upsert import recompute_from_sources

    project, campus, _building = row
    triage.apply_rule_out(
        session,
        project,
        {"field": "mw_built", "source_ids": [campus.id], "confidence": 0.95},
        articles={},
        require_quote=False,
    )
    assert project.mw_built == 19.2

    recompute_from_sources(session, project)
    assert project.mw_built == 19.2, "a misread claim came back into the merge"


def test_a_misread_relabels_a_claim_previously_filed_as_superseded(session, row):
    """The correction project #14's source 2790 needs: it is marked `superseded`
    when the article was never right about that row."""
    import json as _json

    from tracker.conflicts import MISREAD, SUPERSEDED, supersede

    _project, campus, _building = row
    assert supersede(campus, "mw_built", reason=SUPERSEDED)
    session.flush()

    assert supersede(campus, "mw_built", reason=MISREAD), "a relabel must not be skipped"
    assert _json.loads(campus.unconfirmed_reasons)["mw_built"] == MISREAD
    # Still idempotent against its own reason.
    assert not supersede(campus, "mw_built", reason=MISREAD)


# --- the ruling has to be readable back as an answer -------------------------
#
# `settled_codes` parses the sentence `record_decision` wrote and decides whether
# the finding is still open. Nothing tested that round trip, and it was broken for
# the one case this module exists for: a ruling that empties a field wrote
# `-> None`, which the reader takes for a value that was reverted. So the model did
# the expensive work, got the right answer, and the finding came back every run —
# while declines, which write no arrow at all, were recorded correctly. That
# asymmetry is what made a broken parser look like a cautious model.


def _decide_and_read_back(session, project, answer, code="built_exceeds_planned"):
    """Rule, record the sentence the CLI would record, and ask if it settled."""
    from tracker.audit import settled_codes
    from tracker.logic import record_decision

    acted, sentence, refusal = triage.apply_rule_out(
        session, project, answer, articles={}, require_quote=False
    )
    assert acted, refusal
    record_decision(project, code, sentence, by="agent (0.95)", detail="scope error")
    return sentence, settled_codes(project)


def test_a_ruling_that_empties_a_field_is_read_back_as_answered(session, row):
    """The case this module was built for, and the one that did not stick."""
    project, campus, building = row
    sentence, settled = _decide_and_read_back(
        session,
        project,
        {
            "field": "mw_built",
            "source_ids": [campus.id, building.id],
            "reason": "neither figure describes this row",
            "confidence": 0.95,
        },
    )
    assert project.mw_built is None
    assert "-> empty" in sentence, f"the reader only understands `empty`: {sentence!r}"
    assert "None" not in sentence
    assert "built_exceeds_planned" in settled, (
        "an emptying ruling was not recognised as an answer, so the finding is "
        "re-offered and re-paid for on every later run"
    )


def test_a_ruling_that_leaves_a_figure_is_read_back_as_answered(session, row):
    """The case that always worked. Kept so a fix to the other cannot break it."""
    project, campus, _building = row
    _sentence, settled = _decide_and_read_back(
        session,
        project,
        {
            "field": "mw_built",
            "source_ids": [campus.id],
            "reason": "the 230 MW figure is the whole campus",
            "confidence": 0.95,
        },
    )
    assert project.mw_built == 19.2
    assert "built_exceeds_planned" in settled


def test_a_ruling_on_a_date_field_is_read_back_as_answered(session, row):
    """Dates take the same path and the same formatter."""
    project, campus, _building = row
    project.expected_online = dt.date(2027, 1, 1)
    campus.claims = json.dumps({"mw_built": 230.0, "expected_online": "2027-01-01"})
    session.flush()

    _sentence, settled = _decide_and_read_back(
        session,
        project,
        {
            "field": "expected_online",
            "source_ids": [campus.id],
            "reason": "that date is the campus, not this building",
            "confidence": 0.95,
        },
        code="online_before_announced",
    )
    assert project.expected_online is None
    assert "online_before_announced" in settled


def test_re_ruling_an_already_ruled_claim_is_refused_not_reported_as_a_repair(session, row):
    """`supersede` is idempotent, so a second ruling changes nothing. Saying it
    repaired something writes a note for work nobody did, and grows the row's
    notes by a line every night."""
    project, campus, _building = row
    answer = {
        "field": "mw_built",
        "source_ids": [campus.id],
        "reason": "the 230 MW figure is the whole campus",
        "confidence": 0.95,
    }
    acted, _sentence, _refusal = triage.apply_rule_out(
        session, project, answer, articles={}, require_quote=False
    )
    assert acted

    acted, _sentence, refusal = triage.apply_rule_out(
        session, project, answer, articles={}, require_quote=False
    )
    assert not acted, "a second ruling on the same claim reported a repair that changed nothing"
    assert "already ruled out" in refusal


def test_a_claim_filed_as_superseded_can_still_be_relabelled_a_misread(session, row):
    """The refusal above must be against *this* reason, not against being out of
    the merge at all. `superseded` and `misread` say different true things to a
    reader — right-then-restated against never-about-this-row — so a relabel is a
    real change and must not be refused."""
    from tracker.conflicts import MISREAD, SUPERSEDED, supersede

    project, campus, _building = row
    assert supersede(campus, "mw_built", reason=SUPERSEDED)
    session.flush()

    acted, _sentence, refusal = triage.apply_rule_out(
        session,
        project,
        {
            "field": "mw_built",
            "source_ids": [campus.id],
            "reason": "the article was never about this row",
            "confidence": 0.95,
        },
        articles={},
        require_quote=False,
    )
    assert acted, refusal
    assert json.loads(campus.unconfirmed_reasons)["mw_built"] == MISREAD


# --- what never reaches a model ----------------------------------------------


def test_a_tranche_finding_is_not_sent_to_a_model():
    """Six rules are about how a campus is split into tranches. Each names a
    project-level field, so each *looks* actionable — and a ruling moves a project
    scalar, which is not where the contradiction is. ~250 of these sat in the
    backlog, each costing a full agent run to reach the only answer available."""
    from tracker.logic import Finding

    for code in (
        "block_past_its_own_date",
        "live_block_without_cited_capacity",
        "built_capacity_uncited_in_blocks",
        "block_label_ambiguous",
        "blocks_may_double_count",
        "no_block_for_energisation",
    ):
        finding = Finding(
            project_id=1,
            code=code,
            severity="warning",
            summary="a tranche disagrees with the campus",
            fields=("mw_built", "mw_planned", "expected_online"),
        )
        assert not triage.can_rule_on(finding), (
            f"`{code}` reached a model that cannot write a block row"
        )


def test_a_value_no_citation_claims_is_not_sent_to_a_model():
    """It fires *because* nothing claims the field, so there is nothing to rule
    out. `apply_rule_out` refuses it — after the articles have been paid for."""
    from tracker.logic import Finding

    finding = Finding(
        project_id=1,
        code="value_without_evidence",
        severity="warning",
        summary="230 MW is stored, and no source on this row claims mw_built at all",
        fields=("mw_built",),
    )
    assert not triage.can_rule_on(finding)


def test_the_findings_a_ruling_can_answer_still_go_to_a_model():
    """The filter must remove spend, never an outcome."""
    from tracker.logic import Finding

    for code, fields in (
        ("built_exceeds_planned", ("mw_built", "mw_planned")),
        ("online_before_announced", ("expected_online", "first_announced")),
        ("value_above_its_evidence", ("investment_usd",)),
        ("energized_but_not_operational", ("phase",)),
    ):
        finding = Finding(project_id=1, code=code, severity="error", summary="x", fields=fields)
        assert triage.can_rule_on(finding), f"`{code}` is exactly what this path is for"


def test_a_finding_naming_no_ruleable_field_is_not_sent_to_a_model():
    """`blocker` is derived from risk rows and `city` is never overwritten once
    set, so there is no claim behind either that superseding could remove."""
    from tracker.logic import Finding

    for fields in ((), ("blocker",), ("city", "county")):
        finding = Finding(
            project_id=1, code="whatever", severity="warning", summary="x", fields=fields
        )
        assert not triage.can_rule_on(finding)


def test_every_unanswerable_code_is_one_a_rule_actually_raises():
    """The list is hand-written because nothing in a finding betrays that its
    subject is a tranche. This is what catches a typo in it, and a rule renamed
    out from under it."""
    from pathlib import Path

    text = Path(triage.__file__).with_name("logic.py").read_text(encoding="utf-8")
    missing = [code for code in triage.UNANSWERABLE_BY_RULING if f'"{code}"' not in text]
    assert not missing, f"no rule in logic.py raises {sorted(missing)}"


# --- the CLI wiring around that filter ---------------------------------------


def test_the_agent_walk_sends_only_what_a_ruling_could_answer(session, row, monkeypatch):
    """`can_rule_on` is only worth anything if the walk actually applies it, and
    applies it *before* `--limit`. Slicing first spends a budget for calls on
    findings that never reach a model — the mistake the menu path documents."""
    from tracker.cli import logic as cli_logic
    from tracker.logic import Finding

    project, _campus, _building = row

    asked: list[str] = []

    def _never_called(session, project, *, question, extractor, min_confidence):
        asked.append(question)
        raise AssertionError("this finding should not have reached a model")

    monkeypatch.setattr("tracker.triage.triage", _never_called)

    findings = [
        Finding(
            project_id=project.id,
            code=code,
            severity="warning",
            summary="a tranche disagrees with the campus",
            fields=("mw_built", "mw_planned"),
        )
        for code in sorted(triage.UNANSWERABLE_BY_RULING)
    ]
    cli_logic._triage_by_agent(session, findings, extractor=None, limit=30)
    assert not asked, "a finding no ruling can answer was sent to a model"


def test_the_agent_walk_applies_the_limit_after_the_filter(session, row, monkeypatch):
    """Twelve unanswerable findings then two real ones, with a limit of two: both
    real ones must be offered. Before the filter moved ahead of the slice, the
    unanswerable ones ate the budget and the model saw nothing."""
    from tracker.cli import logic as cli_logic
    from tracker.logic import Finding

    project, _campus, _building = row
    seen: list[str] = []

    def _record(session, project, *, question, extractor, min_confidence):
        seen.append(question)
        return triage.Outcome(verdict="left", note="not settled by the evidence")

    monkeypatch.setattr("tracker.triage.triage", _record)

    padding = [
        Finding(
            project_id=project.id,
            code="block_label_ambiguous",
            severity="warning",
            summary=f"tranche {n} is unplaceable",
            fields=("mw_planned",),
        )
        for n in range(12)
    ]
    real = [
        Finding(
            project_id=project.id,
            code="built_exceeds_planned",
            severity="error",
            summary="230 MW built against 19.2 MW planned",
            fields=("mw_built", "mw_planned"),
        ),
        # A field the row's citations claim: one nobody claims is filtered out as
        # having nothing to rule out (`triage.has_live_claim`), which is not what
        # this test is about.
        Finding(
            project_id=project.id,
            code="operational_without_built_capacity",
            severity="error",
            summary="operational, yet no built capacity is cited",
            fields=("mw_built",),
        ),
    ]
    cli_logic._triage_by_agent(session, padding + real, extractor=None, limit=2)
    assert len(seen) == 2, "the limit was spent on findings that cannot be answered"
    assert any("built_exceeds_planned" in q for q in seen)
    assert any("operational_without_built_capacity" in q for q in seen)


def test_the_agent_is_told_which_obstacle_the_finding_is_about(session, row, monkeypatch):
    """`Finding.subjects` is plural and this line read it in the singular, so the
    `About:` line was never emitted once — the exact blindness those tokens were
    added to fix."""
    from tracker.cli import logic as cli_logic
    from tracker.logic import Finding

    project, _campus, _building = row
    seen: list[str] = []

    def _record(session, project, *, question, extractor, min_confidence):
        seen.append(question)
        return triage.Outcome(verdict="left", note="no")

    monkeypatch.setattr("tracker.triage.triage", _record)

    cli_logic._triage_by_agent(
        session,
        [
            Finding(
                project_id=project.id,
                code="built_exceeds_planned",
                severity="error",
                summary="230 MW built against 19.2 MW planned",
                fields=("mw_built", "mw_planned"),
                subjects=("risk:grid_capacity", "track:power"),
            )
        ],
        extractor=None,
    )
    assert seen and "About: risk:grid_capacity, track:power" in seen[0]


def test_a_finding_with_nothing_left_to_rule_out_never_reaches_a_model(session, row, monkeypatch):
    """Every claim on the finding's field already filed `misread`: the only answer
    possible is "unusable", so it is known before the call rather than paid for.
    One night spent ~2.75M tokens on ten findings, seven of them this shape."""
    import json

    from tracker.cli import logic as cli_logic
    from tracker.logic import Finding

    project, _campus, _building = row
    for source in project.sources:
        claims = json.loads(source.claims or "{}")
        if claims.get("mw_planned") is not None:
            source.unconfirmed_reasons = json.dumps({"mw_planned": "misread"})
    session.flush()

    def _never_called(session, project, *, question, extractor, min_confidence):
        raise AssertionError("a finding with nothing left to rule out reached a model")

    monkeypatch.setattr("tracker.triage.triage", _never_called)
    finding = Finding(
        project_id=project.id,
        code="built_exceeds_planned",
        severity="warning",
        summary="more is built than planned",
        fields=("mw_planned",),
    )
    assert not triage.has_live_claim(project, finding)
    cli_logic._triage_by_agent(session, [finding], extractor=None, limit=30)
