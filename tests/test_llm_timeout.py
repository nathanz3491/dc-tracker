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
    """Sent whole, the read timeout is the whole reply: nothing arrives until it is done."""
    from tracker import llm

    monkeypatch.setattr(keyed, "deepseek_stream", False)
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
    # The pool a streamed call uses: its read timeout is the silence allowed.
    first = llm.api_client(keyed.deepseek_idle_timeout_s)
    with respx.mock:
        respx.post(ENDPOINT).mock(
            side_effect=[httpx.PoolTimeout("no free connection"), httpx.Response(200, json=_OK)]
        )
        llm.DeepSeekExtractor(keyed).complete(system="s", user="u")
    assert first.is_closed, "the broken pool was dropped"
    assert llm.api_client(keyed.deepseek_idle_timeout_s) is not first


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


# --- streamed replies: silence ends an attempt, the reply reads as a whole one -----


def _sse(*chunks: dict, done: bool = True) -> str:
    """An OpenAI-style event stream, as OpenCode Go sent it on 2026-11-22."""
    import json

    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines)


def _delta(**delta) -> dict:
    return {
        "id": "gw-1",
        "model": "deepseek-v4.1-flash",
        "choices": [{"index": 0, "finish_reason": None, "delta": delta}],
    }


_USAGE = {
    "prompt_tokens": 43,
    "completion_tokens": 9,
    "prompt_tokens_details": {"cached_tokens": 40},
}


def _finish(reason: str = "stop", usage: dict | None = _USAGE) -> dict:
    chunk = {
        "id": "gw-1",
        "model": "deepseek-v4.1-flash",
        "choices": [{"index": 0, "finish_reason": reason, "delta": {}}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _streamed(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"Content-Type": "text/event-stream"})


@pytest.fixture
def ledgered(keyed, monkeypatch, tmp_path):
    import threading

    from tracker import llm

    monkeypatch.setattr(keyed, "spend_ledger", tmp_path / "night.spend")
    # Process-wide memory: a test that turns streaming off must not turn it off
    # for every test after it.
    monkeypatch.setattr(llm, "_STREAM_OFF", threading.Event())
    return keyed


def test_a_streamed_reply_reads_like_a_whole_one(ledgered):
    import json

    from tracker import llm

    body = _sse(
        _delta(role="assistant", content=""),
        _delta(reasoning_content="17*23 "),
        _delta(reasoning_content="is 391."),
        _delta(content='{"answer"'),
        _delta(content=": 391}"),
        _finish(),
    )
    with respx.mock:
        route = respx.post(ENDPOINT).mock(return_value=_streamed(body))
        reply = llm.DeepSeekExtractor(ledgered, effort="high").complete(system="s", user="u")
    sent = json.loads(route.calls[0].request.content)
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    assert reply.text == '<think>17*23 is 391.</think>{"answer": 391}'
    assert reply.finish_reason == "stop"
    assert (reply.prompt_tokens, reply.completion_tokens) == (43, 9)
    assert (reply.cache_hit_tokens, reply.cache_miss_tokens) == (40, 3)
    (line,) = ledgered.spend_ledger.read_text(encoding="utf-8").splitlines()
    assert line.split("\t")[3:8] == ["deepseek-v4.1-flash", "43", "9", "40", "3"]


def test_a_streamed_tool_call_is_put_back_together(ledgered):
    from tracker import llm

    body = _sse(
        _delta(role="assistant", content=""),
        _delta(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": ""},
                }
            ]
        ),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": '{"word":"'}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": 'apple"}'}}]),
        _finish("tool_calls"),
    )
    with respx.mock:
        respx.post(ENDPOINT).mock(return_value=_streamed(body))
        reply = llm.DeepSeekExtractor(ledgered, effort="high").converse(
            system="s", messages=[{"role": "user", "content": "u"}], tools=[]
        )
    (call,) = reply.tool_calls
    assert (call.id, call.name, call.arguments) == ("call_1", "lookup", {"word": "apple"})
    assert not call.parse_failed and reply.finish_reason == "tool_calls"


def test_a_streamed_call_waits_only_the_idle_timeout(ledgered, monkeypatch):
    """A dead connection costs three minutes, not ten: the read timeout is the silence
    allowed between chunks, and the setting is the deadline for the whole reply."""
    from tracker import llm

    asked: list[float] = []
    real = llm.api_client

    def recording(timeout_s: float = 600.0):
        asked.append(timeout_s)
        return real(timeout_s)

    monkeypatch.setattr(llm, "api_client", recording)
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            side_effect=[httpx.ReadTimeout(""), _streamed(_sse(_delta(content="{}"), _finish()))]
        )
        reply = llm.DeepSeekExtractor(ledgered).complete(system="s", user="u")
    assert reply.text == "{}" and route.call_count == 2
    assert asked == [ledgered.deepseek_idle_timeout_s] * 2
    assert ledgered.deepseek_idle_timeout_s < ledgered.deepseek_timeout_s


def test_a_silent_stream_says_how_long_it_waited(ledgered, monkeypatch, caplog):
    from tracker import llm

    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    caplog.set_level("WARNING")
    with respx.mock:
        respx.post(ENDPOINT).mock(side_effect=httpx.ReadTimeout(""))
        with pytest.raises(llm.LLMError, match="nothing sent for 180s"):
            llm.DeepSeekExtractor(ledgered).complete(system="s", user="u")
    assert "ReadTimeout nothing sent for 180s" in caplog.text


def test_a_stream_cut_short_is_asked_again(ledgered, monkeypatch):
    """No finish and no [DONE]: the connection went, so the reply is incomplete."""
    from tracker import llm

    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    cut = _sse(_delta(content='{"half'), done=False)
    whole = _sse(_delta(content="{}"), _finish())
    with respx.mock:
        route = respx.post(ENDPOINT).mock(side_effect=[_streamed(cut), _streamed(whole)])
        reply = llm.DeepSeekExtractor(ledgered).complete(system="s", user="u")
    assert reply.text == "{}" and route.call_count == 2


def test_a_stream_past_its_deadline_is_ended():
    import json

    from tracker import llm

    ticks = iter([0.0, 10.0, 10_000.0])
    lines = iter(["data: " + json.dumps(_delta(content="x"))] * 3)
    with pytest.raises(httpx.ReadTimeout, match="deadline"):
        llm.assemble_stream(lines, deadline=600.0, clock=lambda: next(ticks))


def test_an_error_inside_a_stream_is_asked_again(ledgered, monkeypatch):
    from tracker import llm

    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    broken = _sse({"error": {"message": "upstream failed"}}, done=False)
    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            side_effect=[_streamed(broken), _streamed(_sse(_delta(content="{}"), _finish()))]
        )
        llm.DeepSeekExtractor(ledgered).complete(system="s", user="u")
    assert route.call_count == 2


def test_a_stream_without_token_counts_is_estimated_then_streaming_stops(ledgered):
    """The money ceiling is counted in these numbers, so a reply that does not report
    them is estimated on the high side, never written as zero, and the rest of the run
    is sent whole, which always reports them."""
    import json

    from tracker import llm

    unpriced = _sse(_delta(content="x" * 300), _finish(usage=None))
    whole = {
        "model": "deepseek-v4.1-flash",
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    with respx.mock:
        route = respx.post(ENDPOINT).mock(
            side_effect=[_streamed(unpriced), httpx.Response(200, json=whole)]
        )
        extractor = llm.DeepSeekExtractor(ledgered)
        extractor.complete(system="s" * 600, user="u")
        extractor.complete(system="s", user="u")
    first, second = (json.loads(c.request.content) for c in route.calls)
    assert first["stream"] is True and second["stream"] is False
    estimated = ledgered.spend_ledger.read_text(encoding="utf-8").splitlines()[0].split("\t")
    prompt, completion = int(estimated[4]), int(estimated[5])
    assert prompt >= 600 // 4 and completion >= 300 // 4, "never under the text's size"


def test_streaming_can_be_turned_off(ledgered, monkeypatch):
    import json

    from tracker import llm

    monkeypatch.setattr(ledgered, "deepseek_stream", False)
    with respx.mock:
        route = respx.post(ENDPOINT).respond(200, json=_OK)
        llm.DeepSeekExtractor(ledgered).complete(system="s", user="u")
    assert json.loads(route.calls[0].request.content)["stream"] is False
