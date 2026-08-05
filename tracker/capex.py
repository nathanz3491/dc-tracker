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
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.dedup import company_key, customer_key, is_undisclosed, looks_like_the_same_site
from tracker.models import Event, Project, Risk, Source
from tracker.vocab import (
    BLOCK_LIVE,
    BLOCK_TERMINAL,
    OPEN_RISK_STATUS,
    PHASE_TERMINAL,
    severity_rank,
)

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
    #: Dollars a source asserted but none confirmed with a quote — most often a
    #: programme-wide total ("OpenAI's $500 billion Stargate") quoted in an article
    #: about one campus and demoted at ingest. Disclosed beside the sum, never
    #: inside it. See `unconfirmed_investment_ids`.
    investment_excluded_usd: int = 0
    #: What the rows set aside as suspected duplicates would have added. The rollup
    #: counts one representative per suspected campus (`suspected_duplicates`);
    #: the others are skipped, never merged, and what they held is disclosed here —
    #: on the bucket their own attribution would have fed, so an Oracle row skipped
    #: in favour of an OpenAI one shows up under Oracle.
    duplicate_rows_skipped: int = 0
    mw_duplicate_skipped: float = 0.0
    investment_duplicate_skipped_usd: int = 0
    #: The rows behind the numbers, ids only — counted rows first, set-aside rows
    #: in their own list. This is what lets a reader click a buyer's `sites`
    #: figure and see the actual campuses, instead of trusting an aggregate. The
    #: page looks the ids up in the project payload it already has, the same
    #: discipline `suspected_duplicates` groups follow.
    project_ids: list[int] = field(default_factory=list)
    duplicate_skipped_ids: list[int] = field(default_factory=list)
    #: Planned MW by the year the project is expected online. Only projects that
    #: cite both a capacity and a date appear here, so it is a floor within a floor.
    mw_by_year: dict[int, float] = field(default_factory=dict)
    #: The same capacity bucketed by calendar quarter, "2026Q1".
    #:
    #: Worth having and worth distrusting in equal measure. "Whose pipeline lands
    #: next quarter" is the question this table exists to answer, and a year
    #: column cannot answer it. But an `expected_online` is very often a month or
    #: a year in the source — "the second half of 2026" normalises to a date, and
    #: that date then looks like a quarter it was never meant to name. Read the
    #: quarters as a shape and the years as the number.
    mw_by_quarter: dict[str, float] = field(default_factory=dict)
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


@dataclass(frozen=True)
class BlockShare:
    """One buyer's slice of one campus, taken from a tranche that names them."""

    name: str
    key: str
    mw_planned: float
    mw_built: float
    #: The tranche's own date, not the campus's. This is most of the value: a
    #: campus with one date cannot say that 60 MW landed last year and 378 MW
    #: lands next.
    online: _dt.date | None


def block_shares(project: Project) -> tuple[list[BlockShare], float]:
    """Per-tranche capacity by buyer, and the campus capacity left over.

    This is why `customer` sits on a block at all. Attributing a whole campus to
    one buyer was adequate when a campus had one; Lake Mariner has 378 MW being
    built for Fluidstack beside 60 MW already serving Core42, and putting all 750
    against whichever name reached `project.customer` first is simply wrong.

    Only confirmed capacities are shared out, for the same reason `rollup` will not
    sum an unquoted figure: a number nobody stated must not become a buyer's
    position. Everything not accounted for by a tranche stays with the campus and is
    attributed the old way, so the total is conserved rather than replaced.

    Megawatts are split and money is not. A tranche states its capacity often and
    its share of the investment almost never, so splitting the money would mean
    inventing a ratio — which is the sort of quiet fabrication this codebase spends
    most of its effort refusing.
    """
    from tracker import blocks as blocks_mod

    blocks = list(getattr(project, "blocks", ()) or ())
    if not blocks:
        return [], float(project.mw_planned or 0.0)

    by_key: dict[str, dict[str, Any]] = {}
    for block in blocks_mod.placeable(blocks):
        if block.mw is None or not blocks_mod.mw_is_confirmed(block):
            continue
        if block.status in PHASE_TERMINAL or block.status in BLOCK_TERMINAL:
            continue
        key = customer_key(block.customer)
        if not key:
            continue
        entry = by_key.setdefault(
            key, {"name": block.customer or key, "planned": 0.0, "built": 0.0, "online": None}
        )
        entry["planned"] += float(block.mw)
        if block.status in BLOCK_LIVE:
            entry["built"] += float(block.mw)
        when = block.energized_on or block.expected_online
        # Earliest tranche date, so a buyer's capacity is booked when it first
        # arrives rather than when the last building finishes.
        if when and (entry["online"] is None or when < entry["online"]):
            entry["online"] = when

    shares = [
        BlockShare(
            name=entry["name"],
            key=key,
            mw_planned=entry["planned"],
            mw_built=entry["built"],
            online=entry["online"],
        )
        for key, entry in by_key.items()
    ]
    claimed = sum(s.mw_planned for s in shares)
    return shares, max(0.0, float(project.mw_planned or 0.0) - claimed)


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

    # One representative per suspected campus. A duplicate is merely untidy in a
    # site-keyed listing; grouped by buyer it becomes a wrong number, because every
    # extra row adds its full capacity again — Abilene was stored four times and
    # 1.2 GW counted four times. Rows are skipped, never merged (`tracker merge`
    # stays a human decision), and everything skipped lands in the `*_skipped`
    # disclosure fields. The representative is the row a merge would most likely
    # keep: a named tenant first, then the largest capacity, then the oldest id.
    # Taking per-field maxima across the group instead was rejected — it invents a
    # synthetic row no citation backs.
    eligible = {p.id: p for p in projects if include_terminal or p.phase not in PHASE_TERMINAL}
    skip_ids: set[int] = set()
    for group in duplicate_groups(suspected_duplicates(session)):
        members = [eligible[pid] for pid in group if pid in eligible]
        if len(members) < 2:
            continue
        representative = max(
            members,
            key=lambda p: (bool(customer_key(p.customer)), float(p.mw_planned or 0.0), -p.id),
        )
        skip_ids.update(p.id for p in members if p.id != representative.id)

    demoted_investment = unconfirmed_investment_ids(session)

    positions: dict[str, Position] = {}
    for project in projects:
        if not include_terminal and project.phase in PHASE_TERMINAL:
            continue
        name, key, self_built = attribute(project)
        bucket = positions.setdefault(key or UNATTRIBUTED, Position(name=name, key=key))

        if project.id in skip_ids:
            # Another row already speaks for this campus. Nothing else about the
            # row is counted — not its phases, its year, its tranches or its risk —
            # because all of it describes the same building the representative
            # already described.
            bucket.duplicate_rows_skipped += 1
            bucket.mw_duplicate_skipped += float(project.mw_planned or 0.0)
            bucket.investment_duplicate_skipped_usd += int(project.investment_usd or 0)
            bucket.duplicate_skipped_ids.append(project.id)
            continue

        bucket.projects += 1
        bucket.project_ids.append(project.id)
        bucket.self_built += int(self_built)
        bucket.undisclosed += int(is_undisclosed(project.customer))
        bucket.phases[project.phase] = bucket.phases.get(project.phase, 0) + 1

        # Capacity a tranche assigns to a named buyer goes to that buyer; the rest of
        # the campus stays here. A project with no blocks has no shares, so `planned`
        # is its whole capacity and this is exactly the previous behaviour.
        shares, planned = block_shares(project)
        bucket.mw_planned += planned
        # Built megawatts are only reassigned when a tranche accounts for them.
        # Otherwise `mw_built` would drop below `mw_planned` for reasons that have
        # nothing to do with what is built.
        claimed_built = sum(s.mw_built for s in shares)
        bucket.mw_built += max(0.0, float(project.mw_built or 0.0) - claimed_built)
        money = int(project.investment_usd or 0)
        if money and project.id in demoted_investment:
            # Asserted by a source, confirmed by none — read back from what the
            # ingest gate decided rather than re-judging the figure here.
            bucket.investment_excluded_usd += money
        else:
            bucket.investment_usd += money

        if project.expected_online and planned:
            year = project.expected_online.year
            bucket.mw_by_year[year] = bucket.mw_by_year.get(year, 0.0) + planned
            quarter = f"{year}Q{(project.expected_online.month - 1) // 3 + 1}"
            bucket.mw_by_quarter[quarter] = bucket.mw_by_quarter.get(quarter, 0.0) + planned

        for share in shares:
            # A buyer with a tranche here genuinely holds a position at this campus,
            # so it counts as one of their projects — which means the `projects`
            # column can now exceed the row count of the database. That is the
            # honest reading: two buyers at one campus is two positions.
            theirs = positions.setdefault(share.key, Position(name=share.name, key=share.key))
            if theirs is not bucket:
                theirs.projects += 1
                theirs.project_ids.append(project.id)
                theirs.phases[project.phase] = theirs.phases.get(project.phase, 0) + 1
            theirs.mw_planned += share.mw_planned
            theirs.mw_built += share.mw_built
            if share.online and share.mw_planned:
                year = share.online.year
                theirs.mw_by_year[year] = theirs.mw_by_year.get(year, 0.0) + share.mw_planned
                quarter = f"{year}Q{(share.online.month - 1) // 3 + 1}"
                theirs.mw_by_quarter[quarter] = (
                    theirs.mw_by_quarter.get(quarter, 0.0) + share.mw_planned
                )
            if project.id in open_risk_ids and theirs is not bucket:
                theirs.at_risk_projects += 1
                theirs.mw_at_risk += share.mw_planned
            if project.id in slipped_ids and theirs is not bucket:
                theirs.slipped += 1

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


def quarters(positions: list[Position]) -> list[str]:
    """Every quarter any position expects capacity online, ascending."""
    seen: set[str] = set()
    for position in positions:
        seen.update(position.mw_by_quarter)
    return sorted(seen)


#: The most bucket columns any surface renders. Six covers the live 2026-2030 span
#: with room to grow; wider pushes the risk columns off the page.
MAX_YEAR_COLUMNS = 6


def year_columns(
    positions: list[Position], *, start: int, limit: int = MAX_YEAR_COLUMNS
) -> list[int]:
    """The year grid: continuous from the first dated year to the last.

    `horizon` reports only the years that carry data, and a table built from that
    silently skips a year — 2028 and 2030 render side by side and 2029 vanishes,
    which reads as "nothing lands in 2029" when the truth is "nothing is *dated*
    2029". An empty column is the honest rendering of a gap.

    Years before `start` (the current year) are dropped rather than gridded: an
    expected-online in the past is a data-quality signal, not a pipeline. Computed
    here rather than in each surface, so the CLI, the dataset payload and the
    browser cannot disagree about which years exist.
    """
    years = [year for year in horizon(positions) if year >= start]
    if not years:
        return []
    return list(range(years[0], years[-1] + 1))[:limit]


def quarter_columns(
    positions: list[Position], *, start: str, limit: int = MAX_YEAR_COLUMNS
) -> list[str]:
    """Quarter columns — data-bearing quarters only, deliberately not continuous.

    The live span gridded by quarter is ~18 columns, and truncating a continuous
    grid at `limit` would spend the whole width on empty quarters. The quarter
    view is read as a shape rather than a schedule (see `Position.mw_by_quarter`),
    and a shape survives gaps; the year view is the number, so it gets the
    continuous treatment in `year_columns`.
    """
    return [quarter for quarter in quarters(positions) if quarter >= start][:limit]


def unconfirmed_investment_ids(session: Session) -> set[int]:
    """Projects whose `investment_usd` a source asserted and none confirmed.

    Reads back what ingest decided, the way `webui.dataset._unconfirmed_because`
    does: the crawl gate lists a figure in `Source.unconfirmed_fields` when no
    verified quote backs it or when it fails the `MAX_USD_PER_MW` plausibility
    ceiling — the signature of a programme-wide total quoted in an article about
    one campus. Recomputing that ratio here instead would accuse figures no gate
    ever demoted, since a merge can legitimately put a large number beside a
    small capacity.

    A project no source mentions at all is *not* in the set: there is no ingest
    decision to read back, so a hand-entered figure keeps counting.
    """
    demoted: set[int] = set()
    confirmed: set[int] = set()
    for pid, fields, unconfirmed in session.execute(
        select(Source.project_id, Source.fields, Source.unconfirmed_fields)
    ):
        if _names_field(fields, "investment_usd"):
            confirmed.add(pid)
        if _names_field(unconfirmed, "investment_usd"):
            demoted.add(pid)
    return demoted - confirmed


def _names_field(comma_list: str | None, name: str) -> bool:
    """Whether a comma-joined field list names `name` exactly."""
    return name in {token.strip() for token in (comma_list or "").split(",")}


def date_precision(session: Session) -> dict[str, float]:
    """How much of `expected_online` is precise enough to bucket by quarter.

    The honest companion to the quarter columns. A date normalised from "2026"
    lands on the 1st of January and is indistinguishable, downstream, from one a
    source pinned to a day — so the quarter view would silently pile a year's
    worth of vagueness into Q1. Counting the January-1st and month-start dates is
    the closest available measure of how much of that is happening.
    """
    projects = session.scalars(select(Project)).all()
    dated = [p for p in projects if p.expected_online and p.mw_planned]
    jan1 = sum(1 for p in dated if p.expected_online.month == 1 and p.expected_online.day == 1)
    month_start = sum(1 for p in dated if p.expected_online.day == 1)
    total = len(dated) or 1
    return {
        "dated": float(len(dated)),
        "projects": float(len(projects)),
        "year_only_pct": jan1 / total * 100,
        "month_start_pct": month_start / total * 100,
    }


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
    #: Tranches both rows hold under the same key. Much harder evidence than a name
    #: resemblance: `block_key` is derived, so two rows carrying `stingray` are two
    #: readings of one building rather than two similarly-named campuses. Three rows
    #: in Andrews, TX each ended up holding the same 70 MW AWS tranche.
    shared_blocks: tuple[str, ...] = ()


def suspected_duplicates(session: Session) -> list[DuplicatePair]:
    """Pairs of rows in one locality that are probably one campus.

    **Why this warning belongs on the capex table specifically.** A duplicate is
    mildly annoying in a site-keyed listing — two rows where there should be one.
    Grouping by end customer used to turn it into a wrong number: the Abilene
    Stargate campus was stored four times, once per company attached to it, and
    1.2 GW was counted four times against OpenAI. `rollup` now counts one row per
    suspected group and sets the others aside in the `*_skipped` disclosure
    fields, so the wrong number is prevented rather than merely flagged — but the
    rows are still there, and `tracker merge` remains the only real repair.

    Flagged and never merged, per `dedup.looks_like_the_same_site`.

    **Blocks are a second, stronger signal.** Two rows holding the same derived
    `block_key` are two readings of one building, not two campuses whose names
    happen to resemble each other — and since a block's megawatts are summed, an
    unmerged pair now double-counts at the tranche grain as well as the campus one.
    So a shared tranche both raises a pair the name test would have missed and is
    reported as the evidence for the pairs it already found.
    """
    projects = session.scalars(select(Project)).all()
    by_locality: dict[tuple[str, str], list[Project]] = {}
    for project in projects:
        locality = (project.city or project.county or "").strip().lower()
        if not locality:
            continue
        by_locality.setdefault((locality, project.state), []).append(project)

    # A generic key like `phase-1` is shared by half the database and would pair
    # every row in a city with every other, so only a key that names something is
    # kept. `generic` is read off the row rather than re-derived from the key: it is
    # what `block_key` decided at write time, and a second derivation could disagree.
    keys: dict[int, set[str]] = {
        p.id: {b.block_key for b in (getattr(p, "blocks", ()) or ()) if not b.generic or b.parent}
        for p in projects
    }

    pairs: list[DuplicatePair] = []
    for (locality, state), group in by_locality.items():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                shared = tuple(sorted(keys[a.id] & keys[b.id]))
                same_site = looks_like_the_same_site(
                    a.name, a.company, b.name, b.company, locality=locality
                )
                if not (same_site or shared):
                    continue
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
                        shared_blocks=shared,
                    )
                )
    return pairs


def duplicate_groups(pairs: list[DuplicatePair]) -> list[list[int]]:
    """Collapse overlapping pairs into one group per campus, largest first.

    Four rows for one site produce six pairs, and an operator wants one decision
    rather than six. Pairs sharing an id are transitively the same building, so
    they are unioned — which is also what makes the group safe to hand to
    `tracker merge`, an operation that takes one survivor and any number of rows
    to fold into it.
    """
    groups: list[set[int]] = []
    for pair in pairs:
        touching = [g for g in groups if pair.a_id in g or pair.b_id in g]
        merged = {pair.a_id, pair.b_id}
        for group in touching:
            merged |= group
            groups.remove(group)
        groups.append(merged)
    return sorted((sorted(g) for g in groups), key=lambda g: (-len(g), g))


def double_counted_mw(pairs: list[DuplicatePair]) -> float:
    """Planned MW stored redundantly — the payoff of merging each pair.

    This measures duplicate *storage*, not what the capex table over-counts:
    `rollup` skips the non-representative rows itself, and the table's own
    exclusions are the `*_skipped` fields on `Position`. The two numbers differ
    by design — this one counts the second row of every pair whatever its phase;
    the skip tally counts live rows a representative displaced.

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
    "MAX_YEAR_COLUMNS",
    "UNATTRIBUTED",
    "Position",
    "as_of",
    "attribute",
    "blocking_risk",
    "coverage",
    "date_precision",
    "double_counted_mw",
    "duplicate_groups",
    "end_user_keys",
    "horizon",
    "quarter_columns",
    "quarters",
    "rollup",
    "suspect_attributions",
    "suspected_duplicates",
    "unconfirmed_investment_ids",
    "year_columns",
]
