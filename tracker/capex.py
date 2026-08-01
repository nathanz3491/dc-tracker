"""Capacity and spend by the company that is actually buying it.

The database is keyed on the *site*: `(operator, locality, state)`. The question
an analyst is paid to answer is on a different axis — how much capacity does Meta
have in flight, whose pipeline lands next year, whose is most exposed to delay —
and those two grains do not coincide, because a large share of hyperscaler
capacity is built by wholesale developers and leased.

Nothing here is stored. It is derived from `project`, `event` and `risk` on every
call, for the same reason `tracks.py` derives rather than stores: a cached rollup
can drift out of agreement with the citations underneath it.

**Attribution, and why it is not just `project.customer`.** That column is
populated on about one project in ten, so grouping by it alone answers the
question for a tenth of the database and silently drops the rest. Three rules,
applied in order:

1. A **named tenant** is the buyer. `customer` when it identifies somebody —
   `dedup.customer_key` folds Meta/Facebook and AWS/Amazon together and returns
   nothing for a hedge like "a Fortune 100 technology company".
2. Otherwise, if the **operator is an end user**, it is buying its own capacity.
   Meta building a Meta campus needs no tenant named; `gaps.py` already reports
   `customer` as unmeasurable for exactly this reason. Who counts as an end user
   comes from `seed/edgar-companies.toml`, which already classifies each company
   as hyperscaler, neocloud or landlord — one list, not a second one here to drift.
3. Otherwise the capacity is **unattributed**: a landlord building without a
   disclosed tenant, or an operator we know nothing about. Reported as its own
   row rather than hidden, because "how much is being built for nobody we can
   name" is itself one of the more interesting numbers on the page.

Every figure is a floor, never a total. A project whose capacity nobody has cited
contributes zero MW, so a customer's real position is at least what is shown and
usually more.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.dedup import company_key, customer_key, is_undisclosed, looks_like_the_same_site
from tracker.models import Event, Project, Risk
from tracker.vocab import OPEN_RISK_STATUS, PHASE_TERMINAL, severity_rank

#: Attribution bucket for capacity with no identifiable buyer.
UNATTRIBUTED = "(no named customer)"

#: `kind` values in the company list that mean "buys capacity for its own use".
#: A landlord does not — it builds for whoever signs.
_END_USER_KINDS = frozenset({"hyperscaler", "neocloud"})

#: End users that file nothing with the SEC, so `edgar-companies.toml` cannot
#: know about them.
#:
#: That file answers "whose filings should we read", and using it alone to answer
#: "who buys capacity" quietly excludes every private company. Measured: OpenAI
#: with 3,200 MW and xAI with 2,000 MW were the second and third largest
#: unattributed operators in the database, both plainly buying for their own use.
#:
#: Kept deliberately short and only for companies that build or lease for their
#: own compute. A private *landlord* does not belong here — the test is who
#: consumes the capacity, not who is private.
_PRIVATE_END_USERS: dict[str, str] = {
    "openai": "OpenAI",
    "xai": "xAI",
    "anthropic": "Anthropic",
    "bytedance": "ByteDance",
    "tiktok": "ByteDance",
    "apple": "Apple",
    "nvidia": "NVIDIA",
    "tesla": "Tesla",
    "lambda": "Lambda",
    "crusoe": "Crusoe",
    "fluidstack": "Fluidstack",
    "core42": "Core42",
}


@lru_cache(maxsize=1)
def end_user_keys() -> dict[str, str]:
    """company_key -> display name, for operators that are their own customer.

    Read out of `seed/edgar-companies.toml` rather than hardcoded here, so the
    one place that says "Meta is an end user and Digital Realty is a landlord"
    also drives which filings get read. A missing or unreadable file is not a
    reason to fail a read-only report, so it degrades to rule 3 for everyone.
    """
    keys = dict(_PRIVATE_END_USERS)
    try:
        from tracker.ingest.edgar import load_companies

        companies, _phrases, _forms = load_companies()
    except Exception:
        return keys
    keys.update(
        {company_key(c.name): c.name for c in companies if c.kind.lower() in _END_USER_KINDS}
    )
    return keys


@dataclass
class Position:
    """One buyer's position across every project attributed to it."""

    name: str
    key: str
    projects: int = 0
    mw_planned: float = 0.0
    mw_built: float = 0.0
    investment_usd: int = 0
    #: Planned MW by the year the project is expected online. Only projects that
    #: cite both a capacity and a date appear here, so it is a floor within a floor.
    mw_by_year: dict[int, float] = field(default_factory=dict)
    #: Projects with at least one open obstacle, and the planned MW behind them.
    at_risk_projects: int = 0
    mw_at_risk: float = 0.0
    #: Projects whose `expected_online` has moved later, per `event.delayed`.
    slipped: int = 0
    #: Projects the operator is building for an unnamed tenant. Counted only in
    #: the UNATTRIBUTED row, where it is the whole story.
    undisclosed: int = 0
    #: Attributed via rule 2 rather than a named tenant — i.e. self-built. Worth
    #: showing, because a reader should know which half of a number is inference
    #: from "who owns it" versus a source naming the tenant.
    self_built: int = 0
    phases: dict[str, int] = field(default_factory=dict)

    @property
    def mw_unbuilt(self) -> float:
        """Planned but not yet energised — the pipeline, not the installed base."""
        return max(0.0, self.mw_planned - self.mw_built)


def attribute(project: Project) -> tuple[str, str, bool]:
    """Who is buying this project's capacity. Returns ``(name, key, self_built)``."""
    key = customer_key(project.customer)
    if key:
        return project.customer or key, key, False

    operator = company_key(project.company)
    end_users = end_user_keys()
    if operator in end_users:
        return end_users[operator], operator, True

    return UNATTRIBUTED, "", False


def rollup(session: Session, *, include_terminal: bool = False) -> list[Position]:
    """Every buyer's position, largest planned capacity first.

    Args:
        include_terminal: count cancelled and paused projects. Off by default —
            a cancelled campus is not part of anybody's forward pipeline, and
            leaving it in overstates the very number this table exists to give.
    """
    projects = session.scalars(select(Project)).all()

    open_risk_ids = {
        pid
        for (pid,) in session.execute(
            select(Risk.project_id).where(Risk.status == OPEN_RISK_STATUS).distinct()
        )
    }
    slipped_ids = {
        pid
        for (pid,) in session.execute(
            select(Event.project_id).where(Event.event_type == "delayed").distinct()
        )
    }

    positions: dict[str, Position] = {}
    for project in projects:
        if not include_terminal and project.phase in PHASE_TERMINAL:
            continue
        name, key, self_built = attribute(project)
        bucket = positions.setdefault(key or UNATTRIBUTED, Position(name=name, key=key))

        bucket.projects += 1
        bucket.self_built += int(self_built)
        bucket.undisclosed += int(is_undisclosed(project.customer))
        bucket.phases[project.phase] = bucket.phases.get(project.phase, 0) + 1

        planned = float(project.mw_planned or 0.0)
        bucket.mw_planned += planned
        bucket.mw_built += float(project.mw_built or 0.0)
        bucket.investment_usd += int(project.investment_usd or 0)

        if project.expected_online and planned:
            year = project.expected_online.year
            bucket.mw_by_year[year] = bucket.mw_by_year.get(year, 0.0) + planned

        if project.id in open_risk_ids:
            bucket.at_risk_projects += 1
            bucket.mw_at_risk += planned
        if project.id in slipped_ids:
            bucket.slipped += 1

    ordered = sorted(
        positions.values(),
        # Unattributed last regardless of size: it is a residual, not a buyer, and
        # it is often large enough to head the table and read as one.
        key=lambda p: (p.key == "", -p.mw_planned, -p.projects, p.name),
    )
    return ordered


def horizon(positions: list[Position]) -> list[int]:
    """Every year any position expects capacity online, ascending."""
    years: set[int] = set()
    for position in positions:
        years.update(position.mw_by_year)
    return sorted(years)


def blocking_risk(session: Session, key: str) -> str | None:
    """The most severe open obstacle across one buyer's projects."""
    rows = session.execute(
        select(Risk.category, Risk.severity, Project.company, Project.customer)
        .join(Project, Risk.project_id == Project.id)
        .where(Risk.status == OPEN_RISK_STATUS)
    ).all()
    mine = [
        (category, severity)
        for category, severity, company, customer in rows
        if (customer_key(customer) or company_key(company)) == key
    ]
    if not mine:
        return None
    category, severity = max(mine, key=lambda r: severity_rank(r[1]))
    return f"{category}/{severity}"


def suspect_attributions(session: Session) -> list[tuple[int, str, str]]:
    """Projects whose `customer` names somebody we track as an *operator*.

    Returns ``(project_id, operator, customer)``.

    A wholesale developer is nobody's tenant. Observed live: one project carried
    `customer = "Aligned Data Centers"`, and Aligned builds its own campuses
    elsewhere in this same table — so either the extractor read the developer's
    name out of a sentence about who is building, or two companies were conflated.

    Surfaced rather than corrected, for the reason `dedup.py` gives about
    cross-granularity duplicates: a wrong merge is invisible and a flagged
    ambiguity is not. A landlord genuinely can lease from another landlord, so
    this is a question for a human and not a rule.
    """
    projects = session.scalars(select(Project)).all()
    operators = {company_key(p.company) for p in projects if p.company}
    end_users = end_user_keys()

    out: list[tuple[int, str, str]] = []
    for project in projects:
        key = customer_key(project.customer)
        if not key or key in end_users:
            continue
        if key == company_key(project.company):
            continue  # the operator is its own tenant, which is ordinary
        if key in operators:
            out.append((project.id, project.company, project.customer or ""))
    return out


@dataclass(frozen=True)
class DuplicatePair:
    """Two rows that look like one campus. Plain values, not ORM instances.

    Detached ORM objects raise the moment a caller touches an attribute after the
    session closes, and every caller of this is a report that renders later.
    """

    a_id: int
    a_company: str
    a_name: str
    b_id: int
    b_company: str
    b_name: str
    locality: str
    state: str
    #: Planned MW on the second row — what stops being double-counted if merged.
    b_mw: float


def suspected_duplicates(session: Session) -> list[DuplicatePair]:
    """Pairs of rows in one locality that are probably one campus.

    **Why this warning belongs on the capex table specifically.** A duplicate is
    mildly annoying in a site-keyed listing — two rows where there should be one.
    Grouping by end customer turns it into a wrong number: the Abilene Stargate
    campus is stored four times, once per company attached to it, so 1.2 GW is
    counted four times against OpenAI. Measured across the database, 15 such pairs
    hold 11,135 MW of double-counted capacity.

    Flagged and never merged, per `dedup.looks_like_the_same_site`.
    """
    projects = session.scalars(select(Project)).all()
    by_locality: dict[tuple[str, str], list[Project]] = {}
    for project in projects:
        locality = (project.city or project.county or "").strip().lower()
        if not locality:
            continue
        by_locality.setdefault((locality, project.state), []).append(project)

    pairs: list[DuplicatePair] = []
    for (locality, state), group in by_locality.items():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if looks_like_the_same_site(a.name, a.company, b.name, b.company, locality=locality):
                    pairs.append(
                        DuplicatePair(
                            a_id=a.id,
                            a_company=a.company,
                            a_name=a.name,
                            b_id=b.id,
                            b_company=b.company,
                            b_name=b.name,
                            locality=a.city or a.county or locality,
                            state=state,
                            b_mw=float(b.mw_planned or 0.0),
                        )
                    )
    return pairs


def double_counted_mw(pairs: list[DuplicatePair]) -> float:
    """Planned MW that would stop being counted twice if each pair were merged.

    Counts the second row of each pair once, however many pairs it appears in —
    four rows for one campus make six pairs but only three redundant rows.
    """
    seen: set[int] = set()
    total = 0.0
    for pair in pairs:
        if pair.b_id in seen:
            continue
        seen.add(pair.b_id)
        total += pair.b_mw
    return total


def coverage(session: Session) -> dict[str, float]:
    """How much of the database this view can actually speak for.

    The honest companion to the table: a rollup that silently covers a third of
    the projects looks authoritative and is not.
    """
    projects = session.scalars(select(Project)).all()
    total = len(projects) or 1
    named = sum(1 for p in projects if customer_key(p.customer))
    self_built = sum(
        1
        for p in projects
        if not customer_key(p.customer) and company_key(p.company) in end_user_keys()
    )
    with_mw = sum(1 for p in projects if p.mw_planned)
    with_date = sum(1 for p in projects if p.expected_online and p.mw_planned)
    return {
        "projects": float(total),
        "named_tenant_pct": named / total * 100,
        "self_built_pct": self_built / total * 100,
        "attributed_pct": (named + self_built) / total * 100,
        "with_capacity_pct": with_mw / total * 100,
        "in_timeline_pct": with_date / total * 100,
    }


def as_of() -> _dt.date:
    from tracker.models import utcnow

    return utcnow().date()


__all__ = [
    "UNATTRIBUTED",
    "Position",
    "as_of",
    "attribute",
    "blocking_risk",
    "coverage",
    "double_counted_mw",
    "end_user_keys",
    "horizon",
    "rollup",
    "suspect_attributions",
    "suspected_duplicates",
]
