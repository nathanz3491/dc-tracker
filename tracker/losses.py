"""Values a run lost with nothing deciding it, found against a snapshot and put back.

**Why this exists.** Over 10-18 to 10-28, 161 stored values went from set to empty.
75 of them a ruling cleared: a model, a rule or a person judged the citation wrong,
and the ruling is written on the claim. The other 81 nobody decided. A second reading
of the same article did not repeat the field, the citation was replaced, and the value
went with it, 42 times with a sentence on file that stated it outright. Each one
dropped its row out of T2 and cost a search to climb back.

`upsert.omitted_quietly` closes the path that did that. This is the net under it,
built so it does not depend on knowing the path: whatever code empties a value, the
question here is only whether the evidence for it was **withdrawn** or **decided
against**. Compared field by field with the snapshot the nightly loop takes before
round 1:

* the citation still claims the field, so a ruling or the merge set it aside: left
  alone, because that is a decision (`changes` lists it for a reader);
* the citation is gone, so a merge folded it or a person removed it: left alone;
* the citation no longer claims it, and the article on disk no longer contains the
  sentence: the article withdrew it, so it stays withdrawn;
* the citation no longer claims it and the article still says it: **put back**, the
  claim exactly as the snapshot held it (value, sentence, envelope, any ruling), and
  the row re-derived from its citations so the merge decides the figure as always;
* the citation no longer claims it and there is no copy of the article to check:
  listed and left alone, since a restore nobody can check is a guess.

Restoring evidence rather than a figure is the point. The figure comes back only if
the same merge every write uses still chooses it, so a ruling made tonight on another
citation still wins, and nothing here can store a value no citation states.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.db import make_engine, session_scope
from tracker.models import Project, Source
from tracker.normalize import url_identity
from tracker.upsert import (
    CLAIM_OWNED_FIELDS,
    SOURCE_NOTE_PREFIX,
    _Citation,
    _json_object,
    recompute_from_sources,
    record_tag,
    reinstate,
)

#: The fields a value can vanish from. `phase` is claim-owned too but falls back to
#: `announced` rather than to empty, so a lost phase is a change `changes` reports,
#: not an empty field.
FIELDS: tuple[str, ...] = tuple(sorted(CLAIM_OWNED_FIELDS - {"phase"}))

RESTORED = "restored"
WITHDRAWN = "withdrawn"
UNCHECKED = "unchecked"
#: Put back and then refused by a relationship every write must keep — a bug in the
#: restore or the row, reported rather than written.
REFUSED = "refused"

_SOURCE_COLUMNS = (
    "project_id",
    "url",
    "claims",
    "fields",
    "quotes",
    "claim_meta",
    "unconfirmed_fields",
    "unconfirmed_reasons",
    "blocks",
    "parties",
    "excerpt",
)


@dataclass
class Loss:
    """One value that went from set to empty with its citation still on the row."""

    project_id: int
    label: str
    field: str
    value: Any
    url: str
    quote: str
    outcome: str
    #: The row's value after the restore re-derived it; None when it stayed empty
    #: (another citation's ruling won) or nothing was restored.
    now: Any = None


@dataclass
class Found:
    losses: list[Loss]
    #: Values emptied while a citation still claims them: a ruling or the merge.
    decided: int = 0
    #: Values whose citation left the row.
    removed: int = 0


def _snapshot(path: Path) -> tuple[dict[int, Any], dict[int, list[Any]]]:
    """The snapshot's rows and citations, read-only and by explicit column."""
    engine = make_engine(path, readonly=True)
    try:
        with session_scope(engine, commit=False) as s:
            rows = {
                r.id: r
                for r in s.execute(
                    select(Project.id, Project.name, Project.company, *map(_col, FIELDS))
                ).all()
            }
            cited: dict[int, list[Any]] = {}
            columns = [getattr(Source, c) for c in _SOURCE_COLUMNS]
            for r in s.execute(select(*columns)).all():
                cited.setdefault(r.project_id, []).append(r)
            return rows, cited
    finally:
        engine.dispose()


def _col(name: str) -> Any:
    return getattr(Project, name)


def find(
    snapshot: Path, session: Session, *, article: Callable[[str], str | None]
) -> tuple[Found, list[tuple[Loss, Source, _Citation]]]:
    """Every value emptied since `snapshot` whose citation stopped claiming it.

    `article(url)` is the stored text of a page, or None. Returns the findings and,
    for each loss that can be put back, what to put back where.
    """
    from tracker.ingest.crawl import still_states

    before, cited = _snapshot(snapshot)
    found = Found(losses=[])
    todo: list[tuple[Loss, Source, _Citation]] = []
    for project in session.scalars(select(Project).where(Project.id.in_(list(before)))):
        then = before[project.id]
        live = {url_identity(s.url): s for s in project.sources}
        for name in FIELDS:
            if getattr(then, name) is None or getattr(project, name) is not None:
                continue
            outcome: tuple[Loss, Source, _Citation] | None = None
            decided = removed = False
            for old in cited.get(project.id, []):
                earlier = _Citation.of_row(old)
                quote = earlier.quotes.get(name)
                if earlier.claims.get(name) is None or name not in earlier.quoted or not quote:
                    continue
                row = live.get(url_identity(old.url))
                if row is None:
                    removed = True
                    continue
                if name in _json_object(row.claims):
                    decided = True
                    continue
                text = article(row.url) or (article(old.url) if old.url != row.url else None)
                if text is None:
                    state = UNCHECKED
                elif still_states(text)(quote):
                    state = RESTORED
                else:
                    state = WITHDRAWN
                loss = Loss(
                    project.id,
                    f"{project.company} — {project.name}",
                    name,
                    earlier.claims[name],
                    row.url,
                    quote,
                    state,
                )
                # One finding per value: the first citation that can put it back,
                # else the first that explains it.
                if state == RESTORED:
                    outcome = (loss, row, earlier)
                    break
                if outcome is None:
                    outcome = (loss, row, earlier)
            if outcome is not None:
                found.losses.append(outcome[0])
                if outcome[0].outcome == RESTORED:
                    todo.append(outcome)
            elif decided:
                found.decided += 1
            elif removed:
                found.removed += 1
    return found, todo


def restore(session: Session, todo: list[tuple[Loss, Source, _Citation]]) -> None:
    """Put each claim back and re-derive its row, one row per savepoint.

    A row the re-derivation would break (`invariants.HeldBroken`) is rolled back and
    its losses marked `refused`: the same rule every write keeps.
    """
    from tracker import invariants

    by_row: dict[int, list[tuple[Loss, Source, _Citation]]] = {}
    for item in todo:
        by_row.setdefault(item[0].project_id, []).append(item)
    for project_id, items in by_row.items():
        project = session.get(Project, project_id)
        try:
            with session.begin_nested():
                lines = []
                for loss, row, earlier in items:
                    reinstate(row, earlier, [loss.field])
                    lines.append(
                        f"{SOURCE_NOTE_PREFIX}[{record_tag([row.url])}] restored {loss.field} = "
                        f"{loss.value!r}: a later reading dropped it with no ruling, and the "
                        "sentence that states it is still in the article"
                    )
                project.notes = "\n".join([*(project.notes or "").splitlines(), *lines])
                recompute_from_sources(session, project)
                invariants.enforce(project)
        except invariants.HeldBroken:
            for loss, _row, _earlier in items:
                loss.outcome = REFUSED
            session.expire(project)
            continue
        for loss, _row, _earlier in items:
            loss.now = getattr(project, loss.field)


def render(found: Found, *, restored: bool) -> list[str]:
    """The report, in the loop log's indentation."""
    counts = {
        state: sum(1 for loss in found.losses if loss.outcome == state)
        for state in (RESTORED, WITHDRAWN, UNCHECKED, REFUSED)
    }
    put = "put back" if restored else "would be put back"
    out = [
        f"    {counts[RESTORED]} value(s) lost with no ruling and {put}, "
        f"{counts[WITHDRAWN]} withdrawn by the article, {counts[UNCHECKED]} with no copy "
        f"of the article to check"
        + (f", {counts[REFUSED]} refused by a held relationship" if counts[REFUSED] else ""),
        f"    {found.decided} cleared by a ruling or the merge, {found.removed} whose citation "
        "left the row — decisions, listed under what the night changed",
    ]
    for loss in found.losses:
        out.append(
            f"  #{loss.project_id} {loss.label}: {loss.field} {loss.value!r} — "
            f"{_outcome(loss, put=put, restored=restored)}"
        )
        out.append(f"      {loss.url}")
        out.append(f"      “{loss.quote[:200]}”")
    return out


def _outcome(loss: Loss, *, put: str, restored: bool) -> str:
    if loss.outcome == WITHDRAWN:
        return "the article no longer says it"
    if loss.outcome == UNCHECKED:
        return "no copy of the article to check"
    if loss.outcome == REFUSED:
        return "put back and refused: it would break a held relationship"
    if not restored:
        return put
    if loss.now is None:
        return f"{put}; the row stays empty, another citation's ruling decides it"
    return f"{put}; the row now holds {loss.now!r}"
