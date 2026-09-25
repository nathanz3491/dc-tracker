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
