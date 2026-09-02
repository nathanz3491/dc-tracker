"""The agent loop: what it runs, what it refuses to die on, and how it stops.

No network and no API key anywhere here. `_ToolModel` plays the provider by
handing back a scripted list of turns, which is the only way to assert that a
tool result actually reaches the model's next request — the property the whole
loop rests on and the one a live call cannot demonstrate.
"""

from __future__ import annotations

import copy
import itertools
import json

import pytest

from tracker import agent
from tracker.llm import LLMError, LLMReply, ToolCall


class _ToolModel:
    """A provider that replays scripted turns and records what it was sent."""

    def __init__(self, *turns: LLMReply):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []
        self.tools_offered: list[list[dict]] | None = None

    def converse(self, *, system, messages, tools=None, max_tokens=None):
        self.seen.append([dict(m) for m in messages])
        self.tools_offered = tools
        if not self.turns:
            return LLMReply(text="nothing further")
        return self.turns.pop(0)


def _call(name: str, **arguments) -> ToolCall:
    import json as _json

    return ToolCall(
        id=f"c{abs(hash(name)) % 1000}",
        name=name,
        arguments=arguments,
        raw_arguments=_json.dumps(arguments),
    )


def _answer_tool() -> agent.Tool:
    return agent.Tool(
        name="decide",
        description="state the conclusion",
        parameters={"type": "object", "properties": {"verdict": {"type": "string"}}},
        terminal=True,
    )


def test_a_terminal_tool_ends_the_run_and_its_arguments_are_the_answer():
    """The whole contract in one test: what the model asked for IS what it decided.

    A terminal tool is never executed, so it needs no `run`. If the loop tried to
    call one it would raise here rather than return.
    """
    model = _ToolModel(LLMReply(text="", tool_calls=(_call("decide", verdict="same"),)))

    result = agent.run("settle it", tools=[_answer_tool()], extractor=model, system="s")

    assert result.outcome == "answered"
    assert result.answer == {"verdict": "same"}
    assert result.tool_name == "decide"


def test_a_tool_result_reaches_the_models_next_turn():
    """The property the loop exists for. Without it the model reads nothing back."""
    looked = agent.Tool(
        name="look",
        description="look something up",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        run=lambda q: f"the answer to {q} is 42",
    )
    model = _ToolModel(
        LLMReply(text="", tool_calls=(_call("look", q="width"),)),
        LLMReply(text="", tool_calls=(_call("decide", verdict="done"),)),
    )

    result = agent.run("go", tools=[looked, _answer_tool()], extractor=model, system="s")

    assert result.answered
    second_request = model.seen[1]
    tool_turns = [m for m in second_request if m.get("role") == "tool"]
    assert tool_turns and "the answer to width is 42" in tool_turns[0]["content"]
    # The assistant's own request has to be echoed back or the provider rejects a
    # `tool` message as answering something it never asked.
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in second_request)


def test_a_tool_that_raises_is_reported_to_the_model_not_raised_at_the_caller():
    """A 404 mid-run should cost one step, not the whole run.

    This is the difference between an agent that recovers and a command that dies
    on the third row of a two-hundred-row sweep.
    """

    def boom(**_):
        raise RuntimeError("upstream said no")

    exploding = agent.Tool(
        name="fetch",
        description="fetch",
        parameters={"type": "object", "properties": {"url": {"type": "string"}}},
        run=boom,
    )
    model = _ToolModel(
        LLMReply(text="", tool_calls=(_call("fetch", url="http://x"),)),
        LLMReply(text="", tool_calls=(_call("decide", verdict="anyway"),)),
    )

    result = agent.run("go", tools=[exploding, _answer_tool()], extractor=model, system="s")

    assert result.answered
    assert result.steps[0].failed
    assert "upstream said no" in result.steps[0].result


def test_an_unknown_tool_name_is_answered_with_the_list_of_real_ones():
    model = _ToolModel(
        LLMReply(text="", tool_calls=(_call("teleport"),)),
        LLMReply(text="", tool_calls=(_call("decide", verdict="ok"),)),
    )

    result = agent.run("go", tools=[_answer_tool()], extractor=model, system="s")

    assert result.answered
    assert "no such tool: teleport" in result.steps[0].result
    assert "decide" in result.steps[0].result


def test_unparseable_arguments_are_handed_back_for_the_model_to_correct():
    """A malformed call is a recoverable event, not a failed run."""
    looked = agent.Tool(
        name="look",
        description="look",
        parameters={"type": "object", "properties": {}},
        run=lambda: "fine",
    )
    bad = ToolCall(id="c2", name="look", arguments={}, raw_arguments="{not json", parse_failed=True)
    model = _ToolModel(
        LLMReply(text="", tool_calls=(bad,)),
        LLMReply(text="", tool_calls=(_call("decide", verdict="ok"),)),
    )

    result = agent.run("go", tools=[looked, _answer_tool()], extractor=model, system="s")

    assert result.answered
    assert "not a JSON object" in result.steps[0].result


def test_a_no_argument_tool_still_runs():
    """The bug this pins: `{}` is what a failed parse falls back to *and* what a
    correct call to a no-argument tool looks like. Reading emptiness as failure
    broke every such tool."""
    looked = agent.Tool(
        name="look",
        description="look",
        parameters={"type": "object", "properties": {}},
        run=lambda: "it ran",
    )
    model = _ToolModel(
        LLMReply(text="", tool_calls=(_call("look"),)),
        LLMReply(text="", tool_calls=(_call("decide", verdict="ok"),)),
    )

    result = agent.run("go", tools=[looked, _answer_tool()], extractor=model, system="s")

    assert result.steps[0].result == "it ran"
    assert not result.steps[0].failed


def test_a_run_that_never_decides_is_exhausted_rather_than_looping_forever():
    forever = agent.Tool(
        name="look",
        description="look",
        parameters={"type": "object", "properties": {}},
        run=lambda: "still nothing",
    )
    model = _ToolModel(*[LLMReply(text="", tool_calls=(_call("look"),)) for _ in range(10)])

    result = agent.run(
        "go", tools=[forever, _answer_tool()], extractor=model, system="s", max_steps=3
    )

    assert result.outcome == "exhausted"
    assert len(result.steps) == 3
    assert "3 steps" in result.note


def test_prose_with_no_tool_call_stops_and_keeps_what_it_said():
    """The most useful thing a run that decided nothing produces is its reason."""
    model = _ToolModel(LLMReply(text="The sources contradict each other and I cannot tell."))

    result = agent.run("go", tools=[_answer_tool()], extractor=model, system="s")

    assert result.outcome == "stopped"
    assert "cannot tell" in result.note


def test_a_provider_without_tool_support_fails_loudly_rather_than_silently():
    """Dropping the tools and answering anyway would look like a cautious model."""

    class OldProvider:
        def complete(self, *, system, user, max_tokens=None):
            return LLMReply(text="{}")

    result = agent.run("go", tools=[_answer_tool()], extractor=OldProvider(), system="s")

    assert result.outcome == "error"
    assert "cannot use tools" in result.note


def test_a_provider_error_ends_the_run_with_its_message():
    class Failing:
        def converse(self, **_kwargs):
            raise LLMError("429 from upstream")

    result = agent.run("go", tools=[_answer_tool()], extractor=Failing(), system="s")

    assert result.outcome == "error"
    assert "429" in result.note


def test_an_oversized_tool_result_is_clipped_so_one_article_cannot_eat_the_context():
    """Cached articles run to 24,000 characters; six of them would be the whole run."""
    huge = agent.Tool(
        name="look",
        description="look",
        parameters={"type": "object", "properties": {}},
        run=lambda: "x" * (agent.MAX_RESULT_CHARS + 5000),
    )
    model = _ToolModel(
        LLMReply(text="", tool_calls=(_call("look"),)),
        LLMReply(text="", tool_calls=(_call("decide", verdict="ok"),)),
    )

    result = agent.run("go", tools=[huge, _answer_tool()], extractor=model, system="s")

    assert len(result.steps[0].result) < agent.MAX_RESULT_CHARS + 200
    assert "truncated" in result.steps[0].result


class _BudgetModel:
    """Records the budget of every turn and can be told to truncate the first."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.budgets: list[int] = []

    def converse(self, *, system, messages, tools=None, max_tokens=None):
        self.budgets.append(max_tokens)
        return self.turns.pop(0) if self.turns else LLMReply(text="done")


def test_a_truncated_turn_is_retried_once_at_the_bigger_budget():
    """Truncation is the one failure worth paying twice for: the model had the
    answer and ran out of room saying it, so a retry at the same budget would
    stop in the same place."""
    model = _BudgetModel(
        LLMReply(text="I was mid-sente", finish_reason="length"),
        LLMReply(text="", tool_calls=(_call("decide", verdict="same"),)),
    )

    result = agent.run("go", tools=[_answer_tool()], extractor=model, system="s")

    assert result.answered
    assert model.budgets == [agent.MAX_TOKENS, agent.MAX_TOKENS_ESCALATED]
    assert result.escalations == 1


def test_the_bigger_budget_is_not_carried_into_the_next_step():
    """One long deliberation must not raise the price of every step behind it."""
    model = _BudgetModel(
        LLMReply(text="cut off", finish_reason="length"),
        LLMReply(text="", tool_calls=(_call("look"),)),
        LLMReply(text="", tool_calls=(_call("decide", verdict="ok"),)),
    )
    looked = agent.Tool(
        name="look",
        description="look",
        parameters={"type": "object", "properties": {}},
        run=lambda: "fine",
    )

    agent.run("go", tools=[looked, _answer_tool()], extractor=model, system="s")

    assert model.budgets == [
        agent.MAX_TOKENS,
        agent.MAX_TOKENS_ESCALATED,
        agent.MAX_TOKENS,
    ]


def test_a_truncated_reply_that_still_carried_a_tool_call_is_not_retried():
    """It is usable as it stands; paying twice for it would be waste."""
    model = _BudgetModel(
        LLMReply(text="", finish_reason="length", tool_calls=(_call("decide", verdict="ok"),)),
    )

    result = agent.run("go", tools=[_answer_tool()], extractor=model, system="s")

    assert result.answered
    assert result.escalations == 0
    assert model.budgets == [agent.MAX_TOKENS]


def test_the_default_budgets_are_the_ones_that_were_asked_for():
    assert agent.MAX_TOKENS == 20_000
    assert agent.MAX_TOKENS_ESCALATED == 50_000


def test_the_tools_are_offered_to_the_provider_as_schemas():
    model = _ToolModel(LLMReply(text="", tool_calls=(_call("decide", verdict="x"),)))

    agent.run("go", tools=[_answer_tool()], extractor=model, system="s")

    assert model.tools_offered
    assert model.tools_offered[0]["function"]["name"] == "decide"


# --- the provider seam -------------------------------------------------------
#
# `converse` is what sets `parse_failed`, and the runner is what reads it. Tested
# together here so the two cannot drift into disagreeing about what an empty
# argument dict means.


def _keyed_settings(monkeypatch):
    """Settings carrying a fake key.

    The suite is keyless by design (`_fast_and_keyless_settings`), and
    `DeepSeekExtractor.__init__` refuses without one. These tests never reach the
    network — `_post` is replaced — but the constructor still has to be satisfied.
    """
    from tracker.config import get_settings

    monkeypatch.setenv("TRACKER_DEEPSEEK_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    from tracker.config import get_settings

    yield
    get_settings.cache_clear()


def _deepseek(monkeypatch, body: dict):
    from tracker.llm import DeepSeekExtractor

    extractor = DeepSeekExtractor(_keyed_settings(monkeypatch))
    monkeypatch.setattr(extractor, "_post", lambda payload, **_: body)
    return extractor


def _reply_with(arguments: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "look", "arguments": arguments}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_converse_marks_malformed_arguments_and_leaves_valid_ones_alone(monkeypatch):
    broken = _deepseek(monkeypatch, _reply_with("{not json"))
    call = broken.converse(system="s", messages=[], tools=[]).tool_calls[0]
    assert call.parse_failed and call.arguments == {}

    empty = _deepseek(monkeypatch, _reply_with("{}"))
    call = empty.converse(system="s", messages=[], tools=[]).tool_calls[0]
    assert not call.parse_failed and call.arguments == {}

    real = _deepseek(monkeypatch, _reply_with('{"url": "http://x"}'))
    call = real.converse(system="s", messages=[], tools=[]).tool_calls[0]
    assert not call.parse_failed and call.arguments == {"url": "http://x"}


def test_converse_sends_tools_and_never_forces_a_json_response_format(monkeypatch):
    """Forcing `response_format` alongside tools is how you get a model that
    *describes* the call it wanted to make instead of making it."""
    from tracker.llm import DeepSeekExtractor

    sent: dict = {}
    extractor = DeepSeekExtractor(_keyed_settings(monkeypatch))
    monkeypatch.setattr(extractor.settings, "deepseek_json_mode", True, raising=False)

    def capture(payload, **_):
        sent.update(payload)
        return _reply_with("{}")

    monkeypatch.setattr(extractor, "_post", capture)
    extractor.converse(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "look"}}],
    )

    assert "response_format" not in sent
    assert sent["tool_choice"] == "auto"
    assert sent["messages"][0]["role"] == "system"


# --- the evidence gate -------------------------------------------------------


_ARTICLE = (
    "Construction began in March at the Mount Pleasant site. "
    "The campus will draw 250 MW of grid power at full build, the operator said. "
    "A second phase remains unfunded."
)


def test_verbatim_accepts_a_sentence_the_article_really_contains():
    accepted, refusal = agent.verbatim(
        "The campus will draw 250 MW of grid power at full build", _ARTICLE
    )
    assert refusal == ""
    assert "250 MW" in accepted


def test_verbatim_refuses_a_paraphrase():
    """The reason the gate is here at all: an unquoted edit stores as `inferred`,
    and `capex` does not sum `inferred`."""
    accepted, refusal = agent.verbatim(
        "the site is a quarter-gigawatt facility drawing power from the regional grid", _ARTICLE
    )
    assert accepted == ""
    assert refusal


def test_verbatim_refuses_a_fragment_too_short_to_evidence_anything():
    """`MIN_RUN_CHARS` is 40. "250 MW" appears in every article about a 250 MW
    site, so a floor is what stops a true fragment passing as evidence."""
    accepted, refusal = agent.verbatim("250 MW", _ARTICLE)
    assert accepted == ""
    assert refusal


@pytest.mark.parametrize(
    ("quote", "article", "expected"),
    [
        ("", "some text", "no quote offered"),
        ("something", "", "no article text"),
    ],
)
def test_verbatim_says_which_side_was_missing(quote, article, expected):
    accepted, refusal = agent.verbatim(quote, article)
    assert accepted == ""
    assert expected in refusal


# --- the shared toolkit ------------------------------------------------------


def test_the_toolkit_reads_a_real_row(session):
    from tracker.models import Project

    session.add(
        Project(
            name="Fairwater",
            company="Microsoft",
            city="Mount Pleasant",
            state="WI",
            dedup_key="microsoft|city:mount pleasant|WI",
        )
    )
    session.flush()

    tools = {t.name: t for t in agent.evidence_toolkit(session)}
    assert {"list_sources", "read_article", "show_project", "find_projects"} <= set(tools)

    found = tools["find_projects"].run(query="Fairwater")
    assert "Fairwater" in found and "Microsoft" in found

    row = session.scalars(__import__("sqlalchemy").select(Project)).one()
    card = tools["show_project"].run(project_id=row.id)
    assert "Mount Pleasant" in card


def test_the_toolkit_answers_for_a_row_that_does_not_exist(session):
    tools = {t.name: t for t in agent.evidence_toolkit(session)}
    assert "no project #9999" in tools["show_project"].run(project_id=9999)
    assert "no project #9999" in tools["list_sources"].run(project_id=9999)


def test_search_can_be_withheld(session):
    names = {t.name for t in agent.evidence_toolkit(session, allow_search=False)}
    assert "search_web" not in names
    assert "search_web" in {t.name for t in agent.evidence_toolkit(session, allow_search=True)}


# --- the cost property -------------------------------------------------------


def test_the_history_is_append_only_so_the_prefix_stays_cacheable():
    """The whole cost argument for the loop.

    DeepSeek bills a prompt token served from its prefix cache at a fraction of an
    uncached one, and turn N's request is turn N-1's plus an append — an exact
    prefix match. Editing any earlier message changes the prefix and makes every
    later turn a full-price miss. Shortening stale tool results was implemented for
    exactly the opposite reason and reverted; this stops it coming back.
    """
    prefixes: list[list[dict]] = []

    def _read(url):
        return "The campus totals 230 MW. " * 200

    tools = [
        agent.Tool(
            name="read_article",
            description="read",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            run=_read,
        ),
        agent.Tool(
            name="done",
            description="finish",
            parameters={"type": "object", "properties": {}},
            terminal=True,
        ),
    ]

    class _Model:
        def __init__(self):
            self.turn = 0

        def converse(self, *, system, messages, tools=None, max_tokens=None):
            # Deep-copied: we are asserting about what was sent, and the caller
            # keeps mutating the same list by appending.
            prefixes.append(copy.deepcopy(messages))
            self.turn += 1
            if self.turn <= 4:
                args = {"url": f"https://example.test/{self.turn}"}
                return LLMReply(
                    text="",
                    tool_calls=(
                        ToolCall(
                            id=str(self.turn),
                            name="read_article",
                            arguments=args,
                            raw_arguments=json.dumps(args),
                        ),
                    ),
                )
            return LLMReply(
                text="",
                tool_calls=(ToolCall(id="z", name="done", arguments={}, raw_arguments="{}"),),
            )

    result = agent.run("check it", tools=tools, extractor=_Model(), system="stable system prompt")

    assert result.answered
    assert len(prefixes) >= 3
    # Every send must begin with the previous send, byte for byte.
    for earlier, later in itertools.pairwise(prefixes):
        assert later[: len(earlier)] == earlier, (
            "an earlier message was edited — the prefix cache cannot hit on this"
        )


def test_cache_tokens_are_totalled_across_turns():
    """A run reports its own cache rate, so the next night is measured not guessed."""

    class _Model:
        def __init__(self):
            self.turn = 0

        def converse(self, *, system, messages, tools=None, max_tokens=None):
            self.turn += 1
            if self.turn == 1:
                return LLMReply(
                    text="",
                    prompt_tokens=1000,
                    cache_hit_tokens=0,
                    cache_miss_tokens=1000,
                    tool_calls=(ToolCall(id="a", name="noop", arguments={}, raw_arguments="{}"),),
                )
            return LLMReply(
                text="",
                prompt_tokens=1200,
                cache_hit_tokens=1000,
                cache_miss_tokens=200,
                tool_calls=(ToolCall(id="b", name="done", arguments={}, raw_arguments="{}"),),
            )

    tools = [
        agent.Tool(
            name="noop",
            description="n",
            parameters={"type": "object", "properties": {}},
            run=lambda: "ok",
        ),
        agent.Tool(
            name="done",
            description="d",
            parameters={"type": "object", "properties": {}},
            terminal=True,
        ),
    ]

    result = agent.run("t", tools=tools, extractor=_Model(), system="s")

    assert result.cache_hit_tokens == 1000
    assert result.cache_miss_tokens == 1200
    assert result.cache_rate == pytest.approx(1000 / 2200)
