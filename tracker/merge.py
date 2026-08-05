"""Folding several rows that turned out to be one campus into a single project.

**Why this cannot be automatic.** `dedup.py` refuses to merge on a guess, and it
is right to: a wrong merge destroys two projects and leaves no trace, while a
wrong split is visible and recoverable. So detection proposes and a human
disposes. This module is the disposing.

**Why it is needed at all.** One campus routinely has three companies attached —
one builds it, one owns the land, one occupies it — and each name a source
chooses produces its own `dedup_key`. Measured on the live database: 30 pairs of
rows describing 24,125 MW of the same capacity twice, of which 29 involve
genuinely *different* companies and so cannot be fixed by any alias table. The
Abilene Stargate campus alone exists four times, as Crusoe, Oracle, OpenAI and
"OpenAI/Oracle".

That is a nuisance in a site listing and a wrong number the moment anything
groups by end customer, which is exactly what `tracker capex` does.

**What a merge does, and does not do.** Citations, milestones and obstacles move
onto the surviving row; the duplicates are deleted; and every field is then
recomputed from the combined set of claims by `upsert.recompute_from_sources`.
Nothing is hand-copied, so the survivor's values are what the citations support
rather than whichever row happened to be kept. The merge is recorded in `notes`
with the ids that were folded in, because a reader who finds one row where a
citation implies two deserves to know why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import Event, Project, ProjectAlias, Risk, utcnow
from tracker.upsert import recompute_from_sources

log = logging.getLogger(__name__)


class MergeError(ValueError):
    """The requested merge cannot be performed. Message is operator-facing."""


@dataclass
class MergeResult:
    kept: int
    removed: list[int] = field(default_factory=list)
    sources_moved: int = 0
    sources_discarded: int = 0
    events_moved: int = 0
    events_discarded: int = 0
    risks_moved: int = 0
    risks_discarded: int = 0
    aliases_recorded: int = 0
    conflicts: list[str] = field(default_factory=list)

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("projects removed", len(self.removed)),
            ("citations moved", self.sources_moved),
            ("citations already held", self.sources_discarded),
            ("milestones moved", self.events_moved),
            ("milestones already held", self.events_discarded),
            ("obstacles moved", self.risks_moved),
            ("obstacles already held", self.risks_discarded),
            ("identities remembered", self.aliases_recorded),
        ]


def merge_projects(session: Session, keep_id: int, dupe_ids: list[int]) -> MergeResult:
    """Fold `dupe_ids` into `keep_id`. Returns what moved.

    Ordering matters and is not arbitrary: sources move first so that events and
    obstacles can be repointed at the citation they came from, and the field
    recompute runs last so it sees the complete set.
    """
    keep = session.get(Project, keep_id)
    if keep is None:
        raise MergeError(f"project #{keep_id} does not exist")
    targets = [i for i in dict.fromkeys(dupe_ids) if i != keep_id]
    if not targets:
        raise MergeError("nothing to merge: no duplicate ids given, or only the kept id")

    result = MergeResult(kept=keep_id)
    #: url -> the surviving source, so a citation held by both rows is not moved
    #: into a UNIQUE violation.
    kept_urls = {s.url: s for s in keep.sources}

    for dupe_id in targets:
        dupe = session.get(Project, dupe_id)
        if dupe is None:
            raise MergeError(f"project #{dupe_id} does not exist")

        # --- Citations -------------------------------------------------------
        # A source already on the survivor is the same citation, not a second
        # one. Its claims are already represented, so the duplicate row is
        # dropped — but anything pointing at it is repointed first, or the FK
        # would null out a milestone's provenance.
        #
        # Reassigned through the RELATIONSHIP, not by setting `project_id`.
        # `Project.sources` cascades "all, delete-orphan", so a row whose foreign
        # key was changed by hand is still sitting in `dupe.sources` when the
        # duplicate is deleted, and the cascade takes the citation with it. The
        # first live run lost sources exactly that way and then failed on a
        # foreign key when a milestone pointed at one of them.
        for source in list(dupe.sources):
            twin = kept_urls.get(source.url)
            if twin is not None:
                _repoint(session, source.id, twin.id)
                dupe.sources.remove(source)
                session.delete(source)
                result.sources_discarded += 1
                continue
            source.project = keep
            kept_urls[source.url] = source
            result.sources_moved += 1
        session.flush()

        # --- Milestones and obstacles ----------------------------------------
        moved, dropped = _move_events(session, keep, dupe)
        result.events_moved += moved
        result.events_discarded += dropped

        moved, dropped = _move_risks(session, keep, dupe)
        result.risks_moved += moved
        result.risks_discarded += dropped

        # --- The identity itself ----------------------------------------------
        # Without this the merge lasts until the next crawl: `upsert_record`
        # matches on exact dedup_key, so an article written from the folded
        # company's angle would re-create the row. Aliases already aimed at the
        # folded row are repointed first, which is what keeps chains flat — a
        # lookup resolves in one step however many merges preceded it.
        for alias in session.scalars(
            select(ProjectAlias).where(ProjectAlias.to_project_id == dupe.id)
        ).all():
            alias.to_project_id = keep.id
        result.aliases_recorded += _record_alias(session, dupe.dedup_key, keep)

        session.delete(dupe)
        result.removed.append(dupe_id)
        session.flush()

    session.refresh(keep)
    result.conflicts = recompute_from_sources(session, keep)
    keep.notes = _record_merge(keep.notes, result.removed)
    keep.updated_at = utcnow()
    session.flush()
    log.info("merged %s into #%d", ", ".join(f"#{i}" for i in result.removed), keep_id)
    return result


def _repoint(session: Session, old_source_id: int, new_source_id: int) -> None:
    """Move every reference to a source that is about to be deleted."""
    for model in (Event, Risk):
        for row in session.scalars(select(model).where(model.source_id == old_source_id)).all():
            row.source_id = new_source_id


def _record_alias(session: Session, from_key: str | None, keep: Project) -> int:
    """Remember that a folded identity belongs to the survivor. Returns rows written.

    An existing alias for the key is retargeted rather than duplicated — the
    latest human decision wins. A key equal to the survivor's own is not
    recorded: the exact-key lookup already answers it.
    """
    if not from_key or from_key == keep.dedup_key:
        return 0
    existing = session.scalar(select(ProjectAlias).where(ProjectAlias.from_dedup_key == from_key))
    if existing is not None:
        existing.to_project_id = keep.id
        return 1
    session.add(ProjectAlias(from_dedup_key=from_key, to_project_id=keep.id))
    return 1


def _move_events(session: Session, keep: Project, dupe: Project) -> tuple[int, int]:
    """Move milestones, collapsing any the survivor already has on that date.

    The (project, type, date) UNIQUE is the same accepted cost migration 0002
    documents: two milestones of one kind on one day become one.
    """
    existing = {
        (e.event_type, e.event_date)
        for e in session.scalars(select(Event).where(Event.project_id == keep.id)).all()
    }
    moved = dropped = 0
    # Through the relationship, for the same cascade reason as the sources above.
    for event in list(dupe.events):
        key = (event.event_type, event.event_date)
        if key in existing:
            dupe.events.remove(event)
            session.delete(event)
            dropped += 1
            continue
        event.project = keep
        existing.add(key)
        moved += 1
    session.flush()
    return moved, dropped


def _move_risks(session: Session, keep: Project, dupe: Project) -> tuple[int, int]:
    """Move obstacles, collapsing on (category, first_seen) per the schema."""
    existing = {
        (r.category, r.first_seen)
        for r in session.scalars(select(Risk).where(Risk.project_id == keep.id)).all()
    }
    moved = dropped = 0
    for risk in list(dupe.risks):
        key = (risk.category, risk.first_seen)
        if key in existing:
            dupe.risks.remove(risk)
            session.delete(risk)
            dropped += 1
            continue
        risk.project = keep
        existing.add(key)
        moved += 1
    session.flush()
    return moved, dropped


def _record_merge(notes: str | None, removed: list[int]) -> str:
    """Append a durable line naming the rows that were folded in.

    A `[tracker]` line would be wiped by the next upsert, which regenerates that
    class wholesale. A merge is a one-off operator decision and has to outlive
    every later ingest, so it is written as plain operator prose — the one kind
    of note `_merge_notes` never touches.
    """
    line = (
        f"merged project(s) {', '.join(f'#{i}' for i in removed)} into this row: "
        "the same campus had been stored once per company attached to it."
    )
    lines = [current for current in (notes or "").splitlines() if current.strip()]
    if line not in lines:
        lines.append(line)
    return "\n".join(lines)


__all__ = ["MergeError", "MergeResult", "merge_projects"]
