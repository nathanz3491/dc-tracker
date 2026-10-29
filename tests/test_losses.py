"""The nightly loss audit: a value emptied with nothing deciding it is put back.

Built to hold whatever path empties the value, so the loss here is made the way the
traced ones were — a re-read that does not repeat a field, written by the replacing
path `crawl.run` no longer takes — and the audit is asked only what happened to the
evidence.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracker import losses
from tracker.cli import app
from tracker.db import init_db, session_scope
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.models import Project, Source
from tracker.upsert import SOURCE_NOTE_PREFIX, upsert_record

runner = CliRunner()

URL = "https://example.com/qts-richmond"
MW_SENTENCE = "That facility offers 110MW of capacity across three buildings."
DATE_SENTENCE = "The campus is scheduled to open in March 2027."
ARTICLE = f"QTS Richmond expands. {MW_SENTENCE} {DATE_SENTENCE}"


def reading(at: dt.datetime, **fields) -> IngestRecord:
    claims = {"name": "Richmond", "company": "QTS", "city": "Richmond", "state": "VA"}
    quotes = {}
    for name, (value, sentence) in fields.items():
        claims[name] = value
        quotes[name] = sentence
    return IngestRecord(
        project={"name": "Richmond", "company": "QTS", "city": "Richmond", "state": "VA"},
        sources=[
            SourceRecord(
                url=URL, source_type="trade_press", fetched_at=at, claims=claims, quotes=quotes
            )
        ],
    )


@pytest.fixture
def night(tmp_path: Path) -> tuple[Path, Path]:
    """A database, its snapshot from before the night, and the night's loss: a re-read
    that did not repeat `mw_built`, replacing the citation."""
    db = tmp_path / "tracker.db"
    engine, _ = init_db(db)
    with session_scope(engine) as s:
        upsert_record(
            s,
            reading(
                dt.datetime(2026, 10, 1),
                mw_built=(110, MW_SENTENCE),
                expected_online=("2027-03-01", DATE_SENTENCE),
            ),
        )
    snapshot = tmp_path / "before.db"
    con = sqlite3.connect(db)
    try:
        con.execute("VACUUM INTO ?", (str(snapshot),))
    finally:
        con.close()
    with session_scope(engine) as s:
        upsert_record(
            s, reading(dt.datetime(2026, 10, 20), expected_online=("2027-03-01", DATE_SENTENCE))
        )
        assert s.query(Project).one().mw_built is None
    engine.dispose()
    return db, snapshot


def _session(db: Path):
    engine, _ = init_db(db)
    return session_scope(engine)


def test_a_dropped_claim_the_article_still_states_is_put_back(night):
    db, snapshot = night
    with _session(db) as s:
        found, todo = losses.find(snapshot, s, article=lambda url: ARTICLE)
        assert [(x.field, x.value, x.outcome) for x in found.losses] == [
            ("mw_built", 110, losses.RESTORED)
        ]
        losses.restore(s, todo)

    with _session(db) as s:
        project = s.query(Project).one()
        assert project.mw_built == 110
        row = s.query(Source).one()
        assert json.loads(row.quotes)["mw_built"] == MW_SENTENCE
        assert any(
            line.startswith(SOURCE_NOTE_PREFIX) and "restored mw_built = 110" in line
            for line in project.notes.splitlines()
        )
    # Run again: nothing left to put back.
    with _session(db) as s:
        found, todo = losses.find(snapshot, s, article=lambda url: ARTICLE)
        assert found.losses == [] and todo == []


def test_an_article_that_no_longer_says_it_keeps_the_value_withdrawn(night):
    db, snapshot = night
    with _session(db) as s:
        found, todo = losses.find(snapshot, s, article=lambda url: DATE_SENTENCE)
        assert [x.outcome for x in found.losses] == [losses.WITHDRAWN]
        assert todo == []


def test_with_no_copy_of_the_article_the_loss_is_listed_not_guessed(night):
    db, snapshot = night
    with _session(db) as s:
        found, todo = losses.find(snapshot, s, article=lambda url: None)
        assert [x.outcome for x in found.losses] == [losses.UNCHECKED]
        assert todo == []


def test_a_value_a_ruling_cleared_is_a_decision_and_is_left_alone(tmp_path):
    db = tmp_path / "tracker.db"
    engine, _ = init_db(db)
    with session_scope(engine) as s:
        upsert_record(s, reading(dt.datetime(2026, 10, 1), mw_built=(110, MW_SENTENCE)))
    snapshot = tmp_path / "before.db"
    con = sqlite3.connect(db)
    con.execute("VACUUM INTO ?", (str(snapshot),))
    con.close()
    from tracker.upsert import recompute_from_sources

    with session_scope(engine) as s:
        row = s.query(Source).one()
        row.unconfirmed_reasons = json.dumps({"mw_built": "superseded"})
        row.unconfirmed_fields = "mw_built"
        recompute_from_sources(s, s.query(Project).one())
        assert s.query(Project).one().mw_built is None
    with session_scope(engine) as s:
        found, todo = losses.find(snapshot, s, article=lambda url: ARTICLE)
        assert found.losses == [] and todo == [] and found.decided == 1
    engine.dispose()


def test_the_command_reports_without_writing_unless_asked(night):
    db, snapshot = night
    result = runner.invoke(app, ["--db", str(db), "losses", "--against", str(snapshot)])
    assert result.exit_code == 0, result.output
    assert "mw_built 110" in " ".join(result.output.split())
    with _session(db) as s:
        assert s.query(Project).one().mw_built is None
