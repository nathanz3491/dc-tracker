"""Who plays which role on a site.

`project.company` means "who builds and operates the site" — that is the
extraction prompt's own wording, and it is two roles in one string. `customer` is
a third, and the utility and the landowner have nowhere to go at all.

**The measured cost.** `docs/duplicate-shapes.md` replays the 90 folds an
operator performed by hand: 48 of them had no key-level signal connecting the two
rows, "because every key comparison holds the company fixed". Abilene was stored
four times — Crusoe builds it, Oracle leases it, OpenAI occupies it, and one
source wrote "OpenAI/Oracle" — and each name minted its own `dedup_key`. Every
one of those rows then contributed its full capacity to a buyer's position.

The parties were in the articles all along. `dedup.shared_parties_across_companies`
recovers them, but only from *inside* one company string, so it fires on
"OpenAI/Oracle" against "Oracle" and cannot fire when four articles each name one
party. This module is where the other case lives.

**A cache, not a fact of record.** Rebuilt wholesale from `source.parties` on
every upsert — the same status `blocks`, `confidence` and `h200_equivalent` have,
and the same obligation: a second pass must change nothing, or every number in
the database is whichever pass ran last. `test_parties_cache_is_consistent` pins
it.

**What this module deliberately does NOT do: rewrite `project.company`.**

The plan for it was to derive `company` from the operator party, on the reasoning
that the scalar should be a cache of the party set like `blocker` is a cache of
the risks. That is wrong here, and the reason is `dedup_key`.

`dedup_key` is `company|locality|state`, it is UNIQUE, and it is computed once at
insert. Nothing recomputes it — deliberately, because re-keying a row means
colliding with whatever else already holds the new key. So a `company` that moves
after insert leaves the key describing a company the row no longer names, and the
duplicate gate then compares a stale key on every subsequent crawl. `company` is
`Policy.FILL_ONLY` for exactly this reason, documented as "churn is worse than
staleness".

So the roles are recorded beside `company` rather than underneath it. `customer`
*is* filled from a party when it is null, because it is nullable, nothing keys on
it, and `blocks.reconcile` already fills it the same way. Everything else reads
the party rows directly — `capex.attribute` most of all, which is where the
double-counting was.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from tracker.dedup import company_key
from tracker.vocab import COMPANY_ROLE_ORDER, PARTY_ROLES, PARTY_ROLES_BUYING

#: The `unconfirmed` reason on a party derived from a citation's own claims
#: rather than read out of a sentence. Named here because three functions test
#: for it and a bare string in each is three places to typo.
INFERRED_ROLE: str = "role_inferred"

log = logging.getLogger(__name__)


def _weight(source: Any) -> int:
    """How much one source's word about a role is worth.

    `confidence.SOURCE_WEIGHTS` rather than a second authority ranking of this
    module's own. Two articles can name the same company in two roles — a
    developer that later operates the site, or a story calling the tenant "the
    operator" loosely — and the display name and the quote come from the heavier
    source.
    """
    from tracker import confidence as conf

    return conf.SOURCE_WEIGHTS.get(getattr(source, "source_type", "") or "", 1)


@dataclass(frozen=True)
class Party:
    """One company's role on one site, as the sources collectively describe it."""

    name: str
    key: str
    role: str
    quote: str | None = None
    unconfirmed: str | None = None
    source_id: int | None = None

    @property
    def confirmed(self) -> bool:
        return self.unconfirmed is None

    @property
    def buys(self) -> bool:
        """Whether this role can hold a position in the capex table.

        A utility connects capacity and a contractor pours the concrete. Neither
        buys it, and attributing to them would credit Entergy Louisiana with
        Meta's campus.
        """
        return self.role in PARTY_ROLES_BUYING


def party_key(name: str | None) -> str:
    """Identity for a party name. `dedup.company_key`, and only that.

    Shared with the dedup path on purpose: a party that normalises differently
    from a company could never be compared against `project.company`, which is
    the comparison the duplicate gate needs.
    """
    return company_key(name)


def parse(raw: str | None) -> list[dict[str, Any]]:
    """One source's `parties` JSON, or an empty list. Never raises.

    A malformed payload is a bad extraction, not a reason to fail a read: the
    rest of the row is still worth having, which is the same call
    `upsert._carried_reasons` makes about `unconfirmed_reasons`.
    """
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    return [entry for entry in loaded if isinstance(entry, dict)]


def parties_by_key(sources: list[Any]) -> dict[tuple[str, str], Party]:
    """Collapse every source's parties onto one row per `(key, role)`.

    Three rules, in order, and each one is a decision a reader can check:

    1. **A confirmed party beats an unconfirmed one.** Same discipline as
       `upsert._resolve`: a value the gate could not tie to a sentence is a last
       resort, never a displacement of one it could.
    2. **Then the heavier source.** `company_filing` over `trade_press` over
       `general_media`, reusing `confidence.SOURCE_WEIGHTS` rather than inventing
       a second authority ranking.
    3. **Then the longer quote**, which is a tiebreak with no opinion in it —
       both sentences are the article's own words, and the fuller one tells a
       reader more.

    Crawl order decides nothing, which is the bug `source.published_at` exists to
    fix one level up and is not worth reintroducing here.

    **A citation that lists no parties still has some**, and this is where every
    write path other than the article reader gets covered. Five places in this
    codebase create a citation and only one of them reads an article: the agent
    pass fills single fields from a page, the seed file is curated by hand, the ISO
    loader promotes an operator name out of a queue row. Each of those can assert
    a `company` — which *is* an operator claim, because that is what the column has
    always meant — and none of them produces a party list.

    Teaching four paths to emit parties would mean four places to forget. Deriving
    it here means one, and it covers whichever path is added next. See
    `_inferred_parties` for what the derived rows may and may not claim.
    """
    out: dict[tuple[str, str], Party] = {}
    ranks: dict[tuple[str, str], tuple[int, int, int]] = {}

    def offer(source: Any, party: Party, weight: int) -> None:
        rank = (0 if party.unconfirmed is None else 1, -weight, -len(party.quote or ""))
        slot = (party.key, party.role)
        if slot in ranks and ranks[slot] <= rank:
            return
        ranks[slot] = rank
        out[slot] = party

    for source in sources:
        weight = _weight(source)
        declared = 0
        for entry in parse(getattr(source, "parties", None)):
            name = str(entry.get("name") or "").strip()
            role = str(entry.get("role") or "").strip().lower()
            key = party_key(name)
            if not name or not key or role not in PARTY_ROLES:
                continue
            declared += 1
            offer(
                source,
                Party(
                    name=name,
                    key=key,
                    role=role,
                    quote=str(entry.get("quote") or "").strip() or None,
                    unconfirmed=str(entry.get("unconfirmed") or "").strip() or None,
                    source_id=getattr(source, "id", None),
                ),
                weight,
            )
        # Only when the citation declared none. A source that listed its parties
        # has already said what it knows, and deriving more from its claims would
        # re-add the company it deliberately left out of its own list.
        if not declared:
            for party in _inferred_parties(source):
                offer(source, party, weight)
    return out


#: Claim -> the role that claim IS an assertion of.
#:
#: Two, and no more. `company` means "who runs it" and `customer` means "who
#: occupies it" — those are the two columns' documented meanings, so reading them
#: as roles restates rather than infers. Nothing here guesses a `developer`, an
#: `owner` or a `utility`: those are the roles the single column was collapsing,
#: they are recoverable only from article text, and manufacturing one would be the
#: fabrication this whole gate exists to refuse.
_CLAIMED_ROLES: dict[str, str] = {"company": "operator", "customer": "customer"}


def _inferred_parties(source: Any) -> list[Party]:
    """Parties implied by one citation's own claims, for a citation that lists none.

    Every one is marked `role_inferred` and carries **no quote**, and both of those
    are the point. Nobody asserted the role — it is what the column means — so a
    quote would be dressing an inference as evidence, and `capex.attribute` will
    not let one decide an attribution.

    They are still worth having for the question they *can* answer. A party key is
    what makes two rows in one locality recognise each other, and the name behind
    a `customer` claim is a real name a real citation asserted however the role
    was arrived at. That is the signal `docs/duplicate-shapes.md` measures as
    missing from 48 of 90 hand-performed merges.
    """
    try:
        claims = json.loads(getattr(source, "claims", None) or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(claims, dict):
        return []

    out: list[Party] = []
    for field, role in _CLAIMED_ROLES.items():
        name = claims.get(field)
        if not isinstance(name, str):
            continue
        name = name.strip()
        key = party_key(name)
        if not name or not key:
            continue
        out.append(
            Party(
                name=name,
                key=key,
                role=role,
                quote=None,
                unconfirmed="role_inferred",
                source_id=getattr(source, "id", None),
            )
        )
    return out


def first_cited(sources: list[Any]) -> dict[str, dt.date]:
    """Party key -> the earliest date one of this row's citations named it.

    The article's publication date where one is recorded, else the day it was read.
    The project page marks a party first cited in the last thirty days as *new*,
    because a new contractor or tenant on a known campus is news the client asked to
    see apart (2026-10-21). Read over the same entries `parties_by_key` reads, the
    inferred ones included, so a party is never dated later than the citation that
    put it on the row.
    """
    out: dict[str, dt.date] = {}
    for source in sources:
        day = _cited_on(source)
        if day is None:
            continue
        declared = [
            party_key(str(entry.get("name") or "").strip())
            for entry in parse(getattr(source, "parties", None))
        ]
        keys = [key for key in declared if key] or [p.key for p in _inferred_parties(source)]
        for key in keys:
            if key not in out or day < out[key]:
                out[key] = day
    return out


def _cited_on(source: Any) -> dt.date | None:
    """The day a citation counts from: the article's publication, else when it was read."""
    when = getattr(source, "published_at", None) or getattr(source, "fetched_at", None)
    if when is None:
        return None
    return when.date() if isinstance(when, dt.datetime) else when


#: How long a party counts as newly named: the project page's *new* mark and the
#: company page's list both read thirty days. The Updates page asks for the window
#: it is showing — a week, a month or two months.
NEW_DAYS: int = 30


def newly_named(
    session: Any,
    *,
    days: int = NEW_DAYS,
    roles: Iterable[str] | None = None,
    project_ids: Iterable[int] | None = None,
    today: dt.date | None = None,
    names: Any = None,
) -> dict[str, Any]:
    """Companies a source named on a campus for the first time in the last `days`.

    The portfolio form of the project page's *new* mark (client review,
    2026-10-21/22: "recently mentioned new suppliers"). A new contractor or utility
    on a known campus is news a reader of one campus could already see; this is the
    same reading across every campus, or across one company's (`project_ids`), so a
    reader can ask which suppliers are winning new work this month.

    **"First named" is `first_cited`, unchanged.** The earliest citation of the
    campus that names the party, dated by the article's publication where recorded,
    so an old article the crawler only just read does not make an old supplier new
    — the same rule the Updates page applies to everything else it calls news.

    **One entry per company, however many campuses named it.** Grouped by party key
    (`dedup.company_key`), so Turner on three campuses is one line listing three,
    and printed under the company registry's one name where it has one. Each campus
    keeps its own role, date, sentence and source, and says whether the campus
    itself is new to the tracker in the window — a party on a campus first reported
    this month is new because the campus is, which is a different piece of news
    from a new supplier on a campus already followed.

    **Left out: a role that has ended** (`ended_reason`, migration 0044). A former
    tenant is not a newly named one, however recent the article that named it.

    `roles` narrows to some of `vocab.PARTY_ROLES`; None is every role, suppliers
    (`vocab.PARTY_ROLES_SUPPLYING`) first, then by date. The console opens on the
    suppliers and offers the rest as filters — see `NewPartiesPanel` in `app.js`.
    A 待确认 role is kept and says so (`unconfirmed`), as the project page shows it:
    the name is in the article even where the role was not licensed by its wording.

    **Three narrow reads, whatever the fleet's size**: the citations of every campus
    with a citation in the window (bare columns, no rows hydrated), the current
    parties of those campuses with their campus and source, and the one-name table.
    No per-campus query — the trap `capex.working_set` records — and nothing loaded
    for a campus no recent article touched.
    """
    from sqlalchemy import func, select

    from tracker import delays
    from tracker.models import Project, ProjectParty, Source
    from tracker.vocab import PARTY_ROLES_SUPPLYING

    wanted = tuple(PARTY_ROLES) if roles is None else tuple(dict.fromkeys(roles))
    for role in wanted:
        if role not in PARTY_ROLES:
            raise ValueError(f"unknown role {role!r}: one of {', '.join(PARTY_ROLES)}")
    if days < 1:
        raise ValueError("days must be at least 1")
    today = today or delays.as_of()
    since = today - dt.timedelta(days=days)
    out: dict[str, Any] = {"days": days, "since": since.isoformat(), "parties": []}
    if not wanted:
        return out

    # The campuses a citation in the window touches. A party first named in the
    # window was named by such a citation, so no other campus can hold one.
    when = func.coalesce(Source.published_at, Source.fetched_at)
    touched = select(Source.project_id).where(when >= dt.datetime.combine(since, dt.time.min))
    if project_ids is not None:
        scope = sorted(set(project_ids))
        if not scope:
            return out
        touched = touched.where(Source.project_id.in_(scope))
    touched = touched.distinct()

    # Every citation of those campuses, because "first" needs the old ones too.
    cited: dict[int, list[Any]] = {}
    for row in session.execute(
        select(
            Source.project_id,
            Source.parties,
            Source.claims,
            Source.published_at,
            Source.fetched_at,
        ).where(Source.project_id.in_(touched))
    ):
        cited.setdefault(row.project_id, []).append(row)
    fresh: dict[tuple[int, str], dt.date] = {}
    new_campus: set[int] = set()
    for pid, rows in cited.items():
        for key, day in first_cited(rows).items():
            if day >= since:
                fresh[(pid, key)] = day
        days_cited = [d for d in map(_cited_on, rows) if d is not None]
        if days_cited and min(days_cited) >= since:
            new_campus.add(pid)
    if not fresh:
        return out

    if names is None:
        from tracker.company import display_names

        names = display_names(session)

    order = {role: i for i, role in enumerate(PARTY_ROLES)}
    # (party key, campus) -> the campus entry; a company named in two roles on one
    # campus is one campus entry, under the role it is likeliest to be read for.
    campuses: dict[tuple[str, int], dict[str, Any]] = {}
    spelled: dict[str, str] = {}
    for row in session.execute(
        select(
            ProjectParty.project_id,
            ProjectParty.party_key,
            ProjectParty.name,
            ProjectParty.role,
            ProjectParty.quote,
            ProjectParty.unconfirmed,
            Source.url,
            Project.name.label("campus"),
            Project.company,
            Project.city,
            Project.county,
            Project.state,
        )
        .join(Project, Project.id == ProjectParty.project_id)
        .outerjoin(Source, Source.id == ProjectParty.source_id)
        .where(
            ProjectParty.project_id.in_(touched),
            ProjectParty.ended_reason.is_(None),
            ProjectParty.role.in_(wanted),
        )
    ):
        day = fresh.get((row.project_id, row.party_key))
        if day is None:
            continue
        rank = (row.unconfirmed is not None, order[row.role])
        slot = (row.party_key, row.project_id)
        entry = campuses.get(slot)
        if entry is not None:
            entry["roles"] = sorted({*entry["roles"], row.role}, key=order.__getitem__)
            if rank >= entry["_rank"]:
                continue
        campuses[slot] = {
            "_rank": rank,
            "id": row.project_id,
            "name": row.campus,
            "company": names.company(row.company),
            "place": row.city or row.county,
            "state": row.state,
            "role": row.role,
            "roles": entry["roles"] if entry is not None else [row.role],
            "first_cited": day.isoformat(),
            "quote": row.quote,
            "unconfirmed": row.unconfirmed,
            "url": row.url,
            "new_campus": row.project_id in new_campus,
        }
        if row.unconfirmed is None or row.party_key not in spelled:
            spelled[row.party_key] = row.name

    grouped: dict[str, list[dict[str, Any]]] = {}
    for (key, _pid), entry in campuses.items():
        entry.pop("_rank")
        grouped.setdefault(key, []).append(entry)
    parties_out = []
    for key, entries in grouped.items():
        entries.sort(
            key=lambda e: (
                -dt.date.fromisoformat(e["first_cited"]).toordinal(),
                e["name"].casefold(),
            )
        )
        held = sorted({r for e in entries for r in e["roles"]}, key=order.__getitem__)
        parties_out.append(
            {
                "key": key,
                "name": names.of_key(key, spelled[key]),
                "roles": held,
                "supplier": any(r in PARTY_ROLES_SUPPLYING for r in held),
                "confirmed": any(e["unconfirmed"] is None for e in entries),
                "latest": entries[0]["first_cited"],
                "campuses": entries,
            }
        )
    parties_out.sort(
        key=lambda p: (
            not p["supplier"],
            -dt.date.fromisoformat(p["latest"]).toordinal(),
            -len(p["campuses"]),
            p["name"].casefold(),
        )
    )
    out["parties"] = parties_out
    return out


def rebuild(session: Any, project: Any) -> int:
    """Rebuild a project's parties from its sources. Returns rows changed.

    Wholesale, not incremental, for the reason `blocks.rebuild` gives: a party is
    a *description* of the site, so its absence from every source means the
    description changed. That reasoning does not hold for `risk`, where absence
    from one article is no evidence the obstacle cleared — which is why risks are
    never dropped this way and parties are.
    """
    from sqlalchemy import select

    from tracker.models import PartyTenure, ProjectParty

    wanted = parties_by_key(list(project.sources))
    existing = {(p.party_key, p.role): p for p in list(project.parties)}
    # Whose role here has ended (migration 0044): a decision, so it lives in its own
    # table and is copied onto the rebuilt rows rather than lost with them.
    ended = {}
    if getattr(project, "id", None) is not None:
        ended = {
            (t.party_key, t.role): t
            for t in session.scalars(
                select(PartyTenure).where(PartyTenure.project_id == project.id)
            )
        }
    changed = 0

    for slot, party in wanted.items():
        row = existing.pop(slot, None)
        tenure = ended.get(slot)
        fresh = {
            "name": party.name,
            "quote": party.quote,
            "unconfirmed": party.unconfirmed,
            "source_id": party.source_id,
            "ended_on": tenure.ended_on if tenure is not None else None,
            "ended_reason": tenure.reason if tenure is not None else None,
        }
        if row is None:
            project.parties.append(ProjectParty(party_key=party.key, role=party.role, **fresh))
            changed += 1
            continue
        if any(getattr(row, name) != value for name, value in fresh.items()):
            for name, value in fresh.items():
                setattr(row, name, value)
            changed += 1

    for orphan in existing.values():
        project.parties.remove(orphan)
        session.delete(orphan)
        changed += 1

    session.flush()
    return changed


def of_role(parties: list[Any], role: str, *, former: bool = False) -> list[Any]:
    """Every current party in one role, confirmed ones first.

    A party whose role here has ended — a former tenant (migration 0044) — is left
    out unless `former` asks for it: every reader of this means "who is it now".
    """
    return sorted(
        (
            p
            for p in parties
            if p.role == role and (former or getattr(p, "ended_reason", None) is None)
        ),
        key=lambda p: (p.unconfirmed is not None, p.party_key),
    )


def former(parties: list[Any]) -> list[Any]:
    """The parties whose role here has ended, by role then name."""
    return sorted(
        (p for p in parties if getattr(p, "ended_reason", None) is not None),
        key=lambda p: (p.role, p.party_key),
    )


def former_customer(project: Any) -> Any | None:
    """The ended customer party that `project.customer` still names, if it does.

    The column is whichever tenant name reached it first, and sources keep naming a
    tenant after it has gone — Cyxtera at Comstock. Every reader of "who buys this
    campus now" asks this first, so the page, Capex, the company pages and a watch
    all drop a former tenant together (`current_customer`).
    """
    from tracker.dedup import customer_key

    key = customer_key(getattr(project, "customer", None))
    if not key:
        return None
    for party in former(list(getattr(project, "parties", ()) or ())):
        if party.role == "customer" and key in (party.party_key, customer_key(party.name)):
            return party
    return None


def current_customer(project: Any) -> str | None:
    """`project.customer`, unless the tenant it names has left (`former_customer`)."""
    if former_customer(project) is not None:
        return None
    return getattr(project, "customer", None)


def operator_party(parties: list[Any]) -> Any | None:
    """The party the site's `company` names, or the nearest thing to it.

    Falls through `COMPANY_ROLE_ORDER` — operator, then developer, then owner —
    because `project.company` has always meant "who runs it" and a campus with no
    named operator is more usefully described by whoever is building it than by
    nobody.
    """
    for role in COMPANY_ROLE_ORDER:
        found = of_role(parties, role)
        if found:
            return found[0]
    return None


def customer_party(parties: list[Any]) -> Any | None:
    """The party that occupies the site and buys the compute."""
    found = of_role(parties, "customer")
    return found[0] if found else None


def named_buyers(parties: list[Any]) -> list[Any]:
    """The confirmed customer parties, one per company, in `of_role` order.

    One per company by `dedup.customer_key`, so "TensorWave" and its lease vehicle
    "TensorWave TEX1" are one buyer, and a hedge ("a leading frontier AI lab")
    names nobody and is left out. More than one means the site names several
    buyers, which `capex.attribute` will not settle by the order of their names.
    """
    from tracker.dedup import customer_key

    seen: dict[str, Any] = {}
    for party in of_role(parties, "customer"):
        if party.unconfirmed is not None:
            continue
        key = customer_key(party.name)
        if key and key not in seen:
            seen[key] = party
    return list(seen.values())


def reconcile(project: Any) -> list[str]:
    """Fill `customer` from the parties where it is empty. Returns disclosures.

    **Fills a null, never overwrites.** Same guarantee `blocks.reconcile` makes
    and for the same reason: a cited value must not be displaced by a derivation,
    and a project with no parties is left exactly as it is — which is what let
    this land on an existing database without moving a single stored figure.

    `company` is deliberately untouched. See the module docstring: `dedup_key` is
    computed once at insert and nothing re-keys it, so a `company` that moves
    afterwards leaves the UNIQUE key naming a company the row does not.
    """
    parties = list(getattr(project, "parties", ()) or ())
    if not parties:
        return []

    notes: list[str] = []

    if project.customer is None:
        found = customer_party(parties)
        if found is not None and found.unconfirmed is None:
            project.customer = found.name

    named = of_role(parties, "customer")
    if len(named) > 1:
        notes.append(f"sources name {len(named)} tenants: {', '.join(p.name for p in named)}")

    operators = of_role(parties, "operator") + of_role(parties, "developer")
    stated = company_key(project.company)
    others = [p for p in operators if p.party_key and p.party_key != stated]
    if stated and others:
        notes.append(
            f"{', '.join(sorted({p.name for p in others}))} named on this site "
            f"alongside {project.company}; if either row is the same campus under "
            "another party's name, tracker duplicates will raise it"
        )

    # **Refusals only.** An inferred role was never offered and never refused —
    # it is what `company` means — and every project in the database has a
    # company, so counting those here would put this line on every row and bury
    # the refusals it exists to surface. See `vocab.UNCONFIRMED_REASONS`.
    refused = [p for p in parties if p.unconfirmed not in (None, INFERRED_ROLE)]
    if refused:
        notes.append(
            f"{len(refused)} of {len(parties)} parties are 待确认 "
            f"({', '.join(sorted({p.name for p in refused}))})"
        )

    return notes


def keys_for(project: Any, *, confirmed_only: bool = False) -> set[str]:
    """Every party key on a project, plus the parties inside its `company` string.

    The union is the point. `dedup.company_parts` already recovers "OpenAI/Oracle"
    into two keys and that path stays live for rows nothing has re-crawled; the
    party rows add the case it cannot see, where each article named one party and
    the composite string never existed.
    """
    from tracker.dedup import company_parts

    rows = list(getattr(project, "parties", ()) or ())
    if confirmed_only:
        rows = [p for p in rows if p.unconfirmed is None]
    keys = {p.party_key for p in rows if p.party_key}
    return keys | company_parts(getattr(project, "company", None))


def shared_across_companies(a: Any, b: Any) -> set[str]:
    """Parties two *differently named* rows have in common. The 48-of-90 signal.

    `dedup.shared_parties_across_companies` answers this from the two company
    strings alone, so it fires on "OpenAI/Oracle" against "Oracle" and is blind to
    the shape that actually dominates: four articles, four rows, each naming one
    party, and the composite string never written anywhere. This asks the same
    question of the party rows, which is where the other three names now live.

    **The same-company guard is kept, and it is load-bearing.** That function
    returns nothing when both rows normalise to one company, because
    `capex.suspected_duplicates` has a pass that buckets rows *by* company — every
    pair on it would report party evidence and none of it would mean anything.
    `dupresolve.HARD_EVIDENCE` trusts `party` enough to carry an unattended merge,
    so the vacuous form would have offered to fold NTT's Itasca campus into NTT's
    Chicago one, 31.7 km away. Dropping the guard here would reintroduce that
    through a different door.

    Unconfirmed parties count. The role may be unevidenced, but the *name* is not:
    `crawl._parties` drops any party it cannot find in the article, so every row
    here is either the project's own company or a string somebody published. It is
    the name that makes two rows recognise each other, and the role is not part of
    the question.
    """
    if company_key(getattr(a, "company", None)) == company_key(getattr(b, "company", None)):
        return set()
    return _identifying_keys(a) & _identifying_keys(b)


#: Roles that say nothing about *which* campus a row is. A utility serves every site
#: in its territory and a contractor builds for anybody — Dominion is a party on ten
#: rows and Oncor on seven — so sharing one is a statement about a region, and
#: `dupresolve.HARD_EVIDENCE` trusts `party` enough to carry a merge. On 2026-10-19
#: the twin pass offered AWS's Project Rainier and Microsoft's La Porte campus, two
#: companies 50 km apart in Indiana, on a shared utility alone; the one pair of that
#: kind already in the report linked two RagingWire rows through a door vendor.
#: A shared tenant still counts: Stargate filed under Crusoe and under Oracle, both
#: naming OpenAI, is the shape this signal was built for.
_NOT_IDENTIFYING_ROLES: frozenset[str] = frozenset({"utility", "contractor"})


def _identifying_keys(project: Any) -> set[str]:
    """`keys_for`, without the parties whose role names a region rather than a site."""
    from tracker.dedup import company_parts

    rows = list(getattr(project, "parties", ()) or ())
    keys = {p.party_key for p in rows if p.party_key and p.role not in _NOT_IDENTIFYING_ROLES}
    return keys | company_parts(getattr(project, "company", None))


def buying_keys(project: Any) -> set[str]:
    """Party keys that could hold a capex position. Excludes utility and contractor,
    and a party whose role here has ended."""
    rows = list(getattr(project, "parties", ()) or ())
    return {
        p.party_key
        for p in rows
        if p.party_key and p.role in PARTY_ROLES_BUYING and getattr(p, "ended_reason", None) is None
    }


class TenureError(ValueError):
    """A role that cannot be ended or resumed, in words a person can act on."""


def find_party(project: Any, text: str, role: str | None = None) -> list[Any]:
    """The party rows a person meant: by key or by name, optionally in one role."""
    from tracker.dedup import company_key as key_of

    wanted = key_of(text) or (text or "").strip().lower()
    return [
        p
        for p in getattr(project, "parties", ()) or ()
        if p.party_key == wanted and (role is None or p.role == role)
    ]


def end_role(
    session: Any,
    project: Any,
    party: str,
    role: str,
    *,
    reason: str,
    by: str,
    ended_on: Any = None,
    source_id: int | None = None,
) -> Any:
    """Record that a party's role on this campus has ended. Idempotent per role.

    The party stays on the page as former and stays evidence of which campus this
    is; it stops being a current buyer. Rebuilds the campus's parties so every
    surface reads it at once.
    """
    from sqlalchemy import select

    from tracker.dedup import company_key as key_of
    from tracker.models import PartyTenure, utcnow

    if role not in PARTY_ROLES:
        raise TenureError(f"role must be one of {', '.join(PARTY_ROLES)}")
    if not (reason or "").strip():
        raise TenureError("a reason is required: why the role ended")
    if not (by or "").strip():
        raise TenureError("an ended role records who decided it")
    key = key_of(party)
    if not key:
        raise TenureError(f"{party!r} names no company")
    if not find_party(project, party, role):
        raise TenureError(f"#{project.id} names no {role} {party!r}")
    row = session.scalar(
        select(PartyTenure).where(
            PartyTenure.project_id == project.id,
            PartyTenure.party_key == key,
            PartyTenure.role == role,
        )
    )
    if row is None:
        row = PartyTenure(project_id=project.id, party_key=key, role=role, reason=reason)
        session.add(row)
    row.reason = " ".join(reason.split())[:400]
    row.ended_on = ended_on
    row.source_id = source_id
    row.decided_by = by.strip()
    row.decided_at = utcnow()
    session.flush()
    _refresh(session, project)
    return row


def resume_role(session: Any, project: Any, party: str, role: str) -> bool:
    """Take back an ended role: the party is current again. True if one was ended."""
    from sqlalchemy import select

    from tracker.dedup import company_key as key_of
    from tracker.models import PartyTenure

    row = session.scalar(
        select(PartyTenure).where(
            PartyTenure.project_id == project.id,
            PartyTenure.party_key == key_of(party),
            PartyTenure.role == role,
        )
    )
    if row is None:
        return False
    session.delete(row)
    session.flush()
    _refresh(session, project)
    return True


def _refresh(session: Any, project: Any) -> None:
    from tracker import companies

    rebuild(session, project)
    reconcile(project)
    companies.attach(session, project)
    session.flush()


__all__ = [
    "INFERRED_ROLE",
    "NEW_DAYS",
    "Party",
    "TenureError",
    "buying_keys",
    "current_customer",
    "customer_party",
    "end_role",
    "find_party",
    "former",
    "former_customer",
    "keys_for",
    "named_buyers",
    "newly_named",
    "of_role",
    "operator_party",
    "parse",
    "parties_by_key",
    "party_key",
    "rebuild",
    "reconcile",
    "resume_role",
    "shared_across_companies",
]
