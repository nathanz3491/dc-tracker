"""A line per ingest run: what it cost, how long it took, what went wrong.

The 2026-08-12 review asked for this as a habit rather than a feature: keep a log
of every model call, how many and roughly what they consumed, so that after a
change somebody can ask which stage is slowest, which errors most, and where the
tokens went.

Most of the answer already existed and was thrown away. `ExtractionOutcome` has
carried `prompt_tokens` and `completion_tokens` per URL since it was written and
nothing summed them; `ingest_url.status` has classified every failure all along.
What was missing is a record that survives the run.

**A JSONL file, not a table**, following `data/runs/clean.jsonl` exactly. The
reasoning there applies unchanged: the one thing a column cannot be is a time
series, and a run log is append-only, read rarely, and useless to `tracker list`.
It also means a bad write costs a line rather than a migration.

**Best-effort.** A ledger that can fail a run is worse than no ledger — losing a
crawl because a disk was full would be a self-inflicted outage — so every error
here is logged and swallowed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from tracker.models import utcnow

log = logging.getLogger(__name__)

#: Beside `clean.jsonl`, for the same reason and read the same way.
LOG_NAME = "ingest.jsonl"


def log_path(db_path: Path) -> Path:
    return Path(db_path).parent / "runs" / LOG_NAME


def append(db_path: Path, entry: dict[str, Any]) -> None:
    """Add one run to the ledger. Never raises."""
    try:
        path = log_path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": utcnow().isoformat(), **entry}, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        log.warning("could not write the run ledger: %s", exc)


def record_ingest(
    db_path: Path,
    *,
    command: str,
    report: Any,
    seconds: float,
    model: str | None = None,
    run_id: str | None = None,
) -> None:
    """Summarise one `crawl.run` into the ledger.

    Reads the report rather than being told, so a counter added there appears here
    without a second place to remember.
    """
    append(
        db_path,
        {
            "command": command,
            "run_id": run_id,
            "seconds": round(seconds, 1),
            "model": model,
            "urls": getattr(report, "read", 0),
            "llm_calls": getattr(report, "llm_calls", 0),
            "prompt_tokens": getattr(report, "prompt_tokens", 0),
            "completion_tokens": getattr(report, "completion_tokens", 0),
            "written": getattr(report, "written", 0),
            "errors": {
                "fetch": getattr(report, "fetch_error", 0),
                "parse": getattr(report, "parse_error", 0),
                "thin": getattr(report, "thin_content", 0),
            },
        },
    )


def read(db_path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    """The ledger, newest last. Malformed lines are skipped, not fatal."""
    path = log_path(db_path)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out[-limit:] if limit else out


__all__ = ["LOG_NAME", "append", "log_path", "read", "record_ingest"]
