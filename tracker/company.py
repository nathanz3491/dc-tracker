"""One company's page, and the one name the console prints for a company.

The console had a page per project and none per company, so a watchlist entry like
"Google" — twenty-five campuses — led nowhere. `profile` is the read behind
`/companies/<slug>`.

**A company is matched the way the watchlist matches it**, through
`watchlist._project_keys`: the operator, the customer, and a tranche's customer,
each normalised by `dedup.company_key`. The page and the watch on the same name
therefore cover the same campuses — a "Google" page that disagreed with a "Google"
watch would be two answers to one question — and each campus says which way it
matched, so a builder can be told from a tenant.

**The slug is the key with hyphens**: `compass datacenters` → `compass-datacenters`.
Letters, digits and hyphens only, because the server puts it into the page shell
and that restriction is the whole defence against script injection there.

**One name per company** (`display_names`). The rows store whatever each source
wrote — NTT sits under eight spellings, Amazon under six — and every list printed
them as stored, so one company read as several. The name printed is the roster's
when the company is on it (`seed/operators.toml`, where a person chose how to write
"NTT"), else the spelling most rows use.

Nothing here is stored; it is a reading of rows that already carry their dates and
citations, like `feed` and `watchfor`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tracker import capex, company_logo, feed, watchlist
from tracker.dedup import company_slug as slug
from tracker.models import CapacityBlock, Project, Source
from tracker.vocab import OPEN_RISK_STATUS

#: A slug as the URL and the page shell carry it. See the module docstring.
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,80}")


# --- the one name ----------------------------------------------------------------


@lru_cache(maxsize=1)
def rostered_names() -> dict[str, str]:
    """Company key -> the roster's own name for it, for every rostered operator.

    Read once per process: the roster is part of the code and changes with a
    deploy. A roster that cannot be read leaves every company named by its rows
    rather than failing the page.
    """
    from tracker import roster

    try:
        operators = roster.load()
    except (roster.RosterError, OSError):
        return {}
    named: dict[str, str] = {}
    for operator in operators:
        key = capex.company_key(operator.name)
        if key:
            named.setdefault(key, operator.name)
    return named


def display_name(
    key: str, spellings: Mapping[str, int], *, roster: Mapping[str, str] | None = None
) -> str:
    """The one name for company `key`, given how many rows store each spelling of it.

    The roster's name when the company is on it, else the spelling most rows use. A
    tie goes to the shorter spelling, then the alphabetically first, so the answer
    never depends on the order rows were read in.
    """
    roster = rostered_names() if roster is None else roster
    if key in roster:
        return roster[key]
    if spellings:
        return min(spellings, key=lambda s: (-spellings[s], len(s), s.casefold(), s))
    return key.title()


@dataclass(frozen=True)
class Names:
    """The one name to print for each company key."""

    by_key: Mapping[str, str] = field(default_factory=dict)

    def of_key(self, key: str | None, fallback: str | None = None) -> str | None:
        return self.by_key.get(key or "") or fallback

    def company(self, raw: str | None) -> str | None:
        """An operator as a source wrote it, under its one name."""
        if not raw:
            return raw
        return self.of_key(capex.company_key(raw), raw.strip())

    def customer(self, raw: str | None) -> str | None:
        """A tenant as a source wrote it, under its one name.

        A hedge — "a Fortune 100 technology company" — names nobody, so it has no
        key and is printed as written rather than given a company's name.
        """
        if not raw:
            return raw
        return self.of_key(capex.customer_key(raw), raw.strip())


def display_names(session: Session) -> Names:
    """Every company the database stores, under the one name the console prints.

    Counted from the three places a name is printed from: a campus's operator, its
    tenant, and a tranche's tenant. A campus counts each spelling once however many
    of its tranches carry it, so one campus listed building by building cannot
    outvote the rest of the database.

    Two narrow queries and no relationship loading, because every request that
    prints a company asks for this once.
    """
    counts: dict[str, Counter[str]] = {}
    seen: set[tuple[int, str, str]] = set()

    def count(project_id: int, raw: str | None, key: str) -> None:
        spelling = (raw or "").strip()
        if not key or not spelling or (project_id, key, spelling) in seen:
            return
        seen.add((project_id, key, spelling))
        counts.setdefault(key, Counter())[spelling] += 1

    for project_id, company, customer in session.execute(
        select(Project.id, Project.company, Project.customer)
    ):
        count(project_id, company, capex.company_key(company))
        count(project_id, customer, capex.customer_key(customer))
    for project_id, customer in session.execute(
        select(CapacityBlock.project_id, CapacityBlock.customer).where(
            CapacityBlock.customer.is_not(None)
        )
    ):
        count(project_id, customer, capex.customer_key(customer))

    roster = rostered_names()
    by_key = dict(roster)
    by_key.update(
        {key: display_name(key, spellings, roster=roster) for key, spellings in counts.items()}
    )
    return Names(by_key)


# --- the page --------------------------------------------------------------------


def profile(session: Session, wanted: str) -> dict[str, Any] | None:
    """Everything the company page shows, or None when no campus matches."""
    if not SLUG.fullmatch(wanted or ""):
        return None
    projects = session.scalars(
        select(Project)
        .options(
            selectinload(Project.blocks),
            selectinload(Project.events),
            selectinload(Project.risks),
        )
        .order_by(Project.id.asc())
    ).all()

    matched: list[tuple[Project, str]] = []
    keys: Counter[str] = Counter()
    for project in projects:
        for key, via in watchlist._project_keys(project):
            if key and slug(key) == wanted:
                matched.append((project, via))
                keys[key] += 1
                break
    if not matched:
        return None

    key = keys.most_common(1)[0][0]
    names = display_names(session)
    name = names.of_key(key) or key.title()
    ids = [p.id for p, _ in matched]
    sources = {
        row.id: row
        for row in session.scalars(
            select(Source).where(Source.project_id.in_(ids)).order_by(Source.id.asc())
        ).all()
    }
    since = dt.datetime.combine(
        dt.date.today() - dt.timedelta(days=feed.REPORT_WINDOW_DAYS), dt.time.min
    )
    signals = [
        s
        for project, via in matched
        for s in feed.signals_for(
            project,
            since=since,
            sources=sources,
            entry=name,
            via=via,
            company=names.company(project.company),
        )
        if s.confirmed
    ]
    rows = []
    for project, via in matched:
        open_risks = [r for r in project.risks if r.status == OPEN_RISK_STATUS]
        rows.append(
            {
                "id": project.id,
                "name": project.name,
                "company": project.company,
                "company_name": names.company(project.company),
                "customer": project.customer,
                "customer_name": names.customer(project.customer),
                "city": project.city,
                "county": project.county,
                "state": project.state,
                "phase": project.phase,
                "mw_planned": project.mw_planned,
                "mw_built": project.mw_built,
                "investment_usd": project.investment_usd,
                "expected_online": (
                    project.expected_online.isoformat() if project.expected_online else None
                ),
                "confidence": project.confidence,
                "via": via,
                "open_obstacles": len(open_risks),
            }
        )
    rows.sort(key=lambda r: (-(r["mw_planned"] or 0), r["name"].lower()))
    # Its own website comes from the campuses it builds, where its newsroom is
    # likeliest to be cited; a tenant's page cites the landlord's site instead.
    own = {p.id for p, via in matched if via == watchlist.VIA_OPERATOR}
    urls = [s.url for s in sources.values() if s.project_id in own] or [
        s.url for s in sources.values()
    ]
    return {
        "slug": wanted,
        "key": key,
        "name": name,
        "domain": company_logo.website(key, urls),
        "states": Counter(r["state"] for r in rows if r["state"]).most_common(),
        "phases": Counter(r["phase"] for r in rows if r["phase"]).most_common(),
        "projects": rows,
        "totals": {
            "projects": len(rows),
            "as_operator": sum(1 for r in rows if r["via"] == watchlist.VIA_OPERATOR),
            "as_tenant": sum(1 for r in rows if r["via"] != watchlist.VIA_OPERATOR),
            "mw_planned": sum(r["mw_planned"] or 0 for r in rows),
            "mw_built": sum(r["mw_built"] or 0 for r in rows),
            "open_obstacles": sum(r["open_obstacles"] for r in rows),
        },
        "updates": [s.as_json() for s in feed.rank(feed.fold(signals))][:20],
    }


__all__ = [
    "SLUG",
    "Names",
    "display_name",
    "display_names",
    "profile",
    "rostered_names",
]
