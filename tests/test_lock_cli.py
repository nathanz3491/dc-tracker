"""`tracker lock`: reading the write lock, and breaking it only as far as asked.

Each test is one promise: a free lock reads as free, a dead holder's lock reads as
stale and `break` removes it, a live holder's lock survives `break` and goes only with
`--force`, and a forced break of a live holder asks first unless told not to.
"""

from __future__ import annotations

import os

import pytest

from tracker.db import AlreadyRunning, break_write_lock, inspect_write_lock

DEAD_PID = 999_999_99  # no process has this id


def _write(db, raw):
    lock = db.parent / (db.name + ".lock")
    lock.write_text(raw, encoding="utf-8")
    return lock


def test_a_free_lock_reads_as_free(tmp_path):
    lock = inspect_write_lock(tmp_path / "tracker.db")
    assert not lock.held and not lock.stale
    assert not break_write_lock(tmp_path / "tracker.db").held


def test_a_dead_holders_lock_is_stale_and_break_removes_it(tmp_path):
    db = tmp_path / "tracker.db"
    path = _write(db, f"{DEAD_PID} logic resolve 2026-11-01 10:00:00.000001")

    lock = inspect_write_lock(db)
    assert lock.stale and lock.pid == DEAD_PID
    assert lock.command == "logic resolve"
    assert lock.since.startswith("2026-11-01")

    gone = break_write_lock(db)
    assert gone.pid == DEAD_PID
    assert not path.exists()


def test_a_live_holders_lock_survives_break_and_goes_only_with_force(tmp_path):
    db = tmp_path / "tracker.db"
    path = _write(db, f"{os.getpid()} sync 2026-11-01 10:00:00.000001")

    with pytest.raises(AlreadyRunning, match="--force"):
        break_write_lock(db)
    assert path.exists()

    gone = break_write_lock(db, force=True)
    assert gone.alive
    assert not path.exists()


def test_the_cli_refuses_a_live_lock_and_asks_before_forcing(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from tracker.cli import app

    db = tmp_path / "tracker.db"
    path = _write(db, f"{os.getpid()} sync 2026-11-01 10:00:00.000001")
    monkeypatch.setattr("tracker.cli.lock._db_path", lambda: db)
    runner = CliRunner()

    refused = runner.invoke(app, ["lock", "break"])
    assert refused.exit_code != 0 and path.exists()

    checked = runner.invoke(app, ["lock", "check"])
    assert "held by pid" in checked.output and "running" in checked.output

    said_no = runner.invoke(app, ["lock", "break", "--force"], input="n\n")
    assert said_no.exit_code != 0 and path.exists()

    forced = runner.invoke(app, ["lock", "break", "--force", "--yes"])
    assert forced.exit_code == 0, forced.output
    assert "running process" in forced.output and not path.exists()
