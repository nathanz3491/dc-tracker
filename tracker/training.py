"""One table to train the suspicion score on: every stored value with all that is known about it.

`tracker export training` writes it as JSON lines, one per campus and field the answer
key can judge (stage, planned MW, investment, online date, announced date). Each line
carries:

* **claims** — every statement any source makes about that field (the fact table's
  rows: value, tier, why it was ruled out, date, publisher, source type, quote). The
  model reads these one by one rather than as counts, because the errors the answer
  key finds are about what a quote describes, which a count cannot see.
* **row** — the row's identity as it holds it (name, company, city, county, state,
  customer) and how many different values its sources give each one: a row whose
  sources name three towns is often several sites under one name.
* **history** — every value the field held across the nightly snapshots
  (`--snapshots`, the directory of `VACUUM INTO` copies the loop takes), one entry
  per change. A value replaced a few nights later was probably wrong; one that stood
  for weeks while new reporting arrived was probably right. These are the plentiful
  labels: thousands, free, and about stored values, which is what the score ranks.
* **findings** — each night's rule-check findings on the field (`tracker.labels`),
  with an outcome read from what the value did next: `confirmed` (it changed),
  `dismissed` (the finding went away and the value stood), `open`.
* **verdicts** — what every paid re-reading pass did to the field (`tracker.labels`).
* **answer** — the answer key's answer and its verdict on the held value, when the
  key has one. The scarce, hand-checked labels; a trainer holds these out.

It reads; it never writes. Snapshots are opened read-only with plain `sqlite3`, so an
old snapshot from before a migration is read for whatever columns it has.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from tracker import labels

FIELDS = labels.FIELDS
IDENTITY = ("name", "company", "city", "county", "state", "customer")
_NIGHT = re.compile(r"(\d{4})(\d{2})(\d{2})-(\d{6})")


def snapshot_nights(directory: Path) -> list[tuple[str, Path]]:
    """(night, file) for each dated snapshot, the last one taken on each day."""
    by_day: dict[str, tuple[str, Path]] = {}
    for path in sorted(Path(directory).glob("*.db")):
        m = _NIGHT.search(path.name)
        if not m:
            continue
        day = f"{m[1]}-{m[2]}-{m[3]}"
        stamp = m[0]
        if day not in by_day or stamp > by_day[day][0]:
            by_day[day] = (stamp, path)
    return [(day, path) for day, (_, path) in sorted(by_day.items())]


def history(directory: Path | None, project_ids: set[int]) -> dict[tuple[int, str], list[dict]]:
    """(project, field) -> [{night, value}], one entry each time the held value changed."""
    out: dict[tuple[int, str], list[dict]] = defaultdict(list)
    if directory is None:
        return out
    for night, path in snapshot_nights(directory):
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            have = {row[1] for row in con.execute("PRAGMA table_info(project)")}
            cols = [f for f in FIELDS if f in have]
            if not cols:
                continue
            for row in con.execute(f"SELECT id, {', '.join(cols)} FROM project"):
                pid = int(row[0])
                if pid not in project_ids:
                    continue
                for f, value in zip(cols, row[1:], strict=True):
                    series = out[(pid, f)]
                    if not series or series[-1]["value"] != value:
                        series.append({"night": night, "value": value})
        except sqlite3.Error:
            continue
        finally:
            con.close()
    return out


def finding_outcomes(
    lines: list[dict], held_now: dict[tuple[int, str], Any]
) -> dict[tuple[int, str], list[dict]]:
    """(project, field) -> each distinct finding with its outcome.

    `confirmed` when the field's value moved after the finding first named it (on a
    later night's record, or by now); `dismissed` when a later night's checks ran and
    no longer raised it while the value stood; `open` otherwise.
    """
    nights = sorted({line["night"] for line in lines})
    last_night = nights[-1] if nights else None
    seen: dict[tuple[int, str, str], dict] = {}
    for line in lines:
        key = (int(line["project_id"]), line["field"], line["code"])
        entry = seen.setdefault(
            key,
            {
                "code": line["code"],
                "check": line.get("check"),
                "first": line["night"],
                "last": line["night"],
                "value": line.get("value"),
            },
        )
        entry["last"] = max(entry["last"], line["night"])
    later_values: dict[tuple[int, str], list[tuple[str, Any]]] = defaultdict(list)
    for line in lines:
        later_values[(int(line["project_id"]), line["field"])].append(
            (line["night"], line.get("value"))
        )
    out: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for (pid, f, _code), entry in sorted(seen.items()):
        flagged = entry["value"]
        moved = any(
            night > entry["first"] and value != flagged for night, value in later_values[(pid, f)]
        )
        moved = moved or ((pid, f) in held_now and held_now[(pid, f)] != flagged)
        if moved:
            outcome = "confirmed"
        elif entry["last"] != last_night:
            outcome = "dismissed"
        else:
            outcome = "open"
        out[(pid, f)].append({**entry, "outcome": outcome})
    return out


def _plain(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def rows(
    session: Session,
    project_ids: Iterable[int],
    *,
    db_path: Path | None,
    snapshots: Path | None = None,
) -> list[dict[str, Any]]:
    """The training table, one dict per (campus, field), in project and field order."""
    from tracker import answer_key, facts
    from tracker.models import Project

    ids = sorted({int(p) for p in project_ids})
    statements: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for row in facts.rows_for(session, ids):
        if row["entity"] == "campus":
            statements[(row["project_id"], row["field"])].append(
                {
                    k: row[k]
                    for k in (
                        "value",
                        "number",
                        "unit",
                        "tier",
                        "reason",
                        "held",
                        "as_of",
                        "source_url",
                        "source_type",
                        "quote",
                    )
                }
            )
    past = history(snapshots, set(ids))
    held_now: dict[tuple[int, str], Any] = {}
    projects = {p.id: p for p in session.query(Project).filter(Project.id.in_(ids))} if ids else {}
    for pid, project in projects.items():
        for f in FIELDS:
            held_now[(pid, f)] = _plain(getattr(project, f, None))
    findings = finding_outcomes(labels.read(db_path, labels.FINDINGS), held_now) if db_path else {}
    verdicts: dict[tuple[int, str], list[dict]] = defaultdict(list)
    if db_path:
        for line in labels.read(db_path, labels.VERDICTS):
            verdicts[(int(line["project_id"]), line["field"])].append(
                {k: line.get(k) for k in ("at", "by", "verdict", "old", "new")}
            )
    key = {
        (a.project_id, a.field): a for a in answer_key.answers(session, ("verified", "unsettled"))
    }

    out = []
    for pid in ids:
        project = projects.get(pid)
        if project is None:
            continue
        held_values = answer_key.held_values(project)
        row = {f: _plain(getattr(project, f, None)) for f in IDENTITY}
        row["distinct"] = {
            f: len(
                {
                    (s["value"] or "").strip().lower()
                    for s in statements[(pid, f)]
                    if s["tier"] != "ruled_out"
                }
            )
            for f in IDENTITY
        }
        for f in FIELDS:
            value, precision = held_values[f]
            entry: dict[str, Any] = {
                "project_id": pid,
                "field": f,
                "held": _plain(value),
                "precision": precision,
                "row": row,
                "claims": statements[(pid, f)],
                "history": past.get((pid, f), []),
                "findings": findings.get((pid, f), []),
                "verdicts": verdicts.get((pid, f), []),
                "answer": None,
            }
            answer = key.get((pid, f))
            if answer is not None:
                entry["answer"] = {
                    "status": answer.status,
                    "value": answer.value,
                    "low": answer.low,
                    "high": answer.high,
                    "precision": answer.precision,
                    "verdict": answer_key.judge(answer, value, precision)
                    if answer.status == "verified"
                    else None,
                }
            out.append(entry)
    return out


def render_jsonl(entries: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(e, ensure_ascii=False, sort_keys=True, default=str) + "\n" for e in entries
    )


__all__ = [
    "FIELDS",
    "IDENTITY",
    "finding_outcomes",
    "history",
    "render_jsonl",
    "rows",
    "snapshot_nights",
]
