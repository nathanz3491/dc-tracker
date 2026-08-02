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
