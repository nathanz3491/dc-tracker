"""Closed vocabularies shared by the schema, the normalizer and the CLI.

Single source of truth for every enum-ish TEXT column. `models.py` builds CHECK
constraints from these tuples and `migrations/*.sql` spells the same values out
literally; `tests/test_db.py` asserts the two agree, so adding a value here
without migrating is caught by the drift test rather than at runtime.

Kept import-free on purpose — normalize.py, models.py and cli.py all depend on
this module, so it must not depend on any of them.
"""

from __future__ import annotations

from typing import Final, Literal

# --- project.phase ---------------------------------------------------------
# Ordered from least to most advanced. The order is load-bearing: upsert.py
# merges two sources by taking the furthest-along phase, and `paused` /
# `cancelled` are deliberately placed outside that progression because they are
# not "more advanced" than operational — they override it when asserted by a
# newer source.
PHASE_PROGRESSION: Final[tuple[str, ...]] = (
    "announced",
    "permitting",
    "construction",
    "operational",
)
PHASE_TERMINAL: Final[tuple[str, ...]] = ("paused", "cancelled")
PHASES: Final[tuple[str, ...]] = PHASE_PROGRESSION + PHASE_TERMINAL

Phase = Literal["announced", "permitting", "construction", "operational", "paused", "cancelled"]

#: Written when no source states a phase. `phase` is NOT NULL and the PRD has no
#: `unknown` member, so we default — but ingest paths must then OMIT `phase` from
#: `source.fields`, which makes confidence.py's coverage penalty fire and routes
#: the row to `tracker review` instead of silently asserting "announced".
DEFAULT_PHASE: Final[str] = "announced"

# --- risk.category ---------------------------------------------------------
#: What is obstructing a project. Grouped by what an analyst reads through to:
#: power, government, supply chain, capital, demand, and the site's neighbours.
#:
#: A closed vocabulary rather than free text because the whole point is to be able
#: to ask "how much planned capacity is blocked on transmission" across projects.
#: One sentence per project cannot answer that; these can.
RISK_CATEGORIES: Final[tuple[str, ...]] = (
    # Power
    "grid_capacity",  # the local grid cannot supply the load
    "transmission",  # substation or line construction lags the campus
    # Government
    "permitting",  # approvals outstanding or taking longer than planned
    "environmental",  # environmental review, air permit, emissions
    # Supply chain
    "equipment_supply",  # transformers, switchgear, chillers
    "chip_supply",  # accelerator allocation
    # Capital and demand
    "financing",  # funding not secured
    "offtake",  # no committed customer for the capacity
    # The site's neighbours
    "community_opposition",  # residents, litigation, referendum, noise
    "water",  # supply, discharge, or aquifer impact
    #: A human asserted an obstacle without saying which kind. Reachable only from
    #: `ingest manual` and the 0004 backfill of the old free-text `blocker` column
    #: — the extractor always classifies, because an unclassified risk cannot be
    #: aggregated and aggregation is the reason this table exists.
    "unclassified",
)

RiskCategory = Literal[
    "grid_capacity",
    "transmission",
    "permitting",
    "environmental",
    "equipment_supply",
    "chip_supply",
    "financing",
    "offtake",
    "community_opposition",
    "water",
    "unclassified",
]

# --- risk.severity ---------------------------------------------------------
#: Ordered least to most severe. **The order is load-bearing**: `project.blocker`
#: is derived as the summary of the most severe open risk, so reordering this tuple
#: silently changes which obstacle every project reports.
#:
#:   watch     — raised, but no stated effect on schedule or scope
#:   material  — a source says the schedule or the scope moved
#:   blocking  — work has stopped; should agree with phase paused/cancelled
RISK_SEVERITIES: Final[tuple[str, ...]] = ("watch", "material", "blocking")

RiskSeverity = Literal["watch", "material", "blocking"]

#: Written when a source names an obstacle but states no effect on the project.
#: The conservative direction on purpose: overstating severity turns a mention into
#: a blocker, and `blocker` is the field an operator acts on.
DEFAULT_RISK_SEVERITY: Final[str] = "watch"

# --- risk.status -----------------------------------------------------------
#: `open` counts toward `project.blocker`; the other two do not. This is what the
#: old free-text column could not express — `_resolve` returns the existing value
#: when a field has no claims, so a `blocker` string could be replaced but never
#: cleared, and a resolved obstacle sat on the row forever.
RISK_STATUSES: Final[tuple[str, ...]] = ("open", "resolved", "superseded")

RiskStatus = Literal["open", "resolved", "superseded"]

#: The one status that means "this is obstructing the project today".
OPEN_RISK_STATUS: Final[str] = "open"

# --- source.source_type ----------------------------------------------------
SOURCE_TYPES: Final[tuple[str, ...]] = (
    "iso_queue",
    "company_filing",
    "government_doc",
    "trade_press",
    "general_media",
    "manual",
)

SourceType = Literal[
    "iso_queue",
    "company_filing",
    "government_doc",
    "trade_press",
    "general_media",
    "manual",
]

# --- event.event_type ------------------------------------------------------
EVENT_TYPES: Final[tuple[str, ...]] = (
    "announced",
    "permit_filed",
    "groundbreaking",
    "energized",
    "first_customer",
    "delayed",
    "expanded",
)

EventType = Literal[
    "announced",
    "permit_filed",
    "groundbreaking",
    "energized",
    "first_customer",
    "delayed",
    "expanded",
]

# --- ingest_url.status -----------------------------------------------------
#: Terminal state of one URL in one crawl run. The PRD asks for a `fetch_error`
#: marker but `source.source_type` is a closed enum without one AND a `source`
#: row requires a project_id — on a fetch failure there is no project. So URL
#: outcomes live in their own table with their own vocabulary.
#: `discovered` comes first because it is the entry state: a feed surfaced the URL
#: and nothing has read it yet. Every other value is a terminal outcome.
URL_STATUSES: Final[tuple[str, ...]] = (
    "discovered",
    "ok",
    "fetch_error",
    "parse_error",
    "llm_error",
    "no_project",
    "skipped",
)

#: The one status that means "there is work to do here".
PENDING_URL_STATUS: Final[str] = "discovered"

# --- project fields --------------------------------------------------------
#: Canonical order for the 12 tracked PRD fields. Used to render `source.fields`
#: deterministically (so re-ingesting the same input produces byte-identical
#: rows) and to drive the export column order.
TRACKED_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "company",
    "customer",
    "city",
    "state",
    "mw_planned",
    "mw_built",
    "investment_usd",
    "phase",
    "first_announced",
    "expected_online",
    "blocker",
)

#: Every column an ingest path may write, in schema order. `notes` and `county`
#: are tracked separately from TRACKED_FIELDS because they are engineering
#: additions, not PRD-required facts, and must not count toward the
#: "9 of 12 fields populated" definition of done.
WRITABLE_FIELDS: Final[tuple[str, ...]] = (
    *TRACKED_FIELDS,
    "county",
    "country",
    "lat",
    "lon",
    "notes",
)


def severity_rank(severity: str) -> int:
    """Position in :data:`RISK_SEVERITIES`, or -1 for an unrecognized value.

    One definition shared by the extractor and the write path, so "the most severe
    open risk" cannot mean two different things in two places.
    """
    try:
        return RISK_SEVERITIES.index(severity)
    except ValueError:
        return -1


def sql_in(column: str, allowed: tuple[str, ...]) -> str:
    """Render a CHECK constraint body: ``phase IN ('announced', ...)``.

    Used by models.py so the Python vocabulary and the SQL constraint cannot
    drift within a single definition. The migration files spell the same lists
    out literally (SQL has no way to import), and the drift test compares them.
    """
    values = ", ".join(f"'{v}'" for v in allowed)
    return f"{column} IN ({values})"
