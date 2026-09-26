"""The reserve: OpenCode Go, used only once DeepSeek says the balance is empty.

An empty balance used to end a night where it happened — every later call failed
the same way, and a loop that had spent half its budget stopped with half its work
undone. The properties, in the order a mistake would cost:

* nothing goes to the reserve until DeepSeek answers 402 — no other error moves it;
* with no reserve configured, a 402 fails as before, and says what to do;
* the two never bounce: a 402 from the reserve itself is an error, not a switch back;
* once switched, the process stays switched, so it does not pay a refusal per call;
* the request is DeepSeek's own, with only the address, key and model name changed.
"""

from __future__ import annotations

import json

import pytest
import respx

DEEPSEEK = "https://api.deepseek.com/chat/completions"
RESERVE = "https://opencode.ai/zen/go/v1/chat/completions"

EMPTY = {"error": {"message": "Insufficient Balance", "type": "unknown_error"}}


def _reply(model: str, text: str = "{}") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_cache_hit_tokens": 4},
    }


@pytest.fixture
def settings(monkeypatch, tmp_path):
    from tracker.config import get_settings

    monkeypatch.setenv("TRACKER_DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("TRACKER_OPENCODE_GO_API_KEY", "reserve-test-key")
    monkeypatch.setenv("TRACKER_SPEND_LEDGER", str(tmp_path / "spend.tsv"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def no_reserve(monkeypatch):
    from tracker.config import get_settings

    monkeypatch.setenv("TRACKER_DEEPSEEK_API_KEY", "deepseek-test-key")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_an_empty_balance_moves_the_same_request_to_the_reserve(settings):
    from tracker import llm

    with respx.mock:
        first = respx.post(DEEPSEEK).respond(402, json=EMPTY)
        reserve = respx.post(RESERVE).respond(200, json=_reply("deepseek-v4.1-flash", "ok"))
        extractor = llm.DeepSeekExtractor(settings, effort="high")
        reply = extractor.complete(system="s", user="u")

    assert reply.text == "ok" and first.call_count == 1 and reserve.call_count == 1
    sent = json.loads(reserve.calls[0].request.content)
    asked = json.loads(first.calls[0].request.content)
    assert sent["model"] == "deepseek-v4.1-flash"
    assert {k: v for k, v in sent.items() if k != "model"} == {
        k: v for k, v in asked.items() if k != "model"
    }, "only the model's name changes; the thinking settings travel with it"
    assert reserve.calls[0].request.headers["Authorization"] == "Bearer reserve-test-key"
    assert extractor.provider == "opencode-go (reserve)"


def test_the_reserve_is_told_the_session_and_who_is_asking(settings):
    """Go refuses a request without `x-opencode-session` (HTTP 400, MissingSessionID)."""
    from tracker import __version__, llm

    with respx.mock:
        first = respx.post(DEEPSEEK).respond(402, json=EMPTY)
        reserve = respx.post(RESERVE).respond(200, json=_reply("deepseek-v4.1-flash"))
        llm.DeepSeekExtractor(settings).complete(system="s", user="u")
        llm.DeepSeekExtractor(settings).complete(system="s", user="u")
    one, two = (call.request.headers for call in reserve.calls)
    assert one["x-opencode-session"] and one["x-opencode-session"] == two["x-opencode-session"]
    assert one["User-Agent"] == f"dc-tracker/{__version__}"
    assert "x-opencode-session" not in first.calls[0].request.headers, "DeepSeek is not told"


def test_the_ledger_names_the_reserve_model(settings):
    from tracker import llm

    with respx.mock:
        respx.post(DEEPSEEK).respond(402, json=EMPTY)
        respx.post(RESERVE).respond(200, json=_reply("deepseek-v4.1-flash"))
        llm.DeepSeekExtractor(settings).complete(system="s", user="u")
    (line,) = settings.spend_ledger.read_text(encoding="utf-8").splitlines()
    assert line.split("\t")[3] == "deepseek-v4.1-flash"


def test_once_switched_the_process_does_not_ask_deepseek_again(settings):
    from tracker import llm

    with respx.mock:
        first = respx.post(DEEPSEEK).respond(402, json=EMPTY)
        respx.post(RESERVE).respond(200, json=_reply("deepseek-v4.1-flash"))
        llm.DeepSeekExtractor(settings).complete(system="s", user="u")
        # A second extractor, as another phase of the same command would build.
        llm.DeepSeekExtractor(settings).complete(system="s", user="u")
    assert first.call_count == 1


def test_without_a_reserve_an_empty_balance_fails_and_says_what_to_do(no_reserve):
    from tracker import llm

    with respx.mock:
        respx.post(DEEPSEEK).respond(402, json=EMPTY)
        with pytest.raises(llm.LLMError, match="TRACKER_OPENCODE_GO_API_KEY"):
            llm.DeepSeekExtractor(no_reserve).complete(system="s", user="u")
    assert not llm._ON_RESERVE.is_set()


def test_an_empty_reserve_is_an_error_not_a_switch_back(settings):
    from tracker import llm

    with respx.mock:
        first = respx.post(DEEPSEEK).respond(402, json=EMPTY)
        reserve = respx.post(RESERVE).respond(402, json=EMPTY)
        with pytest.raises(llm.LLMError, match="balance is empty"):
            llm.DeepSeekExtractor(settings).complete(system="s", user="u")
    assert first.call_count == 1 and reserve.call_count == 1


@pytest.mark.parametrize("status", [400, 429, 500])
def test_no_other_error_touches_the_reserve(settings, monkeypatch, status):
    from tracker import llm

    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    with respx.mock:
        respx.post(DEEPSEEK).respond(status, json={"error": {"message": "no"}})
        reserve = respx.post(RESERVE).respond(200, json=_reply("deepseek-v4.1-flash"))
        with pytest.raises(llm.LLMError, match=f"DeepSeek returned HTTP {status}"):
            llm.DeepSeekExtractor(settings).complete(system="s", user="u")
    assert reserve.call_count == 0 and not llm._ON_RESERVE.is_set()


def test_a_rejected_reserve_key_names_the_reserve(settings):
    from tracker import llm

    with respx.mock:
        respx.post(DEEPSEEK).respond(402, json=EMPTY)
        respx.post(RESERVE).respond(401, json={"error": "bad key"})
        with pytest.raises(llm.LLMUnavailable, match="OpenCode Go rejected"):
            llm.DeepSeekExtractor(settings).complete(system="s", user="u")


def test_the_tool_loop_moves_too(settings):
    """`converse` is the agent's path, and the heaviest spender at night."""
    from tracker import llm

    with respx.mock:
        respx.post(DEEPSEEK).respond(402, json=EMPTY)
        reserve = respx.post(RESERVE).respond(200, json=_reply("deepseek-v4.1-flash", "done"))
        reply = llm.DeepSeekExtractor(settings, effort="high").converse(
            system="s", messages=[{"role": "user", "content": "u"}], tools=[]
        )
    assert reply.text == "done" and reserve.call_count == 1


def test_a_stream_moves_before_anything_is_shown(settings):
    from tracker import llm

    body = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    with respx.mock:
        respx.post(DEEPSEEK).respond(402, json=EMPTY)
        respx.post(RESERVE).respond(200, text=body, headers={"Content-Type": "text/event-stream"})
        text = "".join(llm.DeepSeekExtractor(settings).stream(system="s", user="u"))
    assert text == "Hello"
