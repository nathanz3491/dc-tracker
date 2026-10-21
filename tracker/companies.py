"""Companies as entities: one row per company, its spellings, its kind and its parent.

Step 4.3 of the client plan's master data model (2026-10-21). A company used to be
a string — each row stored the spelling its first source used — folded to one key
by `dedup.company_key` with an alias list in the code and the operator roster. That
made "META" and "Meta Platforms, Inc." one company wherever two keys were compared,
and left nowhere to say what a company *is*.

**The key stays the identity.** Twenty-odd places store or compare it — a campus's
`dedup_key`, a party's `party_key`, a watch, a Capex position, a page's slug — and
`keys.sync` already re-keys stored rows when the rules for it change. So `company` is
keyed by it and adds what a string could not hold: the one name printed everywhere
and who chose it, the kind of company, and its parent. `project.company_id` and
`project_party.company_id` point each operator and party at its row; they are
derived like the tranches, recomputed whenever a row is written (`attach`).

**Spellings.** Every alias the code and the roster give is written into
`company_alias` by `sync` ('code' and 'roster' rows), so the registry lists what
folds where. A person can fold a spelling the code does not know (`add_alias`,
`tracker company alias`): that row is the one this table is the source of, and
`dedup.company_key` reads it (`load_aliases`, on every database connection and
whenever the console sees the database change). Adding one re-keys what it touches
through the same machinery a deploy uses: `keys.sync`, the party rebuild, and the
watches filed under the old key.

**Names.** The roster's name when the company is on it, else the spelling most
campuses use (`company.display_name`), re-chosen by every `sync`; a name a person set
stands until they change it.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker import dedup
from tracker.models import (
    CapacityBlock,
    Company,
    CompanyAlias,
    Project,
    ProjectParty,
    utcnow,
)

log = logging.getLogger(__name__)

#: `decided_by` on aliases copied from the code, re-seeded by every `sync`.
CODE: Final = "code"
ROSTER: Final = "roster"
SEEDED: Final = frozenset({CODE, ROSTER})

#: `name_by` for a name chosen by the rules rather than a person.
BY_RULE: Final = frozenset({"rows", ROSTER})


class CompanyError(ValueError):
    """A change to the registry that cannot be made, in words a person can act on."""


# --- the person's aliases, read into `company_key` ------------------------------


_PERSON_ALIASES_SQL: Final = (
    "SELECT a.spelling, c.key FROM company_alias a JOIN company c ON c.id = a.company_id "
    "WHERE a.decided_by NOT IN ('code', 'roster')"
)


def load_aliases(connection: Any) -> bool:
    """Read a person's aliases from an open database into `company_key`.

    `connection` is a DB-API connection (`sqlite3`), so this runs from a connection
    event and from the console's change probe without a session. A database without
    the table — one migrated before 0043 — has no person's aliases. True if what
    `company_key` reads changed.
    """
    try:
        rows = connection.execute(_PERSON_ALIASES_SQL).fetchall()
    except sqlite3.Error:
        rows = []
    except Exception as exc:
        log.debug("could not read company aliases: %s", exc)
        return False
    return dedup.use_person_aliases(dict(rows))


def reload(session: Session) -> bool:
    """`load_aliases` on the session's own connection, after a write to the table."""
    session.flush()
    return load_aliases(session.connection().connection.driver_connection)


# --- what the code says about companies -------------------------------------------


@dataclass(frozen=True)
class Seed:
    """What the roster and the EDGAR list say about one company."""

    name: str
    kind: str | None
    rostered: bool


def seeds() -> dict[str, Seed]:
    """Company key -> its seed, from the roster first and then the EDGAR list."""
    out: dict[str, Seed] = {}
    try:
        from tracker import roster

        for operator in roster.load():
            key = dedup.company_key(operator.name)
            if key:
                out.setdefault(key, Seed(operator.name, operator.kind, True))
    except Exception as exc:
        log.warning("roster unreadable for the company registry: %s", exc)
    try:
        from tracker.ingest.edgar import load_companies

        companies, _phrases, _forms = load_companies()
        for company in companies:
            key = dedup.company_key(company.name)
            if not key:
                continue
            seen = out.get(key)
            if seen is None:
                out[key] = Seed(company.name, (company.kind or "").lower() or None, False)
            elif seen.kind is None and company.kind:
                out[key] = Seed(seen.name, company.kind.lower(), seen.rostered)
    except Exception as exc:
        log.warning("EDGAR company list unreadable for the company registry: %s", exc)
    return out


def _alias_sources() -> dict[str, tuple[str, str]]:
    """Spelling -> (company key, 'code' | 'roster'), for every alias the code gives."""
    hand = set(dedup._COMPANY_ALIASES)
    return {
        spelling: (key, CODE if spelling in hand else ROSTER)
        for spelling, key in dedup.code_aliases().items()
        if spelling != key
    }


# --- the registry ------------------------------------------------------------------


def _cache(session: Session) -> dict[str, int]:
    return session.info.setdefault("company_ids", {})


def ensure(session: Session, key: str, spelling: str | None = None) -> Company:
    """The company row for `key`, made if it is new.

    A new company is named by the roster when it is on it, else by the spelling it
    arrived under; `sync` re-chooses names nightly from every row.
    """
    cache = _cache(session)
    company_id = cache.get(key)
    if company_id is not None:
        row = session.get(Company, company_id)
        if row is not None:
            return row
    row = session.scalar(select(Company).where(Company.key == key))
    if row is None:
        seed = seeds_cached().get(key)
        if seed is not None:
            name, by, kind = seed.name, ROSTER if seed.rostered else "rows", seed.kind
        else:
            name, by, kind = (spelling or "").strip() or key.title(), "rows", None
        row = Company(key=key, name=name, name_by=by, kind=kind, created_at=utcnow())
        session.add(row)
        session.flush()
    cache[key] = row.id
    return row


_SEEDS: dict[str, Seed] | None = None


def seeds_cached() -> dict[str, Seed]:
    """`seeds()`, read once per process and again when a person's alias changes."""
    global _SEEDS
    if _SEEDS is None:
        _SEEDS = seeds()
    return _SEEDS


def _forget_seeds() -> None:
    global _SEEDS
    _SEEDS = None


dedup.alias_listeners.append(_forget_seeds)


def attach(session: Session, project: Project) -> None:
    """Point a campus's operator and every party at their company rows.

    Called wherever a row's parties are rebuilt (`upsert`), so it follows every
    re-read and every alias. A party or an operator with no key — a hedge, "a
    Fortune 100 company" — points at nothing.
    """
    key = dedup.company_key(project.company)
    project.company_id = ensure(session, key, project.company).id if key else None
    for party in getattr(project, "parties", ()) or ():
        party.company_id = (
            ensure(session, party.party_key, party.name).id if party.party_key else None
        )


def spelling_counts(session: Session) -> dict[str, Counter[str]]:
    """Company key -> how many campuses write it each way.

    The counting `company.display_names` has always done: a campus's operator, its
    tenant and its tranches' tenants, each spelling once per campus, so one campus
    listed building by building cannot outvote the rest. A party's name counts only
    for a company no campus names otherwise.
    """
    counts: dict[str, Counter[str]] = {}
    seen: set[tuple[int, str, str]] = set()

    def count(project_id: int, raw: str | None, key: str, into: dict) -> None:
        spelling = (raw or "").strip()
        if not key or not spelling or (project_id, key, spelling) in seen:
            return
        seen.add((project_id, key, spelling))
        into.setdefault(key, Counter())[spelling] += 1

    for project_id, company, customer in session.execute(
        select(Project.id, Project.company, Project.customer)
    ):
        count(project_id, company, dedup.company_key(company), counts)
        count(project_id, customer, dedup.customer_key(customer), counts)
    for project_id, customer in session.execute(
        select(CapacityBlock.project_id, CapacityBlock.customer).where(
            CapacityBlock.customer.is_not(None)
        )
    ):
        count(project_id, customer, dedup.customer_key(customer), counts)
    party_only: dict[str, Counter[str]] = {}
    for project_id, name, key in session.execute(
        select(ProjectParty.project_id, ProjectParty.name, ProjectParty.party_key)
    ):
        if key and key not in counts:
            count(project_id, name, key, party_only)
    counts.update(party_only)
    return counts


@dataclass
class SyncReport:
    created: int = 0
    renamed: list[tuple[str, str, str]] = field(default_factory=list)
    aliases_added: int = 0
    aliases_dropped: int = 0
    attached: int = 0
    orphans_removed: int = 0
    #: Companies on file afterwards. `created` alone undercounts a first run: the
    #: party rebuild before it has already filed every company a row names.
    total: int = 0


def sync(session: Session) -> SyncReport:
    """Bring the registry in line with the code and the rows. Idempotent.

    One company per key the roster, the EDGAR list or any row names; names re-chosen
    for those no person named; the code's and the roster's aliases copied in, and the
    ones the code no longer gives dropped; every campus and party pointed at its
    company; and a company nothing names any more, no person touched and the code
    does not seed, removed. Run by `tracker init` and every whole-table `backfill
    derive`.
    """
    from tracker.company import display_name

    report = SyncReport()
    session.info.pop("company_ids", None)
    known = {row.key: row for row in session.scalars(select(Company))}
    seed = seeds_cached()
    counts = spelling_counts(session)
    roster_names = {k: s.name for k, s in seed.items() if s.rostered}

    wanted = set(seed) | set(counts)
    for key in sorted(wanted):
        name = display_name(key, counts.get(key, {}), roster=roster_names)
        if key not in roster_names and key in seed and not counts.get(key):
            name = seed[key].name
        row = known.get(key)
        if row is None:
            row = Company(
                key=key,
                name=name,
                name_by=ROSTER if key in roster_names else "rows",
                kind=seed[key].kind if key in seed else None,
                created_at=utcnow(),
            )
            session.add(row)
            known[key] = row
            report.created += 1
            continue
        if row.name_by in BY_RULE and row.name != name:
            report.renamed.append((key, row.name, name))
            row.name = name
            row.name_by = ROSTER if key in roster_names else "rows"
            row.updated_at = utcnow()
        if key in seed and seed[key].kind and row.kind != seed[key].kind:
            row.kind = seed[key].kind
            row.updated_at = utcnow()
    session.flush()

    sources = _alias_sources()
    existing = {a.spelling: a for a in session.scalars(select(CompanyAlias))}
    for spelling, (key, by) in sorted(sources.items()):
        company = known.get(key) or ensure(session, key)
        known[key] = company
        row = existing.get(spelling)
        if row is None:
            session.add(CompanyAlias(company_id=company.id, spelling=spelling, decided_by=by))
            report.aliases_added += 1
        elif row.decided_by in SEEDED and (row.company_id != company.id or row.decided_by != by):
            row.company_id, row.decided_by = company.id, by
    for spelling, row in existing.items():
        if row.decided_by in SEEDED and spelling not in sources:
            session.delete(row)
            report.aliases_dropped += 1
    session.flush()

    for project in session.scalars(select(Project)):
        before = (project.company_id, tuple(p.company_id for p in project.parties))
        attach(session, project)
        if before != (project.company_id, tuple(p.company_id for p in project.parties)):
            report.attached += 1
    session.flush()

    # A tenant named only on a campus or a tranche points at nothing, and is kept.
    report.orphans_removed = _remove_orphans(session, keep=wanted)
    session.flush()
    report.total = session.scalar(select(func.count()).select_from(Company)) or 0
    return report


def _remove_orphans(session: Session, *, keep: set[str]) -> int:
    """Delete companies nothing points at, no person touched and the code does not seed."""
    used = set(session.scalars(select(Project.company_id).where(Project.company_id.is_not(None))))
    used |= set(
        session.scalars(select(ProjectParty.company_id).where(ProjectParty.company_id.is_not(None)))
    )
    used |= set(session.scalars(select(Company.parent_id).where(Company.parent_id.is_not(None))))
    personal = set(
        session.scalars(
            select(CompanyAlias.company_id).where(CompanyAlias.decided_by.not_in(list(SEEDED)))
        )
    )
    removed = 0
    for row in session.scalars(select(Company)):
        if row.id in used or row.id in personal or row.key in keep:
            continue
        if row.name_by not in BY_RULE or row.parent_id is not None:
            continue
        if any(a.decided_by in SEEDED for a in row.aliases):
            continue
        session.delete(row)
        removed += 1
    session.flush()
    return removed


# --- a person's hand ------------------------------------------------------------------


def find(session: Session, text: str) -> Company | None:
    """A company from what a person typed: its key, its name, or any spelling of it."""
    wanted = (text or "").strip()
    if not wanted:
        return None
    row = session.scalar(select(Company).where(Company.key == wanted))
    if row is not None:
        return row
    row = session.scalar(select(Company).where(func.lower(Company.name) == wanted.lower()))
    if row is not None:
        return row
    key = dedup.company_key(wanted)
    return session.scalar(select(Company).where(Company.key == key)) if key else None


@dataclass
class Rekeyed:
    """What folding one spelling into a company moved."""

    spelling: str
    old_key: str
    new_key: str
    keys_remembered: int = 0
    parties_rebuilt: int = 0
    watches_moved: int = 0
    removed_company: str | None = None


def add_alias(session: Session, spelling_text: str, target: Company, *, by: str) -> Rekeyed:
    """Fold a spelling into `target`: from now on `company_key` reads it as that company.

    Re-keys what it touches the way a deploy does: every campus becomes reachable by
    its new key (`keys.sync`), parties are rebuilt under it, watches on the old key
    follow it, and the old company, once nothing names it, is removed.
    """
    if not by.strip() or by in SEEDED:
        raise CompanyError("an alias a person adds needs the person's name")
    spelling = dedup.spelling_of(spelling_text)
    if not spelling:
        raise CompanyError(f"{spelling_text!r} has nothing to key on")
    old_key = dedup.company_key(spelling_text)
    if old_key == target.key:
        raise CompanyError(f"{spelling_text!r} already reads as {target.name} ({target.key})")
    if spelling == target.key:
        raise CompanyError("a company's own key is not an alias of it")
    row = session.scalar(select(CompanyAlias).where(CompanyAlias.spelling == spelling))
    if row is None:
        row = CompanyAlias(spelling=spelling, company_id=target.id)
        session.add(row)
    row.company_id, row.decided_by, row.decided_at = target.id, by.strip(), utcnow()
    reload(session)
    report = Rekeyed(spelling, old_key, dedup.company_key(spelling_text))
    _rekey(session, old_key, report)
    return report


def remove_alias(session: Session, spelling_text: str) -> Rekeyed:
    """Take back a person's alias. The code's and the roster's cannot be removed here."""
    spelling = dedup.spelling_of(spelling_text)
    row = session.scalar(select(CompanyAlias).where(CompanyAlias.spelling == spelling))
    if row is None:
        raise CompanyError(f"no alias {spelling_text!r}")
    if row.decided_by in SEEDED:
        raise CompanyError(
            f"{spelling_text!r} folds by the {row.decided_by}'s own list; change it there"
        )
    old_key = row.company.key
    session.delete(row)
    reload(session)
    report = Rekeyed(spelling, old_key, dedup.company_key(spelling_text))
    _rekey(session, old_key, report)
    return report


def _rekey(session: Session, old_key: str, report: Rekeyed) -> None:
    from tracker import keys as keys_mod
    from tracker.models import CompanyWatch, Watch
    from tracker.upsert import recompute_parties

    session.info.pop("company_ids", None)
    report.keys_remembered = len(keys_mod.sync(session).added)
    report.parties_rebuilt = recompute_parties(session)
    new_key = report.new_key
    if old_key != new_key:
        for watch in session.scalars(select(Watch).where(Watch.company_key == old_key)):
            clash = session.scalar(
                select(Watch).where(
                    Watch.account_id == watch.account_id,
                    Watch.company_key == new_key,
                    Watch.project_key == watch.project_key,
                )
            )
            if clash is not None:
                session.delete(watch)
            else:
                watch.company_key = new_key
            report.watches_moved += 1
        old = session.get(CompanyWatch, old_key)
        if old is not None:
            if session.get(CompanyWatch, new_key) is None:
                session.add(
                    CompanyWatch(
                        company_key=new_key,
                        baseline_at=old.baseline_at,
                        checked_at=old.checked_at,
                        queries=old.queries,
                        results=old.results,
                        queued=old.queued,
                        last_queued_at=old.last_queued_at,
                    )
                )
            session.delete(old)
    session.flush()
    before = {c.key for c in session.scalars(select(Company))}
    sync(session)
    gone = before - {c.key for c in session.scalars(select(Company))}
    report.removed_company = old_key if old_key in gone else None


def rename(session: Session, company: Company, name: str, *, by: str) -> None:
    """Print a company under a name a person chose. It stands until they change it."""
    name = " ".join((name or "").split())
    if not name:
        raise CompanyError("a name is required")
    if not by.strip():
        raise CompanyError("a renaming records who made it")
    company.name, company.name_by, company.updated_at = name, by.strip(), utcnow()
    session.flush()


def set_parent(session: Session, company: Company, parent: Company | None, *, by: str) -> None:
    """Record whose subsidiary a company is, or that it is nobody's."""
    if parent is not None:
        seen, above = {company.id}, parent
        while above is not None:
            if above.id in seen:
                raise CompanyError(f"{parent.name} is already under {company.name}")
            seen.add(above.id)
            above = above.parent
    company.parent_id = parent.id if parent is not None else None
    company.parent_by = by.strip() if parent is not None else None
    company.updated_at = utcnow()
    session.flush()


# --- reading it ------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One company as a listing shows it."""

    id: int
    key: str
    name: str
    name_by: str
    kind: str | None
    parent: str | None
    campuses: int
    roles: int
    spellings: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "slug": dedup.company_slug(self.key),
            "name": self.name,
            "name_by": self.name_by,
            "kind": self.kind,
            "parent": self.parent,
            "campuses": self.campuses,
            "roles": self.roles,
            "spellings": list(self.spellings),
        }


def listing(session: Session) -> list[Entry]:
    """Every company, most campuses first: as operator, and in any role besides."""
    operated = Counter(
        session.scalars(select(Project.company_id).where(Project.company_id.is_not(None)))
    )
    roles = Counter(
        cid
        for cid, _pid in session.execute(
            select(ProjectParty.company_id, ProjectParty.project_id)
            .where(ProjectParty.company_id.is_not(None))
            .distinct()
        )
    )
    spellings: dict[int, list[str]] = {}
    for alias in session.scalars(select(CompanyAlias).order_by(CompanyAlias.spelling)):
        spellings.setdefault(alias.company_id, []).append(alias.spelling)
    names = {row.id: row.name for row in session.scalars(select(Company))}
    out = [
        Entry(
            row.id,
            row.key,
            row.name,
            row.name_by,
            row.kind,
            names.get(row.parent_id) if row.parent_id else None,
            operated.get(row.id, 0),
            roles.get(row.id, 0),
            tuple(spellings.get(row.id, ())),
        )
        for row in session.scalars(select(Company))
    ]
    return sorted(out, key=lambda e: (-e.campuses, -e.roles, e.name.casefold()))


def family(session: Session, key: str) -> dict[str, Any]:
    """What a company page says about the entity: kind, parent, subsidiaries, spellings."""
    row = session.scalar(select(Company).where(Company.key == key))
    if row is None:
        return {"kind": None, "parent": None, "subsidiaries": [], "spellings": []}
    subsidiaries = session.scalars(
        select(Company).where(Company.parent_id == row.id).order_by(Company.name)
    ).all()

    def ref(company: Company) -> dict[str, str]:
        return {"name": company.name, "slug": dedup.company_slug(company.key)}

    return {
        "kind": row.kind,
        "parent": ref(row.parent) if row.parent is not None else None,
        "subsidiaries": [ref(c) for c in subsidiaries],
        "spellings": sorted(a.spelling for a in row.aliases),
    }


def names(session: Session) -> dict[str, str]:
    """Company key -> the one name, for every company the registry holds."""
    return dict(session.execute(select(Company.key, Company.name)).all())


__all__ = [
    "CODE",
    "ROSTER",
    "CompanyError",
    "Entry",
    "Rekeyed",
    "Seed",
    "SyncReport",
    "add_alias",
    "attach",
    "ensure",
    "family",
    "find",
    "listing",
    "load_aliases",
    "names",
    "reload",
    "remove_alias",
    "rename",
    "seeds",
    "set_parent",
    "spelling_counts",
    "sync",
]
