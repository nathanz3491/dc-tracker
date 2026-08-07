"""Measure whether the data is getting better — not whether the code still runs.

Every other module here asks a question about one row. This one asks a question
about the database: what share of what we store is actually backed by a sentence
somebody published, and is that share moving in the right direction.

It exists because the two numbers that sound like an answer are not one:

* "98.7% of stored quotes are exact substrings of their own article"
  (`scripts/measure_evidence_gate.py`) measures the quotes that *exist*. It says
  nothing about values that never produced one, so it reads as reassurance about
  a population it never looked at.
* "66% of claims carry no quote" counts the model's raw output, most of which the
  evidence gate correctly demoted to 待确认. It reads as a scandal and is mostly
  the gate working.

The number that matters is neither: it is the share of **stored** values whose
own winning source recorded a sentence for them. Measured when this module was
written, over 748 values on the live database — 49.2% quote-backed, 38.2%
correctly flagged 待确认, and **11.9% (89 values) confirmed with no quote at
all**. That last bucket is the defect: the row presents them as established, and
nothing in the system says otherwise.

All 89 came from two prompt vintages that predate migration `0007`, which is the
column `quotes` lives in — 61 from `extract-v1@8eb51f2a` and 28 from
`extract-v1@cef10fb4`. **None came from the current extractor.** So the gate
works and the damage is stratigraphy: a fact about *history*, invisible to every
per-row check, and findable only by counting.

That 89 is worth stating precisely, because a hand-rolled version of this
measurement got 83. The difference is `mw_built`, `first_announced` and `phase`,
whose merge policies are MAX / MIN / PHASE rather than PREFER_WEIGHT — a
re-derived sort picks a different winning source for them than the write path
did. Which is the whole reason nothing here re-derives anything.

Nothing here is reimplemented. The winning source comes from `gaps.provenance`,
the merge order from `upsert.claims_by_field`, the weights from `confidence` —
the same definitions the write path and both display surfaces use, so a number
reported here cannot disagree with what `tracker show` prints. Free: no LLM, no
network, read-only.
"""

from __future__ import annotations

import collections
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.gaps import DEFAULTED, DERIVED, INFERRED, UNCONFIRMED, provenance
from tracker.models import IngestUrl, Project, Source

#: Fields the census covers: `confidence.KEY_FIELDS` plus the two dates.
#:
#: These are the columns a reader acts on and every rollup reads. Identity fields
#: (`name`, `company`, `state`) are deliberately excluded — they are FILL_ONLY, so
#: "which source won" is answered by arrival order rather than by evidence, and
#: counting them would dilute the measure with a question it is not asking.
MEASURED_FIELDS: Final[tuple[str, ...]] = (
    "mw_planned",
    "mw_built",
    "investment_usd",
    "phase",
    "customer",
    "first_announced",
    "expected_online",
)

#: Census buckets. The split that matters is the last two: both are values with
#: no sentence behind them, and only one of them admits it.
QUOTE_BACKED: Final = "quote_backed"
FLAGGED: Final = "flagged_unconfirmed"
SILENT_DEFECT: Final = "confirmed_without_quote"
DERIVED_VALUE: Final = "derived"
DEFAULTED_VALUE: Final = "schema_default"
NO_SOURCE: Final = "no_source"

BUCKETS: Final[tuple[str, ...]] = (
    QUOTE_BACKED,
    FLAGGED,
    SILENT_DEFECT,
    DERIVED_VALUE,
    DEFAULTED_VALUE,
    NO_SOURCE,
)

#: `crawl:extract-v1@5d479a68:MiniMax-M2.7-highspeed:httpx` -> `extract-v1@5d479a68`.
_STAMP = re.compile(r"^[a-z]+:([a-z0-9-]+@[0-9a-f]+):")


def vintage(extractor: str | None) -> str:
    """Which prompt version produced a source, for grouping.

    A bare prefix (`derived:census-place-2020`) has no prompt behind it and is
    returned whole — those rows are lookups, not extractions, and lumping them in
    with a prompt stamp would invent a version that never existed.
    """
    text = (extractor or "").strip()
    if not text:
        return "unknown"
    found = _STAMP.match(text)
    return found.group(1) if found else text.split(":", 1)[0]


@dataclass(frozen=True)
class ValueBasis:
    """One stored value and what actually stands behind it."""

    project_id: int
    field: str
    bucket: str
    #: The prompt vintage of the source that won the merge, for finding strata.
    vintage: str = "unknown"
    source_url: str | None = None

    @property
    def is_defect(self) -> bool:
        return self.bucket == SILENT_DEFECT


def _bucket_for(prov: Any) -> str:
    """Which census bucket one field's provenance falls in."""
    if prov is None:
        return NO_SOURCE
    if prov.tier == DEFAULTED:
        return DEFAULTED_VALUE
    if prov.tier in (DERIVED, INFERRED):
        return DERIVED_VALUE
    if prov.tier == UNCONFIRMED:
        return FLAGGED
    # REPORTED: the source lists this field in `source.fields`, which means the
    # gate confirmed it. `quote_is_exact` is False when `provenance` fell back to
    # the source's whole excerpt because no per-field sentence was recorded — a
    # value presented as established with nothing specific behind it.
    return QUOTE_BACKED if prov.quote_is_exact else SILENT_DEFECT


def value_bases(session: Session, *, project_ids: list[int] | None = None) -> Iterator[ValueBasis]:
    """Every stored value in `MEASURED_FIELDS`, with what supports it."""
    query = select(Project)
    if project_ids:
        query = query.where(Project.id.in_(project_ids))
    for project in session.scalars(query).all():
        by_url = {s.url: s for s in (project.sources or ())}
        for name in MEASURED_FIELDS:
            if getattr(project, name, None) is None:
                continue
            prov = provenance(project, name)
            source = by_url.get(prov.source_url) if prov and prov.source_url else None
            yield ValueBasis(
                project_id=project.id,
                field=name,
                bucket=_bucket_for(prov),
                vintage=vintage(source.extractor if source else None),
                source_url=prov.source_url if prov else None,
            )


@dataclass(frozen=True)
class Census:
    """The evidence tier of every stored value, and where the defects sit."""

    total: int = 0
    buckets: dict[str, int] = dc_field(default_factory=dict)
    by_field: dict[str, dict[str, int]] = dc_field(default_factory=dict)
    #: Defects only, grouped by the prompt vintage that produced them. A defect
    #: count concentrated in old vintages is a remediation job; one spread evenly
    #: is a live bug.
    defects_by_vintage: dict[str, int] = dc_field(default_factory=dict)

    def share(self, bucket: str) -> float:
        return self.buckets.get(bucket, 0) / self.total if self.total else 0.0

    @property
    def defects(self) -> int:
        return self.buckets.get(SILENT_DEFECT, 0)


def evidence_census(session: Session, *, project_ids: list[int] | None = None) -> Census:
    """Bucket every stored value by what stands behind it."""
    buckets: collections.Counter[str] = collections.Counter()
    by_field: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    by_vintage: collections.Counter[str] = collections.Counter()
    total = 0

    for basis in value_bases(session, project_ids=project_ids):
        total += 1
        buckets[basis.bucket] += 1
        by_field[basis.field][basis.bucket] += 1
        if basis.is_defect:
            by_vintage[basis.vintage] += 1

    return Census(
        total=total,
        buckets=dict(buckets),
        by_field={f: dict(c) for f, c in by_field.items()},
        defects_by_vintage=dict(by_vintage),
    )


def silent_defects(session: Session, *, project_ids: list[int] | None = None) -> list[ValueBasis]:
    """Values stored as established with no sentence recorded for them.

    The headline number. This is what remediation has to move, and a test that
    asserts it strictly decreased is the only test that proves the data improved
    rather than that the code ran.
    """
    found = [b for b in value_bases(session, project_ids=project_ids) if b.is_defect]
    found.sort(key=lambda b: (b.project_id, b.field))
    return found


@dataclass(frozen=True)
class Inversion:
    """A field whose value was decided by crawl order against publication order."""

    project_id: int
    field: str
    kept: Any
    kept_published: str
    passed_over: Any
    passed_over_published: str

    @property
    def summary(self) -> str:
        return (
            f"#{self.project_id} {self.field}: kept {self.kept!r} "
            f"(published {self.kept_published[:10]}) over {self.passed_over!r} "
            f"(published {self.passed_over_published[:10]})"
        )


def _published_index(session: Session) -> dict[str, str]:
    """url -> publication date.

    `source.published_at` (migration `0014`) first, because that is the column the
    merge tiebreak reads — a report about which claim *would* win has to consult
    the same field the write path does, or it describes a rule nobody applies.
    `ingest_url` is the fallback for a source written before the backfill.
    """
    out: dict[str, str] = {}
    # `source` last so it wins on conflict: it is the column the merge reads.
    for url_col, date_col in (
        (IngestUrl.url, IngestUrl.published_at),
        (Source.url, Source.published_at),
    ):
        for url, published in session.execute(
            select(url_col, date_col).where(date_col.is_not(None))
        ):
            if url and published:
                out[url] = (
                    published.isoformat() if hasattr(published, "isoformat") else str(published)
                )
    return out


def recency_inversions(
    session: Session, *, project_ids: list[int] | None = None
) -> list[Inversion]:
    """Fields where a tied-but-older article beat a newer one.

    `upsert.claims_by_field` breaks a tie on `fetched_at` — when the crawler
    happened to visit the page, not when anybody published it. Where two claims
    tie on (confirmed, weight), the winner is therefore decided by crawl order,
    and crawl order is arbitrary with respect to the truth.

    Only ties are reported. An old high-weight source beating a new low-weight one
    is the same disease and is deliberately *not* counted here, because it is the
    weighting policy working as designed rather than a tiebreak accident — so this
    number is a floor, not a total.
    """
    from tracker.upsert import FIELD_POLICY, Policy, claims_by_field

    published = _published_index(session)
    query = select(Project)
    if project_ids:
        query = query.where(Project.id.in_(project_ids))

    out: list[Inversion] = []
    for project in session.scalars(query).all():
        by_field = claims_by_field(list(project.sources or ()))
        for name, claims in by_field.items():
            # Only PREFER_WEIGHT consults the tiebreak at all. MAX/MIN/PHASE scan
            # every claim and FILL_ONLY takes arrival order, so a "loser" under
            # those policies was never in a tiebreak to lose.
            if FIELD_POLICY.get(name, Policy.PREFER_WEIGHT) is not Policy.PREFER_WEIGHT:
                continue
            if len(claims) < 2:
                continue
            winner = claims[0]
            won_at = published.get(winner.url)
            if not won_at:
                continue
            for other in claims[1:]:
                if (other.confirmed, other.weight) != (winner.confirmed, winner.weight):
                    break  # sorted, so everything after this is weaker still
                other_at = published.get(other.url)
                if other_at and other_at > won_at and other.value != winner.value:
                    out.append(
                        Inversion(
                            project_id=project.id,
                            field=name,
                            kept=winner.value,
                            kept_published=won_at,
                            passed_over=other.value,
                            passed_over_published=other_at,
                        )
                    )
                    break
    out.sort(key=lambda i: (i.project_id, i.field))
    return out


@dataclass(frozen=True)
class AxisStats:
    """How much information one claim-envelope axis is actually carrying."""

    axis: str
    populated: int = 0
    total: int = 0
    values: dict[str, int] = dc_field(default_factory=dict)

    @property
    def coverage(self) -> float:
        return self.populated / self.total if self.total else 0.0

    @property
    def modal_value(self) -> str | None:
        return max(self.values, key=lambda k: self.values[k]) if self.values else None

    @property
    def modal_share(self) -> float:
        """Share of populated values sitting on the single most common answer.

        The default-collapse detector. `risk.severity` is the cautionary case:
        every risk on the live database reads `watch`, which is `vocab`'s default,
        so the column looks populated and carries nothing. An axis above the
        threshold is a default wearing a field's clothes and should be deleted
        rather than displayed.
        """
        if not self.populated or not self.values:
            return 0.0
        return max(self.values.values()) / self.populated


#: Above this share on one value, an axis is reporting its default rather than
#: the article. Set deliberately high: a genuinely skewed axis is possible —
#: most claims really are about `this_site` — so this catches collapse, not skew.
DEFAULT_COLLAPSE_CEILING: Final = 0.95


def axis_census(session: Session, *, axes: tuple[str, ...] = ()) -> dict[str, AxisStats]:
    """Coverage and default-collapse per claim-envelope axis.

    Reads `source.claim_meta`, which arrives with the envelope. Returns empty
    stats before that column exists, so the harness can be written, committed and
    running against a real baseline *before* the thing it measures is built —
    which is the only order in which "did this improve" is answerable.
    """
    counts: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    populated: collections.Counter[str] = collections.Counter()
    total = 0

    for project in session.scalars(select(Project)).all():
        for source in project.sources or ():
            raw = getattr(source, "claim_meta", None)
            claims = json.loads(source.claims) if source.claims else {}
            if not isinstance(claims, dict):
                continue
            meta: dict[str, Any] = {}
            if raw:
                try:
                    loaded = json.loads(raw)
                except (TypeError, ValueError):
                    loaded = {}
                if isinstance(loaded, dict):
                    meta = loaded
            for name in claims:
                if name not in MEASURED_FIELDS:
                    continue
                total += 1
                entry = meta.get(name)
                if not isinstance(entry, dict):
                    continue
                for axis in axes or tuple(entry):
                    value = entry.get(axis)
                    if value is None:
                        continue
                    populated[axis] += 1
                    counts[axis][str(value)] += 1

    names = axes or tuple(counts)
    return {
        axis: AxisStats(
            axis=axis,
            populated=populated.get(axis, 0),
            total=total,
            values=dict(counts.get(axis, {})),
        )
        for axis in names
    }


__all__ = [
    "BUCKETS",
    "DEFAULTED_VALUE",
    "DEFAULT_COLLAPSE_CEILING",
    "DERIVED_VALUE",
    "FLAGGED",
    "MEASURED_FIELDS",
    "NO_SOURCE",
    "QUOTE_BACKED",
    "SILENT_DEFECT",
    "AxisStats",
    "Census",
    "Inversion",
    "ValueBasis",
    "axis_census",
    "evidence_census",
    "recency_inversions",
    "silent_defects",
    "value_bases",
    "vintage",
]
