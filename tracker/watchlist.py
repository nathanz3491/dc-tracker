"""Who the updates page is about: the entities somebody asked to be told about.

One row of `watch` per entity — a company, or one project of one company — and
this module is the only thing that turns those rows into project ids. The console
and `tracker watch` both go through it, for the reason `required.py` gives for
existing at all: two implementations of "does this row match what I typed" drift,
and the whole value of the list is that it means one thing everywhere.

**Matching follows `required.match`, with one deliberate addition.** The company
part is normalized by `dedup.company_key`, so "Microsoft Corporation" and
"Microsoft" are one watch; the project part matches as a substring in either
direction, because the list is typed by hand and the database holds whatever the
first article called the campus.

The addition is the **customer** side. A watch on a company matches projects that
company is *building*, and also projects somebody else is building **for** it —
`project.customer`, and the per-block customers that exist precisely because
attributing a whole campus to one tenant is wrong (see `capex.attribute`). In this
dataset that is not an edge case: the interesting news about a hyperscaler is
routinely filed under the developer's name, and a watchlist that missed it would
be answering a different question from the one that was asked. Every match says
which way it came, so a reader can tell a builder from a tenant.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.dedup import company_key, customer_key
from tracker.models import Project, Watch, utcnow

#: What separates the company from the project in an entry, as in
#: `seed/required-projects.txt`. Same format on purpose: somebody who has typed
#: one list should not have to learn a second syntax.
SEPARATOR = "|"

#: How a project came to be on the watchlist.
VIA_OPERATOR = "operator"
VIA_CUSTOMER = "customer"
VIA_BLOCK = "block_customer"


class WatchError(ValueError):
    """An entry that cannot be stored, with an operator-facing message."""


@dataclass(frozen=True)
class Entity:
    """One watchlist row, and the projects it resolves to."""

    entry: str
    company_key: str
    project_key: str
    note: str | None = None
    added_at: dt.datetime | None = None
    #: project id -> how it matched, in `TRACKS`-style stable order.
    matches: dict[int, str] = field(default_factory=dict)

    @property
    def whole_company(self) -> bool:
        return not self.project_key

    @property
    def project_ids(self) -> tuple[int, ...]:
        return tuple(self.matches)

    def as_json(self) -> dict[str, object]:
        return {
            "entry": self.entry,
            "company_key": self.company_key,
            "project_key": self.project_key,
            "note": self.note,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "project_ids": list(self.project_ids),
            "matched_via": dict(self.matches),
        }


def parse(entry: str) -> tuple[str, str]:
    """``"xAI | Colossus"`` → ``("xai", "colossus")``. Raises `WatchError`.

    A lone token is the *company*: "watch xAI" is what somebody actually types,
    and it is the whole point of the feature. What is refused is an entry with no
    company part at all ("| Colossus") — a project name on its own matches across
    operators, and a list that matches across operators is a search, not a
    watchlist.
    """
    company, _, project = entry.partition(SEPARATOR)
    key = company_key(company.strip())
    if not key:
        raise WatchError(
            f"{entry!r} names no company. Write a company "
            f'("xAI"), or a company and a project ("xAI {SEPARATOR} Colossus").'
        )
    return key, project.strip().lower()


def entries(session: Session) -> list[Watch]:
    """Every watch, oldest first — the order somebody built the list in."""
    return list(session.scalars(select(Watch).order_by(Watch.added_at.asc(), Watch.id.asc())).all())


def add(session: Session, entry: str, *, note: str | None = None) -> tuple[Watch, bool]:
    """Store one entity. Returns the row and whether it was new.

    Idempotent by `(company_key, project_key)` rather than by text, so adding
    "Microsoft" after "Microsoft Corporation" updates the note instead of raising
    on the UNIQUE constraint. The text as typed is *not* overwritten: it is what
    the person who set the watch wrote, and rewriting their words to match a
    later spelling of the same key gains nothing.
    """
    company, project = parse(entry)
    found = session.scalar(
        select(Watch).where(Watch.company_key == company, Watch.project_key == project)
    )
    if found is not None:
        if note is not None:
            found.note = note
        session.flush()
        return found, False

    row = Watch(
        entry=entry.strip(),
        company_key=company,
        project_key=project,
        note=note,
        added_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row, True


def remove(session: Session, entry: str) -> bool:
    """Drop one entity. Returns False if it was not being watched."""
    company, project = parse(entry)
    found = session.scalar(
        select(Watch).where(Watch.company_key == company, Watch.project_key == project)
    )
    if found is None:
        return False
    session.delete(found)
    session.flush()
    return True


def _project_keys(project: Project) -> list[tuple[str, str]]:
    """Every company key this project answers to, and how.

    Operator first, so a project whose builder and tenant are both watched reports
    the builder — the row is filed under the company that is doing the work.
    """
    keys = [(company_key(project.company), VIA_OPERATOR)]
    tenant = customer_key(project.customer)
    if tenant:
        keys.append((tenant, VIA_CUSTOMER))
    for block in getattr(project, "blocks", ()) or ():
        block_tenant = customer_key(getattr(block, "customer", None))
        if block_tenant:
            keys.append((block_tenant, VIA_BLOCK))
    return keys


def _name_matches(project_key: str, name: str) -> bool:
    """Loose in both directions, exactly like `required.match`.

    A false positive costs one glance; a false negative silently drops the project
    somebody explicitly asked to be told about, which is the failure that matters.
    """
    if not project_key:
        return True
    name = (name or "").lower()
    return bool(name) and (project_key in name or name in project_key)


def resolve(watches: list[Watch], projects) -> list[Entity]:
    """Pair every watch with the projects it covers, in list order.

    `projects` is any iterable of rows carrying `company`, `customer`, `name` and
    optionally `blocks` — structural rather than ORM-typed, like `tracks.standing`,
    so this stays testable without a database.
    """
    indexed = [(p, _project_keys(p)) for p in projects]
    out: list[Entity] = []
    for watch in watches:
        matches: dict[int, str] = {}
        for project, keys in indexed:
            if not _name_matches(watch.project_key, project.name):
                continue
            for key, via in keys:
                if key == watch.company_key:
                    # First key wins: `_project_keys` is ordered so that is the
                    # operator, and a project is not reported twice.
                    matches.setdefault(project.id, via)
                    break
        out.append(
            Entity(
                entry=watch.entry,
                company_key=watch.company_key,
                project_key=watch.project_key,
                note=watch.note,
                added_at=watch.added_at,
                matches=matches,
            )
        )
    return out


def watched(session: Session) -> list[Entity]:
    """Every watch, resolved against the whole database.

    Loads projects with the blocks the customer match needs, and nothing else:
    the caller that wants events and risks (`feed.digest`) fetches those itself
    with its own loader options.
    """
    from sqlalchemy.orm import selectinload

    rows = session.scalars(
        select(Project).options(selectinload(Project.blocks)).order_by(Project.id.asc())
    ).all()
    return resolve(entries(session), list(rows))


__all__ = [
    "SEPARATOR",
    "VIA_BLOCK",
    "VIA_CUSTOMER",
    "VIA_OPERATOR",
    "Entity",
    "WatchError",
    "add",
    "entries",
    "parse",
    "remove",
    "resolve",
    "watched",
]
