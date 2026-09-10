"""Closed vocabularies shared by the schema, the normalizer and the CLI.

Single source of truth for every enum-ish TEXT column. `models.py` builds CHECK
constraints from these tuples and `migrations/*.sql` spells the same values out
literally; `tests/test_db.py` asserts the two agree, so adding a value here
without migrating is caught by the drift test rather than at runtime.

Kept import-free on purpose — normalize.py, models.py and the cli package all depend on
this module, so it must not depend on any of them.
"""

from __future__ import annotations

from typing import Any, Final, Literal

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
#: `PHASE_TERMINAL` is read as membership by three more: `capex` excludes it from the
#: totals, `blocks.furthest_status` gives it the same override one level down, and
#: `normalize._PHASE_FALLBACK` resolves a phrase naming two phases to it.
PHASE_TERMINAL: Final[tuple[str, ...]] = ("paused", "cancelled")
PHASES: Final[tuple[str, ...]] = PHASE_PROGRESSION + PHASE_TERMINAL

Phase = Literal["announced", "permitting", "construction", "operational", "paused", "cancelled"]

#: A *capacity block's* own ladder — one tranche of a campus, not the campus.
#:
#: Separate from `PHASE_PROGRESSION` because it has to make two distinctions the
#: project enum cannot, and those two distinctions are the whole reason blocks
#: exist:
#:
#: * `shell_complete` — the building is up and there is no power. `tracks.py` calls
#:   a finished shell waiting on a substation the most informative signal in the
#:   dataset, and today it has nowhere to live at all.
#: * `energized` vs `serving` — power on, versus a customer actually running on it.
#:   A campus can be energised for months before first revenue, and in the AI era
#:   it can be pre-leased years before it is energised.
#:
#: Measured on the live database, 28 projects are partly built, 15 are
#: `construction` with megawatts already live and 12 have power energised while
#: construction is mid-track. One enum per campus cannot say any of that; one enum
#: per *block* can.
BLOCK_PROGRESSION: Final[tuple[str, ...]] = (
    "planned",
    "permitting",
    "under_construction",
    "shell_complete",
    "energized",
    "serving",
)
#: Outside the progression, for the same reason `PHASE_TERMINAL` is: a cancelled
#: tranche is not further along than a live one, it overrides.
BLOCK_TERMINAL: Final[tuple[str, ...]] = ("paused", "cancelled")
BLOCK_STATUSES: Final[tuple[str, ...]] = BLOCK_PROGRESSION + BLOCK_TERMINAL

DEFAULT_BLOCK_STATUS: Final[str] = "planned"

#: Block status -> the project `phase` it implies, for the rollup. Total over
#: `BLOCK_STATUSES`, and its range is a subset of `PHASES`; both are asserted in
#: `tests/test_blocks.py`. Two statuses collapse onto `construction` and two onto
#: `operational` — that loss of detail going *up* to the project row is precisely
#: what the block row is now keeping.
BLOCK_STATUS_TO_PHASE: Final[dict[str, str]] = {
    "planned": "announced",
    "permitting": "permitting",
    "under_construction": "construction",
    "shell_complete": "construction",
    "energized": "operational",
    "serving": "operational",
    "paused": "paused",
    "cancelled": "cancelled",
}

#: Block statuses that mean megawatts are actually delivering power. This is what
#: `mw_built` rolls up from.
BLOCK_LIVE: Final[frozenset[str]] = frozenset({"energized", "serving"})

#: Written when no source states a phase. `phase` is NOT NULL and the PRD has no
#: `unknown` member, so we default — but ingest paths must then OMIT `phase` from
#: `source.fields`, which makes confidence.py's coverage penalty fire and routes
#: the row to `tracker review` instead of silently asserting "announced".
DEFAULT_PHASE: Final[str] = "announced"

# --- project_party.role ----------------------------------------------------
#: What a company *does* on a site. A closed vocabulary for the reason
#: `RISK_CATEGORIES` is one: the point of the table is to be able to ask "how much
#: capacity does this party build for somebody else", and free text cannot answer
#: it.
#:
#: Ordered from the party furthest from the compute to the party closest to it,
#: which is also the order `parties.derive_company` falls back through — a site's
#: `company` has always meant "who runs it", so `operator` answers first and the
#: parties that merely built or own it answer only when nobody named an operator.
#:
#: Why six and not three. `developer`/`owner`/`operator` is the split the
#: extraction prompt was collapsing ("who builds AND operates the site"), and
#: `customer` already existed as its own column. The last two are here because
#: they arrive whether or not there is a role for them:
#:
#: * `utility` — Richland Parish is in the live database twice, as Meta and as
#:   Entergy Louisiana. The prompt carries a standing instruction that `company`
#:   is "NOT the electric utility" precisely because utilities keep turning up in
#:   the sentence that names the site, and a filing by the utility reads as though
#:   the utility runs the campus. A role they belong in beats an exclusion rule
#:   that has already failed.
#: * `contractor` — same shape, same standing instruction ("NOT the general
#:   contractor"), and the general contractor is often the only party a
#:   groundbreaking story names.
#:
#: Neither buys capacity, so `capex.attribute` must never attribute to them; that
#: is asserted rather than remembered. See `PARTY_ROLES_BUYING`.
PARTY_ROLES: Final[tuple[str, ...]] = (
    "developer",  # builds it, and may hand it over
    "owner",  # holds the asset or the land
    "operator",  # runs it — what `project.company` has always meant
    "customer",  # occupies it and buys the compute
    "utility",  # sells it power
    "contractor",  # builds it under contract to somebody else
)

PartyRole = Literal[
    "developer",
    "owner",
    "operator",
    "customer",
    "utility",
    "contractor",
]

#: Roles that can hold a position in the capex table. A utility connects capacity
#: and a contractor pours the concrete; neither buys it, which is the same
#: distinction `seed/edgar-companies.toml`'s `kind` column already draws for
#: whether an operator is its own end user.
PARTY_ROLES_BUYING: Final[frozenset[str]] = frozenset({"customer", "operator", "owner"})

#: The role `parties.derive_company` reads first, then in order. `project.company`
#: is NOT NULL, so this ladder must be total over any non-empty party set that
#: contains one of these — `derive_company` falls back to the stored value
#: otherwise rather than inventing one.
COMPANY_ROLE_ORDER: Final[tuple[str, ...]] = ("operator", "developer", "owner")

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
#: Milestones. Ordered as they occur, but NOT a progression: these belong to five
#: independent tracks (see `tracker.tracks`), and a project can reach a late
#: construction milestone while an early power one is still outstanding.
#:
#: `land_acquired`, `permit_approved`, `interconnection_agreement`, `site_work` and
#: `equipment_install` were added in migration 0005. They are the transitions the
#: PRD calls the most important judgement in the whole exercise —
#: 判断一个项目究竟走到了哪一步 — and `project.phase`, being one enum with four
#: states, could not express any of them.
EVENT_TYPES: Final[tuple[str, ...]] = (
    "announced",
    "land_acquired",
    "permit_filed",
    "permit_approved",
    "interconnection_agreement",
    "site_work",
    "groundbreaking",
    "equipment_install",
    "energized",
    "first_customer",
    "delayed",
    "expanded",
)

EventType = Literal[
    "announced",
    "land_acquired",
    "permit_filed",
    "permit_approved",
    "interconnection_agreement",
    "site_work",
    "groundbreaking",
    "equipment_install",
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
    # Fetched, but the body was navigation furniture rather than an article, so it
    # was refused before the LLM call. Distinct from `no_project`, which means a
    # model read the page and found nothing: here nothing read it, and a site that
    # later serves the full body makes the URL worth another attempt.
    "thin_content",
    "skipped",
)

#: The one status that means "there is work to do here".
PENDING_URL_STATUS: Final[str] = "discovered"

# --- why something is unconfirmed ------------------------------------------
#: Why the evidence gate did not confirm a value or a risk. Absent means it did.
#:
#: The tier alone is not enough to act on. A figure that is perfectly quoted but
#: whose label did not match reads identically to a programme-wide total caught
#: by the `$/MW` ceiling, and they call for opposite work: the first wants
#: another source, the second wants the figure corrected. Anything reading the
#: flag back — the capex exclusion most of all — over-excludes without this.
UNCONFIRMED_REASONS: Final[tuple[str, ...]] = (
    #: Nothing was offered to support it.
    "no_quote",
    #: A quote was offered and is not in the article, even after recovery. This
    #: is the one that means a model wrote a sentence nobody published.
    "quote_unverified",
    #: The quote is genuinely the article's, but does not state this particular
    #: thing — a real sentence filed against the wrong value or category.
    "quote_off_target",
    #: Quoted, verified, and still not credible for this project: the `$/MW`
    #: ceiling, which fires on a programme-wide total quoted in an article about
    #: one campus.
    "out_of_scale",
    #: Quoted, verified, correct when written, and since restated by the same
    #: project. Not a defect in the source — the article was right in 2024.
    #:
    #: This is the only reason here that records a decision rather than a
    #: measurement, and it exists because supersession cannot be resolved by any
    #: merge tiebreak we have. Hyperion (#10) kept $10B over $50B because both
    #: claims come from `opportunitylouisiana.gov` at weight 3, neither carries a
    #: `published_at` (they came from a search, not a feed), and the older story
    #: was crawled second — so `fetched_at` decided it. Publication-date merging
    #: does not help when both dates are NULL.
    #:
    #: Marking the losing claim here is durable in the way an edit to
    #: `project.investment_usd` is not: `upsert.resolve` discards unconfirmed
    #: claims outright whenever any confirmed claim exists, so the current figure
    #: wins on every recompute, forever — and the superseded one stays visible,
    #: attributed, and labelled, rather than being deleted.
    "superseded",
    #: Quoted, verified, and about something else — one building of the campus, one
    #: phase of a programme, a regional total. The article is not wrong; it was
    #: never talking about this row's quantity.
    #:
    #: The second reason that records a DECISION rather than a measurement, and it
    #: is split from `superseded` because the two have opposite lifetimes.
    #: `superseded` says "true in 2024, restated since" — a fact about the world,
    #: which can change again: a campus descoped back to its 2024 figure makes the
    #: mark wrong. `misread` says "this sentence is about a different object" — a
    #: fact about the sentence, which cannot change, because the article will
    #: always have been describing what it described.
    #:
    #: Recording a misread as a supersession is what made "the decision holds
    #: forever" look like an overreach. Measured: project #14's source 2790 claims
    #: `mw_planned = 19.2`, which is Building K's figure, and was filed
    #: `superseded` as though the campus had once planned 19.2 MW and grown.
    #:
    #: Both are in `upsert.DECIDED_REASONS`, so both leave the merge and both
    #: survive a re-crawl. The difference is what a reader — or a future rule about
    #: expiry — can tell about them afterwards.
    "misread",
    #: `project_party` only. Nobody claimed this role — it is what the column the
    #: party came from *means*. A citation asserting `company` is asserting an
    #: operator, because that is what `company` has always meant, so the party is
    #: derived from the claim rather than read out of a sentence.
    #:
    #: Its own reason rather than `no_quote`, and the distinction earns its place:
    #: `no_quote` on a party means an article offered one and the gate could not
    #: tie the *role* to a sentence, which is a refusal a reader may want to look
    #: at. This means nothing was offered and nothing was refused. Collapsing them
    #: would put a disclosure on every row in the database — every project has a
    #: company — and bury the refusals that matter among them.
    #:
    #: Carries no quote on purpose. A party's `quote` means "the sentence that
    #: evidences this role", and no sentence does; the citation is on `source_id`
    #: for a reader who wants to see where the name came from.
    "role_inferred",
)

# --- the claim envelope ----------------------------------------------------
#
# A value alone cannot say what it is a value *of*. Hyperion (#10) carries three
# investment figures — $10B for "the buildout of the infrastructure itself", $27B
# for the Blue Owl campus JV, $50B "of investment to the region" — and they are
# not a disagreement. They are three measurements of three different things,
# collapsed into one scalar because that is all the schema had.
#
# The three vocabularies below name what was being thrown away. Each is stored
# per (source, field) in `source.claim_meta` and each has a verification
# predicate in `crawl.axis_gate`: a value survives only if the sentence it was
# quoted from licenses it. That is the whole design. `risk.severity` is the
# cautionary case — a judgement no article ever states, uniformly `watch` on
# every risk in the database, fully populated and carrying nothing.

#: How exactly the article stated a quantity.
#:
#: Prompt RULE 4 used to say `"500-700 MW" -> 500 (the LOWER bound; say so in
#: "notes")` — the range deliberately destroyed and routed to prose. Nothing
#: could read it back, so `values_conflict` sees "about 2 GW" and 1,950 MW as a
#: 20% disagreement when they are the same claim at different precision.
CLAIM_BOUNDS: Final[tuple[str, ...]] = (
    #: The article states this figure and no hedge. The default.
    "exact",
    #: "about", "roughly", "~", "approximately".
    "approximate",
    #: "more than", "at least", "over", "upwards of". The stored value is a floor.
    "at_least",
    #: "up to", "as much as", "no more than". The stored value is a ceiling.
    "at_most",
)

#: Wording that licenses each non-default `bound`. One list, read by the extraction
#: gate that *assigns* a bound and by the readers that *display* one.
#:
#: This is the difference between an axis that carries information and one that
#: becomes the next `severity` — a judgement no article states, uniformly `watch`
#: on every risk in the database. A bound is not a judgement: the article either
#: hedged the number or it did not, and the hedge is a word in the sentence.
#:
#: It lives here rather than in `crawl` because two surfaces need it and this
#: codebase's recurring defect is the same rule written down twice —
#: `confidence.find_conflicts` is documented as a third copy of the 待确认 rule,
#: already inconsistent with the other two on screen.
BOUND_MARKERS: Final[dict[str, tuple[str, ...]]] = {
    "approximate": (
        "about ",
        "approximately",
        "roughly",
        "around ",
        "~",
        "some ",
        "nearly",
        "circa",
        "estimated",
        "an estimated",
        "close to",
        "almost",
    ),
    "at_least": (
        "more than",
        "at least",
        "over ",
        "upwards of",
        "north of",
        "in excess of",
        "+",
        # `exceeds` was missing, and it is the commonest form of all: Fairwater's
        # own `mw_built` rests on "Each exceeds 350 MW and is scaling toward
        # multi-GW", a floor across two sites that the row stored as a point value
        # on both of them.
        "exceeds",
        "exceeding",
        "greater than",
        "at minimum",
        "or more",
        "plus",
        "surpasses",
        "beyond ",
    ),
    "at_most": (
        "up to",
        "as much as",
        "no more than",
        "as many as",
        "fewer than",
        "less than",
        "under ",
        "below ",
        "at most",
        "or less",
        "capped at",
    ),
}

#: How many characters before a figure a hedge may sit and still be *its* hedge.
#:
#: **The check is positional, and the version it replaces was not.** Asking only
#: whether a hedge appears anywhere in the sentence licensed the wrong one on
#: Hyperion: source 12 reads "require more than $50 billion in investment, up from
#: the roughly $27 billion plan" — two figures, two hedges — and `approximate` was
#: read off the *other* number's "roughly".
#:
#: Measured against the cases that decide it, rather than picked:
#:
#:     "more than approximately 350 MW"                       gap 15  -> must match
#:     "roughly a decade of planning ... before the 350 MW"   gap 52  -> must NOT
#:
#: 32 sits between them with room for stacked hedges ("in excess of approximately"
#: is 27) and none for reaching across a clause into another figure's hedge.
BOUND_WINDOW: Final = 32

#: A stated direction outranks a stated imprecision when both attach to one figure.
#:
#: "more than approximately 350 MW" is a floor around an estimate, and the floor is
#: the load-bearing half: rendering it `~350` throws away the one thing the sentence
#: committed to. Nearest-marker-wins gets this backwards, because "approximately"
#: always sits closer to the number than the qualifier wrapping it.
_BOUND_PRECEDENCE: Final[dict[str, int]] = {"at_least": 2, "at_most": 2, "approximate": 1}

#: Whether the article says a thing has happened, is contracted, or is hoped for.
#:
#: The domain is almost entirely about the future, and the schema had one `phase`
#: enum per campus and dates with no modality at all. So Hyperion's "interim
#: milestone of 1.5 GW is being targeted by the end of 2027" was stored as an
#: `announced` event dated 2027-12-31 and counted as *reached* on the track strip.
#: `logic.milestone_in_the_future` exists to catch that, which is a rule patching
#: a category the type system was missing.
#:
#: Ordered least to most committed, so a reader can compare two claims.
CLAIM_MODALITIES: Final[tuple[str, ...]] = (
    #: Somebody floated it. "reports have surfaced", "could", "may".
    "speculated",
    #: An intention with a date attached. "targeted", "aims to", "hopes to".
    "targeted",
    #: A plan of record. "plans to", "expected to", "is set to".
    "planned",
    #: Signed, filed or committed. "has agreed", "filed an application", "signed".
    "contracted",
    #: It happened. "came online", "broke ground", "completed", "has begun".
    "achieved",
)

#: What object a figure describes. The axis Hyperion needed.
#:
#: `block:<label>` is not listed here because it is parameterised — it names a
#: tranche on the same record and is validated by resolving against it, not by
#: membership. Everything else is a closed set.
CLAIM_SCOPES: Final[tuple[str, ...]] = (
    #: This campus, the row the claim is being written to.
    "this_site",
    #: A programme this campus belongs to — "OpenAI's $500 billion Stargate"
    #: quoted in an article about one site. The `$/MW` ceiling already catches the
    #: loudest of these; this names the rest.
    "programme",
    #: Economic impact on a region, which is not capex. Meta's "more than $50B of
    #: investment to the region" includes roads, water and sewage.
    "region",
    #: The operator's whole estate, not this site.
    "portfolio",
    #: The article states the figure and does not say what it is a figure of.
    #: The honest answer, and the default when nothing licenses another.
    "unnamed",
)

# --- claim_meta.basis ------------------------------------------------------
#: What KIND of megawatt a capacity figure is. `scope` says which *thing* a figure
#: describes; this says which *quantity* was measured, and they are independent —
#: a figure can be unambiguously about this campus and still be the wrong kind of
#: megawatt for the column it lands in.
#:
#: Three quantities, routinely differing by 30-40% and occasionally by 10x:
#:
#:   it_load    the computing load. What `mw_planned` is defined as, and what the
#:              extraction prompt asks for.
#:   facility   the whole site's draw — IT load times PUE, plus cooling, plus
#:              everything else behind the meter. `_industry.txt` puts modern
#:              hyperscale PUE at 1.1-1.3, so this runs 10-30% above `it_load`.
#:   nameplate  a generator's rated output. Not a data center quantity at all:
#:              `iso_maps.py` opens by saying so, and `ingest/pjm.py` already
#:              refuses to put one in `mw_planned` for that reason.
#:
#: `unspecified` is the honest majority answer and the default. An article that
#: writes "a 200 MW campus" has not said which of the three it means, and the
#: whole point of recording the axis is that it stops that being invisible.
CLAIM_BASES: Final[tuple[str, ...]] = (
    "it_load",
    "facility",
    "nameplate",
    "unspecified",
)

#: The neutral value of each axis — what a claim degrades to when the gate cannot
#: license what the model asserted. Never a rejection of the *value*: the figure
#: survives exactly as it did before the envelope existed.
CLAIM_AXIS_DEFAULTS: Final[dict[str, str]] = {
    "bound": "exact",
    "modality": "planned",
    "scope": "unnamed",
    "basis": "unspecified",
}

CLAIM_AXES: Final[tuple[str, ...]] = ("scope", "bound", "modality", "as_of", "basis")

#: Fields a `basis` can qualify. Money has a `scope`, not a basis; a date has
#: neither. Restricting it is what keeps the coverage measurement honest — an
#: axis reported over fields it cannot apply to reads as 90% `unspecified` and
#: says nothing about the fields where it matters.
BASIS_FIELDS: Final[frozenset[str]] = frozenset({"mw_planned", "mw_built"})

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
    # Usually derived from megawatts (`tracker.compute`), but an article that
    # states a chip count outright has answered the question better than any
    # conversion, so a source is allowed to write it.
    "h200_equivalent",
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


def risk_precedence(risk: Any) -> tuple[int, int]:
    """Sort key for the obstacle that should speak for a project. Highest wins.

    Confirmed before unconfirmed, then severity. A 待确认 risk may become
    `project.blocker` only when nothing evidenced is available — the same rule a
    field value follows, and it matters here because `blocker` is one of the
    twelve tracked fields. Without it, an obstacle the gate refused would be
    promoted into `source.fields` and read as cited by confidence and by the
    9-of-12 count, which is the laundering the 待确认 tier exists to prevent.

    Duck-typed on `severity` and `unconfirmed` so the extractor's `RiskRecord`
    and the stored `Risk` row are ranked by one definition rather than two.
    """
    return (0 if getattr(risk, "unconfirmed", None) else 1, severity_rank(risk.severity))


def sql_in(column: str, allowed: tuple[str, ...]) -> str:
    """Render a CHECK constraint body: ``phase IN ('announced', ...)``.

    Used by models.py so the Python vocabulary and the SQL constraint cannot
    drift within a single definition. The migration files spell the same lists
    out literally (SQL has no way to import), and the drift test compares them.
    """
    values = ", ".join(f"'{v}'" for v in allowed)
    return f"{column} IN ({values})"


def bound_from_quote(quote: str | None, value: object) -> str:
    """The bound a quoted sentence puts on one figure. `exact` when it hedges none.

    Reads the *stored* quote, which is verbatim article text — the same rule the
    evidence gate follows, and the reason this cannot be talked into a hedge:
    `_verbatim_run` may have repaired the model's wording to the article's own, and
    checking the model's version would let it license a bound by writing one into a
    sentence nobody published.

    **Positional.** The hedge has to sit within `BOUND_WINDOW` characters before
    the figure itself, so a sentence carrying two figures and two hedges gives each
    number its own — Hyperion's "more than $50 billion ... up from the roughly $27
    billion plan" no longer reads `approximate` off the wrong one.

    `value` is matched loosely, because the sentence writes "1.2 GW" or "$50
    billion" where the column holds 1200.0 or 50000000000. Every plausible spelling
    of the leading digits is tried, and a figure the sentence does not contain
    yields `exact` rather than a guess.
    """
    text = (quote or "").lower()
    if not text:
        return "exact"

    positions = [index for token in _value_spellings(value) for index in _find_all(text, token)]
    if not positions:
        return "exact"

    # A stated direction beats a stated imprecision, and only then does the nearer
    # marker win. Ordering by distance alone reads "more than approximately 350" as
    # an estimate, because "approximately" is always the closer of the two.
    best: tuple[int, int, str] | None = None
    for bound, markers in BOUND_MARKERS.items():
        rank = _BOUND_PRECEDENCE.get(bound, 0)
        for marker in markers:
            for start in _find_all(text, marker):
                for figure in positions:
                    gap = figure - (start + len(marker))
                    if not 0 <= gap <= BOUND_WINDOW:
                        continue
                    candidate = (-rank, gap, bound)
                    if best is None or candidate < best:
                        best = candidate
    return best[2] if best else "exact"


#: How far either side of a figure a basis phrase may sit and still qualify it.
#:
#: Wider than `BOUND_WINDOW`, and two-sided rather than one-sided, because the two
#: read differently in English. A hedge leans on the number from in front — "more
#: than 50 billion" — while a basis phrase attaches from either side with a unit
#: and a preposition in between: "200 MW **of critical IT load**", "**gross
#: utility power** to the site totals 260 MW". Thirty-two characters clears the
#: first shape and not the second.
BASIS_WINDOW: Final = 44

#: Tiebreak only, when two basis phrases sit the same distance from one figure.
#:
#: Ordered away from `it_load`, which is the direction that cannot inflate a
#: capacity total: reading a facility figure as IT load overstates the computing
#: capacity of a site, and reading it the other way understates it. Understating
#: is the error this project takes everywhere else — every capacity sum is
#: documented as a floor — so it is the error to take here.
_BASIS_PRECEDENCE: Final[dict[str, int]] = {"nameplate": 3, "facility": 2, "it_load": 1}


def basis_from_quote(quote: str | None, value: object, markers: dict[str, tuple[str, ...]]) -> str:
    """Which KIND of megawatt a quoted sentence says one figure is.

    `unspecified` when the sentence does not say, which is the common and correct
    answer: most articles write "a 200 MW campus" and never qualify it.

    **Positional, for the reason `bound_from_quote` is.** A sentence contrasting
    two bases — *"the 260 MW gross figure corresponds to roughly 200 MW of IT
    load"* — carries both phrases, so a presence test licenses whichever the model
    happened to name and gets one of the two figures wrong every time. Each figure
    now takes the phrase nearest to *it*.

    Reads the **stored** quote, which is verbatim article text. The model's own
    wording is never consulted: `_verbatim_run` may have repaired it, and checking
    the repair would let a model license a basis by writing one into a sentence
    nobody published. Same rule the evidence gate follows for values.

    `markers` is passed in rather than imported so this stays a pure function of
    its arguments and the extraction module keeps one table of wording. See
    `crawl._BASIS_MARKERS`.
    """
    text = (quote or "").lower()
    if not text:
        return "unspecified"

    figures = [
        (index, index + len(token))
        for token in _value_spellings(value)
        for index in _find_all(text, token)
    ]
    if not figures:
        return "unspecified"

    best: tuple[int, int, str] | None = None
    for basis, phrases in markers.items():
        rank = _BASIS_PRECEDENCE.get(basis, 0)
        for phrase in phrases:
            for start in _find_all(text, phrase):
                span = (start, start + len(phrase))
                for figure in figures:
                    gap = _span_gap(span, figure)
                    if gap > BASIS_WINDOW:
                        continue
                    # Distance first, then the tiebreak — the opposite ordering
                    # from `bound`, where a stated direction outranks a stated
                    # imprecision however far away it sits. Nothing here outranks
                    # proximity: two basis phrases in one sentence are describing
                    # two different figures, not qualifying one twice.
                    candidate = (gap, -rank, basis)
                    if best is None or candidate < best:
                        best = candidate
    return best[2] if best else "unspecified"


def _span_gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Characters between two spans; 0 when they touch or overlap."""
    if a[1] <= b[0]:
        return b[0] - a[1]
    if b[1] <= a[0]:
        return a[0] - b[1]
    return 0


def _find_all(text: str, needle: str) -> list[int]:
    out: list[int] = []
    start = text.find(needle)
    while start != -1:
        out.append(start)
        start = text.find(needle, start + 1)
    return out


def _value_spellings(value: object) -> set[str]:
    """How a sentence might write this number: 1200 as "1,200", "1.2", "1200"."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {str(value).lower()} if value else set()
    if number <= 0:
        return set()

    out: set[str] = set()
    whole = int(number)
    for candidate in (whole, number):
        text = f"{candidate:,.0f}" if candidate == int(candidate) else f"{candidate:,}"
        out.add(text)
        out.add(text.replace(",", ""))
    # Scaled forms: 1200 MW is written "1.2 GW", 50_000_000_000 as "50 billion".
    for divisor in (1_000, 1_000_000, 1_000_000_000):
        scaled = number / divisor
        if 0.1 <= scaled < 1000:
            trimmed = f"{scaled:.10f}".rstrip("0").rstrip(".")
            out.add(trimmed)
    return {t for t in out if t}
