"""Rows that are not data centers: taken out, and kept out.

**Where they come from.** A reading sometimes names a facility of another kind beside
the campuses it is about, and the model files it like any other project. Industrial
Info's round-up of "products for data centers" gave the database a Ford battery
plant, a Corning fiber-optic cable plant and a Siemens switchgear plant; SpaceX's
chip fab in Grimes County and Crusoe's factory for modular data centers arrived the
same way. Five rows until 2026-10-20: about $19.5 billion of the investment total and
no megawatts.

**Why removing one needs a memory.** The article is still there. The next re-read of
it, or the next story about the same plant, computes the same identity and inserts
the row again — the way a merge would come undone without `project_alias`. So
`exclude` records every key the row could come back under (the key it was born with,
the key it would be born with today, each alias that pointed at it, and its county's
key when it also had a town) in `excluded_key`, and `upsert_record` creates no row
under any of them. A row that exists is never touched by this; only an insert is
refused.

**Why delete rather than hide.** A hidden row has to be skipped by every reader of
`project` — the console, the public API, `capex`, the exports, each nightly phase
that pays per row — and the first reader that forgot would publish it. Deleted, it is
gone from all of them at once, with its citations, milestones, obstacles, aliases and
watches: every child of `project` is ON DELETE CASCADE.

**And the kind, not only these five.** `not_a_data_center` reads a new row's name for
a word that names another kind of facility — a fab, a foundry, a factory,
manufacturing, a plant — with no word beside it that names a data center. So "Nebius
AI Factory", a "data campus" at a generating station, and "Imperial Valley Computer
Manufacturing" (a 330 MW data center, whatever its company is called) are created as
before. It reads names only, and only for rows not yet created.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker import keys as keys_mod
from tracker.dedup import dedup_key
from tracker.models import ExcludedKey, Project, ProjectAlias

log = logging.getLogger(__name__)

#: What `tracker exclude` records when it is given no other reason.
NOT_A_DATA_CENTER: Final = "not a data center"

#: A word that names a facility of another kind. Whole words, except that a word
#: ending in "fab" is a fab ("Terafab", "Megafab") unless it is "prefab", which is
#: how modular data halls are sold.
#:
#: The generation words were added 2026-10-21: Entergy's two power stations and a
#: solar farm built to supply AWS in Mississippi were stored as three campuses with
#: Amazon as the buyer, 1,588 MW on Amazon's page. Checked against every stored
#: name, they matched those three and nothing else.
_OTHER_KIND = re.compile(
    r"\b(?:(?!prefabs?\b)\w*fabs?|foundry|foundries|(?:giga)?factory|(?:giga)?factories"
    r"|manufacturing|plants?|power\s+stations?|generating\s+stations?|substations?"
    r"|solar|wind\s+farms?|(?:battery|energy)\s+storage|reactors?)\b",
    re.IGNORECASE,
)

#: A word that says the facility is a data center after all. "AI factory" is the
#: industry's name for a GPU campus; a data campus can sit inside a generating plant.
_DATA_CENTER = re.compile(
    r"\b(?:data|datacenters?|campus(?:es)?|compute|computing|computer|cloud|hyperscale"
    r"|colocation|colo|servers?|hpc|gpus?)\b|\bai\s+factor(?:y|ies)\b",
    re.IGNORECASE,
)


def not_a_data_center(name: str | None) -> str | None:
    """The word that makes `name` another kind of facility, or None when it is not.

    None whenever a data-center word stands beside it, so this can only ever keep a
    row out on its name, never on a word it happens to contain.
    """
    if not name:
        return None
    other = _OTHER_KIND.search(name)
    if other is None or _DATA_CENTER.search(name):
        return None
    return other.group(0).lower()


def excluded(session: Session, key: str) -> ExcludedKey | None:
    """The exclusion `key` falls under, read under today's company rules.

    Both sides are renormalised (`keys.renormalise`), so a key recorded before the
    company rules learned a spelling still answers the key an article computes now.
    """
    wanted = keys_mod.renormalise(key)
    for row in session.scalars(select(ExcludedKey)):
        if row.dedup_key == key or keys_mod.renormalise(row.dedup_key) == wanted:
            return row
    return None


def barred(session: Session, key: str, name: str | None) -> str | None:
    """Why a NEW row under `key` called `name` may not be created, or None.

    Called by `upsert_record` only when nothing answers the key — no row holds it and
    no alias routes it — so an existing row is never refused by its own name.
    """
    row = excluded(session, key)
    if row is not None:
        return f"{row.reason}: removed as #{row.project_id} {row.company} — {row.name}"
    word = not_a_data_center(name)
    if word is not None:
        return f"{NOT_A_DATA_CENTER}: named a {word}"
    return None


class ExcludeError(ValueError):
    """The exclusion cannot be done. Message is operator-facing."""


@dataclass
class ExcludeResult:
    #: (id, "company — name") for each row removed.
    removed: list[tuple[int, str]] = field(default_factory=list)
    #: Keys written to `excluded_key`, or already there for the same reason.
    keys: list[str] = field(default_factory=list)
    #: (key, the other row it reaches): left unrecorded, because a key another row
    #: holds or is reached by belongs to that row, and refusing it would refuse it.
    left: list[tuple[str, int]] = field(default_factory=list)


def keys_of(session: Session, project: Project) -> list[str]:
    """Every key `project` could come back under, the one it was born with first."""
    found = [project.dedup_key, keys_mod.natural_key(project)]
    if project.city and project.county:
        # A later article that names only the county computes this one.
        found.append(dedup_key(project.company, None, project.county, project.state))
    found += session.scalars(
        select(ProjectAlias.from_dedup_key).where(ProjectAlias.to_project_id == project.id)
    ).all()
    found += [keys_mod.renormalise(key) for key in list(found)]
    return list(dict.fromkeys(key for key in found if key))


def _reached_by_another(session: Session, key: str, leaving: set[int]) -> int | None:
    """The row outside `leaving` that `key` already reaches, if any."""
    held = session.scalar(
        select(Project.id).where(Project.dedup_key == key, Project.id.not_in(leaving))
    )
    if held is not None:
        return held
    return session.scalar(
        select(ProjectAlias.to_project_id).where(
            ProjectAlias.from_dedup_key == key, ProjectAlias.to_project_id.not_in(leaving)
        )
    )


def exclude(
    session: Session,
    project_ids: list[int],
    *,
    reason: str = NOT_A_DATA_CENTER,
    by: str = "operator",
) -> ExcludeResult:
    """Delete `project_ids` and record every key each could come back under.

    Irreversible for the rows themselves: their citations, milestones and obstacles
    go with them. `lift` only lets a later reading create them afresh.
    """
    reason = reason.strip()
    if not reason:
        raise ExcludeError("a reason is required: it is what the listing shows")
    projects: list[Project] = []
    for project_id in dict.fromkeys(project_ids):
        project = session.get(Project, project_id)
        if project is None:
            raise ExcludeError(f"project #{project_id} does not exist")
        projects.append(project)
    if not projects:
        raise ExcludeError("no project ids given")

    leaving = {p.id for p in projects}
    result = ExcludeResult()
    for project in projects:
        for key in keys_of(session, project):
            other = _reached_by_another(session, key, leaving)
            if other is not None:
                result.left.append((key, other))
                continue
            if session.scalar(select(ExcludedKey.id).where(ExcludedKey.dedup_key == key)) is None:
                session.add(
                    ExcludedKey(
                        dedup_key=key,
                        project_id=project.id,
                        company=project.company,
                        name=project.name,
                        reason=reason,
                        decided_by=by,
                    )
                )
            result.keys.append(key)
        result.removed.append((project.id, f"{project.company} — {project.name}"))
        session.delete(project)
        log.info("excluded #%d %s — %s: %s", project.id, project.company, project.name, reason)
    session.flush()
    return result


def lift(session: Session, project_id: int) -> list[str]:
    """Forget the keys taken from the row that was `project_id`. Returns them.

    The row does not come back; a later reading may create it again.
    """
    rows = session.scalars(select(ExcludedKey).where(ExcludedKey.project_id == project_id)).all()
    for row in rows:
        session.delete(row)
    session.flush()
    return [row.dedup_key for row in rows]


def listing(session: Session) -> list[ExcludedKey]:
    """Every excluded key, newest decision first."""
    return list(
        session.scalars(
            select(ExcludedKey).order_by(
                ExcludedKey.decided_at.desc(), ExcludedKey.project_id, ExcludedKey.dedup_key
            )
        )
    )


__all__ = [
    "NOT_A_DATA_CENTER",
    "ExcludeError",
    "ExcludeResult",
    "barred",
    "exclude",
    "excluded",
    "keys_of",
    "lift",
    "listing",
    "not_a_data_center",
]
