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

    def __init__(
        self,
        tool: str,
        *,
        confidence: float,
        quote: str = "",
        read: str = "",
        contradictions: list[str] | None = None,
    ):
        self.tool, self.confidence, self.quote, self.read = tool, confidence, quote, read
        self.contradictions = contradictions
        self.systems: list[str] = []
        self.turn = 0

    def converse(self, *, system, messages, tools=None, max_tokens=None):
        self.turn += 1
        self.systems.append(system)
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
        if self.contradictions is not None:
            args["contradictions"] = self.contradictions
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
    a = _project(
        session, name="Polaris Forge 1", city="Ellendale", dedup_key="ad|city:ellendale|ND"
    )
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


# --- the rails that had drifted ---------------------------------------------
#
# `pair_triage` restated two of `merge_blocked`'s rails by hand and lacked the
# others. Each test here is a rail the docs promised and the code did not check,
# pinned against `dupresolve.evidence_blocks_merge` so the two judges cannot
# disagree again without a test saying so.


def _pair(a, b, **kw) -> DuplicatePair:
    return DuplicatePair(
        a_id=a.id,
        a_company=a.company,
        a_name=a.name,
        b_id=b.id,
        b_company=b.company,
        b_name=b.name,
        locality=a.city or a.county or "",
        state=a.state,
        b_mw=100.0,
        **kw,
    )


def test_a_market_sequence_tranche_alone_does_not_carry_an_agent_merge(session, cached):
    """`hillsboro-1` is held by Flexential's Hillsboro site and NTT's. The key names
    a market and a sequence number, so it is no more identity for a judge that read
    the articles than for one that did not — and the docstring already said so."""
    a = _project(
        session,
        company="Flexential",
        name="Portland 3",
        city="Hillsboro",
        state="OR",
        dedup_key="flexential|city:hillsboro|OR",
    )
    b = _project(
        session,
        company="NTT",
        name="Global Hillsboro Data Center",
        city="Hillsboro",
        state="OR",
        dedup_key="ntt|city:hillsboro|OR",
    )
    pair = _pair(a, b, shared_blocks=("hillsboro-1",))
    assert pair.kinds == ("tranche",)

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model("rule_same", confidence=0.99, quote="x"),
        allow_merge=True,
        require_quote=False,
    )

    assert got.action == "left"
    assert "market and a sequence number" in got.detail
    assert session.query(Project).count() == 2


def test_a_name_word_alone_does_not_carry_an_agent_merge(session, cached):
    """`--weak` puts name-only pairs in front of the agent so they can be *parked*.
    "A shared name word is a word" holds for a merge whoever is judging."""
    a = _project(
        session,
        company="Aligned",
        name="Stingray Campus",
        city="Andrews",
        state="TX",
        dedup_key="aligned|city:andrews|TX",
    )
    b = _project(
        session,
        company="Cipher",
        name="Stingray Facility",
        city="Andrews",
        state="TX",
        dedup_key="cipher|city:andrews|TX",
    )
    pair = _pair(a, b, shared_tokens=("stingray",))
    assert pair.kinds == ("name",)

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model("rule_same", confidence=0.99, quote="x"),
        allow_merge=True,
        require_quote=False,
    )

    assert got.action == "left"
    assert "shared name word" in got.detail
    assert session.query(Project).count() == 2


def test_granularity_beside_a_market_sequence_is_still_the_agents_to_rule(
    session, pair_rows, cached
):
    """The asymmetry, pinned. Granularity alone is the refusal the agent may pass;
    adding a market-sequence key to it must not make the pair *less* mergeable."""
    a, b, pair = pair_rows
    pair = _pair(a, b, shared_keys=pair.shared_keys, shared_blocks=("phx-1",))
    assert set(pair.kinds) == {"identity", "tranche"}

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model(
            "rule_same",
            confidence=0.95,
            quote="its El Mirage campus, in Maricopa County, is the same development",
            read="https://example.test/el-mirage",
        ),
        allow_merge=True,
    )

    assert got.action == "merged", got.detail


def test_the_two_judges_apply_the_same_evidence_rails(session, pair_rows):
    """Everything but the granularity exception is one function, and the one-call
    path's `merge_blocked` is that function behind its own confidence floor."""
    from tracker.dupresolve import Judgement, evidence_blocks_merge, merge_blocked

    a, b, pair = pair_rows
    a.lat, a.lon = 33.61, -112.32
    b.lat, b.lon = 35.20, -111.65
    session.flush()
    confident = Judgement("same", 0.99, "one campus")

    for kw in ({"shared_keys": pair.shared_keys}, {"shared_blocks": ("phx-1",)}, {"exact": True}):
        candidate = _pair(a, b, **kw)
        assert merge_blocked(candidate, confident, a, b) == evidence_blocks_merge(candidate, a, b)
        if "identity" not in candidate.kinds:
            reading = evidence_blocks_merge(candidate, a, b, judge_read_the_sources=True)
            assert reading == evidence_blocks_merge(candidate, a, b)


def test_a_park_below_the_floor_is_left_in_the_report(session, pair_rows, cached):
    """The one-call path has refused a park under 0.6 since it was written; the agent
    path parked at any confidence. A park releases capacity into a published total,
    so a shrug must not be the thing that does it."""
    _a, _b, pair = pair_rows

    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model("rule_different", confidence=0.3),
        allow_merge=False,
    )

    assert got.action == "left"
    assert "below the 0.6 floor" in got.detail
    from tracker.pairs import parked_keys

    assert not parked_keys(session)


def test_the_decision_names_both_rows(session, pair_rows, cached):
    """`DuplicatePair` has no `label`; the agent path read one anyway and every line
    of a run printed its verdict against an empty string."""
    a, b, pair = pair_rows

    got = triage.pair_triage(session, pair, extractor=_Model("leave_alone", confidence=0.0))

    assert f"#{a.id}" in got.label and f"#{b.id}" in got.label
    assert a.name in got.label and b.name in got.label


# --- what the judge checked, and the question it was asked ----------------------
#
# Framing the question as a search for contradictions is borrowed from OpenSanctions
# Pairs (arXiv 2603.11051). It is worth measuring here because six of the seven pairs
# a judge has ruled `different` on this database have no rail refusing them at all —
# see `docs/duplicate-shapes.md`. On that population the judgement is the whole
# decision, so what the judge is asked, and what it says it checked, are the product.


def test_the_pair_judges_are_asked_the_same_question(session):
    """One checklist, not three that drift. The prompt files mirror the constant."""
    from tracker.prompts import load_prompt

    assert triage.CONTRADICTIONS in triage.PAIR_SYSTEM
    assert triage.PAIR_SYSTEM_BASE in triage.PAIR_SYSTEM
    assert triage.CONTRADICTIONS in load_prompt("duplicates-resolve-v3").system


def test_what_it_ruled_out_is_recorded_on_the_parked_pair(session, pair_rows, cached):
    """`duplicates parked` is read months later by somebody deciding whether to
    reopen a question, and "different sites" is a far weaker thing to inherit than
    the list of what was checked."""
    from tracker.pairs import listing

    _a, _b, pair = pair_rows
    got = triage.pair_triage(
        session,
        pair,
        extractor=_Model(
            "rule_different",
            confidence=0.9,
            contradictions=["two addresses: 100 Innovation Dr vs 4500 Beaumont Rd"],
            read="https://example.test/el-mirage",
        ),
        allow_merge=False,
    )

    assert got.action == "parked", got.detail
    reason = listing(session)[0].reason
    assert "ruled out:" in reason and "100 Innovation Dr" in reason


def test_a_judge_that_reports_nothing_is_still_obeyed(session, pair_rows, cached):
    """The rails decide what may happen to a pair; this field only records the
    account. A model that omits it must not be refused."""
    from tracker.pairs import parked_keys

    a, b, pair = pair_rows
    got = triage.pair_triage(
        session, pair, extractor=_Model("rule_different", confidence=0.9), allow_merge=False
    )

    assert got.action == "parked", got.detail
    assert (min(a.id, b.id), max(a.id, b.id)) in parked_keys(session)


def test_a_contradictions_field_of_the_wrong_shape_is_ignored(session, pair_rows, cached):
    """A string where a list belongs, or a number, must not end the run."""
    _a, _b, pair = pair_rows
    model = _Model("rule_different", confidence=0.9, contradictions="one address, not two")

    got = triage.pair_triage(session, pair, extractor=model, allow_merge=False)

    assert got.action == "parked", got.detail


def test_the_baseline_prompt_can_be_put_to_the_same_pair(session, pair_rows, cached):
    """What makes the change measurable: `scripts/eval_pairs.py` runs both arms on
    one pair without keeping a stale copy of the old wording."""
    _a, _b, pair = pair_rows
    model = _Model("rule_different", confidence=0.9)

    triage.pair_triage(
        session, pair, extractor=model, allow_merge=False, system=triage.PAIR_SYSTEM_BASE
    )

    assert model.systems, "the run must have reached the model"
    assert triage.CONTRADICTIONS not in model.systems[0]
    assert model.systems[0] == triage.PAIR_SYSTEM_BASE
