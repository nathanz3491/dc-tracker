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

import json
import logging
from dataclasses import dataclass
from typing import Any

from tracker.dedup import company_key
from tracker.vocab import COMPANY_ROLE_ORDER, PARTY_ROLES, PARTY_ROLES_BUYING

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
    """
    out: dict[tuple[str, str], Party] = {}
    ranks: dict[tuple[str, str], tuple[int, int, int]] = {}

    for source in sources:
        weight = _weight(source)
        for entry in parse(getattr(source, "parties", None)):
            name = str(entry.get("name") or "").strip()
            role = str(entry.get("role") or "").strip().lower()
            key = party_key(name)
            if not name or not key or role not in PARTY_ROLES:
                continue
            quote = str(entry.get("quote") or "").strip() or None
            unconfirmed = str(entry.get("unconfirmed") or "").strip() or None
            rank = (0 if unconfirmed is None else 1, -weight, -len(quote or ""))
            slot = (key, role)
            if slot in ranks and ranks[slot] <= rank:
                continue
            ranks[slot] = rank
            out[slot] = Party(
                name=name,
                key=key,
                role=role,
                quote=quote,
                unconfirmed=unconfirmed,
                source_id=getattr(source, "id", None),
            )
    return out


def rebuild(session: Any, project: Any) -> int:
    """Rebuild a project's parties from its sources. Returns rows changed.

    Wholesale, not incremental, for the reason `blocks.rebuild` gives: a party is
    a *description* of the site, so its absence from every source means the
    description changed. That reasoning does not hold for `risk`, where absence
    from one article is no evidence the obstacle cleared — which is why risks are
    never dropped this way and parties are.
    """
    from tracker.models import ProjectParty

    wanted = parties_by_key(list(project.sources))
    existing = {(p.party_key, p.role): p for p in list(project.parties)}
    changed = 0

    for slot, party in wanted.items():
        row = existing.pop(slot, None)
        fresh = {
            "name": party.name,
            "quote": party.quote,
            "unconfirmed": party.unconfirmed,
            "source_id": party.source_id,
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


def of_role(parties: list[Any], role: str) -> list[Any]:
    """Every party in one role, confirmed ones first."""
    return sorted(
        (p for p in parties if p.role == role),
        key=lambda p: (p.unconfirmed is not None, p.party_key),
    )


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

    unconfirmed = [p for p in parties if p.unconfirmed is not None]
    if unconfirmed:
        notes.append(
            f"{len(unconfirmed)} of {len(parties)} parties are 待确认 "
            f"({', '.join(sorted({p.name for p in unconfirmed}))})"
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
    return keys_for(a) & keys_for(b)


def buying_keys(project: Any) -> set[str]:
    """Party keys that could hold a capex position. Excludes utility and contractor."""
    rows = list(getattr(project, "parties", ()) or ())
    return {p.party_key for p in rows if p.party_key and p.role in PARTY_ROLES_BUYING}


__all__ = [
    "Party",
    "buying_keys",
    "customer_party",
    "keys_for",
    "of_role",
    "operator_party",
    "parse",
    "parties_by_key",
    "party_key",
    "rebuild",
    "reconcile",
    "shared_across_companies",
]
