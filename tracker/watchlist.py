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

**A row can also name one project exactly** (migration 0033): the button on a
Projects row or a project's own page. Typed text stays loose because the database
holds whatever the first article called the campus; a button is pressed *on* the
row, so it means that row and no other — a loose "Colossus" also matches
"Colossus 2", and a star that lights three rows is broken. Such a row carries the
project's id, and a merge moves it to the surviving project (`repoint`).

**Every row has an owner, and the two callers want opposite scopes.** A console
request is always for one account — anything else would show one reader another's
interests — so `account_id` is required to write and named to read. The CLI is the
other way round: `account_id=None` reads *every* account's entries, because a
terminal on the host is reading the database rather than one person's slice of it,
and it prints an owner column so the rows stay distinguishable.
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
#: Watched by id, from the project's own button. Not a builder or a tenant match.
VIA_PROJECT = "project"


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
    #: Whose entry this is. Set on every row read from a database; None only for a
    #: `resolve` call assembled by hand, as the tests do. The CLI prints it,
    #: because the terminal reads the whole database and a list of entries with no
    #: owner column would be several people's interests run together.
    owner: str | None = None
    #: Set when this row watches exactly one project. See the module docstring.
    project_id: int | None = None

    @property
    def whole_company(self) -> bool:
        return not self.project_key and self.project_id is None

    @property
    def project_ids(self) -> tuple[int, ...]:
        return tuple(self.matches)

    def as_json(self) -> dict[str, object]:
        """What the console renders.

        `owner` is deliberately absent. A page only ever asks for one account's
        list — its own — so sending the address back would be telling the browser
        something it already knows, and a payload that *can* carry another
        account's address is one that eventually does.
        """
        return {
            "entry": self.entry,
            "company_key": self.company_key,
            "project_key": self.project_key,
            "note": self.note,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "project_ids": list(self.project_ids),
            "matched_via": dict(self.matches),
            "project_id": self.project_id,
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
    if project.strip().startswith("#"):
        # The key space of exact watches (`_exact_key`). Typed, it would match
        # nothing and could block the button on that project from ever working.
        raise WatchError(
            f"{entry!r}: a project name cannot start with '#'. Use the watch button "
            "on the project to follow it by id."
        )
    key = company_key(company.strip())
    if not key:
        raise WatchError(
            f"{entry!r} names no company. Write a company "
            f'("xAI"), or a company and a project ("xAI {SEPARATOR} Colossus").'
        )
    return key, project.strip().lower()


def entries(session: Session, *, account_id: int | None = None) -> list[Watch]:
    """Watches, oldest first — the order somebody built the list in.

    `account_id=None` means **every account**, which is what the CLI wants: the
    terminal reads the database rather than one person's slice of it. A console
    request always names an account, because a page that could ask for everybody's
    list is a page that leaks one reader's interests to another.
    """
    query = select(Watch).order_by(Watch.added_at.asc(), Watch.id.asc())
    if account_id is not None:
        query = query.where(Watch.account_id == account_id)
    return list(session.scalars(query).all())


def add(
    session: Session, entry: str, *, account_id: int, note: str | None = None
) -> tuple[Watch, bool]:
    """Store one entity. Returns the row and whether it was new.

    Idempotent by `(account_id, company_key, project_key)` rather than by text, so
    adding "Microsoft" after "Microsoft Corporation" updates the note instead of
    raising on the UNIQUE constraint. The text as typed is *not* overwritten: it is
    what the person who set the watch wrote, and rewriting their words to match a
    later spelling of the same key gains nothing.

    `account_id` is required rather than defaulted, and that is the point of the
    keyword: there is no shared list to fall back to, so a caller that has not
    worked out whose list this is has to say so at the call site rather than
    silently write to somebody's.
    """
    company, project = parse(entry)
    found = _find(session, account_id, company, project)
    if found is not None:
        if note is not None:
            found.note = note
        session.flush()
        return found, False

    row = Watch(
        account_id=account_id,
        entry=entry.strip(),
        company_key=company,
        project_key=project,
        note=note,
        added_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row, True


def add_project(
    session: Session, project_id: int, *, account_id: int, note: str | None = None
) -> tuple[Watch, bool]:
    """Watch exactly one project, by id. Returns the row and whether it was new.

    The entry is written as "Company | Name" so the list reads the same as a typed
    one; what it matches is the id alone.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise WatchError(f"no project #{project_id}")
    found = _find_project(session, account_id, project_id)
    if found is not None:
        if note is not None:
            found.note = note
        session.flush()
        return found, False
    company = company_key(project.company) or company_key(project.name)
    if not company:
        raise WatchError(f"project #{project_id} has no company to file the watch under")
    row = Watch(
        account_id=account_id,
        entry=f"{project.company} {SEPARATOR} {project.name}",
        company_key=company,
        project_key=_exact_key(project_id),
        project_id=project_id,
        note=note,
        added_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row, True


def remove_project(session: Session, project_id: int, *, account_id: int) -> bool:
    """Stop watching one project by id. False if it was not watched that way.

    Only the exact row: a company-wide watch that also covers this project is the
    company's, and is removed from the list, not from a row.
    """
    found = _find_project(session, account_id, project_id)
    if found is None:
        return False
    session.delete(found)
    session.flush()
    return True


def repoint(session: Session, old_id: int, new_id: int) -> int:
    """Move exact watches from a project being merged away onto the survivor.

    Called by `merge.merge_projects` before it deletes the folded row, which would
    otherwise take every exact watch on it down with the cascade. An account that
    already watches the survivor keeps that row and loses the duplicate. Returns
    how many rows were moved or folded.
    """
    survivor = session.get(Project, new_id)
    if survivor is None:
        return 0
    rows = session.scalars(select(Watch).where(Watch.project_id == old_id)).all()
    for row in rows:
        if _find_project(session, row.account_id, new_id) is not None:
            session.delete(row)
            continue
        row.project_id = new_id
        row.project_key = _exact_key(new_id)
        row.company_key = company_key(survivor.company) or row.company_key
        row.entry = f"{survivor.company} {SEPARATOR} {survivor.name}"
    session.flush()
    return len(rows)


def _exact_key(project_id: int) -> str:
    """`project_key` for an exact watch. "#" never starts a lowercased name."""
    return f"#{project_id}"


def _find_project(session: Session, account_id: int, project_id: int) -> Watch | None:
    return session.scalar(
        select(Watch).where(Watch.account_id == account_id, Watch.project_id == project_id)
    )


def remove(session: Session, entry: str, *, account_id: int) -> bool:
    """Drop one entity from one account's list. False if it was not being watched.

    Scoped, so one reader cannot drop another's entry — which on a shared list was
    not even expressible, and is the second reason 0021 gave for the owner column.
    """
    company, project = parse(entry)
    found = _find(session, account_id, company, project)
    if found is None:
        # An exact watch reads "Company | Name" in the list, so the same words
        # typed at the CLI should drop it too.
        found = session.scalar(
            select(Watch).where(
                Watch.account_id == account_id,
                Watch.project_id.is_not(None),
                Watch.entry == entry.strip(),
            )
        )
    if found is None:
        return False
    session.delete(found)
    session.flush()
    return True


def _find(session: Session, account_id: int, company: str, project: str) -> Watch | None:
    """One account's row for one normalized entity, matching `uq_watch_entity`."""
    return session.scalar(
        select(Watch).where(
            Watch.account_id == account_id,
            Watch.company_key == company,
            Watch.project_key == project,
        )
    )


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


def resolve(
    watches: list[Watch], projects, *, owners: dict[int, str] | None = None
) -> list[Entity]:
    """Pair every watch with the projects it covers, in list order.

    `projects` is any iterable of rows carrying `company`, `customer`, `name` and
    optionally `blocks` — structural rather than ORM-typed, like `tracks.standing`,
    so this stays testable without a database.

    `owners` maps account id to email, for the CLI's owner column. Passed in
    rather than read off `watch.account` so this function still needs no database
    and no relationship loading, which is what keeps it testable with plain
    objects.
    """
    indexed = [(p, _project_keys(p)) for p in projects]
    out: list[Entity] = []
    for watch in watches:
        matches: dict[int, str] = {}
        exact = getattr(watch, "project_id", None)
        for project, keys in indexed:
            if exact is not None:
                if project.id == exact:
                    matches[project.id] = VIA_PROJECT
                continue
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
                owner=(owners or {}).get(getattr(watch, "account_id", None)),
                project_id=exact,
            )
        )
    return out


def watched(session: Session, *, account_id: int | None = None) -> list[Entity]:
    """Watches, resolved against the whole database.

    `account_id=None` is every account's entries — the CLI's view. A console
    request always names one; see `entries`.

    Loads projects with the blocks the customer match needs, and nothing else:
    the caller that wants events and risks (`feed.digest`) fetches those itself
    with its own loader options.
    """
    from sqlalchemy.orm import selectinload

    from tracker.models import Account

    rows = session.scalars(
        select(Project).options(selectinload(Project.blocks)).order_by(Project.id.asc())
    ).all()
    # Only when it can be needed. One account's own list has one owner and the
    # page does not show it, so the whole-database path is the only one that pays.
    owners = (
        None
        if account_id is not None
        else dict(session.execute(select(Account.id, Account.email)).all())
    )
    return resolve(entries(session, account_id=account_id), list(rows), owners=owners)


__all__ = [
    "SEPARATOR",
    "VIA_BLOCK",
    "VIA_CUSTOMER",
    "VIA_OPERATOR",
    "VIA_PROJECT",
    "Entity",
    "WatchError",
    "add",
    "add_project",
    "entries",
    "parse",
    "remove",
    "remove_project",
    "repoint",
    "resolve",
    "watched",
]
