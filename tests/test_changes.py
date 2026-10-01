"""`tracker changes` and `tracker logic rule-out`: reading what a night changed, and
taking back a claim a person finds wrong.

Both came out of reading the night of 2026-09-30 by hand. The quality counts held
steady while four values went wrong, each with a verbatim sentence behind it — wrong
about what the sentence was about, which only a reader catches. One command puts each
change beside its sentence; the other undoes one in the way the audit's own repair
does, so the row is re-derived rather than typed into.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracker.cli import app
from tracker.db import init_db, session_scope
from tracker.models import Project

runner = CliRunner()
NOW = dt.datetime(2026, 9, 30, 12, 0, 0)


def _cite(url: str, value: float, quote: str, when: dt.datetime = NOW):
    from tracker.ingest.records import SourceRecord

    return SourceRecord(
        url=url,
        source_type="trade_press",
        excerpt="e",
        claims={"investment_usd": value},
        quotes={"investment_usd": quote},
        fetched_at=when,
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A database holding one campus with two quoted investment figures."""
    from tracker.ingest.records import IngestRecord
    from tracker.upsert import upsert_record

    path = tmp_path / "tracker.db"
    engine, _ = init_db(path)
    with session_scope(engine) as s:
        upsert_record(
            s,
            IngestRecord(
                project={
                    "company": "Google",
                    "name": "New Albany",
                    "city": "New Albany",
                    "state": "OH",
                },
                sources=[
                    _cite(
                        "https://a.test/campus",
                        600_000_000,
                        "Google to go ahead with $600 million data center in New Albany",
                    ),
                    _cite(
                        "https://b.test/state",
                        20_000_000_000,
                        "The company has invested $20 billion+ in Ohio since 2019",
                        NOW + dt.timedelta(days=30),
                    ),
                ],
            ),
        )
    engine.dispose()
    return path


def _snapshot(path: Path, to: Path) -> Path:
    con = sqlite3.connect(path)
    try:
        con.execute("VACUUM INTO ?", (str(to),))
    finally:
        con.close()
    return to


def _row(path: Path) -> Project:
    engine, _ = init_db(path)
    with session_scope(engine, commit=False) as s:
        row = s.query(Project).one()
        s.expunge(row)
    engine.dispose()
    return row


def test_rule_out_takes_one_claim_back_and_the_row_rederives(db):
    """The repair the audit itself makes: mark the claim, empty the field, re-derive."""
    held = _row(db).investment_usd
    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "logic",
            "rule-out",
            str(_row(db).id),
            "investment_usd",
            "--citation",
            "https://b.test/state" if held == 20_000_000_000 else "https://a.test/campus",
            "--why",
            "a statewide total, not this campus's",
        ],
    )
    assert result.exit_code == 0, result.output
    row = _row(db)
    assert row.investment_usd != held, "the other quoted figure now decides the field"
    assert "operator resolved `operator_ruled_out`" in row.notes
    assert "a statewide total" in row.notes


def test_rule_out_names_the_citations_it_could_have_meant(db):
    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "logic",
            "rule-out",
            str(_row(db).id),
            "investment_usd",
            "--citation",
            "https://nowhere.test/x",
            "--why",
            "w",
        ],
    )
    assert result.exit_code == 2
    assert "https://a.test/campus" in result.output and "https://b.test/state" in result.output


def test_a_dry_run_writes_nothing(db):
    before = _row(db).investment_usd
    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "logic",
            "rule-out",
            str(_row(db).id),
            "investment_usd",
            "--citation",
            "https://a.test/campus",
            "--citation",
            "https://b.test/state",
            "--why",
            "w",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output
    assert _row(db).investment_usd == before


def test_changes_lists_each_new_value_beside_its_sentence(db, tmp_path):
    """The counts cannot see a wrong value that has a quote; a reader can, given both."""
    snapshot = _snapshot(db, tmp_path / "before.db")
    row = _row(db)
    held = row.investment_usd
    loser = "https://b.test/state" if held == 20_000_000_000 else "https://a.test/campus"
    runner.invoke(
        app,
        [
            "--db",
            str(db),
            "logic",
            "rule-out",
            str(row.id),
            "investment_usd",
            "--citation",
            loser,
            "--why",
            "test",
        ],
    )

    result = runner.invoke(app, ["--db", str(db), "changes", "--against", str(snapshot)])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "investment_usd:" in out and "->" in out
    assert "behind it:" in out, "the sentence now behind the new value is shown"
    assert "operator_ruled_out" in out, "and the decision that made the change"


def test_a_reused_id_is_a_new_row_not_a_changed_one(db, tmp_path):
    """SQLite can hand a merged-away row's id to the next insert, as #1557 was on
    2026-09-30. Read by id alone, Skybox would have looked like Nebius edited."""
    from tracker.ingest.records import IngestRecord
    from tracker.upsert import upsert_record

    snapshot = _snapshot(db, tmp_path / "before.db")
    engine, _ = init_db(db)
    with session_scope(engine) as s:
        old = s.query(Project).one()
        reused = old.id
        s.delete(old)
        s.flush()
        upsert_record(
            s,
            IngestRecord(
                project={"company": "Skybox", "name": "Austin", "city": "Austin", "state": "TX"},
                sources=[_cite("https://c.test/x", 1.0, "a quoted sentence about Skybox Austin")],
            ),
        )
        assert s.query(Project).one().id == reused
    with session_scope(engine, commit=False) as s:
        from tracker import changes

        found = changes.diff(snapshot, s)
    engine.dispose()
    assert [pid for pid, _ in found.added] == [reused]
    assert [pid for pid, _ in found.removed] == [reused]
    assert found.values == []


def test_an_untouched_database_has_nothing_to_report(db, tmp_path):
    snapshot = _snapshot(db, tmp_path / "before.db")
    result = runner.invoke(app, ["--db", str(db), "changes", "--against", str(snapshot)])
    assert result.exit_code == 0, result.output
    assert "nothing" in result.output
