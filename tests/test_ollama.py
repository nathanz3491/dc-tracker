"""The local provider: routing, the silent-truncation guard, and honest failures.

Offline throughout — respx stands in for the Ollama server. The load-bearing
assertions are the routing ones (`--llm-provider` and TRACKER_LLM_PROVIDER decide
which class answers, and the DeepSeek-specific model names never leak into a local
call) and the `num_ctx` one, because Ollama's default context is small and input
beyond it is truncated silently — the one failure mode worse than an error.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from tracker.cli import app
from tracker.config import Settings
from tracker.llm import (
    LLM_PROVIDERS,
    DeepSeekExtractor,
    LLMUnavailable,
    MissingApiKey,
    OllamaExtractor,
    OllamaUnavailable,
    build_extractor,
    default_extractor,
    fast_extractor,
    reasoning_extractor,
    split_thinking,
)

runner = CliRunner()

BASE = "http://127.0.0.1:11434"


def ollama_settings(**kwargs) -> Settings:
    return Settings(llm_provider="ollama", **kwargs)


def mock_version(router) -> None:
    router.get(f"{BASE}/api/version").respond(200, json={"version": "0.32.15"})


def chat_reply(content: str, *, thinking: str | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if thinking is not None:
        message["thinking"] = thinking
    return {
        "model": "qwen3.8:27b-mlx",
        "message": message,
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 96,
        "eval_count": 80,
    }


# --- Reaching the server -----------------------------------------------------


@respx.mock
def test_an_unreachable_server_fails_at_construction_with_the_fix():
    """The same early-fail property the DeepSeek key check gives every command:
    nothing is spent — no feed poll, no fetch — before the provider is known dead.
    """
    respx.get(f"{BASE}/api/version").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(OllamaUnavailable) as exc:
        OllamaExtractor(ollama_settings())
    message = str(exc.value)
    assert "ollama serve" in message, "the fix has to be in the message"
    assert "qwen3.8:27b-mlx" in message
    assert "TRACKER_OLLAMA_BASE_URL" in message


@respx.mock
def test_a_missing_model_is_named_not_wrapped():
    """404 is the one status with an unambiguous meaning: server up, model absent."""
    mock_version(respx)
    respx.post(f"{BASE}/api/chat").respond(404, text='{"error": "model not found"}')
    extractor = OllamaExtractor(ollama_settings())
    with pytest.raises(OllamaUnavailable, match="ollama pull"):
        extractor.complete(system="s", user="u")


@respx.mock
def test_a_loading_model_is_retried_not_failed():
    """The first request after idle can 503 while 18 GB maps in."""
    mock_version(respx)
    route = respx.post(f"{BASE}/api/chat")
    route.side_effect = [
        httpx.Response(503, text="loading"),
        httpx.Response(200, json=chat_reply("OK")),
    ]
    reply = OllamaExtractor(ollama_settings()).complete(system="s", user="u")
    assert reply.text == "OK"
    assert route.call_count == 2


# --- The reply ----------------------------------------------------------------


@respx.mock
def test_the_reply_maps_ollamas_fields_onto_the_shared_shape():
    mock_version(respx)
    respx.post(f"{BASE}/api/chat").respond(200, json=chat_reply('{"a": 1}'))
    reply = OllamaExtractor(ollama_settings()).complete(system="s", user="u")
    assert reply.text == '{"a": 1}'
    assert reply.finish_reason == "stop"
    assert reply.prompt_tokens == 96
    assert reply.completion_tokens == 80
    assert reply.model == "qwen3.8:27b-mlx"


@respx.mock
def test_thinking_is_folded_into_the_tag_form_the_module_understands():
    """Ollama returns thinking in its own field; DeepSeek may too. One place folds
    both back into `<think>` tags so `split_thinking` — and therefore every store
    and every page — keeps working without learning provider shapes.
    """
    mock_version(respx)
    respx.post(f"{BASE}/api/chat").respond(
        200, json=chat_reply("the answer", thinking="deliberation")
    )
    extractor = OllamaExtractor(ollama_settings(), effort="high")
    reply = extractor.complete(system="s", user="u")
    answer, thinking = split_thinking(reply.text)
    assert answer == "the answer"
    assert thinking == "deliberation"


@respx.mock
def test_the_stream_yields_content_and_never_the_deliberation():
    mock_version(respx)
    lines = [
        json.dumps({"message": {"content": "Meta is "}, "done": False}),
        json.dumps({"message": {"content": "building"}, "done": False}),
        json.dumps({"message": {"content": ""}, "done": True}),
    ]
    respx.post(f"{BASE}/api/chat").respond(200, text="\n".join(lines))
    pieces = list(OllamaExtractor(ollama_settings()).stream(system="s", user="u"))
    assert "".join(pieces) == "Meta is building"


@respx.mock
def test_check_reports_the_same_keys_as_the_api_provider():
    """`ingest crawl --check` prints whatever this returns; comparing providers
    should be reading one table, not two vocabularies."""
    mock_version(respx)
    respx.post(f"{BASE}/api/chat").respond(200, json=chat_reply("OK"))
    got = OllamaExtractor(ollama_settings()).check()
    expected = {
        "base_url",
        "model",
        "latency_s",
        "reply",
        "finish_reason",
        "thinking_tokens",
        "prompt_tokens",
        "completion_tokens",
    }
    assert set(got) == expected
    assert got["reply"] == "OK"


# --- The payload ---------------------------------------------------------------


@respx.mock
def test_num_ctx_rides_on_every_request():
    """The silent-truncation guard. Ollama's default context is a few thousand
    tokens and input beyond it is dropped without an error — against article-sized
    prompts that means extracting from half the article and failing the evidence
    gate on quotes the model never saw.
    """
    mock_version(respx)
    extractor = OllamaExtractor(ollama_settings())
    payload = extractor._payload(system="s", user="u", max_tokens=None, stream=False)
    assert payload["options"]["num_ctx"] == 32768
    custom = OllamaExtractor(ollama_settings(ollama_num_ctx=8192))
    assert custom._payload(system="s", user="u", max_tokens=None, stream=False)["options"][
        "num_ctx"
    ] == 8192


@respx.mock
def test_effort_collapses_to_the_think_flag():
    mock_version(respx)
    thinking = OllamaExtractor(ollama_settings(), effort="high")
    plain = OllamaExtractor(ollama_settings())
    assert thinking._payload(system="s", user="u", max_tokens=8, stream=False)["think"] is True
    assert plain._payload(system="s", user="u", max_tokens=8, stream=False)["think"] is False


@respx.mock
def test_sampling_matches_the_api_provider():
    """Switching provider must not also switch temperament."""
    mock_version(respx)
    options = OllamaExtractor(ollama_settings())._payload(
        system="s", user="u", max_tokens=64, stream=False
    )["options"]
    assert options["temperature"] == 0.1
    assert options["top_p"] == 0.9
    assert options["num_predict"] == 64


# --- Routing --------------------------------------------------------------------


@respx.mock
def test_the_factories_route_on_the_provider_and_keep_the_tier_policy():
    """One tier policy, two providers: extraction and inference think, the drawer
    does not — whoever answers."""
    mock_version(respx)
    settings = ollama_settings()
    assert isinstance(default_extractor(settings), OllamaExtractor)
    assert default_extractor(settings).thinking is True
    assert isinstance(reasoning_extractor(settings), OllamaExtractor)
    assert reasoning_extractor(settings).thinking is True
    assert isinstance(fast_extractor(settings), OllamaExtractor)
    assert fast_extractor(settings).thinking is False


def test_deepseek_stays_the_default_provider():
    settings = Settings(deepseek_api_key="test-key")
    assert settings.llm_provider == "deepseek"
    assert isinstance(default_extractor(settings), DeepSeekExtractor)


@respx.mock
def test_the_deepseek_reasoning_model_name_never_leaks_into_a_local_call():
    """`reasoning_extractor` names `deepseek-v4-pro` explicitly on the API path;
    a local server asked for that tag would 404 on every `infer`."""
    mock_version(respx)
    extractor = reasoning_extractor(ollama_settings())
    assert extractor.model == "qwen3.8:27b-mlx"


@respx.mock
def test_build_extractor_passes_model_and_effort_through():
    mock_version(respx)
    extractor = build_extractor(ollama_settings(), model="other-tag", effort="max")
    assert isinstance(extractor, OllamaExtractor)
    assert extractor.model == "other-tag"
    assert extractor.thinking is True
    api = build_extractor(Settings(deepseek_api_key="k"), model="deepseek-v4-pro")
    assert isinstance(api, DeepSeekExtractor)
    assert api.model == "deepseek-v4-pro"


def test_the_local_provider_needs_no_api_key():
    """The point of having it: `MissingApiKey` is a DeepSeek failure, and both are
    the one `LLMUnavailable` every call site catches."""
    assert issubclass(MissingApiKey, LLMUnavailable)
    assert issubclass(OllamaUnavailable, LLMUnavailable)
    with pytest.raises(MissingApiKey):
        build_extractor(Settings())  # deepseek default, no key


# --- The flag -------------------------------------------------------------------


def invoke(db: Path, *args: str):
    return runner.invoke(app, ["--db", str(db), *args])


@pytest.fixture
def initialized(tmp_path: Path) -> Path:
    db = tmp_path / "t.db"
    assert invoke(db, "init").exit_code == 0
    return db


def test_an_unknown_provider_is_refused_by_name(initialized: Path):
    result = invoke(initialized, "sync", "--llm-provider", "chatgpt")
    assert result.exit_code == 2
    for name in LLM_PROVIDERS:
        assert name in result.output


def test_the_flag_reaches_the_settings(initialized: Path, monkeypatch):
    """`--llm-provider ollama` with no server must fail with the ollama message —
    and must NOT ask for an API key, which is the other provider's problem."""
    monkeypatch.setenv("TRACKER_OLLAMA_BASE_URL", "http://127.0.0.1:9")
    from tracker.config import get_settings

    get_settings.cache_clear()
    result = invoke(initialized, "ingest", "crawl", "--check", "--llm-provider", "ollama")
    assert result.exit_code == 2
    assert "ollama" in result.output.lower()
    assert "TRACKER_DEEPSEEK_API_KEY" not in result.output


def test_every_llm_command_offers_the_flag():
    """The user's ask, as a property: every command that spends LLM calls can
    choose the provider. Measured against the same catalog the console gates on,
    so a new LLM command cannot arrive without the flag failing this test."""
    from tracker.webui import catalog

    missing = [
        name
        for name, command in catalog.by_name().items()
        if (name in catalog.LLM_COMMANDS or name == "logic resolve")
        and not any(flag.name == "--llm-provider" for flag in command.flags)
    ]
    assert missing == [], f"LLM commands without --llm-provider: {missing}"


def test_the_flag_is_a_dropdown_not_a_text_box():
    from tracker.webui import catalog

    sync = catalog.by_name()["sync"]
    flag = next(f for f in sync.flags if f.name == "--llm-provider")
    assert flag.choices == ("deepseek", "ollama")


def test_completion_offers_the_two_providers():
    from tracker.tui import completion
    from tracker.webui import catalog

    result = completion.complete("sync --llm-provider ", catalog.by_name())
    assert [c.text for c in result.items] == ["deepseek", "ollama"]
