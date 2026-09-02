"""Agent-backed gap filling: the fact must arrive as a citation, not a column.

Why this path exists at all: `fields_present` is the sole condition holding 283 of
437 rows at T1 on the live database. Every other T2 condition passes on all of
them, so nothing in `logic resolve` or `duplicates resolve` can move one — they are
not wrong, they are empty.

The two properties that matter here are
`test_a_filled_field_survives_a_recompute` (a value stored as a claim is durable,
where an assigned column is not) and `test_a_value_without_a_real_quote_is_refused`
(this must not become `infer` with a search engine attached).
"""

from __future__ import annotations

import json

import pytest

from tracker import gapfill
from tracker.llm import LLMReply, ToolCall
from tracker.models import Project, Source

_ARTICLE = (
    "Vantage Data Centers said its Port Washington campus in Ozaukee County will "
    "reach 1,400 MW at full build-out, representing a total investment of "
    "$2.5 billion across the site. The first hall is expected online in 2027. "
    "The company named OpenAI as the anchor tenant for the campus."
)

_URL = "https://example.test/port-washington"


@pytest.fixture
def thin(session):
    """A real row with an identity, one citation, and empty measurables."""
    project = Project(
        name="Port Washington Campus",
        company="Vantage Data Centers",
        city="Port Washington",
        county="Ozaukee",
        state="WI",
        dedup_key="vantage|city:port washington|WI",
        phase="construction",
    )
    session.add(project)
    session.flush()
    session.add(
        Source(
            project_id=project.id,
            url="https://example.test/announcement",
            source_type="trade_press",
            excerpt="Vantage announced a campus in Port Washington.",
        )
    )
    session.flush()
    return project


@pytest.fixture
def cached(tmp_path, monkeypatch):
    """`_ARTICLE` where `read_article` will find it without a fetch."""
    from tracker import agent
    from tracker.ingest.fetch import cache_path

    cache_path(_URL, tmp_path).write_text(_ARTICLE, encoding="utf-8")
    real = agent.evidence_toolkit
    monkeypatch.setattr(
        agent,
        "evidence_toolkit",
        lambda s, **kw: real(s, cache_dir=tmp_path, allow_search=kw.get("allow_search", True)),
    )
    return tmp_path


class _Model:
    """Reads the article, then reports whatever facts it was built with."""

    def __init__(self, *facts, read: str | None = _URL, tool: str = "record_facts"):
        self.facts, self.read, self.tool = list(facts), read, tool
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
        args = (
            {"facts": self.facts, "reason": "found in the operator's own release"}
            if self.tool == "record_facts"
            else {"reason": "nobody has published a capacity figure"}
        )
        return LLMReply(
            text="",
            tool_calls=(
                ToolCall(id="f", name=self.tool, arguments=args, raw_arguments=json.dumps(args)),
            ),
        )


# --- the property this path exists for ---------------------------------------


def test_a_filled_field_survives_a_recompute(session, thin, cached):
    """Stored as a claim on a citation, so `backfill derive` re-derives it rather
    than reverting it. An assigned column would not survive — see
    `test_triage.py::test_assigning_the_column_does_not_survive`."""
    from tracker.upsert import recompute_from_sources

    model = _Model(
        {
            "field": "mw_planned",
            "value": 1400,
            "url": _URL,
            "quote": (
                "Vantage Data Centers said its Port Washington campus in Ozaukee "
                "County will reach 1,400 MW at full build-out"
            ),
        }
    )

    out = gapfill.fill(session, thin, extractor=model)

    assert out.verdict == "filled", (out.note, out.refused)
    assert thin.mw_planned == 1400.0

    recompute_from_sources(session, thin)
    assert thin.mw_planned == 1400.0, "the filled value did not survive a re-derive"


def test_the_fact_is_stored_as_a_quote_backed_citation(session, thin, cached):
    """The quote is what makes the value `reported` rather than `inferred`, and
    `capex` does not sum `inferred`."""
    model = _Model(
        {
            "field": "investment_usd",
            "value": 2500000000,
            "url": _URL,
            "quote": "representing a total investment of $2.5 billion across the site",
        }
    )

    out = gapfill.fill(session, thin, extractor=model)
    assert out.verdict == "filled", (out.note, out.refused)

    added = next(s for s in thin.sources if s.url == _URL)
    assert json.loads(added.claims)["investment_usd"] == 2500000000
    assert "investment_usd" in (added.fields or ""), "not counted as a quote-backed field"


def test_several_facts_from_one_article_become_one_citation(session, thin, cached):
    """Independence is counted by domain, so three citations for one article would
    look like three sources agreeing."""
    model = _Model(
        {
            "field": "mw_planned",
            "value": 1400,
            "url": _URL,
            "quote": "Port Washington campus in Ozaukee County will reach 1,400 MW at full",
        },
        {
            "field": "investment_usd",
            "value": 2500000000,
            "url": _URL,
            "quote": "representing a total investment of $2.5 billion across the site",
        },
        {
            "field": "customer",
            "value": "OpenAI",
            "url": _URL,
            "quote": "The company named OpenAI as the anchor tenant for the campus",
        },
    )

    out = gapfill.fill(session, thin, extractor=model)

    assert out.verdict == "filled", (out.note, out.refused)
    assert len(out.stored) == 3
    assert sum(1 for s in thin.sources if s.url == _URL) == 1


# --- the refusals, which are the whole safety argument -----------------------


def test_a_value_without_a_real_quote_is_refused(session, thin, cached):
    """Otherwise this is `infer` with a search engine bolted on."""
    model = _Model(
        {
            "field": "mw_planned",
            "value": 900,
            "url": _URL,
            "quote": "the campus is planned at 900 megawatts across three phases",
        }
    )

    out = gapfill.fill(session, thin, extractor=model)

    assert out.verdict == "unusable"
    assert thin.mw_planned is None
    assert any("not in the source text" in r for r in out.refused), out.refused


def test_a_quote_from_a_url_the_run_never_read_is_refused(session, thin, cached):
    """A search snippet is not a document. The quote is checked against what the
    run actually fetched."""
    model = _Model(
        {
            "field": "mw_planned",
            "value": 1400,
            "url": "https://example.test/never-opened",
            "quote": "will reach 1,400 MW at full build-out across the whole campus",
        }
    )

    out = gapfill.fill(session, thin, extractor=model)

    assert out.verdict == "unusable"
    assert any("never read" in r for r in out.refused), out.refused


def test_a_short_quote_is_refused(session, thin, cached):
    """ "1,400 MW" appears in every article about a 1,400 MW site."""
    model = _Model({"field": "mw_planned", "value": 1400, "url": _URL, "quote": "1,400 MW"})

    out = gapfill.fill(session, thin, extractor=model)

    assert out.verdict == "unusable"
    assert any("under" in r for r in out.refused), out.refused


def test_an_unusable_value_is_refused_rather_than_coerced(session, thin, cached):
    model = _Model(
        {
            "field": "mw_planned",
            "value": "about a gigawatt",
            "url": _URL,
            "quote": "Port Washington campus in Ozaukee County will reach 1,400 MW at full",
        }
    )

    out = gapfill.fill(session, thin, extractor=model)

    assert out.verdict == "unusable"
    assert thin.mw_planned is None
    assert any("not a usable mw_planned" in r for r in out.refused), out.refused


def test_a_field_that_is_not_a_gap_is_not_overwritten(session, thin, cached):
    thin.mw_planned = 500.0
    session.flush()

    model = _Model(
        {
            "field": "mw_planned",
            "value": 1400,
            "url": _URL,
            "quote": "Port Washington campus in Ozaukee County will reach 1,400 MW at full",
        }
    )

    out = gapfill.fill(session, thin, extractor=model, gaps=["investment_usd"])

    assert out.verdict == "unusable"
    assert thin.mw_planned == 500.0
    assert any("not a gap" in r for r in out.refused), out.refused


def test_an_identity_field_cannot_be_filled_this_way(session, thin, cached):
    """Identity is never overwritten once set, so a citation claiming it would
    change nothing and only look like it had."""
    assert "company" not in gapfill.FILLABLE_FIELDS
    assert "name" not in gapfill.FILLABLE_FIELDS
    assert "blocker" not in gapfill.FILLABLE_FIELDS


# --- absence is an answer ----------------------------------------------------


def test_nothing_found_is_recorded_as_a_real_answer(session, thin, cached):
    model = _Model(tool="nothing_found")

    out = gapfill.fill(session, thin, extractor=model)

    assert out.verdict == "nothing"
    assert "nobody has published" in out.note
    assert thin.mw_planned is None


def test_a_row_with_no_gaps_costs_nothing(session, thin):
    """No call is made at all — the cheapest possible outcome."""

    class _Never:
        def converse(self, **_kwargs):
            raise AssertionError("should not have called the model")

    out = gapfill.fill(session, thin, extractor=_Never(), gaps=[])

    assert out.verdict == "nothing"
    assert out.prompt_tokens == 0
