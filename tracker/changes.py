"""What a run changed: every tracked value that differs between a snapshot taken before
it and the database now, each with the sentence now standing behind it.

**Why this exists.** The quality numbers `tracker clean` reports — the share of values
a quote backs, the rows at each tier — cannot see a wrong value that has a real quote.
On 2026-09-30 they held steady while the night's models replaced a campus's $600M
with a statewide "$20 billion+ in Ohio", kept a land price as a build investment,
stored one building's cost as a whole campus's, and wrote a campus's own operator in
as its customer. Each of those had a verbatim sentence behind it; each was wrong
about *what the sentence was about*, which only a reader catches. This puts every
change in front of one, with the sentence beside it, so reviewing a night takes a few
minutes rather than a forensic audit.

It reads; it never writes. The snapshot is the one `scripts/overnight.sh` takes with
`VACUUM INTO` before round 1, or any other copy of the database.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.db import make_engine, session_scope
from tracker.models import Project
from tracker.vocab import TRACKED_FIELDS

#: Compared between the two copies. `county` is not one of the twelve but is written
#: by the same paths, and a wrong county moves a row between markets.
FIELDS: tuple[str, ...] = (*TRACKED_FIELDS, "county")

#: Columns read from the snapshot, explicitly: an older snapshot may lack a column
#: added since, and selecting the whole model would fail on it.
_COLUMNS = ("id", "dedup_key", "company", "name", "city", "state", "notes", *FIELDS)

#: A claim the merge no longer counts. See `upsert.DECIDED_REASONS`.
_DECIDED = {"superseded", "misread"}


@dataclass(frozen=True)
class ValueChange:
    project_id: int
    label: str
    field: str
    before: Any
    after: Any
    #: The citation now behind `after`, and its sentence. Empty when nothing stands
    #: behind the new value — itself worth a reader's attention.
    url: str = ""
    quote: str = ""


@dataclass
class Changes:
    values: list[ValueChange] = field(default_factory=list)
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[tuple[int, str]] = field(default_factory=list)
    #: Decision lines the run wrote into rows' notes: who resolved what, and why.
    decisions: list[tuple[int, str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.values or self.added or self.removed or self.decisions)


def _norm(value: Any) -> Any:
    """One spelling per value, so a date and its ISO string, or 2.0 and 2, compare equal."""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return value.strip()
    return value


def _label(row: Any) -> str:
    where = row.city or getattr(row, "county", None) or ""
    return f"{row.company} — {row.name}" + (f" ({where}, {row.state})" if where else "")


def _load(blob: str | None) -> dict:
    try:
        value = json.loads(blob or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def backing(project: Project, field_name: str) -> tuple[str, str]:
    """`(url, quote)` of a citation whose standing claim is the row's current value.

    A quoted claim is preferred over an unquoted one. `("", "")` when no standing claim
    states the value — a value nothing currently supports.
    """
    value = _norm(getattr(project, field_name))
    best = ("", "")
    for source in project.sources or ():
        claims = _load(source.claims)
        if field_name not in claims or _norm(claims[field_name]) != value:
            continue
        if _load(source.unconfirmed_reasons).get(field_name) in _DECIDED:
            continue
        quote = _load(source.quotes).get(field_name) or ""
        if quote:
            return source.url, quote
        best = best if best[0] else (source.url, "")
    return best


def _snapshot_rows(path: Path) -> dict[int, Any]:
    engine = make_engine(path, readonly=True)
    try:
        with session_scope(engine, commit=False) as s:
            columns = [getattr(Project, name) for name in _COLUMNS]
            return {row.id: row for row in s.execute(select(*columns)).all()}
    finally:
        engine.dispose()


def diff(snapshot: Path, session: Session) -> Changes:
    """Everything that differs between `snapshot` and the database `session` reads."""
    before = _snapshot_rows(snapshot)
    out = Changes()
    now = {p.id: p for p in session.scalars(select(Project))}
    for pid, project in sorted(now.items()):
        old = before.get(pid)
        # A merge deletes a row, and before migration 0032 SQLite handed its id to the
        # next insert; backups from then hold such ids. So an id alone does not say
        # "same row": the identity key has to match too.
        if old is None or old.dedup_key != project.dedup_key:
            out.added.append((pid, _label(project)))
            if old is not None:
                out.removed.append((pid, _label(old)))
            continue
        for name in FIELDS:
            was, is_ = _norm(getattr(old, name)), _norm(getattr(project, name))
            if was != is_:
                url, quote = backing(project, name) if is_ is not None else ("", "")
                out.values.append(ValueChange(pid, _label(project), name, was, is_, url, quote))
        old_lines = set((old.notes or "").splitlines())
        for line in (project.notes or "").splitlines():
            if line.strip() and line not in old_lines and " resolved `" in line:
                out.decisions.append((pid, _label(project), line.strip()))
    for pid in sorted(before.keys() - now.keys()):
        out.removed.append((pid, _label(before[pid])))
    return out


def _fmt(value: Any) -> str:
    if value is None:
        return "empty"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


def render(changes: Changes, *, since: str = "") -> list[str]:
    """The report's lines, plain text, for a log a person reads in the morning."""
    head = f"what changed{f' since {since}' if since else ''}"
    if not changes:
        return [f"{head}: nothing"]
    lines = [
        f"{head}: {len(changes.values)} value(s) on "
        f"{len({c.project_id for c in changes.values})} row(s), "
        f"{len(changes.added)} row(s) created, {len(changes.removed)} removed, "
        f"{len(changes.decisions)} decision(s) noted"
    ]
    for c in changes.values:
        lines.append(
            f"  #{c.project_id} {c.label[:60]}  {c.field}: {_fmt(c.before)} -> {_fmt(c.after)}"
        )
        if c.after is None:
            continue
        if c.quote:
            lines.append(f'      behind it: "{c.quote[:220]}" — {c.url[:90]}')
        elif c.url:
            lines.append(f"      behind it: no quote — {c.url[:90]}")
        else:
            lines.append("      behind it: NOTHING — no standing claim states this value")
    for pid, label in changes.added:
        lines.append(f"  created  #{pid} {label[:80]}")
    for pid, label in changes.removed:
        lines.append(f"  removed  #{pid} {label[:80]}")
    for pid, label, line in changes.decisions:
        lines.append(f"  decided  #{pid} {label[:40]}: {line[:240]}")
    return lines


__all__ = ["FIELDS", "Changes", "ValueChange", "backing", "diff", "render"]
