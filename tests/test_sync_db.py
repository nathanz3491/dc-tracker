"""`scripts/sync_db.py`, which is a script and not a module, so it is loaded by path."""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_db.py"


def _sync_db():
    spec = importlib.util.spec_from_file_location("sync_db", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_failed_copy_still_removes_the_snapshot_left_on_the_host(monkeypatch, tmp_path):
    sync_db = _sync_db()
    monkeypatch.setattr(sync_db, "remote_counts", lambda host, remote: {"project": 3})
    monkeypatch.setattr(
        sync_db, "remote_snapshot", lambda host, remote: "/tmp/sync-db-x/tracker.db"
    )

    def dropped(command):
        raise SystemExit("failed: scp\nConnection reset by peer")

    monkeypatch.setattr(sync_db, "run", dropped)
    cleaned = []
    monkeypatch.setattr(subprocess, "run", lambda command, **_: cleaned.append(command))

    args = SimpleNamespace(host="prod", remote="~/tracker.db", force=False, dry_run=False)
    with pytest.raises(SystemExit):
        sync_db.pull(args, tmp_path / "tracker.db")

    assert cleaned == [["ssh", "prod", "rm -rf /tmp/sync-db-x"]]
    assert not (tmp_path / "tracker.db").exists()


# --- the one snapshot implementation (`tracker.db`), which both sides use ----------


def _db_with_rows(path: Path, n: int) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode=wal")
    con.execute("pragma wal_autocheckpoint=0")
    con.execute("create table project (id integer primary key, name text)")
    con.executemany("insert into project (name) values (?)", [(f"p{i}",) for i in range(n)])
    con.commit()
    return con


def test_a_snapshot_carries_what_is_still_in_the_wal(tmp_path):
    """A copy of the main file alone opens cleanly and is silently stale."""
    from tracker.db import take_snapshot

    source = tmp_path / "tracker.db"
    held = _db_with_rows(source, 7)  # held open: nothing checkpointed into the main file
    try:
        assert (tmp_path / "tracker.db-wal").stat().st_size > 0
        target = tmp_path / "snap.db"
        assert take_snapshot(source, target) == []
        copy = sqlite3.connect(target)
        try:
            assert copy.execute("select count(*) from project").fetchone()[0] == 7
        finally:
            copy.close()
    finally:
        held.close()


def test_a_snapshot_missing_rows_is_reported(tmp_path):
    from tracker.db import verify_snapshot

    _db_with_rows(tmp_path / "short.db", 2).close()
    assert verify_snapshot(tmp_path / "short.db", {"project": 3}) == [
        "project: 2 rows, source had 3"
    ]


def test_a_database_with_no_counted_table_is_refused(tmp_path):
    """Counting nothing would pass the row check having compared nothing."""
    from tracker.db import take_snapshot

    sqlite3.connect(tmp_path / "empty.db").close()
    problems = take_snapshot(tmp_path / "empty.db", tmp_path / "snap.db")
    assert problems and problems[0].startswith("none of project")
    assert not (tmp_path / "snap.db").exists()


def test_the_script_uses_the_library_snapshot():
    from tracker import db

    sync_db = _sync_db()
    assert sync_db.snapshot is db.vacuum_into
    assert sync_db.verify is db.verify_snapshot
    assert sync_db.counts is db.table_counts
