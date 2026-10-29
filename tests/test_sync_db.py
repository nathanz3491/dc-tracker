"""`scripts/sync_db.py`, which is a script and not a module, so it is loaded by path."""

from __future__ import annotations

import importlib.util
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
