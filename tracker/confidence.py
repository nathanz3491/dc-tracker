"""Confidence scoring, 0..3.

The PRD specifies the inputs ("number of sources, source-type weights, agreement
on key fields") but not the arithmetic. This module fixes one, with each rule
isolated so it can be tested independently of the others.

The score answers: *how much should an operator trust this row without going and
checking it themselves?*

    0  no citation at all — only reachable for a hand-edited row
    1  cited, but weakly: one media source, or an ISO-queue keyword guess
    2  solidly cited by a single source, e.g. one company release
    3  corroborated by independent sources, or operator-verified

Three rules do most of the work and are worth stating plainly:

* **One source alone never reaches 3, however authoritative.** A single company
  press release is good evidence (2) but it is uncorroborated, and the top of
  the scale should mean "more than one party says so, or a human checked".
* **Independence is counted by registrable domain, not by row.** Five articles
  on datacenterdynamics.com are one source, not five. Aggregators recycle each
  other's reporting, so counting rows would inflate confidence exactly where
  it should not be.
* **Any citation floors the score at 1.** A project we have a real URL for is
  never confidence 0, per the PRD's definition of done.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field

from tracker.vocab import TRACKED_FIELDS

log = logging.getLogger(__name__)

#: How much a single source of each type is worth on its own.
#:
#:   3 — the entity itself or a government body said it, on the record
#:   2 — a filing-adjacent or industry-specialist account, or our own curation
#:   1 — general media, or a keyword match against a generation-queue row
#:
#: `iso_queue` is deliberately 1, not 2: the public ISO queues are *generator*
#: interconnection queues with no data-center column, so a match there is a
#: keyword heuristic over a project name, not an authoritative statement that a
#: data center exists. See README "Why the ISO queue path caps at confidence 1".
SOURCE_WEIGHTS: dict[str, int] = {
    "company_filing": 3,
    "government_doc": 3,
    "trade_press": 2,
    "manual": 2,
    "general_media": 1,
    "iso_queue": 1,
}

#: Source types that are authoritative rather than reported-at-second-hand.
OFFICIAL_TYPES = frozenset({"company_filing", "government_doc"})

#: Fields whose agreement (or disagreement) across sources moves the score.
#: Restricted to the quantitative claims that actually get contested; nobody
#: disputes a project's state.
KEY_FIELDS: tuple[str, ...] = ("mw_planned", "mw_built", "investment_usd", "phase", "customer")

#: A project this sparsely described is not trustworthy however good its source.
MIN_FIELDS_FOR_HIGH_CONFIDENCE = 5

#: Highest score reachable from a single source. Reaching 3 requires either
#: independent corroboration or an operator signing off on the row.
UNCORROBORATED_CEILING = 2

#: Relative difference above which two numeric claims are a conflict rather than
#: rounding. Matches the PRD's Q2 threshold for flagging a delta in `notes`.
CONFLICT_TOLERANCE = 0.20

#: A seed-file URL the operator has not replaced yet. `ingest manual` refuses such
#: a file unless --allow-placeholders is passed for a smoke test, but once that
#: data is in the database it must not be able to earn trust: a placeholder is not
#: a citation, and a `company_filing` weight on a URL that does not exist would
#: hand a project confidence 3 on the strength of nothing.
#:
#: Observed live: a real Microsoft project reached 3 because a placeholder seed row
#: contributed the "strongest source".
PLACEHOLDER_MARKER = "PLACEHOLDER"

#: `source.extractor` prefix marking a row computed from reference data rather than
#: read from a publication. Such a source cites a real, checkable document, so it
#: satisfies traceability — but it is not testimony about the project and must not
#: move the score. See :func:`compute`.
DERIVED_PREFIX = "derived:"

#: Registrable domains that are *tertiary*: they aggregate and summarize other
#: publications rather than reporting first-hand. A citation from one is kept —
#: it floors the score at 1 and its quotes are real — but it never counts toward
#: domain independence, key-field agreement, or conflict: Wikipedia's capacity
#: figure IS the trade-press figure one step removed, so letting it corroborate
#: would launder aggregation into independence. The same reasoning already
#: counts five articles on one outlet as one source; this extends it to an
#: outlet whose every article is a digest of the others.
TERTIARY_DOMAINS = frozenset({"wikipedia.org"})

#: Multi-part public suffixes we must not truncate to two labels, or
#: "bbc.co.uk" and "guardian.co.uk" would collapse into one "source".
_COMPOUND_SUFFIXES = frozenset(
    {
        "co.uk",
        "com.au",
        "co.jp",
        "com.br",
        "co.in",
        "com.cn",
        "co.nz",
        "com.mx",
        "gov.uk",
        "org.uk",
        "ac.uk",
        "com.tw",
        "co.za",
        "com.sg",
    }
)


@dataclass(frozen=True)
class SourceView:
    """The subset of a `source` row that scoring needs.

    A plain value object rather than the ORM model so this module stays
    importable and testable without a database.
    """

    source_type: str
    url: str
    fields: str | None = None
    claims: dict[str, object] = dc_field(default_factory=dict)
    extractor: str | None = None

    @classmethod
    def from_row(cls, row) -> SourceView:
        """Build from a `tracker.models.Source` (or anything shaped like one)."""
        raw = getattr(row, "claims", None)
        parsed: dict[str, object] = {}
        if raw:
            try:
                loaded = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(loaded, dict):
                    parsed = loaded
            except (TypeError, ValueError):
                log.warning(
                    "source %s has unparseable claims JSON; ignoring", getattr(row, "id", "?")
                )
        return cls(
            source_type=row.source_type,
            url=row.url or "",
            fields=getattr(row, "fields", None),
            claims=parsed,
            extractor=getattr(row, "extractor", None),
        )


def is_derived(source: SourceView) -> bool:
    """True when this row was computed from reference data, not reported."""
    return (source.extractor or "").startswith(DERIVED_PREFIX)


def is_tertiary(source: SourceView) -> bool:
    """True when this citation aggregates other coverage rather than reporting."""
    return registrable_domain(source.url) in TERTIARY_DOMAINS


@dataclass(frozen=True)
class Score:
    """A confidence value plus why, so `review` can explain itself."""

    value: int
    reasons: tuple[str, ...] = ()

    def __int__(self) -> int:
        return self.value


def registrable_domain(url: str) -> str:
    """Host reduced to its registrable domain, for independence counting.

    Deliberately a small heuristic rather than a public-suffix-list dependency:
    the only decision it drives is whether two citations count as one, and the
    compound-suffix set above covers the cases that show up in practice.
    """
    if not url:
        return ""
    host = url.split("//", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    host = host.removeprefix("www.")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _COMPOUND_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def independent_domains(sources: Iterable[SourceView]) -> set[str]:
    """Distinct registrable domains among the citations."""
    return {d for d in (registrable_domain(s.url) for s in sources) if d}


def cited_fields(sources: Iterable[SourceView]) -> set[str]:
    """Every project field claimed by at least one source's `fields` list."""
    out: set[str] = set()
    for s in sources:
        if s.fields:
            out.update(part.strip() for part in s.fields.split(",") if part.strip())
    return out


def values_conflict(a: object, b: object) -> bool:
    """True if two claims for the same field genuinely disagree."""
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return a != b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return False
        scale = max(abs(float(a)), abs(float(b)))
        if scale == 0:
            return False
        return abs(float(a) - float(b)) / scale > CONFLICT_TOLERANCE
    return str(a).strip().lower() != str(b).strip().lower()


def find_conflicts(sources: Sequence[SourceView]) -> dict[str, list[object]]:
    """Key fields where two sources assert materially different values.

    Numeric claims within :data:`CONFLICT_TOLERANCE` are treated as agreeing —
    "900 MW" and "1,000 MW" are the same story told twice, not a contradiction.
    """
    conflicts: dict[str, list[object]] = {}
    for field_name in KEY_FIELDS:
        values = [s.claims[field_name] for s in sources if s.claims.get(field_name) is not None]
        if len(values) < 2:
            continue
        if any(values_conflict(values[0], other) for other in values[1:]):
            conflicts[field_name] = values
    return conflicts


def find_agreements(sources: Sequence[SourceView]) -> set[str]:
    """Key fields where two or more *independent* sources agree.

    Independence is by domain: one outlet repeating itself is not corroboration.
    """
    agreed: set[str] = set()
    for field_name in KEY_FIELDS:
        by_domain: dict[str, object] = {}
        for s in sources:
            value = s.claims.get(field_name)
            if value is None:
                continue
            by_domain.setdefault(registrable_domain(s.url), value)
        values = list(by_domain.values())
        if len(values) >= 2 and not any(values_conflict(values[0], v) for v in values[1:]):
            agreed.add(field_name)
    return agreed


def compute(
    sources: Sequence[SourceView],
    *,
    operator_verified: bool = False,
    populated_tracked_fields: int | None = None,
) -> Score:
    """Score one project from its citations.

    Args:
        sources: every `source` row for the project.
        operator_verified: True when `project.last_verified_at` is set — a human
            checked this row, which is the only path to 3 from a single source.
        populated_tracked_fields: how many of the 12 PRD fields are non-null.
            Defaults to the count of *cited* fields, which is the honest
            fallback: a field nothing cites should not earn confidence.
    """
    reasons: list[str] = []

    if not sources:
        return Score(0, ("no citations",))

    # A placeholder URL is not a citation. Dropped before any weighting so it can
    # neither supply the "strongest source" nor count toward domain independence.
    placeholders = sum(1 for s in sources if PLACEHOLDER_MARKER in (s.url or ""))
    sources = [s for s in sources if PLACEHOLDER_MARKER not in (s.url or "")]
    if placeholders:
        reasons.append(f"ignored {placeholders} placeholder citation(s)")

    # A derived source carries geography, not testimony. The Census confirms that
    # Mount Pleasant sits in Racine County; it says nothing about whether this data
    # center exists or how large it is. Counting one as an independent domain would
    # let a single press release reach 3 by being "corroborated" about a city.
    derived = sum(1 for s in sources if is_derived(s))
    sources = [s for s in sources if not is_derived(s)]
    if derived:
        reasons.append(f"{derived} derived source(s) do not corroborate")

    if not sources:
        return Score(0, (*reasons, "no real citations"))

    weights = [SOURCE_WEIGHTS.get(s.source_type, 1) for s in sources]
    best = max(weights)
    best_type = sources[weights.index(best)].source_type
    # A tertiary source (Wikipedia) is a citation and floors the score at 1, but
    # it is one step removed from the coverage it summarizes, so it takes no part
    # in corroboration, agreement, or conflict below. Without this, one press
    # release plus the Wikipedia paragraph written from it would read as two
    # independent domains and reach 3 — the score reserved for two parties.
    tertiary = sum(1 for s in sources if is_tertiary(s))
    testimony = [s for s in sources if not is_tertiary(s)]
    if tertiary:
        reasons.append(f"{tertiary} tertiary source(s) do not corroborate")
    # Corroboration is counted only over citations that support a confirmed value.
    # `source.fields` lists quote-backed values only, so an empty one means every
    # claim that source made is 待确认 — and counting it as an independent domain
    # took a project from 2 to 3 purely for having a second URL full of unquoted
    # guesses. That is a guess buying the trust of a quote, which the tier exists to
    # prevent. Such a source is still a citation, so the floor rule below still
    # grants it 1: it just cannot corroborate anything.
    domains = independent_domains(s for s in testimony if (s.fields or "").strip())
    conflicts = find_conflicts(testimony)
    agreements = find_agreements(testimony)

    # A single source caps at 2 no matter how authoritative it is. 3 means
    # "corroborated or human-checked", and one press release is neither — it is
    # one party's account of its own project.
    score = min(best, UNCORROBORATED_CEILING)
    reasons.append(f"strongest source is {best_type} (weight {best})")
    if best > UNCORROBORATED_CEILING and len(domains) < 2:
        reasons.append("single source, so capped below full confidence")

    if len(domains) >= 2:
        score += 1
        reasons.append(f"{len(domains)} independent domains")
    if agreements:
        score += 1
        reasons.append("independent agreement on " + ", ".join(sorted(agreements)))
    if len(domains) >= 2 and any(s.source_type in OFFICIAL_TYPES for s in sources):
        reasons.append("corroborated official source")

    if conflicts:
        score -= 1
        reasons.append("unresolved conflict on " + ", ".join(sorted(conflicts)))

    coverage = (
        populated_tracked_fields
        if populated_tracked_fields is not None
        else len(cited_fields(sources) & set(TRACKED_FIELDS))
    )
    if coverage < MIN_FIELDS_FOR_HIGH_CONFIDENCE:
        score = min(score, 1)
        reasons.append(f"only {coverage}/{len(TRACKED_FIELDS)} tracked fields cited")

    if operator_verified:
        score = 3
        reasons.append("operator verified")

    # Floor: any citation at all is worth at least 1. The PRD's definition of
    # done requires that no project with a credible source sits at 0.
    score = max(1, min(3, score))
    return Score(score, tuple(reasons))


def compute_for_project(project, sources: Iterable) -> Score:
    """Convenience wrapper over ORM objects."""
    views = [SourceView.from_row(s) for s in sources]
    populated = sum(1 for f in TRACKED_FIELDS if getattr(project, f, None) is not None)
    return compute(
        views,
        operator_verified=getattr(project, "last_verified_at", None) is not None,
        populated_tracked_fields=populated,
    )


def needs_review(score: int) -> bool:
    """PRD open question Q3: below 2 always needs a human; 2+ is auto-approved."""
    return score < 2


__all__ = [
    "CONFLICT_TOLERANCE",
    "DERIVED_PREFIX",
    "KEY_FIELDS",
    "MIN_FIELDS_FOR_HIGH_CONFIDENCE",
    "OFFICIAL_TYPES",
    "PLACEHOLDER_MARKER",
    "SOURCE_WEIGHTS",
    "TERTIARY_DOMAINS",
    "Score",
    "SourceView",
    "cited_fields",
    "compute",
    "compute_for_project",
    "find_agreements",
    "find_conflicts",
    "independent_domains",
    "is_derived",
    "is_tertiary",
    "needs_review",
    "registrable_domain",
]
