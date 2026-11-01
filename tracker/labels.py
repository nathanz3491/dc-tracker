"""What the tracker learns about its own values, kept as training labels.

The suspicion score that aims re-checks (docs: "prediction head") learns from labels,
and the scarce ones — an answer key researched by hand — are a few hundred. The
tracker already makes judgements about its own values every night and throws the
record away. This keeps two of them:

* **Verdicts.** Every paid pass that re-reads sources and writes what it decides —
  `logic resolve`, `audit resolve`, `duplicates resolve`, `risks confirm`,
  `tranches review`, enrich's settle and agent steps — goes through `judged.run`.
  There, each answer's effect is measured directly: the row's values are read just
  before the answer is applied and again after, and every field that moved is a line
  ("changed", old and new). A pass that says which fields an item is about (a logic
  or audit finding names them) also records the fields it looked at and left alone
  ("kept"), and the ones it could not decide ("declined"). Measured rather than
  reported, so no pass has to remember to say what it did, and a pass added later is
  covered the day it uses `judged.run`.
* **Findings.** The free rule checks (`logic check`'s rules, `audit check`) are run
  once a night, before round 1, and every finding is written with the value it
  flagged. Whether a finding was right is then a question the record can answer:
  did that value later change (confirmed), or did the finding go away with the
  value standing (dismissed)? Measured on 2026-11-01 against the answer key, finding
  types range from 2 wrong in 10 (an ambiguous phase label) to 4 in 4 (investment
  below build cost), so one reliability for all of them is wrong — each type earns
  its own from this record.

**JSONL files, not tables**, beside `ingest.jsonl` and for the same reasons
(`tracker.runlog`): append-only, read rarely and in bulk, and a bad write costs a
line rather than a migration. **Best-effort**: nothing here may fail a run, so every
error is logged and swallowed.

`tracker export training` reads both, with the nightly snapshots, the claims and the
answer key, into one training table.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from tracker.models import utcnow

log = logging.getLogger(__name__)

VERDICTS = "verdicts.jsonl"
FINDINGS = "findings.jsonl"

#: The fields a value-level label is about: the answer key's fields.
FIELDS = ("phase", "mw_planned", "investment_usd", "expected_online", "first_announced")

#: `audit check` findings carry a code but no fields; these are the fields each is about.
AUDIT_FIELDS: dict[str, tuple[str, ...]] = {
    "investment_below_build_cost": ("investment_usd",),
    "usd_per_mw_out_of_band": ("investment_usd", "mw_planned"),
}
AUDIT_DEFAULT = ("mw_planned",)

Held = dict[int, dict[str, Any]]


def path_of(db_path: Path | str, name: str) -> Path:
    return Path(db_path).parent / "runs" / name


def db_path_of(session: Any) -> Path | None:
    """The database file behind a session, or None for one in memory."""
    try:
        name = session.get_bind().url.database
    except Exception:
        return None
    return Path(name) if name and name != ":memory:" else None


def _append(db_path: Path | None, name: str, lines: Iterable[dict[str, Any]]) -> None:
    if db_path is None:
        return
    try:
        rows = [json.dumps(line, ensure_ascii=False, sort_keys=True, default=str) for line in lines]
        if not rows:
            return
        path = path_of(db_path, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + "\n")
    except OSError as exc:
        log.warning("could not write %s: %s", name, exc)


def read(db_path: Path | str, name: str) -> list[dict[str, Any]]:
    """Every line of one log, oldest first. Malformed lines are skipped."""
    path = path_of(db_path, name)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _plain(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def held(session: Any, project_ids: Iterable[int]) -> Held:
    """{project: {field: value}} for the label fields, as the rows hold them now."""
    from tracker.models import Project

    out: Held = {}
    for pid in {int(p) for p in project_ids if p is not None}:
        try:
            row = session.get(Project, pid)
        except Exception:
            continue
        if row is not None:
            out[pid] = {f: _plain(getattr(row, f, None)) for f in FIELDS}
    return out


def record_verdicts(
    session: Any,
    *,
    by: str,
    before: Held,
    after: Held,
    looked_at: Iterable[tuple[int, str]] = (),
    declined: bool = False,
) -> None:
    """Write what one answer did to its rows. Never raises.

    A field whose value moved is "changed"; a field in `looked_at` that did not move
    is "kept", or "declined" when the pass could not decide. A row that is gone after
    the answer (merged away) records nothing: its values went with it.
    """
    try:
        at = utcnow().isoformat()
        looked = {(int(p), f) for p, f in looked_at}
        lines = []
        for pid, old in before.items():
            new = after.get(pid)
            if new is None:
                continue
            for f in FIELDS:
                if old.get(f) != new.get(f):
                    verdict = "changed"
                elif (pid, f) in looked:
                    verdict = "declined" if declined else "kept"
                else:
                    continue
                lines.append(
                    {
                        "at": at,
                        "by": by,
                        "project_id": pid,
                        "field": f,
                        "verdict": verdict,
                        "old": old.get(f),
                        "new": new.get(f),
                    }
                )
        _append(db_path_of(session), VERDICTS, lines)
    except Exception as exc:  # a label must never cost a pass its answer
        log.warning("could not record verdicts: %s", exc)


def record_findings(session: Any, *, night: str) -> int:
    """Run the free rule checks and write every finding with the value it flagged.

    Rules only: no model is asked, so this costs nothing. Returns how many lines were
    written (0 on any error, which is logged).
    """
    try:
        from tracker import audit, logic

        report = logic.review(session, extractor=None)
        flagged: list[tuple[str, str, int, tuple[str, ...]]] = [
            ("logic", f.code, f.project_id, tuple(f.fields))
            for f in report.findings
            if not f.inferred
        ]
        flagged += [
            ("audit", f.code, f.project_id, AUDIT_FIELDS.get(f.code, AUDIT_DEFAULT))
            for f in audit.run(session)
        ]
        values = held(session, {pid for _, _, pid, _ in flagged})
        lines = [
            {
                "night": night,
                "check": check,
                "code": code,
                "project_id": pid,
                "field": f,
                "value": values.get(pid, {}).get(f),
            }
            for check, code, pid, fields in flagged
            for f in fields
            if f in FIELDS
        ]
        _append(db_path_of(session), FINDINGS, lines)
        return len(lines)
    except Exception as exc:
        log.warning("could not record the night's findings: %s", exc)
        return 0


__all__ = [
    "AUDIT_FIELDS",
    "FIELDS",
    "FINDINGS",
    "VERDICTS",
    "db_path_of",
    "held",
    "path_of",
    "read",
    "record_findings",
    "record_verdicts",
]
