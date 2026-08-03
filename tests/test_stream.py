"""Streaming the briefing, and the reasoning block that must not reach the reader.

`split_thinking` handles the whole-reply case and is tested with the JSON paths.
A stream is the harder version of the same problem: `<think>` arrives split across
frames, so the decision to hide a character has to be made before the tag that
would justify it has finished arriving.
"""

from __future__ import annotations

import pytest

from tracker import overview
from tracker.llm import _held_back, _sse_delta, _ThinkFilter
from tracker.models import Project


def drain(chunks: list[str]) -> str:
    filter_ = _ThinkFilter()
    out: list[str] = []
    for chunk in chunks:
        out += list(filter_.feed(chunk))
    out += list(filter_.finish())
    return "".join(out)


def test_a_reasoning_block_is_stripped_from_a_stream():
    assert drain(["<think>deliberating</think>Meta is building"]) == "Meta is building"


@pytest.mark.parametrize(
    "chunks",
    [
        ["<th", "ink>hid", "den</thi", "nk>", "visible"],
        ["<", "think>", "hidden", "<", "/", "think>", "visible"],
        ["<think>hidden</think>vis", "ible"],
    ],
)
def test_a_tag_split_across_frames_is_still_recognised(chunks):
    """The whole reason this is a state machine rather than a regex.

    Emitting the first half of `<think>` means the reader watches an angle bracket
    appear and then get taken away — and worse, the reasoning behind it arrives
    before the close tag can retract anything.
    """
    assert drain(chunks) == "visible"


def test_text_without_any_reasoning_passes_through_unchanged():
    assert drain(["no think block ", "at all"]) == "no think block at all"


def test_a_lone_angle_bracket_is_not_held_forever():
    """`a<b` must not be mistaken for the start of a tag and swallowed."""
    assert drain(["a<", "b"]) == "a<b"


@pytest.mark.parametrize(
    "chunks",
    [
        ["<think>never closed and then the budget ran out"],
        # Cut off while the close tag itself was arriving, so the buffer is not
        # empty at the end. This is the case that catches an unconditional flush.
        ["<think>reasoning that stops right at the tag</"],
        ["<think>and here</thi"],
    ],
)
def test_a_reply_cut_off_inside_its_own_reasoning_yields_nothing(chunks):
    """Honest: there was no answer, only a truncated thought.

    Flushing the buffer here would print the model's private deliberation into the
    drawer as though it were the briefing.
    """
    assert drain(chunks) == ""


def test_nothing_is_emitted_before_it_is_known_to_be_safe():
    """A partial tag is held, not guessed at."""
    filter_ = _ThinkFilter()
    assert list(filter_.feed("Meta is building<thi")) == ["Meta is building"]
    assert list(filter_.feed("nk>secret</think> more")) == [" more"]


@pytest.mark.parametrize(
    ("text", "tag", "expected"),
    [("abc<thi", "<think>", 4), ("abc", "<think>", 0), ("x<", "<think>", 1)],
)
def test_held_back_measures_the_partial_tag(text, tag, expected):
    assert _held_back(text, tag) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('data: {"choices":[{"delta":{"content":"hi"}}]}', "hi"),
        ("data: [DONE]", ""),
        ("data: not json", ""),
        ("event: ping", ""),
        ('data: {"choices":[]}', ""),
        ('data: {"choices":[{"delta":{}}]}', ""),
    ],
)
def test_sse_frames_are_read_leniently(line, expected):
    """One torn frame costs a few words of prose, not the run."""
    assert _sse_delta(line) == expected


# --- the overview stream ----------------------------------------------------


class _Streamer:
    model = "test-model"

    def __init__(self, pieces: list[str]) -> None:
        self._pieces = pieces
        self.calls = 0

    def stream(self, *, system, user, max_tokens):
        self.calls += 1
        yield from self._pieces


class _NonStreamer:
    def __init__(self, body: str) -> None:
        self._body = body
        self.calls = 0

    def complete(self, *, system, user, max_tokens):
        self.calls += 1

        class R:
            text = self._body
            model = "fallback-model"

        return R()


BODY = [
    "Meta is building a 1 GW campus in New Albany.\n\n",
    "Power is the binding constraint here.\n\n",
    "Watch for an interconnection agreement.\n\n",
    "One trade-press source sits behind most of this.",
]


def _project(session) -> Project:
    row = Project(
        name="Prometheus",
        company="Meta",
        city="New Albany",
        state="OH",
        dedup_key="meta|prometheus",
        phase="construction",
        confidence=2,
        mw_planned=1000.0,
    )
    session.add(row)
    session.flush()
    return row


def test_a_streamed_briefing_arrives_in_pieces_and_is_then_cached(session):
    project = _project(session)
    pieces = list(overview.stream(project, extractor=_Streamer(BODY)))

    assert len(pieces) > 1, "the point is that it does not arrive all at once"
    assert "".join(pieces).startswith("Meta is building")

    ready = overview.cached(project)
    assert ready is not None
    assert ready.text == "".join(BODY).strip()
    assert ready.model == "test-model"


def test_a_stream_that_dies_partway_is_not_cached(session):
    """Half a briefing must not become this row's reading forever.

    It stops mid-sentence, and the cache is keyed on content that has not changed,
    so nothing would ever invalidate it.
    """
    project = _project(session)

    class _Dies:
        model = "test-model"

        def stream(self, *, system, user, max_tokens):
            yield "Meta is building a 1 GW campus in New Albany and then"
            raise __import__("tracker.llm", fromlist=["LLMError"]).LLMError("connection reset")

    got = list(overview.stream(project, extractor=_Dies()))
    assert got, "what did arrive is still shown"
    assert overview.cached(project) is None


def test_an_empty_stream_caches_nothing(session):
    project = _project(session)
    assert list(overview.stream(project, extractor=_Streamer(["", " "]))) == ["", " "]
    assert overview.cached(project) is None


def test_an_extractor_that_cannot_stream_still_produces_a_briefing(session):
    """The protocol is optional, so a fake in a test does not have to implement it."""
    project = _project(session)
    writer = _NonStreamer("Meta is building a 1 GW campus in New Albany, and that is that.")
    got = "".join(overview.stream(project, extractor=writer))

    assert writer.calls == 1
    assert got.startswith("Meta is building")
    assert overview.cached(project) is not None


def test_the_streamed_briefing_never_becomes_data(session):
    """Same guarantee as the non-streaming path, checked separately.

    Streaming added a second way in, and it would be an easy one to write without
    the constraint the first one was built under.
    """
    project = _project(session)
    list(overview.stream(project, extractor=_Streamer(BODY)))

    assert project.notes is None
    assert list(project.sources) == []
    assert project.confidence == 2


# --- which model writes it --------------------------------------------------


def test_the_briefing_uses_the_fast_model_not_the_reasoning_one():
    """Latency is this call's whole constraint.

    The panel generates when a row is opened, so the model's speed is the page's
    speed. Measured on the same project: the reasoning model took 46.6s to its
    first word and returned nothing, the default 2.7s. `infer` keeps the reasoning
    model — it is one call nobody is watching, and there depth is worth the wait.
    """
    from tracker.config import Settings
    from tracker.llm import fast_extractor, reasoning_extractor

    settings = Settings(minimax_api_key="test-key")
    assert fast_extractor(settings).model == settings.minimax_fast_model
    assert reasoning_extractor(settings).model != settings.minimax_fast_model


def test_the_default_briefing_model_can_actually_be_called():
    """The default is `M2-her`, which rejects the budget every other model takes.

    Without the clamp this configuration is not merely slow or sloppy — it is an
    HTTP 400 on every request and a drawer that never shows a briefing at all. The
    two settings have to agree, and they are declared in different files.
    """
    from tracker.config import Settings
    from tracker.llm import MODEL_TOKEN_CAP, MiniMaxExtractor

    settings = Settings(minimax_api_key="test-key")
    extractor = MiniMaxExtractor(settings, model=settings.minimax_fast_model)
    cap = MODEL_TOKEN_CAP.get(settings.minimax_fast_model)
    if cap is not None:
        assert extractor._budget(overview.MAX_TOKENS) <= cap


def test_the_console_asks_for_the_fast_model():
    """The setting is only worth having if the route that waits actually uses it."""
    import inspect

    from tracker.webui import server

    source = inspect.getsource(server.Handler._overview_stream)
    assert "fast_extractor" in source
    assert "reasoning_extractor" not in source


def test_the_briefing_prompt_asks_for_short_markdown(session):
    """The two things the panel's shape depends on, asserted against the prompt.

    A prose-only briefing renders as one grey slab, and a 231-word one is scrolled
    past — both were the previous version, and both are prompt properties rather
    than code properties, so this is where they can be checked.
    """
    from tracker.prompts import load_prompt

    prompt = load_prompt("overview-v2")
    system = prompt.system.lower()
    assert "markdown" in system
    assert "110 words" in system
    assert "nothing reached" in system, "the track-reading rule must survive edits"


def test_the_default_prompt_is_the_short_one(session):
    """`write` and `stream` must not drift apart on which prompt they use."""
    import inspect

    for fn in (overview.write, overview.stream):
        assert inspect.signature(fn).parameters["prompt_name"].default == "overview-v2"


# --- stopping a model that will not stop -------------------------------------


class _Runaway:
    """Answers, then keeps going. Exactly what `M2-her` does on this prompt."""

    model = "test-model"

    def __init__(self) -> None:
        self.pieces_read = 0

    def stream(self, *, system, user, max_tokens):
        for piece in [
            "Meta is building a 1 GW campus in New Albany, now in construction.\n\n",
            "- **power gap** — no interconnection agreement is on file\n",
            "[[END]]\n",
            "Total word count: **75** (markdown consumed)\n",
            "Final answer (last round): Meta is building a 1 GW campus...\n",
        ]:
            self.pieces_read += 1
            yield piece


def test_a_model_that_carries_on_past_its_answer_is_cut_off(session):
    """The API's own `stop` is accepted and ignored, so this is the only tap.

    Cutting the *stream* rather than the finished text is the point: abandoning
    the generator closes the connection, and not waiting for the tokens after the
    answer is where the time is saved.
    """
    project = _project(session)
    extractor = _Runaway()
    got = "".join(overview.stream(project, extractor=extractor))

    assert got.strip().endswith("no interconnection agreement is on file")
    assert "[[END]]" not in got
    assert "Total word count" not in got
    assert "Final answer" not in got
    assert extractor.pieces_read == 3, "reading must stop at the sentinel, not run to the end"


def test_what_survives_the_cut_is_what_gets_cached(session):
    project = _project(session)
    list(overview.stream(project, extractor=_Runaway()))
    ready = overview.cached(project)
    assert ready is not None
    assert "[[END]]" not in ready.text
    assert "Total word count" not in ready.text


@pytest.mark.parametrize(
    "tail",
    ["[[END]]", "\nTotal word count: 80", "\n]\n", "\nFinal answer: again", "\nOne last time:"],
)
def test_each_runaway_marker_ends_the_briefing(tail):
    assert overview.RUNAWAY.search("A real briefing." + tail)


def test_ordinary_prose_is_not_mistaken_for_a_runaway():
    """The guard must not eat a briefing that simply uses one of these words."""
    for text in (
        "The final answer to the interconnection question is not on file.",
        "Here the power track is the gap.",
        "A list ] of things",
    ):
        cut = overview.RUNAWAY.search(text)
        assert cut is None or cut.start() > 0, text


def test_a_models_token_ceiling_is_respected(session):
    """`M2-her` answers HTTP 400 to anything over 2048, so an unclamped budget
    means no briefing at all rather than a shorter one."""
    from tracker.config import Settings
    from tracker.llm import MODEL_TOKEN_CAP, MiniMaxExtractor

    settings = Settings(minimax_api_key="test-key")
    assert MiniMaxExtractor(settings, model="M2-her")._budget(4096) == 2048
    assert MiniMaxExtractor(settings, model="MiniMax-M2")._budget(4096) == 4096
    assert MiniMaxExtractor(settings, model="M2-her")._budget(512) == 512, "a smaller ask stands"
    assert "M2-her" in MODEL_TOKEN_CAP
