"""Run history: what was executed, what it printed, and what it changed.

Files, not a table. The schema holds the tracked data and is guarded by a drift
test; a command's stdout is operational exhaust with a different lifetime and no
relational shape. One JSONL per run beside the database, and an index for the
listing.

`.gitignore` already carries an unanchored `runs/` pattern that nothing wrote to,
so `data/runs/` is ignored the moment it appears.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tracker.models import utcnow

log = logging.getLogger(__name__)

#: Lines kept in memory for a running command, so a browser that connects late
#: still sees the beginning. The file on disk keeps everything.
TAIL = 4000

#: Runs listed in the index. Older files stay on disk; they are just not listed.
INDEX_LIMIT = 200

_lock = threading.Lock()


@dataclass
class Run:
    id: str
    cmd: str
    argv: list[str]
    started_at: str
    cost: str = "free"
    status: str = "running"  # running | ok | failed | cancelled
    exit_code: int | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    #: Projects whose `updated_at` moved while this run was in flight. Counted
    #: rather than diffed: field-level history does not exist in the schema, and a
    #: count that is true beats a diff that is reconstructed.
    projects_touched: int = 0
    lines: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("lines")
        return data


def runs_dir(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / "runs"


def _index_path(db_path: str | Path) -> Path:
    return runs_dir(db_path) / "index.json"


def _log_path(db_path: str | Path, run_id: str) -> Path:
    return runs_dir(db_path) / f"{run_id}.jsonl"


def new_id(db_path: str | Path) -> str:
    """A sortable id that is also unique against what is already on disk."""
    stamp = utcnow().strftime("%Y%m%dT%H%M%S")
    directory = runs_dir(db_path)
    candidate, n = stamp, 1
    while (directory / f"{candidate}.jsonl").exists():
        n += 1
        candidate = f"{stamp}-{n}"
    return candidate


def begin(db_path: str | Path, run: Run) -> None:
    directory = runs_dir(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    with _log_path(db_path, run.id).open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"t": "start", **run.summary()}, ensure_ascii=False) + "\n")


def append(db_path: str | Path, run: Run, line: str) -> None:
    run.lines.append(line)
    if len(run.lines) > TAIL:
        del run.lines[: len(run.lines) - TAIL]
    try:
        with _log_path(db_path, run.id).open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"t": "out", "s": line}, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("could not append to the run log for %s", run.id)


def finish(db_path: str | Path, run: Run) -> None:
    try:
        with _log_path(db_path, run.id).open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"t": "end", **run.summary()}, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("could not close the run log for %s", run.id)
    _record(db_path, run)


def _record(db_path: str | Path, run: Run) -> None:
    """Put the finished run at the head of the index, atomically."""
    path = _index_path(db_path)
    with _lock:
        entries = _read_index(db_path)
        entries = [e for e in entries if e.get("id") != run.id]
        entries.insert(0, run.summary())
        del entries[INDEX_LIMIT:]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def _read_index(db_path: str | Path) -> list[dict[str, Any]]:
    path = _index_path(db_path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("run index at %s is unreadable; starting a fresh listing", path)
        return []
    return data if isinstance(data, list) else []


def history(db_path: str | Path, limit: int = 50) -> list[dict[str, Any]]:
    return _read_index(db_path)[:limit]


def read_log(db_path: str | Path, run_id: str) -> dict[str, Any] | None:
    """One run's header, output and footer, replayed from its file."""
    path = _log_path(db_path, run_id)
    if not path.is_file():
        return None
    header: dict[str, Any] = {}
    footer: dict[str, Any] = {}
    lines: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            kind = entry.get("t")
            if kind == "out":
                lines.append(entry.get("s", ""))
            elif kind == "start":
                header = entry
            elif kind == "end":
                footer = entry
    return {**header, **footer, "lines": lines}


__all__ = [
    "INDEX_LIMIT",
    "TAIL",
    "Run",
    "append",
    "begin",
    "finish",
    "history",
    "new_id",
    "read_log",
    "runs_dir",
]
