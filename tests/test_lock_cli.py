"""`tracker lock`: reading the write lock, and breaking it only as far as asked.

Each test is one promise: a free lock reads as free, a dead holder's lock reads as
stale and `break` removes it, a live holder's lock survives `break` and goes only with
`--force`, and only on the second `--force` about that same lock: the first warns.
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


def _cli(tmp_path, monkeypatch, raw):
    from typer.testing import CliRunner

    from tracker.cli import app

    db = tmp_path / "tracker.db"
    path = _write(db, raw)
    monkeypatch.setattr("tracker.cli.lock._db_path", lambda: db)
    runner = CliRunner()
    return (lambda *args: runner.invoke(app, ["lock", *args])), path


def test_a_live_lock_is_refused_without_force(tmp_path, monkeypatch):
    run, path = _cli(tmp_path, monkeypatch, f"{os.getpid()} sync 2026-11-01 10:00:00.000001")

    assert run("break").exit_code != 0 and path.exists()
    checked = run("check")
    assert "held by pid" in checked.output and "running" in checked.output


def test_the_first_force_warns_and_the_second_breaks(tmp_path, monkeypatch):
    run, path = _cli(tmp_path, monkeypatch, f"{os.getpid()} sync 2026-11-01 10:00:00.000001")

    first = run("break", "--force")
    assert first.exit_code != 0 and path.exists()
    assert "WARNING" in first.output and "Nothing was done" in first.output

    second = run("break", "--force")
    assert second.exit_code == 0, second.output
    assert "running process" in second.output and not path.exists()
    assert not (tmp_path / "tracker.db.lock.force-armed").exists(), "the warning is spent"


def test_a_warning_about_one_lock_does_not_arm_a_break_of_another(tmp_path, monkeypatch):
    run, path = _cli(tmp_path, monkeypatch, f"{os.getpid()} sync 2026-11-01 10:00:00.000001")
    assert "WARNING" in run("break", "--force").output

    path.write_text(f"{os.getpid()} enrich 2026-11-01 10:05:00.000001", encoding="utf-8")
    again = run("break", "--force")
    assert "WARNING" in again.output and path.exists(), "a new holder is warned about anew"


def test_a_stale_warning_warns_again(tmp_path, monkeypatch):
    run, path = _cli(tmp_path, monkeypatch, f"{os.getpid()} sync 2026-11-01 10:00:00.000001")
    run("break", "--force")
    armed = tmp_path / "tracker.db.lock.force-armed"
    old = armed.stat().st_mtime - 3600
    os.utime(armed, (old, old))

    assert "WARNING" in run("break", "--force").output and path.exists()


def test_a_dead_holders_lock_breaks_at_once_even_with_force(tmp_path, monkeypatch):
    run, path = _cli(tmp_path, monkeypatch, f"{DEAD_PID} sync 2026-11-01 10:00:00.000001")

    done = run("break", "--force")
    assert done.exit_code == 0 and "dead process" in done.output and not path.exists()


def _holder(monkeypatch, *argv):
    """A sleeping process whose command line says what `argv` says.

    Its command line is reported from here rather than read with `ps`, which the host's
    test sandbox refuses; reading it is `process_command`'s job, not these tests'.
    """
    import subprocess
    import sys
    import threading

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", *argv])
    # Reaped as soon as it ends, as a real holder's parent would: an unreaped child is
    # a zombie, and a zombie's pid still answers as alive.
    threading.Thread(target=process.wait, daemon=True).start()
    command = " ".join([sys.executable, "-c", "pass", *argv])
    monkeypatch.setattr(
        "tracker.db.process_command", lambda pid: command if pid == process.pid else None
    )
    return process


def test_stop_process_warns_first_then_ends_the_holder_and_breaks_its_lock(tmp_path, monkeypatch):
    holder = _holder(monkeypatch, "tracker", "sync")
    try:
        run, path = _cli(tmp_path, monkeypatch, f"{holder.pid} sync 2026-11-01 10:00:00.000001")

        first = run("break", "--stop-process")
        assert first.exit_code != 0 and "WARNING" in first.output
        assert holder.poll() is None and path.exists(), "the first run does nothing"

        done = run("break", "--stop-process")
        assert done.exit_code == 0, done.output
        assert "broke its write lock" in done.output and not path.exists()
        assert holder.wait(timeout=20) is not None
    finally:
        holder.kill()


def test_a_force_warning_does_not_arm_a_stop(tmp_path, monkeypatch):
    holder = _holder(monkeypatch, "tracker", "sync")
    try:
        run, _ = _cli(tmp_path, monkeypatch, f"{holder.pid} sync 2026-11-01 10:00:00.000001")
        run("break", "--force")

        stop = run("break", "--stop-process")
        assert "WARNING" in stop.output and holder.poll() is None
    finally:
        holder.kill()


def test_stop_process_refuses_a_pid_that_is_not_a_tracker_command(tmp_path, monkeypatch):
    other = _holder(monkeypatch, "something-else")
    try:
        run, path = _cli(tmp_path, monkeypatch, f"{other.pid} sync 2026-11-01 10:00:00.000001")

        run("break", "--stop-process")
        refused = run("break", "--stop-process")

        assert refused.exit_code != 0 and "not a tracker command" in refused.output
        assert path.exists() and other.poll() is None, "nothing was stopped or broken"
    finally:
        other.kill()


def test_a_phase_of_the_nightly_loop_gets_the_loud_warning(tmp_path, monkeypatch):
    holder = _holder(monkeypatch, "tracker", "logic", "resolve")
    try:
        run, path = _cli(
            tmp_path, monkeypatch, f"{holder.pid} logic resolve 2026-11-01 18:30:00.000001"
        )
        night = tmp_path / "runs" / "overnight.lock"
        night.mkdir(parents=True)
        (night / "pid").write_text(str(os.getpid()), encoding="utf-8")  # the holder's parent

        warned = run("break", "--force")

        assert "THE NIGHTLY LOOP IS RUNNING" in warned.output
        assert path.exists() and holder.poll() is None
    finally:
        holder.kill()


def test_force_and_stop_process_together_are_refused(tmp_path, monkeypatch):
    run, path = _cli(tmp_path, monkeypatch, f"{os.getpid()} sync 2026-11-01 10:00:00.000001")
    assert run("break", "--force", "--stop-process").exit_code != 0 and path.exists()


@pytest.mark.parametrize(
    ("command", "expect"),
    [
        ("/Users/nz/.local/bin/tracker logic resolve --limit 60", True),
        ("/Users/nz/dev/tracker/repo/.venv/bin/python -m tracker enrich --t2", True),
        (r'"C:\Users\x\database_tracker\.venv\Scripts\tracker.exe" sync', True),
        (r"C:\Users\x\database_tracker\.venv\Scripts\python.exe -c pass other", False),
        ("/usr/bin/vim tracker.db", False),
    ],
)
def test_only_a_command_that_runs_the_tracker_counts_as_one(command, expect):
    from tracker.db import is_tracker_command

    assert is_tracker_command(command) is expect
