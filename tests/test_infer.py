"""Reasoned judgement, and the boundary it must not cross.

The PRD asks for analysis a document does not contain (what is obstructing this
project, what would show it advancing) while forbidding a model to assert facts:

    不能直接把AI的回答当作事实。关键数字…要尽量找到公司公告、政府文件…确认

So the load-bearing tests here are the *refusals*:
:func:`test_a_model_may_not_assert_a_fact` and
:func:`test_a_quantitative_field_is_never_inferable`. Everything else is parsing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tracker.infer import (
    INFERABLE,
    MAX_PER_KIND,
    MIN_CONFIDENCE,
    Analysis,
    analyse,
    build_context,
    parse_analysis,
)
from tracker.llm import LLMError, LLMReply
from tracker.vocab import TRACKED_FIELDS


def payload(obstacles=(), signals=(), **extra):
    return {
        "analysis": {
            "likely_obstacles": list(obstacles),
            "next_signals": list(signals),
            **extra,
        }
    }


def obstacle(category="grid_capacity", severity="material", reasoning="because", confidence=0.8):
    return {
        "category": category,
        "severity": severity,
        "reasoning": reasoning,
        "confidence": confidence,
    }


def signal(text="a signed interconnection agreement", reasoning="power is binding", confidence=0.7):
    return {"signal": text, "reasoning": reasoning, "confidence": confidence}


# --- the boundary ------------------------------------------------------------


def test_a_quantitative_field_is_never_inferable():
    """The whitelist must contain no field a document could confirm."""
    for field in TRACKED_FIELDS:
        assert field not in INFERABLE, f"{field} is a fact and must not be inferable"
    assert {"likely_obstacles", "next_signals"} == INFERABLE


def test_a_model_may_not_assert_a_fact():
    """A prompt is a request; the whitelist is the mechanism.

    If a future model helpfully volunteers `investment_usd`, it must be dropped and
    the attempt surfaced — silently ignoring it would leave nobody aware the model
    is trying to write facts.
    """
    analysis = parse_analysis(
        1,
        payload(obstacles=[obstacle()], investment_usd=1_200_000_000, expected_online="2027-01-01"),
    )
    assert analysis.rejected == ["expected_online", "investment_usd"]
    assert len(analysis.obstacles) == 1, "the legitimate part still comes through"


def test_an_inference_is_not_stored_as_a_confirmed_fact():
    """`inferred:` sources are excluded from confidence, like `derived:` ones."""
    from tracker.confidence import SourceView, compute

    reported = SourceView.from_row(
        SimpleNamespace(
            source_type="company_filing",
            url="https://news.acme.com/x",
            fields="mw_planned",
            claims=None,
            extractor="crawl:extract-v1@abc:m:httpx",
        )
    )
    inferred = SourceView.from_row(
        SimpleNamespace(
            source_type="manual",
            url="https://acme.example/inferred",
            fields=None,
            claims=None,
            extractor="inferred:infer-v1@abc:MiniMax-M3",
        )
    )
    alone = compute([reported]).value
    with_inference = compute([reported, inferred]).value
    assert with_inference == alone, "a judgement cannot corroborate a fact"


# --- parsing -----------------------------------------------------------------


def test_a_low_confidence_conclusion_is_dropped():
    below = parse_analysis(1, payload(obstacles=[obstacle(confidence=MIN_CONFIDENCE - 0.01)]))
    assert below.obstacles == []
    at = parse_analysis(1, payload(obstacles=[obstacle(confidence=MIN_CONFIDENCE)]))
    assert len(at.obstacles) == 1


@pytest.mark.parametrize("bad", [None, "high", -0.5, 1.5, ""])
def test_an_unusable_confidence_is_dropped(bad):
    """A conclusion with no honest confidence is not usable."""
    assert parse_analysis(1, payload(obstacles=[obstacle(confidence=bad)])).obstacles == []


def test_an_unknown_category_is_dropped():
    """The taxonomy is closed; a category outside it maps to no track."""
    assert parse_analysis(1, payload(obstacles=[obstacle(category="vibes")])).obstacles == []


def test_an_unknown_severity_is_dropped():
    assert parse_analysis(1, payload(obstacles=[obstacle(severity="catastrophic")])).obstacles == []


def test_a_conclusion_without_reasoning_is_dropped():
    """An obstacle with no reasoning is an assertion, which is what is forbidden."""
    assert parse_analysis(1, payload(obstacles=[obstacle(reasoning="")])).obstacles == []


def test_conclusions_are_ordered_by_confidence_and_capped():
    many = [
        obstacle(category=c, confidence=conf)
        for c, conf in zip(
            ("water", "financing", "offtake", "permitting", "transmission"),
            (0.4, 0.9, 0.5, 0.8, 0.6),
            strict=False,
        )
    ]
    analysis = parse_analysis(1, payload(obstacles=many))
    assert len(analysis.obstacles) == MAX_PER_KIND
    assert [o.confidence for o in analysis.obstacles] == [0.9, 0.8, 0.6]


def test_an_empty_analysis_is_a_valid_answer():
    """ "I cannot tell from this" must be representable, not forced into a guess."""
    analysis = parse_analysis(1, payload())
    assert analysis.empty
    assert analysis.rejected == []


def test_malformed_entries_are_skipped_not_fatal():
    analysis = parse_analysis(
        1, payload(obstacles=["not a dict", None, obstacle()], signals=[42, signal()])
    )
    assert len(analysis.obstacles) == 1
    assert len(analysis.signals) == 1


def test_a_bare_payload_without_the_analysis_wrapper_is_accepted():
    """Models wrap inconsistently; the contract is the content."""
    analysis = parse_analysis(1, {"likely_obstacles": [obstacle()], "next_signals": []})
    assert len(analysis.obstacles) == 1


# --- context -----------------------------------------------------------------


def fake_project():
    return SimpleNamespace(
        id=7,
        name="Stargate",
        company="Crusoe",
        customer=None,
        city="Abilene",
        county="Taylor County",
        state="TX",
        mw_planned=1200.0,
        mw_built=None,
        investment_usd=None,
        first_announced=None,
        expected_online=None,
        phase="construction",
        events=[SimpleNamespace(event_type="groundbreaking", event_date="2024-06-01")],
        risks=[
            SimpleNamespace(
                category="financing",
                severity="material",
                summary="expansion talks failed",
                status="open",
            )
        ],
        sources=[],
    )


def test_the_context_includes_what_the_database_could_not_find():
    """A gap is itself evidence, and the model needs to see it.

    A project announced years ago with no interconnection agreement and no
    expected-online date is telling you something about why.
    """
    from tracker.tracks import standing

    project = fake_project()
    context = build_context(project, standing(project.id, project.events, project.risks))

    assert "expected_online" in context["gaps"]
    assert "financing" in context["known_risks"]
    assert "groundbreaking" in context["milestones"]
    assert context["missing_milestones"], "the model must see what has NOT happened"
    assert context["mw_planned"] == "1200.0"


def test_the_context_never_says_none():
    """A literal "None" reads as a value; "unknown" reads as a gap."""
    project = fake_project()
    from tracker.tracks import standing

    context = build_context(project, standing(project.id, project.events, project.risks))
    assert "None" not in " ".join(context.values())


# --- failure modes -----------------------------------------------------------


class BoomLLM:
    def complete(self, **_: object) -> LLMReply:
        raise LLMError("provider down")


class GibberishLLM:
    def complete(self, **_: object) -> LLMReply:
        return LLMReply("I'm afraid I can't help with that.", "stop", 1, 1, "fake")


def test_a_provider_failure_yields_an_empty_analysis_not_an_exception():
    """One project's inference failing must not abort a batch."""
    analysis = analyse(fake_project(), extractor=BoomLLM())
    assert isinstance(analysis, Analysis)
    assert analysis.empty


def test_an_unparseable_reply_yields_an_empty_analysis():
    assert analyse(fake_project(), extractor=GibberishLLM()).empty
