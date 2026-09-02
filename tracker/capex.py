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
import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.dedup import company_key, customer_key, is_undisclosed, is_vocabulary_block_key
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
    #: Dollars counted in `investment_usd` that no quote backs, but which the gate
    #: had no positive reason to doubt — it simply was not quoted. Counted, unlike
    #: `investment_excluded_usd`, because excluding a figure that is very likely
    #: correct understates the one number this table exists to state; disclosed,
    #: because a sum whose composition a reader cannot see is one they must trust.
    #: See `unquoted_investment_ids`.
    investment_unquoted_usd: int = 0
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
    #: Of `at_risk_projects`, those whose *only* open obstacles are 待确认 — a
    #: source reported them and no quote stood up. They are counted in the two
    #: figures above, deliberately: understating exposure is the worse error, and
    #: an obstacle the model found is information even before it is evidenced.
    #: Disclosed rather than hidden, on the same principle as `*_skipped` — a
    #: total whose composition a reader cannot see is one they have to trust.
    at_risk_unconfirmed: int = 0
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
    # Projects whose obstacles are *all* unconfirmed. A project with one quoted
    # obstacle and one 待确认 one is not in doubt, so it does not belong in a
    # disclosure about doubt.
    cited_risk_ids = {
        pid
        for (pid,) in session.execute(
            select(Risk.project_id)
            .where(Risk.status == OPEN_RISK_STATUS, Risk.unconfirmed.is_(None))
            .distinct()
        )
    }
    vague_risk_ids = open_risk_ids - cited_risk_ids
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
    unquoted_investment = unquoted_investment_ids(session)

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
            if money and project.id in unquoted_investment:
                bucket.investment_unquoted_usd += money

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
                if project.id in vague_risk_ids:
                    theirs.at_risk_unconfirmed += 1
            if project.id in slipped_ids and theirs is not bucket:
                theirs.slipped += 1

        if project.id in open_risk_ids:
            bucket.at_risk_projects += 1
            bucket.mw_at_risk += planned
            if project.id in vague_risk_ids:
                bucket.at_risk_unconfirmed += 1
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
    """Projects whose `investment_usd` the gate judged not to be this site's money.

    Reads back what ingest decided rather than recomputing it: the crawl gate
    records a reason in `Source.unconfirmed_reasons` when it refuses a figure, and
    recomputing the ratio here would accuse figures no gate ever demoted, since a
    merge can legitimately put a large number beside a small capacity.

    **Only `out_of_scale` is excluded**, which is the whole point of storing the
    reason. That is the programme-wide total — "OpenAI's $500 billion Stargate"
    quoted in an article about one campus — caught by the `$/MW` ceiling, and it
    genuinely is not this site's money. A figure that merely went unquoted is a
    different thing: it is very likely correct and nobody sourced it, and dropping
    it from the sum understates the one number this table exists to state. Those
    are reported by `unquoted_investment_ids` instead, disclosed separately.

    Sources written before migration 0013 have no reason recorded, and are treated
    as excluded — the conservative reading, and the behaviour this replaces.

    A project no source mentions at all is *not* in the set: there is no ingest
    decision to read back, so a hand-entered figure keeps counting.
    """
    return _demoted_investment(session)[0]


def unquoted_investment_ids(session: Session) -> set[int]:
    """Projects whose `investment_usd` is merely unquoted — counted, and disclosed.

    The other half of what `unconfirmed_investment_ids` used to return in one
    undifferentiated set. See its docstring for why the split matters.
    """
    return _demoted_investment(session)[1]


def _demoted_investment(session: Session) -> tuple[set[int], set[int]]:
    """(implausible, merely unquoted) project ids, each net of any confirmation."""
    out_of_scale: set[int] = set()
    unquoted: set[int] = set()
    confirmed: set[int] = set()
    for pid, fields, unconfirmed, reasons in session.execute(
        select(
            Source.project_id,
            Source.fields,
            Source.unconfirmed_fields,
            Source.unconfirmed_reasons,
        )
    ):
        if _names_field(fields, "investment_usd"):
            confirmed.add(pid)
        if not _names_field(unconfirmed, "investment_usd"):
            continue
        why = _reason_for(reasons, "investment_usd")
        # No reason recorded means a source older than migration 0013. Read as
        # out_of_scale, which is exactly the behaviour this split replaced: the
        # old code excluded every unconfirmed figure, so treating the unknown as
        # excluded cannot change any number that was already being reported.
        #
        # The two buckets are "excluded from the sum" (out_of_scale) and "counted
        # even though nothing quotes it" (unquoted), so the classification decides
        # a published number.
        #
        # `misread` is listed on the excluded side deliberately rather than by
        # falling through: a figure about a different building or a whole programme
        # must not be summed as this site's capex, which is exactly what
        # out_of_scale means. The bucket's *wording* is loose for it — a source
        # confirmed the figure perfectly well, just about another object — and that
        # is a reporting nicety, not a number.
        #
        # Spelled out as a membership test on both sides so that the next decision
        # reason added to `DECIDED_REASONS` has to be classified here on purpose.
        # Left as an else-branch it would join the excluded pile silently, and the
        # silence is the danger, not the destination.
        if why in {"no_quote", "quote_unverified", "quote_off_target"}:
            unquoted.add(pid)
        else:  # out_of_scale, misread, superseded, or a pre-0013 source with none
            out_of_scale.add(pid)
    return out_of_scale - confirmed, unquoted - confirmed - out_of_scale


def _reason_for(blob: str | None, name: str) -> str | None:
    """One field's refusal reason out of a source's JSON reason map."""
    if not blob:
        return None
    try:
        parsed = json.loads(blob)
    except (TypeError, ValueError):
        return None
    value = parsed.get(name) if isinstance(parsed, dict) else None
    return value if isinstance(value, str) else None


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


#: How a pair was raised, strongest evidence first. The order is the sort order of
#: the report and is a claim about how much each signal is worth:
#:
#: * an **exact** match is the same company and the same name, outright. Two rows
#:   agreeing on both of the fields a person reads first are not a resemblance;
#: * a **tranche** both rows hold under one derived key, where that key appears in
#:   no locality but theirs, is two readings of one building;
#: * a **party** in common means one company string names the *other's* operator —
#:   "OpenAI/Oracle" against "Oracle" — which is how one campus becomes four rows.
#:   Two spellings of one company is not this signal; see
#:   `dedup.shared_parties_across_companies`;
#: * an **identity** match is structural rather than textual: the two dedup keys
#:   describe the same place at different granularity. It is a strong statement
#:   about *place* and a weak one about *building*, which is why it sorts below the
#:   two signals that name a building and why it cannot carry a merge on its own.
#:   Measured on the live database it pairs NTT's Itasca campus with NTT's Chicago
#:   one, 31.7 km apart: same company, same county, two buildings;
#: * a **name** token in common is the weakest — it is a word, and words recur.
#:
#: **The order changed, and the reason is what a reader can act on.** `identity`
#: led this list on the strength of being structural, and the report therefore
#: opened with 31 of its 49 pairs — every one of them a class that no automated
#: path can settle, because `dupresolve.merge_blocked` refuses granularity alone.
#: The classes something can be done about now sort first.
EVIDENCE_ORDER: tuple[str, ...] = ("exact", "tranche", "party", "identity", "name")

#: What each class is called where a person reads it. One table, because the CLI and
#: the console must not name the same evidence differently — the console's whole
#: contract is that a judgement is made once and drawn twice
#: (`docs/architecture.md`), and a label is part of the judgement when the classes
#: are unequal enough that one carries a merge and another is a word.
EVIDENCE_LABELS: dict[str, str] = {
    "exact": "same name",
    "tranche": "same tranche",
    "party": "shared operator",
    "identity": "city vs county",
    "name": "name overlap",
}


def strongest_evidence(pairs: list[DuplicatePair], ids: list[int]) -> str:
    """The class a group is best described by: its strongest pair's strongest signal.

    Computed here rather than in the browser or twice, for the reason
    `docs/architecture.md` gives about the port: the moment two implementations of
    one rule exist they are free to disagree and nothing says when they start.
    """
    inside = [p for p in pairs if p.a_id in ids and p.b_id in ids and p.kinds]
    if not inside:
        return ""
    return min(inside, key=lambda p: p.rank).kinds[0]


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
    #: Operators both company strings name — "OpenAI/Oracle" against "Oracle".
    shared_parties: tuple[str, ...] = ()
    #: Name words that survive the generic and locality filters.
    shared_tokens: tuple[str, ...] = ()
    #: Dedup keys the two rows share once each is expanded to every granularity it
    #: could be written at, or the reason a cross-granularity match was declared.
    #: This is the signal the locality bucketing structurally cannot see, because
    #: the two rows are in different buckets by construction.
    shared_keys: tuple[str, ...] = ()
    #: Same company and same name, character for character once normalized. Its own
    #: field rather than something a reader infers from the two labels, because
    #: `dupresolve` trusts it for a merge and a rail has to read a value.
    exact: bool = False

    @property
    def kinds(self) -> tuple[str, ...]:
        """Which signals raised this pair, strongest first."""
        got = {
            "exact": self.exact,
            "identity": bool(self.shared_keys),
            "tranche": bool(self.shared_blocks),
            "party": bool(self.shared_parties),
            "name": bool(self.shared_tokens),
        }
        return tuple(k for k in EVIDENCE_ORDER if got[k])

    @property
    def rank(self) -> int:
        """Sort key: 0 is the strongest evidence available."""
        kinds = self.kinds
        return EVIDENCE_ORDER.index(kinds[0]) if kinds else len(EVIDENCE_ORDER)

    @property
    def why(self) -> str:
        """One line naming the evidence, for a reader deciding whether to merge."""
        parts: list[str] = []
        if self.exact:
            parts.append("same company and the same name")
        if self.shared_blocks:
            listed = ", ".join(self.shared_blocks[:3])
            parts.append(
                f"both hold tranche {listed}"
                + (f" (+{len(self.shared_blocks) - 3} more)" if len(self.shared_blocks) > 3 else "")
            )
        if self.shared_parties:
            parts.append(f"both name {', '.join(self.shared_parties[:3])}")
        if self.shared_keys:
            parts.append(f"same place at different granularity ({self.shared_keys[0]})")
        if self.shared_tokens:
            parts.append(f"both names carry {', '.join(sorted(self.shared_tokens)[:3])}")
        return "; ".join(parts) or "same locality"

    def as_json(self) -> dict[str, Any]:
        return {
            "a_id": self.a_id,
            "a": f"{self.a_company} — {self.a_name}",
            "b_id": self.b_id,
            "b": f"{self.b_company} — {self.b_name}",
            "locality": f"{self.locality}, {self.state}",
            "b_mw": self.b_mw,
            "evidence": list(self.kinds),
            "shared_keys": list(self.shared_keys),
            "why": self.why,
        }


def identifying_block_keys(projects) -> dict[int, set[str]]:
    """Per project, the tranche keys that could identify a *building*.

    Two filters, and the second is the one that matters.

    **Generic labels are dropped**, as they always were: `phase-1` is held by 26
    rows in 25 different towns, and pairing on it would pair every row in a city
    with every other.

    **A key that turns up in more than one locality is vocabulary, not identity.**
    `generic` is decided from the label's own words, so it cannot catch `existing`,
    `expansion`, `hyperscale` or `planned` — real words that name a kind of tranche
    and no particular one. Measured on the live database, `existing` alone paired
    Element Critical's Houston One with Switch's Houston campus: two unrelated
    operators, one shared word, and a false pair sitting above the two real ones.

    Rarity is measured across localities rather than across rows on purpose. A
    campus stored four times — Abilene was — has four rows holding `building-1`,
    and a count-based rule would throw the flagship case away. All four are in
    Abilene, so the locality test keeps it and still discards a key that shows up
    in Ashburn and Corsicana as well.

    **Rarity has moved out of this function**, to :func:`shared_identity_keys`,
    because "appears in one locality" and "appears in no locality but these two"
    are the same rule asked of one row and of a pair — and only the second can see
    a cross-granularity duplicate, which is two localities by construction. What
    stays here is the judgement that does not depend on a pair: whether the key
    names a building at all. `dedup.is_vocabulary_block_key` is the third filter
    the paragraph above asks for, since `generic` reads a label's own words and
    cannot know that `IAD3` is an airport.
    """
    per_project: dict[int, set[str]] = {}
    for project in projects:
        localities = {
            (project.city or "").strip().lower(),
            (project.county or "").strip().lower(),
        }
        per_project[project.id] = {
            b.block_key
            for b in (getattr(project, "blocks", ()) or ())
            if (not b.generic or b.parent)
            and not is_vocabulary_block_key(b.block_key, localities=localities)
        }
    return per_project


def block_key_localities(projects) -> dict[str, set[tuple[str, str]]]:
    """Which localities each tranche key appears in. The rarity denominator."""
    where: dict[str, set[tuple[str, str]]] = {}
    for project in projects:
        locality = (project.city or project.county or "").strip().lower()
        for block in getattr(project, "blocks", ()) or ():
            where.setdefault(block.block_key, set()).add((locality, project.state))
    return where


def shared_identity_keys(
    a, b, keys: dict[int, set[str]], where: dict[str, set[tuple[str, str]]]
) -> tuple[str, ...]:
    """Tranche keys two rows share that appear in no locality except their own.

    The rule the old one could not express. `identifying_block_keys` kept a key
    only when it appeared in exactly *one* locality, which is right for two rows in
    one town and is precisely backwards for the case that matters most: a campus
    stored once as a city and once as the county containing it holds its tranche
    key in two localities, so the evidence that would settle it was thrown away for
    being evidence. Measured on the live database, eight suspected pairs shared a
    real tranche key that this function reports and the old filter discarded —
    among them Stargate Abilene (#3) against Stargate Shackelford County (#182) on
    `county.shackelford`, and the IREN/Iris Energy Sweetwater rename.

    Asking it of a pair rather than of a row costs nothing and collapses two rules
    into one: when both rows sit in the same locality, "no locality but ours" *is*
    "exactly one locality", so pass one keeps the behaviour it had.

    The pair is also the only place a facility number can be judged. Two rows in
    one market holding `va-4` are one building; two rows in different markets
    holding `iad-3` share an airport. See `dedup.is_facility_number`.
    """
    from tracker.dedup import is_facility_number

    a_locality = ((a.city or a.county or "").strip().lower(), a.state)
    b_locality = ((b.city or b.county or "").strip().lower(), b.state)
    one_market = a_locality == b_locality
    return tuple(
        sorted(
            k
            for k in keys[a.id] & keys[b.id]
            if where.get(k, set()) <= {a_locality, b_locality}
            and (one_market or not is_facility_number(k))
        )
    )


def _shared_name_tokens(a, b) -> tuple[str, ...]:
    """Name words two rows have in common that identify a site rather than a place.

    Both rows' localities are dropped, not just one. Within a locality bucket that
    is the same thing, but a cross-granularity pair has two localities by
    construction — "Gainesville" against "Prince William County" — and keeping
    either row's town would let the town's own name look like identity.

    **The operator's name goes too, but only when both rows have the same one.**
    Every STACK project is called "STACK something", so on a pair that already
    shares an operator the word is tautological — it was the whole of the `name`
    evidence pairing `STACK Portland Expansion` with `STACK Infrastructure Hillsboro
    Campus`, two towns apart. Where the companies differ the word is doing real
    work, and dropping it unconditionally cost three genuine pairs: an operator's
    name is sometimes the *site's* name too. `Crusoe — Stargate Abilene` against
    `Stargate — Stargate Abilene` shares "stargate", which is one row's company and
    the other's campus; `Illinois Quantum and Microelectronics Park` is a project
    whose developer is named after it.
    """
    from tracker.dedup import company_key, distinctive_name_tokens

    places = " ".join(part for part in (a.city, a.county, b.city, b.county) if part)
    one_operator = company_key(a.company) == company_key(b.company)
    firms = a.company if one_operator else None
    return tuple(
        sorted(
            distinctive_name_tokens(a.name, locality=places, company=firms)
            & distinctive_name_tokens(b.name, locality=places, company=firms)
        )
    )


def suspected_duplicates(session: Session, *, include_parked: bool = False) -> list[DuplicatePair]:
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
    reported as the evidence for the pairs it already found. Which keys count is
    `identifying_block_keys`, and it is stricter than it was.

    **A pair an operator has ruled out is gone from here**, not merely marked. That
    is deliberate and it is the reason parking exists at all: `rollup` reads this
    function, so a false pair does not just clutter a report — it holds a real
    campus out of the buyer table. `include_parked=True` shows them anyway, which
    is what `tracker duplicates --parked` uses to let somebody review their own
    past decisions.
    """
    from tracker.dedup import exact_identity, shared_parties_across_companies
    from tracker.pairs import canonical, parked_keys

    projects = session.scalars(select(Project)).all()
    by_locality: dict[tuple[str, str], list[Project]] = {}
    for project in projects:
        locality = (project.city or project.county or "").strip().lower()
        if not locality:
            continue
        by_locality.setdefault((locality, project.state), []).append(project)

    keys = identifying_block_keys(projects)
    where = block_key_localities(projects)
    parked = set() if include_parked else parked_keys(session)

    def evidence(a: Project, b: Project) -> dict[str, Any]:
        """Every signal that holds for one pair.

        One function for all three passes. The second pass used to record `shared_keys`
        and nothing else, so a cross-granularity duplicate carried exactly one
        evidence class however much evidence existed — measured on the live
        database, 12 of those pairs shared a distinctive name token, 8 shared a real
        tranche key, and 6 were byte-identical in name and company. None of it
        reached the report, and `dupresolve.merge_blocked` refused all of them for
        having only granularity to go on.
        """
        return {
            "exact": exact_identity(a.name, a.company, b.name, b.company),
            "shared_blocks": shared_identity_keys(a, b, keys, where),
            "shared_parties": tuple(sorted(shared_parties_across_companies(a.company, b.company))),
            "shared_tokens": _shared_name_tokens(a, b),
        }

    pairs: list[DuplicatePair] = []
    for (locality, state), group in by_locality.items():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if canonical(a.id, b.id) in parked:
                    continue
                found = evidence(a, b)
                if not any(found.values()):
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
                        **found,
                    )
                )
    # --- The pairs the locality bucket structurally cannot see -----------------
    #
    # Everything above compares rows *within* one `(city or county, state)` bucket.
    # That is exactly what a cross-granularity duplicate is not: Hyperion is stored
    # four times as `richland parish`, `holly ridge`, `richland` and `richmond
    # parish`, so the four rows sit in four buckets and no amount of name or
    # tranche evidence is ever consulted. `tracker duplicates` found none of them.
    #
    # `dedup.all_keys` and `is_cross_granularity_match` already answer this, and
    # `upsert._find_duplicate_candidate` already calls them at ingest time — it
    # wrote "possible duplicate of project #284" into row #10's notes. The read
    # path simply never asked. So this is a second pass, unioned rather than
    # substituted: measured against the live database the structural pass alone
    # finds 259 pairs but LOSES 225 of the 230 the locality pass finds, because it
    # cannot see the same-locality/different-company case that made `capex` need
    # this in the first place.
    from tracker.dedup import all_keys, is_cross_granularity_match

    seen = {canonical(p.a_id, p.b_id) for p in pairs}
    expanded = {p.id: set(all_keys(p.company, p.city, p.county, p.state)) for p in projects}
    by_company: dict[tuple[str, str], list[Project]] = {}
    for project in projects:
        # Bucketed on the company/state prefix of the key rather than on locality,
        # which is the whole point — locality is the axis that disagrees.
        stem = next(iter(expanded[project.id]), "").split("|")[0]
        if stem:
            by_company.setdefault((stem, project.state), []).append(project)

    for group in by_company.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                key = canonical(a.id, b.id)
                if key in seen or key in parked:
                    continue
                overlap = tuple(sorted(expanded[a.id] & expanded[b.id]))
                # Both take *keys*. A row knowing only its city and one knowing
                # only its county never share a key, so the granularity test is
                # the only thing that connects them — it is what pairs #10
                # (`county:richland`) with #929 (`city:richland`).
                cross = any(
                    is_cross_granularity_match(one, other)
                    for one in expanded[a.id]
                    for other in expanded[b.id]
                )
                if not overlap and not cross:
                    continue
                seen.add(key)
                pairs.append(
                    DuplicatePair(
                        a_id=a.id,
                        a_company=a.company,
                        a_name=a.name,
                        b_id=b.id,
                        b_company=b.company,
                        b_name=b.name,
                        locality=a.city or a.county or "",
                        state=a.state,
                        b_mw=float(b.mw_planned or 0.0),
                        shared_keys=overlap or ("city and county granularity differ",),
                        # Everything the locality pass would have asked, asked here
                        # too. Granularity is what *raised* the pair; it is not all
                        # that is known about it, and recording it alone is what
                        # left 31 pairs on the live database with one evidence class
                        # and no route to a decision.
                        **evidence(a, b),
                    )
                )

    # --- The pairs neither key comparison can reach: one tranche, two localities ---
    #
    # Both passes above start from a *key*: pass one compares rows filed under one
    # locality, pass two rows whose dedup keys describe one place at two
    # granularities. A campus stored once as a city and once as a county whose names
    # do not match is in neither — `is_cross_granularity_match` needs the locality
    # names to agree once the "County" word is dropped, and "Abilene" is not
    # "Shackelford". So Stargate, stored as Crusoe's Abilene row and Oracle's
    # Shackelford County row, was invisible to the report while both rows carried
    # the tranche key `county.shackelford`.
    #
    # Starting from the tranche key instead reaches it, and `shared_identity_keys`
    # is what makes that safe: the key must appear in no locality but these two, it
    # must not be industry vocabulary, and across two markets it must not be a
    # facility number — which is `IAD3` held by two operators sixty kilometres
    # apart. Measured on the live database this pass finds nine pairs, seven of
    # them real, including Cipher's Stingray facility filed under both `andrews`
    # and `andrews county` and the IREN/Iris Energy Sweetwater rename.
    by_key: dict[str, list[Project]] = {}
    for project in projects:
        for block_key in keys[project.id]:
            by_key.setdefault(block_key, []).append(project)

    for group in by_key.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                key = canonical(a.id, b.id)
                if key in seen or key in parked or a.state != b.state:
                    continue
                found = evidence(a, b)
                if not found["shared_blocks"]:
                    continue
                seen.add(key)
                pairs.append(
                    DuplicatePair(
                        a_id=a.id,
                        a_company=a.company,
                        a_name=a.name,
                        b_id=b.id,
                        b_company=b.company,
                        b_name=b.name,
                        locality=a.city or a.county or "",
                        state=a.state,
                        b_mw=float(b.mw_planned or 0.0),
                        **found,
                    )
                )

    # Strongest evidence first, so the pair most worth merging is the one on
    # screen. `looks_like_the_same_site` decided the same things in the same order
    # and threw the reason away; nothing is detected differently here.
    return sorted(pairs, key=lambda p: (p.rank, p.a_id, p.b_id))


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
    "unquoted_investment_ids",
    "year_columns",
]
