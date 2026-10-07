"""How long an API call may think, and what a call that runs out of time says.

The judgement tier writes answers past 30,000 tokens, and a non-streamed reply
arrives only when it is finished. A fixed two-minute timeout cut every such call
off, and each retry was cut off the same way — on one `logic conflicts` run, 52 of
197 fields failed like that, logged as "LLM request error:" with nothing after it,
because a timeout's message is empty.
"""

from __future__ import annotations

import httpx
import pytest
import respx

ENDPOINT = "https://api.deepseek.com/chat/completions"


@pytest.fixture
def keyed(monkeypatch):
    from tracker.config import get_settings

    monkeypatch.setenv("TRACKER_DEEPSEEK_API_KEY", "test-key-not-real")
    monkeypatch.setenv("TRACKER_DEEPSEEK_TIMEOUT_S", "900")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_the_timeout_is_the_setting_not_two_minutes(keyed, monkeypatch):
    from tracker import llm

    asked: list[float] = []
    real = llm.api_client

    def recording(timeout_s: float = 600.0):
        asked.append(timeout_s)
        return real(timeout_s)

    monkeypatch.setattr(llm, "api_client", recording)
    with respx.mock:
        respx.post(ENDPOINT).respond(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )
        llm.DeepSeekExtractor(keyed).complete(system="s", user="u")
    assert asked == [900.0]
    assert real(900.0).timeout.read == 900.0


def test_a_call_that_times_out_says_so(keyed, monkeypatch, caplog):
    from tracker import llm

    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    caplog.set_level("WARNING")
    with respx.mock:
        respx.post(ENDPOINT).mock(side_effect=httpx.ReadTimeout(""))
        with pytest.raises(llm.LLMError, match="ReadTimeout"):
            llm.DeepSeekExtractor(keyed).complete(system="s", user="u")
    assert "ReadTimeout" in caplog.text


# --- a broken pool, and a provider that has gone away ---------------------------

_OK = {
    "model": "deepseek-flash",
    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
}


def test_waiting_for_a_connection_is_not_the_read_timeout(keyed):
    """Ten minutes is how long a reply may take, not how long to wait for a socket.
    On 2026-10-07 a leaked pool made every call wait the full ten minutes, three
    times, and enrich wrote nothing for three hours."""
    from tracker import llm

    client = llm.api_client(900.0)
    assert client.timeout.read == 900.0
    assert client.timeout.pool == llm.POOL_WAIT_S < 60


def test_a_broken_connection_rebuilds_the_pool_before_the_retry(keyed, monkeypatch):
    from tracker import llm

    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    first = llm.api_client(keyed.deepseek_timeout_s)
    with respx.mock:
        respx.post(ENDPOINT).mock(
            side_effect=[httpx.PoolTimeout("no free connection"), httpx.Response(200, json=_OK)]
        )
        llm.DeepSeekExtractor(keyed).complete(system="s", user="u")
    assert first.is_closed, "the broken pool was dropped"
    assert llm.api_client(keyed.deepseek_timeout_s) is not first


def test_an_unreachable_provider_fails_fast_then_is_tried_again(keyed, monkeypatch):
    """After OUTAGE_FAILURES calls each fail every attempt, calls fail at once,
    without touching the network, until the cool-down ends; one that answers then
    resumes everything."""
    from tracker import llm

    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    clock = [1000.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])
    extractor = llm.DeepSeekExtractor(keyed)
    with respx.mock:
        route = respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("SSL EOF"))
        for _ in range(llm.OUTAGE_FAILURES):
            with pytest.raises(llm.LLMError, match="ConnectError"):
                extractor.complete(system="s", user="u")
        tried = route.call_count

        with pytest.raises(llm.LLMError, match="unreachable"):
            extractor.complete(system="s", user="u")
        assert route.call_count == tried, "failing fast: no request at all"

        clock[0] += llm.OUTAGE_COOLDOWN_S + 1
        route.mock(side_effect=None, return_value=httpx.Response(200, json=_OK))
        extractor.complete(system="s", user="u")
        extractor.complete(system="s", user="u")
    assert route.call_count == tried + 2, "answering again, so asked again"
