"""Every fetcher asks the site's robots.txt first — no network: transports are fakes."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tracker import robots
from tracker.config import get_settings
from tracker.ingest import fetch as fetch_mod
from tracker.ingest.discover import _RawFetcher
from tracker.ingest.fetch import HttpxFetcher, fetch_all, fetch_with_retry, should_escalate

UA = "dc-tracker/0.1 (+https://example.com/crawler.md; someone@example.com)"

ROBOTS = """User-agent: *
Disallow: /private/
Crawl-delay: 5
"""


@pytest.fixture(autouse=True)
def respect(monkeypatch):
    monkeypatch.setenv("TRACKER_RESPECT_ROBOTS", "1")
    monkeypatch.setenv("TRACKER_USER_AGENT", UA)
    monkeypatch.setenv("TRACKER_POLITENESS_DELAY_S", "0")
    get_settings.cache_clear()
    robots.clear()
    yield
    robots.clear()


def site(robots_status=200, robots_body=ROBOTS):
    """A fake site, and the list of paths it was asked for."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status, text=robots_body)
        body = "<html><body><article>" + "A data-center campus. " * 40 + "</article></body></html>"
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return httpx.MockTransport(handler), asked


def test_a_200_file_is_read_and_its_rules_apply():
    rules = robots.interpret(200, ROBOTS)
    assert rules.parser.can_fetch(UA, "https://a.example/news/x")
    assert not rules.parser.can_fetch(UA, "https://a.example/private/x")


def test_a_rule_naming_us_applies_to_us():
    rules = robots.interpret(
        200, "User-agent: dc-tracker\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    )
    assert not rules.parser.can_fetch(UA, "https://a.example/news/x")


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_a_4xx_means_no_file_and_nothing_disallowed(status):
    assert robots.interpret(status, "").parser.can_fetch(UA, "https://a.example/x")


@pytest.mark.parametrize("status", [500, 503, None])
def test_a_site_that_cannot_answer_is_disallowed_for_now(status):
    rules = robots.interpret(status, "")
    assert rules.parser is None
    assert rules.expires - robots.TTL_S < rules.expires - robots.UNREACHABLE_TTL_S


def test_each_site_is_asked_once():
    calls = []

    def get(url):
        calls.append(url)
        return 200, ROBOTS

    assert robots.allowed("https://a.example/news/1", UA, get)
    assert not robots.allowed("https://a.example/private/2", UA, get)
    assert robots.allowed("https://b.example/news/3", UA, get)
    assert calls == ["https://a.example/robots.txt", "https://b.example/robots.txt"]


def test_crawl_delay_is_read_and_capped():
    robots.allowed("https://a.example/x", UA, lambda u: (200, ROBOTS))
    assert robots.crawl_delay("https://a.example/y", UA) == 5
    robots.allowed(
        "https://slow.example/x", UA, lambda u: (200, "User-agent: *\nCrawl-delay: 3600\n")
    )
    assert robots.crawl_delay("https://slow.example/y", UA) == robots.MAX_CRAWL_DELAY_S
    assert robots.crawl_delay("https://never-asked.example/", UA) == 0


def test_the_article_client_does_not_request_a_disallowed_page():
    transport, asked = site()
    fetcher = HttpxFetcher(get_settings(), transport=transport)
    refused = asyncio.run(fetcher.fetch("https://a.example/private/report"))
    assert not refused.ok and refused.via == robots.VIA and refused.attempts == 0
    assert asked == ["/robots.txt"]

    allowed = asyncio.run(fetcher.fetch("https://a.example/news/campus"))
    assert allowed.ok
    assert asked == ["/robots.txt", "/news/campus"]


def test_a_refusal_is_neither_retried_nor_escalated():
    transport, asked = site()
    fetcher = HttpxFetcher(get_settings(), transport=transport)
    result = asyncio.run(fetch_with_retry(fetcher, "https://a.example/private/x"))
    assert result.via == robots.VIA and result.attempts == 0
    assert not should_escalate(result)
    assert asked == ["/robots.txt"]


def test_sec_is_sent_the_identity_without_the_link():
    """SEC answers 403 to a User-Agent carrying a URL, robots.txt included, and every
    SEC request failed from the night the crawler page's link was added (2026-10-11).
    It gets the name and the contact; every other site still gets the link."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host + request.url.path, request.headers["user-agent"]))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        body = "<html><body><article>" + "A data-center campus. " * 40 + "</article></body></html>"
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    fetcher = HttpxFetcher(get_settings(), transport=httpx.MockTransport(handler))
    assert asyncio.run(fetcher.fetch("https://www.sec.gov/Archives/edgar/data/1/x.htm")).ok
    assert asyncio.run(fetcher.fetch("https://a.example/news/campus")).ok
    sent = dict(seen)
    assert sent["www.sec.gov/robots.txt"] == "dc-tracker/0.1 (someone@example.com)"
    assert sent["www.sec.gov/Archives/edgar/data/1/x.htm"] == "dc-tracker/0.1 (someone@example.com)"
    assert sent["a.example/news/campus"] == UA


def test_edgar_search_sends_the_identity_without_the_link():
    from tracker.ingest import edgar

    assert edgar._headers(get_settings())["User-Agent"] == "dc-tracker/0.1 (someone@example.com)"


def test_the_feed_client_asks_too():
    transport, asked = site(robots_body="User-agent: *\nDisallow: /\n")
    result = asyncio.run(
        _RawFetcher(get_settings(), transport=transport).fetch("https://a.example/feed")
    )
    assert result.via == robots.VIA
    assert asked == ["/robots.txt"]


def test_the_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("TRACKER_RESPECT_ROBOTS", "0")
    get_settings.cache_clear()
    transport, asked = site(robots_body="User-agent: *\nDisallow: /\n")
    result = asyncio.run(
        HttpxFetcher(get_settings(), transport=transport).fetch("https://a.example/x")
    )
    assert result.ok and asked == ["/x"]


def test_the_batch_waits_out_a_crawl_delay(monkeypatch):
    transport, _ = site()
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(fetch_mod.asyncio, "sleep", fake_sleep)
    fetcher = HttpxFetcher(get_settings(), transport=transport)
    asyncio.run(fetch_all(["https://a.example/news/1"], fetcher=fetcher, escalate=False))
    assert 5 in slept


def test_the_reader_view_asks_before_it_fetches(monkeypatch):
    from tracker.webui import article

    asked = []

    def fake_get(url, **kw):
        asked.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        raise AssertionError("fetched a page robots.txt disallows")

    monkeypatch.setattr(httpx, "get", fake_get)
    body, error = article._get("https://a.example/news/1")
    assert body == "" and error == robots.REFUSED
    assert asked == ["https://a.example/robots.txt"]
