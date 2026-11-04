"""The last rung of the fetch ladder: DeepSeek's Fetch tool, for a page our own
clients were refused.

On 2026-10-19 four local-news pages about Google's Chesterfield campus answered 403
to every client of ours (Cloudflare, CloudFront, Varnish); DeepSeek's Fetch tool
returned one of them whole. These tests pin what may be asked of it, what comes back,
and what it costs, without a network.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from tracker import spend
from tracker.config import Settings
from tracker.ingest import fetch
from tracker.ingest.fetch import DeepSeekFetcher, FetchResult, escalation_ladder

ENDPOINT = "https://api.deepseek.com/v1/tools/fetch"


def _settings(tmp_path=None, **kw) -> Settings:
    ledger = {"spend_ledger": tmp_path / "night.spend"} if tmp_path else {}
    return Settings(_env_file=None, deepseek_api_key="k", **ledger, **kw)


def test_its_markdown_is_read_as_the_page_text_the_gate_checks_quotes_against():
    """DeepSeek escapes punctuation and keeps link syntax; an article does neither, so
    a quote would never be found word for word."""
    text = fetch._markdown_text(
        "\\# Google advances\\: data center\\, [Chesterfield](https://x.test/c) ![map](m.png)"
    )
    assert text == "# Google advances: data center, Chesterfield map"


def test_a_page_comes_back_as_text_and_is_counted(tmp_path):
    settings = _settings(tmp_path)
    with respx.mock:
        route = respx.post(ENDPOINT).respond(
            200,
            json={"data": {"title": "Google advances", "content": "CHESTERFIELD \\(WRIC\\) — A"}},
        )
        result = asyncio.run(DeepSeekFetcher(settings).fetch("https://www.wric.com/a"))
    assert route.calls[0].request.headers["Authorization"] == "Bearer k"
    assert result.ok and result.via == DeepSeekFetcher.VIA
    assert result.markdown.startswith("# Google advances") and "(WRIC)" in result.markdown
    calls = spend.read(settings.spend_ledger)
    assert spend.fetches(calls) == 1 and spend.total(calls) == 0 and spend.searches(calls) == 0
    assert spend.report(calls)[0] == (
        "    1 page(s) read through DeepSeek's fetch, about ¥0.01 from the DeepSeek account"
    )


def test_a_page_it_cannot_fetch_is_a_failure_tried_once(tmp_path):
    settings = _settings(tmp_path)
    with respx.mock:
        route = respx.post(ENDPOINT).respond(
            404, json={"error": {"message": "failed to fetch URL", "type": "not_found_error"}}
        )
        result = asyncio.run(
            fetch.fetch_with_retry(DeepSeekFetcher(settings), "https://www.wtvr.com/a")
        )
    assert not result.ok and "failed to fetch URL" in result.error
    assert route.call_count == 1 and result.attempts == 1, "the service retries on its own side"
    assert spend.fetches(spend.read(settings.spend_ledger)) == 0, "a failure is not billed"


def test_a_page_robots_txt_refuses_us_is_never_sent_to_it(monkeypatch):
    async def refused(*_a, **_k):
        return False

    monkeypatch.setattr(fetch, "permitted", refused)
    with respx.mock(assert_all_called=False):
        route = respx.post(ENDPOINT).respond(200, json={"data": {"content": "x"}})
        result = asyncio.run(DeepSeekFetcher(_settings()).fetch("https://refuses.test/a"))
    assert result.via == "robots" and route.call_count == 0


def test_it_is_the_last_rung_and_can_be_switched_off():
    ladder = escalation_ladder(_settings())
    assert isinstance(ladder[-1], DeepSeekFetcher)
    assert not any(
        isinstance(r, DeepSeekFetcher) for r in escalation_ladder(_settings(deepseek_fetch=False))
    )
    assert not any(
        isinstance(r, DeepSeekFetcher) for r in escalation_ladder(Settings(_env_file=None))
    ), "no key, no rung"


@pytest.mark.parametrize("status", [403, 503])
def test_a_blocked_page_climbs_to_it(status):
    blocked = FetchResult("https://x.test/a", False, status=status, via="httpx")
    assert fetch.should_escalate(blocked) is True


def test_a_rate_limited_page_does_not_climb_to_it():
    """A 429 is the site asking for fewer requests; asking through DeepSeek is one
    more request to it, from DeepSeek's servers, and billed if it answers."""
    limited = FetchResult("https://x.test/a", False, status=429, via="httpx")
    assert fetch.should_escalate(limited) is False


def test_an_empty_balance_stands_the_rung_down_for_the_run(tmp_path):
    """The tool bills the DeepSeek account; on 2026-10-19 the first page took the
    last of it, and every later blocked page would have asked again."""
    fetch.DeepSeekFetcher._EMPTY.clear()
    try:
        with respx.mock:
            route = respx.post(ENDPOINT).respond(
                402, json={"error": {"message": "Insufficient Balance"}}
            )
            first = asyncio.run(DeepSeekFetcher(_settings(tmp_path)).fetch("https://x.test/a"))
            second = asyncio.run(DeepSeekFetcher(_settings(tmp_path)).fetch("https://x.test/b"))
            assert route.call_count == 1, "asked once, not once a page"
        assert "Insufficient Balance" in first.error and not second.ok
    finally:
        fetch.DeepSeekFetcher._EMPTY.clear()


def test_a_page_it_answered_for_is_never_sent_again(tmp_path):
    """Of 3,385 escalations in the 14 nights to 11-04, 522 were a page already sent:
    a page it cannot read stays failed in the queue and is retried night after night."""
    settings = _settings(tmp_path)
    # A router of its own: the global one hands back an earlier test's route for the
    # same pattern, whose count this request does not move.
    with respx.mock() as router:
        route = router.post(ENDPOINT).respond(404, json={"error": {"message": "failed"}})
        asyncio.run(DeepSeekFetcher(settings).fetch("https://www.wtvr.com/a"))
        again = asyncio.run(DeepSeekFetcher(settings).fetch("https://www.wtvr.com/a"))
    assert route.call_count == 1
    assert not again.ok and "sent before" in again.error and "HTTP 404" in again.error


@pytest.mark.parametrize("status", [402, 429, 503])
def test_an_answer_about_deepseek_not_the_page_is_asked_again(tmp_path, status):
    fetch.DeepSeekFetcher._EMPTY.clear()
    try:
        with respx.mock:
            respx.post(ENDPOINT).respond(status, json={"error": {"message": "busy"}})
            asyncio.run(DeepSeekFetcher(_settings(tmp_path)).fetch("https://x.test/a"))
        assert DeepSeekFetcher.answered("https://x.test/a") is None
    finally:
        fetch.DeepSeekFetcher._EMPTY.clear()


@pytest.mark.parametrize(
    "url", ["https://www.bizjournals.com/phoenix/news/a", "https://www.bloomberg.com/news/b"]
)
def test_a_subscribers_page_is_not_sent(url):
    """It would bring back the teaser our own clients already got. bizjournals.com
    alone was 288 of the 3,385 escalations in the 14 nights to 11-04."""
    with respx.mock(assert_all_called=False):
        route = respx.post(ENDPOINT).respond(200, json={"data": {"content": "x"}})
        result = asyncio.run(DeepSeekFetcher(_settings()).fetch(url))
    assert route.call_count == 0 and not result.ok and "subscriber" in result.error
    assert not DeepSeekFetcher.paywalled("https://www.notbloomberg.com/a")


def test_a_network_error_is_reported_not_raised(tmp_path):
    with respx.mock:
        respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("refused"))
        result = asyncio.run(DeepSeekFetcher(_settings(tmp_path)).fetch("https://x.test/a"))
    assert not result.ok and "DeepSeek fetch" in result.error
