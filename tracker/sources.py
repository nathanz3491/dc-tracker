"""Which publishers actually decide a stored value.

`confidence.SOURCE_WEIGHTS` assigns trust by *type* — six categories, weights 1
to 3, set by hand. `docs/plan-claim-envelope.md` names `source_type` as its
cautionary example: nothing can check it, so a wrong entry is invisible. A
hand-ranked table of every publisher would be the same mistake at higher
resolution — ten asserted numbers per host instead of three per category, still
with nothing to compare them against.

So this module assigns no weight. It counts what each publisher's claims actually
**did**, which is checkable by construction: re-run it and the numbers move only
if the database moved.

    cited         source rows on this host
    decisive      (project, field) resolutions this host's claim won
    contributing  a claim survived into the merge but won nothing
    inert         cited, and asserted nothing the merge could use
    yield         decisive per citation — the ranking key

`cited == decisive_sources + contributing + inert` holds by construction, so a
row that does not add up is a bug in this module rather than a judgement call.

**Why the win is not simply `claims[0]`.** The claim list is sorted
strongest-first, but only `PREFER_WEIGHT` and `FILL_ONLY` take the head of it.
`mw_built` takes the MAX, `first_announced` the MIN, `phase` the furthest rung of
a progression — so on those fields the strongest source routinely loses to a
weaker one carrying a bigger number or a later stage. Attributing by sort order
would credit the wrong publisher on four of the twelve tracked fields.

This asks :func:`upsert.resolve_field` instead — the same function the write path
asks — and then credits the strongest claim holding the value it returned. That
alias exists precisely so a read path cannot drift from the write path; re-deriving
the order by hand once reported 73 rows as changed when nothing had changed.

`existing=None` is passed deliberately: it neutralises the ratchet, so the answer
is what the claim set alone implies rather than what the row happens to be
carrying. That matches `upsert.recompute_from_sources`, which is what ultimately
decides a stored value.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker import confidence as conf
from tracker.models import Project, Source
from tracker.upsert import claims_by_field, resolve_field
from tracker.vocab import TRACKED_FIELDS

#: Identity fields, excluded from every count here.
#:
#: They are in `TRACKED_FIELDS`, and scoring them wrecks the report twice over.
#: `name`, `company`, `city` and `state` are `FILL_ONLY` — the first source to
#: arrive sets them and no later source can displace one — so a "win" on them
#: records crawl order, not judgement. And because nearly every extracted source
#: asserts all four, counting them made every citation in the database look like a
#: participant: `inert` came out as 0 across 2,758 rows on the first pass.
#:
#: Excluding them is what makes `inert` mean the thing worth knowing — a citation
#: that restated who and where, and contributed no fact anybody was missing.
IDENTITY_FIELDS: frozenset[str] = frozenset({"name", "company", "city", "state"})

#: The eight facts a citation is actually worth something for.
SCORED_FIELDS: tuple[str, ...] = tuple(f for f in TRACKED_FIELDS if f not in IDENTITY_FIELDS)


def host_of(url: str | None) -> str:
    """The publisher a URL belongs to.

    Delegates to `confidence.registrable_domain`, which is already this codebase's
    answer to "are these two citations the same party?" — it is what
    `independent_domains` counts when deciding whether a project has corroboration.
    Ranking publishers by a *different* notion of publisher than the confidence
    score uses would let a host be one source here and two there.
    """
    return conf.registrable_domain(url or "") or "(no host)"


def is_publisher(source: Source) -> bool:
    """False for rows that are reference data or a seed placeholder, not a source.

    Census geography is written as a `source` row so the derivation is cited like
    anything else, but it publishes nothing and claims none of `SCORED_FIELDS` — so
    ranking it produces `www2.census.gov: 184 cited, 184 inert, 0 decisive`, which
    reads as the worst publisher in the database rather than as a lookup table. A
    placeholder is excluded for the reason `upsert.is_placeholder` gives: its URL
    does not exist, so there is no publisher to credit.
    """
    if conf.PLACEHOLDER_MARKER in (source.url or ""):
        return False
    return not conf.is_derived(conf.SourceView.from_row(source))


@dataclass
class HostStat:
    """One publisher's record across the whole database."""

    host: str
    cited: int = 0
    #: (project, field) pairs this host's claim decided.
    decisive: int = 0
    #: The subset of `decisive` where a rival asserted something different. See
    #: `Attribution.contested` for why this is the column that means anything.
    contested: int = 0
    #: Source rows that won at least one field.
    decisive_sources: int = 0
    #: Source rows whose claims entered the merge and won nothing.
    contributing: int = 0
    #: Source rows that asserted nothing the merge could use.
    inert: int = 0
    #: Which fields it wins, so "good at capacity, useless on dates" is visible.
    fields: Counter[str] = dc_field(default_factory=Counter)
    #: Every source_type this host has been classified as, with counts. More than
    #: one entry means the classifier is inconsistent about it, which is worth
    #: seeing on its own.
    types: Counter[str] = dc_field(default_factory=Counter)

    @property
    def yield_per_citation(self) -> float:
        return self.decisive / self.cited if self.cited else 0.0

    @property
    def contested_per_citation(self) -> float:
        return self.contested / self.cited if self.cited else 0.0

    @property
    def type_weight(self) -> int:
        """The weight the hand-set table currently gives this host's modal type."""
        if not self.types:
            return 0
        return conf.SOURCE_WEIGHTS.get(self.types.most_common(1)[0][0], 1)

    def adds_up(self) -> bool:
        return self.cited == self.decisive_sources + self.contributing + self.inert

    def as_json(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "cited": self.cited,
            "decisive": self.decisive,
            "contested": self.contested,
            "decisive_sources": self.decisive_sources,
            "contributing": self.contributing,
            "inert": self.inert,
            "yield_per_citation": round(self.yield_per_citation, 3),
            "contested_per_citation": round(self.contested_per_citation, 3),
            "type_weight": self.type_weight,
            "types": dict(self.types),
            "fields": dict(self.fields),
        }


@dataclass
class Survey:
    """The whole picture, plus the totals a caller wants to sanity-check against."""

    hosts: list[HostStat] = dc_field(default_factory=list)
    projects_read: int = 0
    sources_read: int = 0
    decisions: int = 0
    contested: int = 0
    #: Rows excluded as reference data or placeholders. Reported rather than
    #: silently dropped, so `sources_read` can be reconciled against `source`.
    skipped: int = 0

    #: Citations a host needs before a *per-citation* ordering will rank it. Not a
    #: filter on the report — `by="decisive"` and the JSON payload always carry
    #: every host — only on the two ratio orderings, where one citation produces a
    #: ratio indistinguishable from a hundred.
    MIN_CITED_FOR_RATIO: int = 5

    def ranked(self, by: str = "decisive", min_cited: int | None = None) -> list[HostStat]:
        """Most valuable publisher first, under one of three explicit orderings.

        * `decisive` (default) — raw count of values this host decided. This is
          "how much do we actually use it", which is the question asked of the
          report, and it cannot be gamed by a small sample.
        * `contested` — wins against a disagreeing rival. The quality signal.
        * `yield` — decisive per citation. Efficiency, and the one that needs
          `min_cited`: a host cited once on a single-source project wins every
          field unopposed and scores higher than any real outlet can.

        Every ordering breaks ties on `cited` then on host name, so it is total
        and the output diffs cleanly between runs.
        """
        floor = self.MIN_CITED_FOR_RATIO if min_cited is None else min_cited
        keys = {
            "decisive": lambda h: (-h.decisive, -h.cited, h.host),
            "contested": lambda h: (-h.contested, -h.cited, h.host),
            "yield": lambda h: (-h.yield_per_citation, -h.cited, h.host),
        }
        if by not in keys:
            raise ValueError(f"unknown ordering {by!r}; expected one of {', '.join(keys)}")
        hosts = self.hosts if by == "decisive" else [h for h in self.hosts if h.cited >= floor]
        return sorted(hosts, key=keys[by])

    def as_json(self) -> dict[str, Any]:
        return {
            "projects_read": self.projects_read,
            "sources_read": self.sources_read,
            "decisions": self.decisions,
            "contested": self.contested,
            "skipped": self.skipped,
            "hosts": [h.as_json() for h in self.ranked()],
        }


@dataclass
class Attribution:
    """For one project: which citation settled what."""

    #: source id -> fields its claim decided.
    won: dict[int, Counter[str]] = dc_field(default_factory=dict)
    #: The subset of `won` where a rival claim asserted a *different* value. This
    #: is the only column that is evidence of anything. An unopposed win means
    #: nobody else spoke, which a single-source project gives away for free — the
    #: first pass at this report ranked eight `.gov` pages cited once apiece above
    #: every trade outlet in the database, purely because each had no rival.
    contested: dict[int, Counter[str]] = dc_field(default_factory=dict)
    #: Every source that put a usable claim into the merge, won or lost.
    entered: set[int] = dc_field(default_factory=set)


def decisive_by_source(sources: list[Source]) -> Attribution:
    """Which of one project's citations decided what.

    One pass, so the three counts cannot disagree about what a usable claim is.

    Split out from :func:`survey` so a single project can be explained on its own
    (`tracker show` and the console drawer both want "what did this citation
    actually settle?"), and so the measurement harness calls the identical
    function rather than reimplementing the attribution.
    """
    # One source per URL within a project — `uq_source_project_url` guarantees it,
    # which is what makes a claim's `url` a usable key back to its row.
    by_url = {s.url: s.id for s in sources}
    out = Attribution()

    for name, claims in claims_by_field(sources).items():
        if name not in SCORED_FIELDS or not claims:
            continue
        for claim in claims:
            if (sid := by_url.get(claim.url)) is not None:
                out.entered.add(sid)
        # The same question the write path asks, so this cannot drift from it.
        # `existing=None` neutralises the ratchet — see the module docstring.
        chosen = resolve_field(name, claims, None)
        if chosen is None:
            continue
        # Claims are sorted strongest-first, so the first one carrying the
        # resolved value is the one the merge credits. `values_conflict` decides
        # what "carrying it" means, rather than `==`: it is the same tolerance
        # `_conflict_notes` applies, so 2000 and 2000.0 are one value here exactly
        # as they are there, and MIN returning an ISO string does not read as a
        # disagreement with the date it was derived from.
        rivals = [c for c in claims if conf.values_conflict(chosen, c.value)]
        agreed = [c for c in claims if c not in rivals]
        if not agreed or (sid := by_url.get(agreed[0].url)) is None:
            continue
        out.won.setdefault(sid, Counter())[name] += 1
        if rivals:
            out.contested.setdefault(sid, Counter())[name] += 1
    return out


def survey(session: Session) -> Survey:
    """Walk every project and total up what each publisher decided."""
    out = Survey()
    stats: dict[str, HostStat] = {}

    def stat(host: str) -> HostStat:
        return stats.setdefault(host, HostStat(host=host))

    for project in session.scalars(select(Project)).all():
        rows = session.scalars(select(Source).where(Source.project_id == project.id)).all()
        # Attribution runs over the FULL claim set, including derived rows: a
        # reference-data claim still competes in the merge, so removing it before
        # resolving would credit a publisher with a win it did not have. Only the
        # per-host tally below skips non-publishers.
        if not rows:
            continue
        attribution = decisive_by_source(list(rows))

        sources = [s for s in rows if is_publisher(s)]
        out.skipped += len(rows) - len(sources)
        if not sources:
            continue
        out.projects_read += 1
        out.sources_read += len(sources)

        for source in sources:
            entry = stat(host_of(source.url))
            entry.cited += 1
            entry.types[source.source_type] += 1
            fields = attribution.won.get(source.id)
            if fields:
                entry.decisive_sources += 1
                entry.decisive += sum(fields.values())
                entry.fields.update(fields)
                out.decisions += sum(fields.values())
                fought = attribution.contested.get(source.id)
                if fought:
                    entry.contested += sum(fought.values())
                    out.contested += sum(fought.values())
            elif source.id in attribution.entered:
                entry.contributing += 1
            else:
                entry.inert += 1

    out.hosts = list(stats.values())
    return out


__all__ = [
    "IDENTITY_FIELDS",
    "SCORED_FIELDS",
    "Attribution",
    "HostStat",
    "Survey",
    "decisive_by_source",
    "host_of",
    "is_publisher",
    "survey",
]
