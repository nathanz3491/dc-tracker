"""The written briefing, and the line it must not blur.

Every other block in the drawer is a value with a citation. This one is prose,
and fluent prose beside quoted evidence is the easiest place in the product to
pass a reading off as a fact. So: never stored, never a source, never able to
move confidence, and always labelled.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker import overview
from tracker.models import Event, Project, Source


class _Writer:
    def __init__(self, body: str) -> None:
        self._body = body
        self.calls = 0

    def complete(self, *, system, user, max_tokens):
        self.calls += 1
        self.user = user
        self.system = system

        class R:
            text = self._body
            model = "test-model"

        return R()


def _project(session, **kwargs) -> Project:
    defaults = {
        "name": "Prometheus",
        "company": "Meta",
        "city": "New Albany",
        "state": "OH",
        "dedup_key": "meta|prometheus",
        "phase": "construction",
        "confidence": 2,
        "mw_planned": 1000.0,
    }
    defaults.update(kwargs)
    row = Project(**defaults)
    session.add(row)
    session.flush()
    return row


BODY = (
    "Meta is building a 1 GW campus in New Albany.\n\n"
    "Power is the binding constraint here.\n\n"
    "Watch for an interconnection agreement.\n\n"
    "One trade-press source sits behind most of this."
)


def test_a_briefing_is_written_and_kept_in_memory_only(session):
    project = _project(session)
    writer = _Writer(BODY)

    got = overview.write(project, extractor=writer)
    assert got is not None
    assert got.model == "test-model"
    assert "1 GW campus" in got.text

    # Never becomes data: no field moved, no source appeared, nothing to reingest.
    assert project.notes is None
    assert list(project.sources) == []
    assert project.confidence == 2


def test_the_same_row_is_not_paid_for_twice(session):
    project = _project(session)
    writer = _Writer(BODY)
    overview.write(project, extractor=writer)

    assert overview.cached(project) is not None
    assert writer.calls == 1


def test_a_row_that_changed_gets_a_new_briefing(session):
    """The fingerprint covers the sources, not just the fields.

    A row can gain a citation that changes how much to trust it without any value
    moving, and the last paragraph of the briefing is about exactly that. Caching
    on the fields alone would serve a stale reading of superseded evidence and
    never notice.
    """
    project = _project(session)
    overview.write(project, extractor=_Writer(BODY))
    before = overview.fingerprint(project)

    session.add(
        Source(
            project_id=project.id,
            url="https://example.test/new",
            source_type="company_filing",
            fetched_at=dt.datetime(2026, 1, 1),
        )
    )
    session.flush()
    session.refresh(project)

    assert overview.fingerprint(project) != before
    assert overview.cached(project) is None, "a new citation must invalidate the briefing"


def test_a_new_milestone_also_invalidates_it(session):
    project = _project(session)
    overview.write(project, extractor=_Writer(BODY))
    session.add(
        Event(
            project_id=project.id,
            event_type="groundbreaking",
            event_date=dt.date(2026, 3, 1),
            description="broke ground",
        )
    )
    session.flush()
    session.refresh(project)
    assert overview.cached(project) is None


def test_a_reasoning_block_never_reaches_the_reader(session):
    """This prompt returns prose and never touches the JSON parser.

    `parse_json_object` strips `<think>` on every other path; without doing it
    here the drawer would render the model's private deliberation as the briefing.
    """
    project = _project(session)
    body = "<think>Let me consider the tracks and the sources.</think>\n\n" + BODY
    got = overview.write(project, extractor=_Writer(body))
    assert got is not None
    assert "<think>" not in got.text
    assert "Let me consider" not in got.text
    assert got.text.startswith("Meta is building")


@pytest.mark.parametrize("body", ["", "   ", "<think>only thinking, cut off here"])
def test_an_empty_or_truncated_briefing_is_no_briefing(session, body):
    """None, not a placeholder — an apology is clutter in a busy drawer."""
    project = _project(session)
    assert overview.write(project, extractor=_Writer(body)) is None


def test_the_model_sees_the_tiers_and_the_gaps_not_just_the_values(session):
    """The last paragraph is about how much to trust the row.

    It cannot be written from the values alone: a briefing that cannot see which
    numbers are 待确认 will describe a guess and a quote in the same confident
    voice.
    """
    project = _project(session)
    writer = _Writer(BODY)
    overview.write(project, extractor=writer)

    assert "WHERE EACH VALUE CAME FROM" in writer.user
    assert "THE FIVE TRACKS" in writer.user
    assert "WHAT IS MISSING" in writer.user
    assert "SOURCES" in writer.user
    assert "1000.0" in writer.user

    # And the instruction that keeps it honest is actually in the system prompt.
    assert "must come from the data given" in writer.system
    assert "Say when you do not know" in writer.system


# --- the buyer-position briefing ---------------------------------------------


def _position(session):
    from tracker import capex

    _project(session, name="Stargate", company="Crusoe", customer="OpenAI", mw_planned=1200.0)
    positions = {p.key: p for p in capex.rollup(session)}
    position = positions["openai"]
    projects = [session.get(Project, pid) for pid in position.project_ids]
    return position, projects


def test_a_position_briefing_streams_and_is_cached(session):
    position, projects = _position(session)
    writer = _Writer(
        "OpenAI's position is one leased campus in Abilene.\n\n- **weight** — all of it sits on one 1,200 MW site. [[END]] Total word count: 14"
    )

    text = "".join(overview.stream_position(position, projects, extractor=writer))
    assert "one leased campus" in text
    assert "[[END]]" not in text and "word count" not in text, "the sentinel must cut the tail"

    ready = overview.cached_position(position, projects)
    assert ready is not None and ready.text == text
    assert writer.calls == 1


def test_a_position_whose_rows_moved_is_not_served_stale(session):
    position, projects = _position(session)
    writer = _Writer("A reading that is long enough to pass the length floor for briefings.")
    "".join(overview.stream_position(position, projects, extractor=writer))
    assert overview.cached_position(position, projects) is not None

    projects[0].mw_planned = 4500.0
    projects[0].updated_at = dt.datetime(2027, 1, 1)
    session.flush()

    from tracker import capex

    moved = {p.key: p for p in capex.rollup(session)}["openai"]
    fresh = [session.get(Project, pid) for pid in moved.project_ids]
    assert overview.cached_position(moved, fresh) is None


def test_the_position_context_names_the_sites_and_the_shaky_money(session):
    position, projects = _position(session)
    context = overview.build_position_context(position, projects)
    assert "Crusoe — Stargate" in context["sites"]
    assert context["projects"] == "1"
    assert context["investment_usd"] == "none"


# --- the analytical shape, and what it must not break ------------------------
#
# `overview-v3` asks for 220 to 400 words across three sections instead of a
# sentence and two bullets. The panel renders full markdown for it, which means
# the pipeline between the model and the drawer now has to carry headings, tables
# and nested lists intact — and the one thing in that pipeline that *cuts* text is
# the runaway sentinel. A longer answer gives it far more line starts to match
# against, so the risk it silently truncates a good briefing went up with the
# length, and that is what these pin.

V3_BRIEFING = """\
Meta is building a 1 GW campus in New Albany. The shell is ahead of the power, \
which is the ordinary shape at this scale and is also the whole risk.

## Read of the build

Construction has reached equipment install while the power track has reached \
nothing at all.

| Track | Reached | What is missing |
| --- | --- | :--- |
| Construction | equipment install | nothing |
| Power | nothing reached | an interconnection agreement |

## What would move it

- **Interconnection agreement** — a signed one would date the energisation
  - the queue in this region runs years, so the date matters more than the figure
- **Named anchor tenant** — would confirm the campus is pre-leased rather than speculative

## How much to trust this

One trade-press article sits behind the capacity, and nothing quotes the money.

> The 1 GW figure is 待确认 and should not be read as cited.
"""


def test_the_briefing_prompt_is_the_analytical_one(session):
    project = _project(session)
    writer = _Writer(V3_BRIEFING)
    overview.write(project, extractor=writer)

    # The shape the renderer was widened for, asked for in the prompt itself.
    assert "## Read of the build" in writer.system
    assert "## What would move it" in writer.system
    assert "## How much to trust this" in writer.system
    assert "220 to 400 words" in writer.system

    # And the honesty rules survived the rewrite — a longer answer is a larger
    # surface for exactly the failure they exist to stop.
    assert "must come from the data given" in writer.system
    assert "Say when you do not know" in writer.system
    assert "nothing reached" in writer.system, "the literal-track rule is load-bearing"


def test_markdown_structure_reaches_the_reader_intact(session):
    """Headings, a table, nested bullets and a quote all survive the pipeline.

    Nothing between the model and the drawer may flatten these: the panel renders
    them as elements, and a briefing that arrives as one long paragraph is the
    failure this format was adopted to avoid.
    """
    project = _project(session)
    got = overview.write(project, extractor=_Writer(V3_BRIEFING))
    assert got is not None

    assert "## Read of the build" in got.text
    assert "| Track | Reached | What is missing |" in got.text
    assert "| --- | --- | :--- |" in got.text
    assert "  - the queue in this region runs years" in got.text, "nesting must survive"
    assert got.text.rstrip().endswith("should not be read as cited.")


def test_the_sentinel_does_not_cut_an_analytical_briefing(session):
    """The runaway guard must survive the longer format.

    It matches on line starts — `here is`, `revised`, `final answer` — and a 400
    word briefing offers many more of those than three lines did. A cut lands
    silently: the reader gets a briefing that stops mid-section and nothing says
    so, because only a near-empty result is rejected.
    """
    project = _project(session)
    # Character at a time, which is the worst case for a guard that matches on
    # line starts across a growing buffer.
    pieces = list(V3_BRIEFING)

    class _Streamer:
        model = "test-model"

        def stream(self, *, system, user, max_tokens):
            yield from pieces

    text = "".join(overview.stream(project, extractor=_Streamer()))
    assert text == V3_BRIEFING, "the sentinel cut a legitimate briefing"


def test_the_sentinel_still_cuts_a_model_that_starts_over(session):
    """The guard is kept, not loosened — the behaviour it was measured against is
    a model writing a good answer and then writing it again."""
    project = _project(session)
    body = V3_BRIEFING + "\n[[END]]\nHere is another version:\nMeta is building..."

    class _Streamer:
        model = "test-model"

        def stream(self, *, system, user, max_tokens):
            yield body

    text = "".join(overview.stream(project, extractor=_Streamer()))
    assert "[[END]]" not in text
    assert "another version" not in text
    assert "## How much to trust this" in text, "it cut before the answer finished"


def test_the_token_ceiling_leaves_room_for_the_longer_answer(session):
    """400 words of visible answer, all of it visible.

    This panel is served by the one tier that does not think, so the whole budget
    is the answer — the old 4096 was sized against a sentence and two bullets, not
    against reasoning it never pays for.
    """
    asked = {}

    class _Counting(_Writer):
        def complete(self, *, system, user, max_tokens):
            asked["max_tokens"] = max_tokens
            return super().complete(system=system, user=user, max_tokens=max_tokens)

    overview.write(_project(session), extractor=_Counting(V3_BRIEFING))
    assert asked["max_tokens"] == overview.MAX_TOKENS >= 8192
