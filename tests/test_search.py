"""Search-based discovery: query generation, filtering, and the fabrication guard.

Offline throughout — a fake provider stands in for Google and a fake LLM for
MiniMax.

The most important assertion here is
:func:`test_a_project_the_model_invented_never_reaches_the_database`. Letting a
language model brainstorm project names is only safe because nothing it says is
stored; if that ever stops being true, this file should fail.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from tracker.ingest import search as srch
from tracker.ingest.discover import load_config
from tracker.ingest.search import (
    GoogleCSEProvider,
    QuotaExhausted,
    SearchError,
    SearchHit,
    SearchReport,
    generate_queries,
    hits_to_candidates,
    is_useful_host,
    known_projects,
)
from tracker.llm import LLMError, LLMReply
from tracker.models import IngestUrl, Project


class FakeProvider:
    """Returns canned hits per query, and records what it was asked."""

    def __init__(self, mapping: dict[str, list[SearchHit]], *, fail: set[str] | None = None):
        self.mapping = mapping
        self.fail = fail or set()
        self.calls: list[str] = []

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        self.calls.append(query)
        if query in self.fail:
            raise SearchError(f"boom for {query}")
        return self.mapping.get(query, [])


class QuotaProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        self.calls.append(query)
        raise QuotaExhausted("out of quota")


class FakeLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.seen: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply:
        self.seen.append((system, user))
        return LLMReply(self.replies.pop(0), "stop", 100, 200, "fake-model")


class BoomLLM:
    def complete(self, **_: object) -> LLMReply:
        raise LLMError("provider down")


def hit(url: str, title: str, snippet: str = "", query: str = "q") -> SearchHit:
    return SearchHit(url=url, title=title, snippet=snippet, query=query)


@pytest.fixture
def spec():
    return load_config()[1]


# --- Query generation -------------------------------------------------------


def test_generate_queries_returns_strings_only():
    llm = FakeLLM([json.dumps({"queries": ["a data center campus 500MW", "b data center 1GW"]})])
    queries = generate_queries(llm, count=2)
    assert queries == ["a data center campus 500MW", "b data center 1GW"]
    assert all(isinstance(q, str) for q in queries)


def test_generate_queries_dedupes():
    llm = FakeLLM([json.dumps({"queries": ["same", "same", "other"]})])
    assert generate_queries(llm, count=3) == ["same", "other"]


def test_generate_queries_tolerates_a_fenced_reply():
    """MiniMax ignores response_format, so replies arrive wrapped."""
    llm = FakeLLM(['```json\n{"queries": ["x data center 100MW"]}\n```'])
    assert generate_queries(llm, count=1) == ["x data center 100MW"]


def test_generate_queries_passes_known_projects_so_it_looks_elsewhere():
    llm = FakeLLM([json.dumps({"queries": ["new one"]})])
    generate_queries(llm, count=1, known=["Microsoft — Fairwater (WI)"])
    _, user = llm.seen[0]
    assert "Microsoft — Fairwater (WI)" in user


def test_generate_queries_reports_a_provider_failure():
    with pytest.raises(SearchError, match="could not generate queries"):
        generate_queries(BoomLLM(), count=5)


def test_generate_queries_rejects_a_reply_without_a_query_list():
    llm = FakeLLM([json.dumps({"projects": ["not queries"]})])
    with pytest.raises(SearchError, match="queries"):
        generate_queries(llm, count=1)


def test_generate_queries_rejects_unparseable_output():
    llm = FakeLLM(["I cannot help with that."])
    with pytest.raises(SearchError, match="did not return a query list"):
        generate_queries(llm, count=1)


def test_the_prompt_tells_the_model_its_output_is_not_stored():
    """The instruction and the mechanism must agree, or one of them is a lie."""
    assert "Nothing you output is stored" in srch.QUERY_PROMPT_SYSTEM
    assert "verified against a fetched article" in srch.QUERY_PROMPT_SYSTEM


# --- Host filtering ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://en.wikipedia.org/wiki/Data_center",
        "https://www.linkedin.com/posts/someone",
        "https://www.reddit.com/r/datacenter/comments/x",
        "https://www.indeed.com/jobs?q=data+center",
        "https://www.datacentermap.com/usa/",
    ],
)
def test_aggregators_and_social_sites_are_rejected(url):
    """None of these carry first-hand project reporting."""
    assert is_useful_host(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://news.microsoft.com/source/2026/06/fairwater/",
        "https://www.datacenterknowledge.com/article/x",
        "https://www.wtmj.com/news/2026/06/microsoft/",
    ],
)
def test_real_outlets_are_kept(url):
    assert is_useful_host(url) is True


# --- Hit filtering ----------------------------------------------------------


def test_hits_use_the_same_two_tier_filter(spec):
    report = SearchReport()
    kept = hits_to_candidates(
        [
            hit("https://a.test/1", "Meta breaks ground on 1GW data center campus in Louisiana"),
            hit("https://a.test/2", "The data center industry faces a reckoning"),
            hit("https://a.test/3", "Microsoft Teams adds meeting features"),
        ],
        spec,
        report=report,
    )
    assert [c.url for c in kept] == ["https://a.test/1"]
    assert report.hits == 3
    assert report.filtered == 2


def test_the_snippet_participates_in_matching(spec):
    """A search result has a snippet, which a feed entry does not."""
    report = SearchReport()
    kept = hits_to_candidates(
        [
            hit(
                "https://a.test/1",
                "Groundbreaking in Abilene",
                snippet="The 1.2GW data center campus will cost $5 billion.",
            )
        ],
        spec,
        report=report,
    )
    assert len(kept) == 1


def test_a_search_hit_never_implies_the_topic(spec):
    """A result can come from anywhere, so it must prove for itself what it is about.

    Feeds may declare `topic_implied`; a search hit has no such standing.
    """
    report = SearchReport()
    kept = hits_to_candidates(
        [hit("https://a.test/1", "Crusoe expands Abilene campus to 1.2GW")],
        spec,
        report=report,
    )
    assert kept == [], "no data-center term, and search results get no benefit of the doubt"


def test_duplicate_urls_across_queries_are_kept_once(spec):
    report = SearchReport()
    kept = hits_to_candidates(
        [
            hit("https://a.test/1", "Meta 1GW data center campus", query="q1"),
            hit("https://a.test/1", "Meta 1GW data center campus", query="q2"),
        ],
        spec,
        report=report,
    )
    assert len(kept) == 1


def test_the_query_is_recorded_on_the_candidate(spec):
    """So a queued row shows which query found it."""
    report = SearchReport()
    kept = hits_to_candidates(
        [hit("https://a.test/1", "Meta 1GW data center campus", query="meta louisiana")],
        spec,
        report=report,
    )
    assert kept[0].feed == "search:meta louisiana"


# --- run() ------------------------------------------------------------------


def test_run_queues_matching_hits(session):
    provider = FakeProvider(
        {"q1": [hit("https://a.test/1", "Meta 1GW data center campus in Louisiana", query="q1")]}
    )
    report, _queued = srch.run(session, ["q1"], provider=provider)
    assert report.queries_run == 1
    assert report.queued == 1
    row = session.scalar(select(IngestUrl))
    assert row.status == "discovered"
    assert row.feed == "search:q1"


def test_run_survives_one_failing_query(session):
    provider = FakeProvider(
        {"good": [hit("https://a.test/1", "Meta 1GW data center campus", query="good")]},
        fail={"bad"},
    )
    report, _ = srch.run(session, ["bad", "good"], provider=provider)
    assert report.queries_run == 1
    assert report.queued == 1
    assert report.errors and report.errors[0][0] == "bad"


def test_quota_exhaustion_stops_immediately(session):
    """Every further query would fail the same way, so do not burn the attempts."""
    provider = QuotaProvider()
    report, _ = srch.run(session, ["a", "b", "c"], provider=provider)
    assert provider.calls == ["a"], "must stop after the first quota error"
    assert report.quota_exhausted is True


def test_run_respects_the_query_cap(session, monkeypatch):
    """A bad query set must not be able to exhaust the daily allowance."""
    from tracker.config import get_settings

    monkeypatch.setenv("TRACKER_SEARCH_MAX_QUERIES", "2")
    get_settings.cache_clear()
    provider = FakeProvider({})
    srch.run(session, ["a", "b", "c", "d"], provider=provider)
    assert provider.calls == ["a", "b"]
    get_settings.cache_clear()


def test_run_does_not_requeue_a_known_url(session):
    session.add(IngestUrl(url="https://a.test/1", run_id="prev", status="ok"))
    session.flush()
    provider = FakeProvider(
        {"q": [hit("https://a.test/1", "Meta 1GW data center campus", query="q")]}
    )
    report, queued = srch.run(session, ["q"], provider=provider)
    assert queued == []
    assert report.already_known == 1
    assert session.scalar(select(IngestUrl)).status == "ok", "left exactly as it was"


def test_run_dry_run_writes_nothing(session):
    provider = FakeProvider(
        {"q": [hit("https://a.test/1", "Meta 1GW data center campus", query="q")]}
    )
    report, candidates = srch.run(session, ["q"], provider=provider, dry_run=True)
    assert report.queued == 1, "the report still describes what would happen"
    assert candidates
    assert list(session.scalars(select(IngestUrl))) == []


# --- The fabrication guard --------------------------------------------------


def test_a_project_the_model_invented_never_reaches_the_database(session):
    """The load-bearing property of this whole module.

    The model is allowed to guess which projects exist because a guess only ever
    becomes a search query. If the search returns nothing, nothing is written --
    no project, no source, not even a queued URL.
    """
    llm = FakeLLM([json.dumps({"queries": ["Fictional Corp Atlantis data center 900MW Nevada"]})])
    queries = generate_queries(llm, count=1)

    provider = FakeProvider({})  # the invented project has no coverage
    report, queued = srch.run(session, queries, provider=provider)

    assert report.queries_run == 1
    assert report.hits == 0
    assert queued == []
    assert list(session.scalars(select(Project))) == [], "no project from an unverified name"
    assert list(session.scalars(select(IngestUrl))) == [], "not even a queued URL"


def test_search_only_ever_produces_queued_urls_never_projects(session):
    """Search cannot write a project directly; only the crawl path can."""
    provider = FakeProvider(
        {"q": [hit("https://a.test/1", "Meta 1GW data center campus in Louisiana", query="q")]}
    )
    srch.run(session, ["q"], provider=provider)
    assert list(session.scalars(select(Project))) == []
    assert len(list(session.scalars(select(IngestUrl)))) == 1


# --- known_projects ---------------------------------------------------------


def test_known_projects_lists_what_is_already_tracked(session):
    from tracker.ingest.records import IngestRecord, SourceRecord
    from tracker.models import utcnow
    from tracker.upsert import upsert_record

    upsert_record(
        session,
        IngestRecord(
            project={"company": "Meta", "name": "Hyperion", "city": "Delhi", "state": "LA"},
            sources=[
                SourceRecord(
                    url="https://a.test/x",
                    source_type="trade_press",
                    fetched_at=utcnow(),
                    claims={"company": "Meta", "city": "Delhi", "state": "LA"},
                )
            ],
        ),
    )
    assert known_projects(session) == ["Meta — Hyperion (LA)"]


# --- Provider configuration -------------------------------------------------


def test_provider_refuses_to_construct_without_keys(monkeypatch):
    from tracker.config import get_settings

    monkeypatch.delenv("TRACKER_GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("TRACKER_GOOGLE_CSE_ID", raising=False)
    get_settings.cache_clear()
    with pytest.raises(SearchError, match="programmablesearchengine"):
        GoogleCSEProvider()
    get_settings.cache_clear()


def test_the_key_help_names_both_required_values():
    assert "TRACKER_GOOGLE_API_KEY" in srch.SEARCH_KEY_HELP
    assert "TRACKER_GOOGLE_CSE_ID" in srch.SEARCH_KEY_HELP
    assert "--print-only" in srch.SEARCH_KEY_HELP, "must offer the no-key fallback"


def test_the_provider_uses_the_official_api_not_a_scraped_page():
    """Scraping result pages breaks Google's terms and would contradict this
    project's refusal to defeat other sites' access controls."""
    assert GoogleCSEProvider.ENDPOINT == "https://www.googleapis.com/customsearch/v1"
