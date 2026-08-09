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

import httpx
import pytest
import respx
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
        "https://www.linkedin.com/posts/someone",
        "https://www.reddit.com/r/datacenter/comments/x",
        "https://www.indeed.com/jobs?q=data+center",
        "https://www.datacentermap.com/usa/",
        "https://x.com/elonmusk/status/1",
        "https://mobile.x.com/someone/status/2",
        "https://www.bloomberg.com/profile/company/0117059D:US",
        # Measured on a live `enrich 10`: four Instagram URLs fetched, 0 characters
        # of prose in each. A reel has no sentence for the gate to quote.
        "https://www.instagram.com/reel/Davo4TaglwZ/",
        "https://www.instagram.com/p/DavxJuyljGD/",
        "https://www.tiktok.com/@someone/video/123",
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
        # Wikipedia is deliberately allowed: the article is quotable, its
        # references are mined, and confidence.TERTIARY_DOMAINS keeps it from
        # ever corroborating the coverage it summarizes.
        "https://en.wikipedia.org/wiki/Hyperion_Data_Center",
    ],
)
def test_real_outlets_are_kept(url):
    assert is_useful_host(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # "x.com" must block only x.com itself, never a host that merely ends in
        # those letters. The substring matcher this pins against silently blocked
        # a top-five operator whose newsroom already answers 403 — search was the
        # one path to its coverage.
        "https://www.equinix.com/newsroom/press-releases/2026/dc-expansion",
        "https://blog.equinix.com/blog/2026/x/",
        "https://www.spacex.com/updates/",
        # Bloomberg articles were never meant to be blocked, only /profile stubs.
        "https://www.bloomberg.com/news/articles/2026-01-01/meta-data-center",
    ],
)
def test_domains_are_matched_on_boundaries_not_substrings(url):
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


def test_run_mines_wikipedia_hits_for_their_references(session, monkeypatch):
    """The top hit for a tracked campus is routinely its Wikipedia article, and
    the article's references name the primary sources. Both must queue."""
    from tracker.ingest import wiki

    monkeypatch.setattr(
        wiki,
        "external_links",
        lambda url, settings=None: [
            "https://investor.example/press/hyperion-data-center-joint-venture",
        ],
    )
    provider = FakeProvider(
        {
            "q": [
                hit(
                    "https://en.wikipedia.org/wiki/Hyperion_Data_Center",
                    "Hyperion Data Center - Wikipedia",
                    snippet="a 2,000 megawatt data center campus in Louisiana",
                    query="q",
                )
            ]
        }
    )
    report, _ = srch.run(session, ["q"], provider=provider)
    urls = {row.url for row in session.scalars(select(IngestUrl))}
    assert "https://en.wikipedia.org/wiki/Hyperion_Data_Center" in urls
    assert "https://investor.example/press/hyperion-data-center-joint-venture" in urls
    assert report.wiki_mined == 1
    mined = session.scalar(
        select(IngestUrl).where(IngestUrl.url.like("https://investor.example/%"))
    )
    assert mined.feed == "wikipedia:Hyperion_Data_Center"


def test_run_mines_even_when_the_wiki_page_itself_is_filtered(session, monkeypatch):
    """An opaque snippet can fail the keyword filter while the article's
    references are still exactly what we want, so mining reads the raw hits."""
    from tracker.ingest import wiki

    monkeypatch.setattr(
        wiki,
        "external_links",
        lambda url, settings=None: [
            "https://investor.example/press/hyperion-data-center-joint-venture",
        ],
    )
    provider = FakeProvider(
        {"q": [hit("https://en.wikipedia.org/wiki/Hyperion", "Hyperion", query="q")]}
    )
    _report, _ = srch.run(session, ["q"], provider=provider)
    urls = {row.url for row in session.scalars(select(IngestUrl))}
    assert "https://en.wikipedia.org/wiki/Hyperion" not in urls, "the page failed the filter"
    assert "https://investor.example/press/hyperion-data-center-joint-venture" in urls


def test_run_can_skip_mining(session):
    provider = FakeProvider(
        {"q": [hit("https://en.wikipedia.org/wiki/Hyperion", "Hyperion", query="q")]}
    )
    report, _ = srch.run(session, ["q"], provider=provider, mine_wikipedia=False)
    assert report.wiki_mined == 0
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


# --- Backend selection ------------------------------------------------------
#
# Bing is the backend people ask for and the one that no longer exists: Microsoft
# retired the standalone Bing Search APIs on 2025-08-11. These tests pin the
# behaviour that an operator who asks for it gets the reason, not a shrug.


def settings_with(**kwargs):
    """Settings that ignore the operator's real .env."""
    from tracker.config import Settings

    Settings.model_config["env_file"] = None
    return Settings(minimax_api_key="x", **kwargs)


def test_bing_is_refused_with_the_reason_not_unknown_provider():
    with pytest.raises(SearchError) as exc:
        srch.build_provider(settings_with(brave_api_key="k"), name="bing")
    message = str(exc.value)
    assert "retired" in message.lower()
    assert "2025-08-11" in message, "say when, so the claim is checkable"
    assert "TRACKER_BRAVE_API_KEY" in message, "name the drop-in replacement"


def test_an_unknown_provider_lists_the_real_ones():
    with pytest.raises(SearchError, match="unknown search provider"):
        srch.build_provider(settings_with(brave_api_key="k"), name="altavista")


def test_auto_picks_whichever_backend_has_a_key():
    assert isinstance(srch.build_provider(settings_with(brave_api_key="k")), srch.BraveProvider)
    assert isinstance(srch.build_provider(settings_with(serper_api_key="k")), srch.SerperProvider)
    google = settings_with(google_api_key="k", google_cse_id="cx")
    assert isinstance(srch.build_provider(google), srch.GoogleCSEProvider)


def test_an_explicit_provider_without_its_key_fails_loudly():
    """Never silently fall back to a different engine than the one asked for."""
    both = settings_with(brave_api_key="k", search_provider="google")
    with pytest.raises(SearchError):
        srch.build_provider(both)


def test_no_keys_at_all_raises_the_help():
    with pytest.raises(SearchError, match="TRACKER_BRAVE_API_KEY"):
        srch.build_provider(settings_with())


def test_the_google_provider_no_longer_accepts_a_brave_only_setup():
    """Regression: `has_search_keys` widened to "any backend"."""
    with pytest.raises(SearchError):
        GoogleCSEProvider(settings_with(brave_api_key="k"))


def test_the_key_help_names_every_backend():
    for token in ("TRACKER_BRAVE_API_KEY", "TRACKER_GOOGLE_API_KEY", "TRACKER_SERPER_API_KEY"):
        assert token in srch.SEARCH_KEY_HELP
    assert "TRACKER_SEARCH_PROVIDER" in srch.SEARCH_KEY_HELP


def test_every_backend_uses_an_official_api_not_a_scraped_page():
    """Scraping result pages breaks the engines' terms and would contradict this
    project's refusal to defeat other sites' access controls."""
    endpoints = {name: cls.ENDPOINT for name, cls in srch.PROVIDERS.items()}
    assert endpoints == {
        "google": "https://www.googleapis.com/customsearch/v1",
        "brave": "https://api.search.brave.com/res/v1/web/search",
        "serper": "https://google.serper.dev/search",
        "bocha": "https://api.bochaai.com/v1/web-search",
    }


# --- Bocha over the wire ----------------------------------------------------


@respx.mock
def test_bocha_parses_its_own_response_shape():
    """Bocha names the title "name" and nests results under data.webPages.value."""
    respx.post(srch.BochaProvider.ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": "STACK expands in Hillsboro",
                                "url": "https://dcf.example/stack-hillsboro",
                                "snippet": "230 megawatts",
                                "summary": "longer text",
                            },
                            {"name": "no url, must be dropped"},
                        ]
                    }
                },
            },
        )
    )
    hits = srch.BochaProvider(settings_with(bocha_api_key="k")).search("stack hillsboro")
    assert len(hits) == 1
    assert hits[0].url == "https://dcf.example/stack-hillsboro"
    assert hits[0].title == "STACK expands in Hillsboro"
    assert hits[0].snippet == "230 megawatts"


@respx.mock
def test_bocha_treats_an_error_code_in_a_200_body_as_a_failure():
    """It answers HTTP 200 with the real status in `code`, so the status line lies."""
    respx.post(srch.BochaProvider.ENDPOINT).mock(
        return_value=httpx.Response(200, json={"code": 401, "msg": "invalid api key"})
    )
    with pytest.raises(SearchError, match="invalid api key"):
        srch.BochaProvider(settings_with(bocha_api_key="k")).search("q")


@respx.mock
def test_bocha_sends_the_key_as_a_bearer_header():
    route = respx.post(srch.BochaProvider.ENDPOINT).mock(
        return_value=httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": []}}})
    )
    srch.BochaProvider(settings_with(bocha_api_key="secret-key")).search("q")

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer secret-key"
    assert "secret-key" not in str(request.url)


@respx.mock
def test_bocha_survives_a_missing_data_block():
    respx.post(srch.BochaProvider.ENDPOINT).mock(
        return_value=httpx.Response(200, json={"code": 200})
    )
    assert srch.BochaProvider(settings_with(bocha_api_key="k")).search("q") == []


def test_bocha_is_the_last_backend_auto_picks():
    """Its index is thin on US trade press, so it should never displace a better one."""
    both = settings_with(serper_api_key="s", bocha_api_key="b")
    assert both.resolve_search_provider() == "serper"
    assert settings_with(bocha_api_key="b").resolve_search_provider() == "bocha"


# --- Host filtering ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Chinese portals and UGC: reposts and translations of US coverage, never
        # first-hand. Measured: one Bocha query returned five of these at once.
        "https://www.sohu.com/a/722030929_100161396",
        "https://zhuanlan.zhihu.com/p/584323426",
        "https://www.toutiao.com/article/123",
        "https://m.blog.csdn.net/x",
        "https://juejin.cn/post/1",
        "https://guba.eastmoney.com/news",
        "https://xueqiu.com/4434592433/316920250",
        "https://www.163.com/dy/article/x.html",
        "https://new.qq.com/rain/a/x",
        # Document dumps and academic indexes
        "https://max.book118.com/html/2019/x.shtm",
        "https://www.researchgate.net/publication/1",
        "https://dl.acm.org/doi/10.1145/1",
    ],
)
def test_second_hand_and_academic_hosts_are_dropped(url):
    """Each of these would otherwise cost a fetch and an LLM call to discard.

    Worse than the cost: a quote from a Chinese translation cannot support an
    English value through the evidence gate, so nothing citable comes back either.
    """
    assert not is_useful_host(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.datacenterfrontier.com/hyperscale/article/1/stack-hillsboro",
        "https://www.datacenterknowledge.com/supercomputers/x",
        "https://www.utilitydive.com/news/x/",
        "https://news.microsoft.com/source/features/x/",
        "https://racinecountyeye.com/2026/06/24/x/",
    ],
)
def test_real_reporting_still_passes(url):
    """The blocklist must not swallow the outlets this tracker actually cites."""
    assert is_useful_host(url)


# --- Brave over the wire ----------------------------------------------------


@respx.mock
def test_brave_parses_its_own_response_shape():
    """Brave names the snippet "description", not "snippet"."""
    respx.get(srch.BraveProvider.ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "url": "https://dcf.example/stack-hillsboro",
                            "title": "STACK expands in Hillsboro",
                            "description": "The campus will draw 230 megawatts.",
                        },
                        {"title": "no url, must be dropped"},
                    ]
                }
            },
        )
    )
    hits = srch.BraveProvider(settings_with(brave_api_key="k")).search("stack hillsboro")
    assert len(hits) == 1
    assert hits[0].url == "https://dcf.example/stack-hillsboro"
    assert hits[0].snippet == "The campus will draw 230 megawatts."
    assert hits[0].query == "stack hillsboro"


@respx.mock
def test_brave_sends_the_key_as_a_header_not_a_query_parameter():
    """A key in the URL leaks into logs and referrers."""
    route = respx.get(srch.BraveProvider.ENDPOINT).mock(
        return_value=httpx.Response(200, json={"web": {"results": []}})
    )
    srch.BraveProvider(settings_with(brave_api_key="secret-key")).search("q")

    request = route.calls.last.request
    assert request.headers["X-Subscription-Token"] == "secret-key"
    assert "secret-key" not in str(request.url)


@respx.mock
def test_brave_retries_once_through_the_per_second_rate_limit(monkeypatch):
    """The free tier answers 429 for pacing, not only for an exhausted quota.

    Treating the first 429 as fatal would abandon a run that a one-second pause
    would have completed.
    """
    monkeypatch.setattr(srch.BraveProvider, "RATE_LIMIT_PAUSE_S", 0)
    route = respx.get(srch.BraveProvider.ENDPOINT).mock(
        side_effect=[
            httpx.Response(429, text="Too Many Requests"),
            httpx.Response(200, json={"web": {"results": [{"url": "https://a.example/x"}]}}),
        ]
    )
    hits = srch.BraveProvider(settings_with(brave_api_key="k")).search("q")
    assert [h.url for h in hits] == ["https://a.example/x"]
    assert route.call_count == 2


@respx.mock
def test_brave_gives_up_after_a_second_rate_limit(monkeypatch):
    monkeypatch.setattr(srch.BraveProvider, "RATE_LIMIT_PAUSE_S", 0)
    respx.get(srch.BraveProvider.ENDPOINT).mock(return_value=httpx.Response(429))
    with pytest.raises(QuotaExhausted, match="2000 per month"):
        srch.BraveProvider(settings_with(brave_api_key="k")).search("q")


@respx.mock
def test_brave_names_the_variable_when_the_key_is_rejected():
    respx.get(srch.BraveProvider.ENDPOINT).mock(return_value=httpx.Response(401, text="nope"))
    with pytest.raises(SearchError, match="TRACKER_BRAVE_API_KEY"):
        srch.BraveProvider(settings_with(brave_api_key="k")).search("q")


@respx.mock
def test_brave_asks_for_us_english_results():
    """The tracker is US-only; foreign hits cost an LLM call to discover and drop."""
    route = respx.get(srch.BraveProvider.ENDPOINT).mock(
        return_value=httpx.Response(200, json={"web": {"results": []}})
    )
    srch.BraveProvider(settings_with(brave_api_key="k")).search("q", limit=10)
    params = route.calls.last.request.url.params
    assert params["country"] == "us"
    assert params["search_lang"] == "en"


@respx.mock
def test_serper_parses_organic_results():
    respx.post(srch.SerperProvider.ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "link": "https://dcf.example/a",
                        "title": "A",
                        "snippet": "230 megawatts",
                    },
                    {"title": "no link"},
                ]
            },
        )
    )
    hits = srch.SerperProvider(settings_with(serper_api_key="k")).search("q")
    assert [h.url for h in hits] == ["https://dcf.example/a"]
    assert hits[0].snippet == "230 megawatts"


@respx.mock
def test_a_provider_swap_needs_no_change_anywhere_else():
    """The point of the protocol: `run()` never learns which engine answered."""
    respx.get(srch.BraveProvider.ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "url": "https://www.datacenterfrontier.com/x/stack-hillsboro-230mw",
                            "title": "STACK breaks ground on 230 MW Hillsboro data center campus",
                            "description": "megawatts",
                        }
                    ]
                }
            },
        )
    )
    hits = srch.BraveProvider(settings_with(brave_api_key="k")).search("q")
    _, spec = load_config()
    candidates = hits_to_candidates(hits, spec, report=SearchReport())
    assert [c.url for c in candidates] == [
        "https://www.datacenterfrontier.com/x/stack-hillsboro-230mw"
    ]


# --- Language filtering -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Stack Infrastructure扩建多伦多数据中心,新增48MW容量_园区_开发_工程",
        "Stack Infrastructure计划在美国凤凰城建立新的数据中心园区_知乎",
        "豪掷 2000 亿美元,消息称 Meta 正洽谈 AI 数据中心园区新项目_公司_人民币",
    ],
)
def test_translated_reposts_are_filtered_by_script(text):
    """Real Bocha headlines. A blocklist cannot keep up with the long tail."""
    assert not srch.looks_english(text)


@pytest.mark.parametrize(
    "text",
    [
        "STACK breaks ground on 230 MW Hillsboro data center campus",
        "Meta's $10B Louisiana data center clears final permit",
        "",
        "2026 Q3",
        "Crusoe / Abilene, TX — 1.2GW",
    ],
)
def test_english_headlines_survive(text):
    assert srch.looks_english(text)


@respx.mock
def test_a_chinese_result_never_becomes_a_candidate():
    """End to end: even when the keyword filter would match, script rejects it."""
    respx.post(srch.BochaProvider.ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "webPages": {
                        "value": [
                            {
                                # Mentions "data center" and a MW figure, so the
                                # two-tier keyword filter alone would keep it.
                                "name": "Stack Infrastructure在美国部署新的data center园区 230MW",
                                "url": "https://example.cn/a/514732496",
                                "snippet": "230兆瓦 data center campus megawatts",
                            }
                        ]
                    }
                },
            },
        )
    )
    hits = srch.BochaProvider(settings_with(bocha_api_key="k")).search("q")
    assert hits, "the provider itself must still return the hit"

    _, spec = load_config()
    report = SearchReport()
    assert hits_to_candidates(hits, spec, report=report) == []
    assert report.filtered == 1
