"""The per-run ledger, and the fetch-failure audit that reads beside it."""

from __future__ import annotations

import json

from sqlalchemy import select

from tracker import funnel, runlog
from tracker.ingest.records import IngestReport
from tracker.models import IngestUrl


def test_a_run_is_one_line(tmp_path):
    db = tmp_path / "tracker.db"
    report = IngestReport(read=10, llm_calls=8, prompt_tokens=1000, completion_tokens=250)
    report.bump("insert")
    runlog.record_ingest(db, command="ingest crawl", report=report, seconds=12.34, model="m")

    entries = runlog.read(db)
    assert len(entries) == 1
    assert entries[0]["command"] == "ingest crawl"
    assert entries[0]["llm_calls"] == 8
    assert entries[0]["prompt_tokens"] == 1000
    assert entries[0]["seconds"] == 12.3
    assert entries[0]["at"]


def test_runs_accumulate(tmp_path):
    db = tmp_path / "tracker.db"
    for _ in range(3):
        runlog.record_ingest(db, command="sync", report=IngestReport(), seconds=1.0)
    assert len(runlog.read(db)) == 3
    assert len(runlog.read(db, limit=2)) == 2


def test_the_ledger_lives_beside_clean_jsonl(tmp_path):
    """Same directory, same shape — `data/runs/` is where a time series goes."""
    db = tmp_path / "data" / "tracker.db"
    assert runlog.log_path(db).parent.name == "runs"
    assert runlog.log_path(db).name.endswith(".jsonl")


def test_a_malformed_line_is_skipped_not_fatal(tmp_path):
    db = tmp_path / "tracker.db"
    runlog.record_ingest(db, command="sync", report=IngestReport(), seconds=1.0)
    path = runlog.log_path(db)
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    assert len(runlog.read(db)) == 1


def test_an_unwritable_path_does_not_break_a_run(tmp_path):
    """A ledger that can fail a run is worse than no ledger.

    Losing a paid crawl because a disk was full would be a self-inflicted outage,
    so the write is best-effort by design.
    """
    blocker = tmp_path / "runs"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    runlog.record_ingest(tmp_path / "tracker.db", command="x", report=IngestReport(), seconds=0.1)
    assert runlog.read(tmp_path / "tracker.db") == []


def test_reading_a_ledger_that_does_not_exist_is_empty(tmp_path):
    assert runlog.read(tmp_path / "nothing.db") == []


def test_the_entry_is_json_serialisable_and_sorted(tmp_path):
    """Sorted keys so two runs diff cleanly."""
    db = tmp_path / "tracker.db"
    runlog.record_ingest(db, command="sync", report=IngestReport(read=1), seconds=0.5)
    line = runlog.log_path(db).read_text(encoding="utf-8").strip()
    assert json.loads(line)["urls"] == 1
    assert line.index('"at"') < line.index('"command"') < line.index('"urls"')


# --- the silent-timeout audit ------------------------------------------------


def test_fetch_failures_are_grouped_commonest_first(session):
    for n in range(3):
        session.add(
            IngestUrl(url=f"https://x.test/{n}", run_id="r", status="fetch_error", error="HTTP 403")
        )
    session.add(
        IngestUrl(url="https://y.test/1", run_id="r", status="fetch_error", error="timed out")
    )
    session.flush()

    assert funnel.fetch_failures(session) == [("HTTP 403", 3), ("timed out", 1)]


def test_only_failures_are_counted(session):
    session.add(IngestUrl(url="https://x.test/1", run_id="r", status="ok", error=None))
    session.add(
        IngestUrl(url="https://y.test/1", run_id="r", status="no_project", error="not an error")
    )
    session.flush()
    assert funnel.fetch_failures(session) == []
    assert session.scalar(select(IngestUrl.url).where(IngestUrl.status == "ok"))
