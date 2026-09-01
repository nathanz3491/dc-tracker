"""Agent-backed duplicate judgement: which rails survived, and which did not.

The design question this file pins. `dupresolve.merge_blocked` refuses a
cross-granularity pair categorically, on the stated grounds that "a model is not a
person with a map". That was true of a model shown two rows and nothing else.
Measured on the live database: it ruled 45 pairs "same" at 0.80-0.85 with
containment reasoning of its own, and every one was left unactionable, holding 78
rows out of T1 — the largest single blocker in the database.

An agent can read the articles and search, so that refusal goes. The rails that
are *fact checks* rather than menus stay, and each has a test here: distance,
ordinal siblings, the confidence floor, and a quote from something actually read.
"""

from __future__ import annotations

import json

import pytest

from tracker import triage
from tracker.capex import DuplicatePair
from tracker.llm import LLMReply, ToolCall
from tracker.models import Project, Source

_ARTICLE = (
    "Compass Datacenters confirmed that its El Mirage campus, in Maricopa County, "
    "is the same development the company previously announced at county level. "
    "The site is a single campus of four buildings on one parcel."
)


def _project(session, **kw):
    defaults = {"state": "AZ", "phase": "construction", "company": "Compass Datacenters"}
    project = Project(**{**defaults, **kw})
    session.add(project)
    session.flush()
    return project


@pytest.fixture
def pair_rows(session):
    """A city row and a county row for one campus — the 28-of-47 shape."""
    a = _project(
        session,
        name="Phoenix - El Mirage",
        city="El Mirage",
        dedup_key="compass|city:el mirage|AZ",
    )
    b = _project(
        session,
        name="Compass Datacenters Maricopa County",
        county="Maricopa",
        dedup_key="compass|county:maricopa|AZ",
    )
    session.add(
        Source(
            project_id=a.id,
            url="https://example.test/el-mirage",
            source_type="trade_press",
            excerpt="El Mirage campus.",
            fields="mw_planned",
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
        # `kinds` is derived: "identity" is what shared_keys means.
        shared_keys=("compass|county:maricopa|AZ",),
    )
    return a, b, pair


class _Model:
    """Reads one article, then returns the verdict it was built with."""

    def __init__(self, tool: str, *, confidence: float, quote: str = "", read: str = ""):
        self.tool, self.confidence, self.quote, self.read = tool, confidence, quote, read
        self.turn = 0

    def converse(self, *, system, messages, tools=None, max_tokens=None):
        self.turn += 1
        if self.turn == 1 and self.read:
            args = {"url": self.read}
            return LLMReply(
                text="",
                tool_calls=(
                    ToolCall(
                        id="r", name="read_article", arguments=args, raw_arguments=json.dumps(args)
                    ),
                ),
            )
        args = {"reason": "one campus at two precisions", "confidence": self.confidence}
        if self.quote:
            args["quote"] = self.quote
        return LLMReply(
            text="",
            tool_calls=(
                ToolCall(id="v", name=self.tool, arguments=args, raw_arguments=json.dumps(args)),
            ),
        )


@pytest.fixture
def cached(tmp_path, monkeypatch):
    """`_ARTICLE` in a cache dir the toolkit will read instead of fetching."""
    from tracker import agent
    from tracker.ingest.fetch import cache_path

    cache_path("https://example.test/el-mirage", tmp_path).write_text(_ARTICLE, encoding="utf-8")
    real = agent.evidence_toolkit
    monkeypatch.setattr(
        agent,
        "evidence_toolkit",
        lambda s, **kw: real(s, cache_dir=tmp_path, allow_search=kw.get("allow_search", True)),
    )
    return tmp_path


# --- the refusal that was dropped on purpose --------------------------------


def test_a_cross_granularity_pair_can_now_be_merged(session, pair_rows, cached):
    """The whole point. `merge_blocked` refuses this shape categorically; an agent
    that has read the article may act on it."""
    a, b, pair = pair_rows
    model = _Model(
        "rule_same",
        confidence=0.95,
        quote="its El Mirage campus, in Maricopa County, is the same development",
        read="https://example.test/el-mirage",
    )

    got = triage.pair_triage(session, pair, extractor=model, allow_merge=True)

    assert got.action == "merged", got.detail
    assert got.kept_id in {a.id, b.id}


def test_the_old_path_still_refuses_the_same_pair(session, pair_rows):
    """Pinned so the comparison in the docstring stays true and checkable."""
    from tracker.dupresolve import Judgement, merge_blocked

    a, b, pair = pair_rows
    blocked = merge_blocked(pair, Judgement("same", 0.99, "one campus"), a, b)

    assert blocked and "cross-granularity" in blocked


# --- the rails that stayed, because they are fact checks --------------------


def test_rows_too_far_apart_are_not_merged_whatever_the_model_says(session, pair_rows, cached):
    """Geography outranks the model."""
    a, b, pair = pair_rows
    a.lat, a.lon = 33.61, -112.32  # El Mirage
    b.lat, b.lon = 35.20, -111.65  # Flagstaff, ~200 km away
    session.flush()

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model(
            "rule_same",
            confidence=0.99,
            quote="is the same development",
            read="https://example.test/el-mirage",
        ),
        allow_merge=True,
    )

    assert got.action == "left"
    assert "km apart" in got.detail


def test_names_differing_only_by_an_ordinal_are_not_merged(session, cached):
    a = _project(session, name="Polaris Forge 1", city="Ellendale", dedup_key="ad|city:ellendale|ND")
    b = _project(session, name="Polaris Forge 2", city="Harwood", dedup_key="ad|city:harwood|ND")
    pair = DuplicatePair(
        a_id=a.id,
        a_company=a.company,
        a_name=a.name,
        b_id=b.id,
        b_company=b.company,
        b_name=b.name,
        locality="ND",
        state="ND",
        b_mw=100.0,
        shared_blocks=("forge-2.polaris",),
    )

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model("rule_same", confidence=0.99, quote="x"),
        allow_merge=True,
        require_quote=False,
    )

    assert got.action == "left"
    assert "ordinal" in got.detail


def test_confidence_below_the_floor_does_not_merge(session, pair_rows, cached):
    _a, _b, pair = pair_rows

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model(
            "rule_same",
            confidence=0.60,
            quote="is the same development",
            read="https://example.test/el-mirage",
        ),
        allow_merge=True,
        min_confidence=0.85,
    )

    assert got.action == "left"
    assert "below" in got.detail


def test_a_merge_needs_a_quote_from_something_the_run_actually_read(session, pair_rows, cached):
    _a, _b, pair = pair_rows

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model(
            "rule_same",
            confidence=0.99,
            quote="the two sites share a substation",  # not in the article
            read="https://example.test/el-mirage",
        ),
        allow_merge=True,
    )

    assert got.action == "left"
    assert "no sentence" in got.detail


def test_without_allow_merge_a_same_verdict_only_reports(session, pair_rows, cached):
    _a, _b, pair = pair_rows

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model(
            "rule_same",
            confidence=0.99,
            quote="is the same development",
            read="https://example.test/el-mirage",
        ),
        allow_merge=False,
    )

    assert got.action == "left"
    assert "needs --merge" in got.detail


# --- the half that needs no rails -------------------------------------------


def test_different_parks_the_pair_without_allow_merge(session, pair_rows, cached):
    """Parking is the half that releases capacity, and it is reversible."""
    a, b, pair = pair_rows

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model("rule_different", confidence=0.80),
        allow_merge=False,
    )

    assert got.action == "parked", got.detail
    from tracker.pairs import parked_keys

    assert (min(a.id, b.id), max(a.id, b.id)) in parked_keys(session)


def test_leave_alone_changes_nothing(session, pair_rows, cached):
    a, b, pair = pair_rows

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model("leave_alone", confidence=0.0),
        allow_merge=True,
    )

    assert got.action == "left"
    assert session.get(Project, a.id) is not None
    assert session.get(Project, b.id) is not None


def test_a_row_already_folded_this_run_is_asked_about_its_survivor(session, pair_rows, cached):
    """What settles a group of nine in one pass instead of one merge per run."""
    a, b, pair = pair_rows

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model("rule_same", confidence=0.99),
        allow_merge=True,
        folded={a.id: b.id},
    )

    assert got.action == "left"
    assert "gone" in got.detail
