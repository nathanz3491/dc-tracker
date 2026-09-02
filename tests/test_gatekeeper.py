"""Stopping a duplicate at write time instead of reporting it afterwards.

The arithmetic this exists for: a duplicate created at ingest costs a row nobody
wanted, one side of the pair held out of every `capex` total until somebody settles
it, and then a person or a merge that deletes a row. Measured here: 47 groups
holding 22,012 MW twice, cleared by a ten-hour agent run. The same judgement before
the insert costs one call and deletes nothing.

Every test below is really about one property — **it fails open.** Unsure, errored,
under-confident, unquoted: all of them insert, which is exactly what happens today.
That is what makes it safe to leave on.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker import gatekeeper
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.llm import LLMReply, ToolCall
from tracker.models import Project
from tracker.upsert import upsert_record

_URL = "https://example.test/racine-county"
_ARTICLE = (
    "Microsoft confirmed that its Racine County development is the Mount Pleasant "
    "campus it announced in 2023, on the same 1,030-acre parcel in the village. "
    "The project is a single campus and not a second site."
)


@pytest.fixture
def stored(session):
    """A city-level row. The arriving record below is its county twin."""
    project = Project(
        name="Fairwater",
        company="Microsoft",
        city="Mount Pleasant",
        county="Racine",
        state="WI",
        dedup_key="microsoft|city:mount pleasant|WI",
        phase="construction",
    )
    session.add(project)
    session.flush()
    return project


def _arriving() -> IngestRecord:
    """The same campus, filed under the county — the shape that makes duplicates."""
    return IngestRecord(
        project={
            "company": "Microsoft",
            "name": "Racine County Data Center",
            "county": "Racine",
            "state": "WI",
            "mw_planned": 1030.0,
        },
        sources=[
            SourceRecord(
                url=_URL,
                source_type="trade_press",
                fetched_at=dt.datetime(2026, 6, 1),
                excerpt="Microsoft's Racine County development.",
                claims={"mw_planned": 1030.0},
                quotes={"mw_planned": "the same 1,030-acre parcel in the village"},
            )
        ],
    )


@pytest.fixture
def cached(tmp_path, monkeypatch):
    from tracker import agent
    from tracker.ingest.fetch import cache_path

    cache_path(_URL, tmp_path).write_text(_ARTICLE, encoding="utf-8")
    real = agent.evidence_toolkit
    monkeypatch.setattr(
        agent,
        "evidence_toolkit",
        lambda s, **kw: real(s, cache_dir=tmp_path, allow_search=kw.get("allow_search", True)),
    )
    return tmp_path


class _Model:
    """Reads the arriving article, then returns the verdict it was built with."""

    def __init__(self, tool: str, *, confidence: float = 0.95, quote: str = "", read: bool = True):
        self.tool, self.confidence, self.quote, self.read = tool, confidence, quote, read
        self.turn = 0

    def converse(self, *, system, messages, tools=None, max_tokens=None):
        self.turn += 1
        if self.turn == 1 and self.read:
            args = {"url": _URL}
            return LLMReply(
                text="",
                tool_calls=(
                    ToolCall(
                        id="r", name="read_article", arguments=args, raw_arguments=json.dumps(args)
                    ),
                ),
            )
        args: dict = {"reason": "one campus filed at two precisions"}
        if self.tool != "unsure":
            args["confidence"] = self.confidence
        if self.quote:
            args["quote"] = self.quote
        return LLMReply(
            text="",
            tool_calls=(
                ToolCall(id="v", name=self.tool, arguments=args, raw_arguments=json.dumps(args)),
            ),
        )


# --- the point ---------------------------------------------------------------


def test_a_confirmed_same_site_routes_instead_of_inserting(session, stored, cached):
    """The duplicate is never created, and nothing is deleted to achieve that."""
    arbiter = gatekeeper.same_site_arbiter(
        _Model("same_site", confidence=0.95, quote="is the Mount Pleasant campus it announced")
    )

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.project_id == stored.id
    assert result.action != "insert"
    assert session.query(Project).count() == 1, "a second row was created anyway"
    assert stored.mw_planned == 1030.0, "the arriving claim did not reach the row"


def test_the_routing_decision_is_written_into_the_row(session, stored, cached):
    """A routing that leaves no trace is indistinguishable later from the row
    having always been this way."""
    arbiter = gatekeeper.same_site_arbiter(
        _Model("same_site", confidence=0.95, quote="is the Mount Pleasant campus it announced")
    )

    upsert_record(session, _arriving(), arbiter=arbiter)

    assert "routed an arriving record" in (stored.notes or "")
    assert "agent (0.95)" in (stored.notes or "")


# --- failing open, which is the whole safety argument ------------------------


def test_unsure_inserts_exactly_as_today(session, stored, cached):
    arbiter = gatekeeper.same_site_arbiter(_Model("unsure"))

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_different_site_inserts(session, stored, cached):
    arbiter = gatekeeper.same_site_arbiter(_Model("different_site", confidence=0.99))

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_confidence_below_the_floor_inserts(session, stored, cached):
    """Higher bar than a merge needs: this is decided from one arriving article."""
    arbiter = gatekeeper.same_site_arbiter(
        _Model("same_site", confidence=0.7, quote="is the Mount Pleasant campus it announced")
    )

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_a_same_site_verdict_without_a_real_quote_inserts(session, stored, cached):
    arbiter = gatekeeper.same_site_arbiter(
        _Model("same_site", confidence=0.99, quote="they share a substation and a switchyard")
    )

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_an_arbiter_that_raises_does_not_break_the_ingest(session, stored, cached):
    """An ingest of 300 articles must not die on one arbitration."""

    def _boom(**_kwargs):
        raise RuntimeError("provider on fire")

    result = upsert_record(session, _arriving(), arbiter=_boom)

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_a_provider_without_tools_inserts(session, stored, cached):
    class _Old:
        def complete(self, *, system, user, max_tokens=None):
            return LLMReply(text="{}")

    arbiter = gatekeeper.same_site_arbiter(_Old())

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.action == "insert"


def test_no_arbiter_is_the_old_behaviour(session, stored):
    """The default path is untouched, so every existing caller is unaffected."""
    result = upsert_record(session, _arriving())

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_a_record_with_no_citation_is_not_arbitrated(session, stored):
    """Hand-curated and ISO rows have no article, so there is nothing to read and
    nothing this can add. No call is made."""

    class _Never:
        def converse(self, **_kwargs):
            raise AssertionError("should not have called the model")

    arbiter = gatekeeper.same_site_arbiter(_Never())
    record = _arriving()
    record.sources.clear()

    result = upsert_record(session, record, arbiter=arbiter)

    assert result.action == "insert"


def test_an_exact_key_match_never_reaches_the_arbiter(session, stored):
    """Only an otherwise-new row is arbitrated. A record that already matches by
    key is an update and costs nothing."""

    class _Never:
        def converse(self, **_kwargs):
            raise AssertionError("should not have called the model")

    arbiter = gatekeeper.same_site_arbiter(_Never())
    record = _arriving()
    record.project["city"] = "Mount Pleasant"
    record.project.pop("county", None)

    result = upsert_record(session, record, arbiter=arbiter)

    assert result.project_id == stored.id
    assert session.query(Project).count() == 1
