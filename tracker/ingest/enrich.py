"""All-out enrichment of a single project: every retrieval method, until it stops paying.

`tracker sync` spreads a fixed budget across the whole database, which is right for
keeping it current and wrong when you want one row *complete*. This module inverts
that: pick one project, recruit every source of URLs the system has, and keep going
while rounds are still filling fields.

Five harvesters, cheapest and most certain first, so an expensive one never runs
for a field a free one would have filled:

1. **derive** — county/lat/lon from Census reference data. No LLM, no network.
2. **queue** — candidates already discovered whose slug names this project.
3. **retry** — this project's URLs that previously failed to fetch.
4. **archive** — sitemap sweep, filtered to this project. Key-free, reaches back
   years, and is the main reason this works without a search API.
5. **search** — Google CSE, with queries built from the project's *own* gaps.
   Skipped with a clear message when no key is configured.
6. **refresh** — re-read the project's existing citations. Articles get edited,
   and a re-read under the current gate can lift a value the old one dropped.

Then extract, re-measure, and repeat. The loop stops when a round fills nothing new
or no harvester has anything left, not at a fixed article count — "cost no object"
means bounded by *diminishing returns*, and the caps exist only so a bug cannot
spend without limit.

What it does not do is claim success it cannot have. `mw_built` on an announced
project is correctly null; `blocker` and `customer` are frequently absent because
there is nothing to report. Those are reported as such, separately from fields that
were genuinely attempted and missed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings
from tracker.gaps import FILLED, NOT_APPLICABLE, FieldState, for_project
from tracker.models import IngestUrl, Project, Source
from tracker.vocab import TRACKED_FIELDS

if TYPE_CHECKING:
    from tracker.ingest.fetch import Fetcher
    from tracker.llm import Extractor

log = logging.getLogger(__name__)

#: Rounds to run before stopping regardless of progress. Generous, because the
#: loop's real stop condition is "a round filled nothing"; this is the backstop
#: against a harvester that keeps returning URLs that never extract.
MAX_ROUNDS = 6

#: Articles per round. High: the point of this command is completeness, not
#: economy. Still finite so a bad archive match cannot spend unbounded budget.
MAX_ARTICLES_PER_ROUND = 25

#: Search queries per run, when a key is configured. Google's free tier allows 100
#: queries a day, and one project asking for more than this is a sign the templates
#: are wrong rather than that the project is unusually obscure.
MAX_QUERIES = 12

#: Search phrases per missing field. Deliberately templates rather than an LLM
#: call: the project is already known, so there is nothing to infer -- the model
#: would only paraphrase what the row already says, at the cost of a call and the
#: risk of drifting onto a different project.
_FIELD_QUERIES: dict[str, tuple[str, ...]] = {
    "mw_planned": ("megawatts capacity", "MW data center campus"),
    "mw_built": ("energized operational first phase", "now online megawatts"),
    "investment_usd": ("investment billion", "capital investment cost"),
    "expected_online": ("expected online date", "operational by 2027 completion"),
    "first_announced": ("announced plans", "announcement date"),
    "customer": ("tenant lease anchor customer", "leased to"),
    "blocker": ("opposition lawsuit zoning denied moratorium", "delayed rezoning appeal"),
    "phase": ("under construction groundbreaking", "opened operational"),
    "county": ("county board data center", "county planning commission"),
}


@dataclass
class Harvest:
    """URLs one harvester produced, and whether it could run at all."""

    name: str
    urls: list[str] = dc_field(default_factory=list)
    skipped: str | None = None
    note: str | None = None


@dataclass
class Round:
    """One pass: what was harvested, what was read, what it filled."""

    number: int
    harvests: list[Harvest] = dc_field(default_factory=list)
    articles_read: int = 0
    fields_filled: tuple[str, ...] = ()

    @property
    def urls(self) -> list[str]:
        seen: list[str] = []
        for harvest in self.harvests:
            for url in harvest.urls:
                if url not in seen:
                    seen.append(url)
        return seen


@dataclass
class EnrichReport:
    """Everything one all-out run did, for a report the operator can audit."""

    project_id: int
    label: str = ""
    before: list[FieldState] = dc_field(default_factory=list)
    after: list[FieldState] = dc_field(default_factory=list)
    rounds: list[Round] = dc_field(default_factory=list)
    derived: tuple[str, ...] = ()
    sources_before: int = 0
    sources_after: int = 0
    confidence_before: int = 0
    confidence_after: int = 0
    stopped_because: str = ""
    skipped: list[tuple[str, str]] = dc_field(default_factory=list)

    @property
    def articles_read(self) -> int:
        return sum(r.articles_read for r in self.rounds)

    @property
    def gained(self) -> tuple[str, ...]:
        """Fields that were empty before and hold a value now."""
        was = {s.field for s in self.before if s.status != FILLED}
        return tuple(s.field for s in self.after if s.field in was and s.status == FILLED)

    def tracked_score(self) -> tuple[int, int]:
        """(filled, attemptable) over the 12 PRD fields.

        The denominator excludes fields a null is *correct* for, so a project with
        nothing built is not marked down for having no `mw_built`.
        """
        states = [s for s in self.after if s.field in TRACKED_FIELDS]
        attemptable = [s for s in states if s.status != NOT_APPLICABLE]
        return sum(1 for s in attemptable if s.status == FILLED), len(attemptable)


def _label(project: Project) -> str:
    where = project.city or project.county or ""
    return f"{project.company} {project.name}".strip() + (
        f" ({where}, {project.state})" if where else ""
    )


def search_queries(
    project: Project, gaps: list[FieldState], *, limit: int = MAX_QUERIES
) -> list[str]:
    """Queries aimed at this project's *own* missing fields.

    Every query is anchored on the quoted company and locality, so a hit about a
    different operator in the same town cannot come back. Without that anchor,
    "data center investment billion" returns the industry, not the project.
    """
    where = project.city or project.county
    if not project.company or not where:
        return []
    anchor = f'"{project.company}" "{where}"'

    queries: list[str] = [f"{anchor} data center"]
    for state in gaps:
        if not state.is_gap:
            continue
        for phrase in _FIELD_QUERIES.get(state.field, ()):
            candidate = f"{anchor} {phrase}"
            if candidate not in queries:
                queries.append(candidate)
    return queries[:limit]


def project_urls(session: Session, project_id: int) -> set[str]:
    """URLs already cited by this project — the set a harvest must beat."""
    return set(session.scalars(select(Source.url).where(Source.project_id == project_id)))


def harvest_queue(session: Session, project_id: int) -> Harvest:
    """Queued candidates whose slug or headline names this project."""
    from tracker.ingest.discover import matches_known_project, project_identities

    identities = [i for i in project_identities(session) if i.project_id == project_id]
    if not identities:
        return Harvest("queue", skipped="project has no company/locality to match on")

    rows = session.scalars(select(IngestUrl).where(IngestUrl.status == "discovered")).all()
    hits = [r.url for r in rows if matches_known_project(r.url, r.title, identities) == project_id]
    return Harvest("queue", hits, note=f"{len(rows)} candidate(s) in the queue")


def harvest_retry(session: Session, project_id: int) -> Harvest:
    """This project's URLs that previously failed to fetch.

    Worth retrying because a failure may have been transient, and because
    `--browser` can read pages plain HTTP cannot.
    """
    from tracker.ingest.discover import (
        RETRYABLE_STATUSES,
        matches_known_project,
        project_identities,
    )

    identities = [i for i in project_identities(session) if i.project_id == project_id]
    if not identities:
        return Harvest("retry", skipped="project has no company/locality to match on")

    rows = session.scalars(select(IngestUrl).where(IngestUrl.status.in_(RETRYABLE_STATUSES))).all()
    hits = [r.url for r in rows if matches_known_project(r.url, r.title, identities) == project_id]
    return Harvest("retry", hits, note=f"{len(rows)} previously-failed URL(s)")


def harvest_archive(
    session: Session,
    project_id: int,
    *,
    settings: Settings,
    fetcher: Fetcher | None = None,
) -> Harvest:
    """Sitemap archives, filtered to this project.

    This is the key-free search. The archives hold thousands of URLs going back
    years, and `matches_known_project` reduces them to the handful about this
    project — which is what a search engine would have done, without the API.
    """
    import asyncio

    from tracker.ingest.discover import (
        _RawFetcher,
        load_config,
        load_sitemaps,
        matches_known_project,
        project_identities,
        sweep_sitemaps,
    )

    specs = load_sitemaps()
    if not specs:
        return Harvest("archive", skipped="no [[sitemap]] entries configured")

    identities = [i for i in project_identities(session) if i.project_id == project_id]
    if not identities:
        return Harvest("archive", skipped="project has no company/locality to match on")

    _, spec = load_config()
    candidates, problems = asyncio.run(
        sweep_sitemaps(specs, fetcher or _RawFetcher(settings), spec)
    )
    hits = [
        c.url for c in candidates if matches_known_project(c.url, c.title, identities) == project_id
    ]
    note = f"{len(candidates)} archived URL(s) swept"
    if problems:
        note += f"; {len(problems)} sitemap problem(s)"
    return Harvest("archive", hits, note=note)


def harvest_search(
    session: Session,
    project: Project,
    gaps: list[FieldState],
    *,
    settings: Settings,
    provider: object | None = None,
) -> Harvest:
    """Google CSE, aimed at this project's gaps. Needs keys."""
    from tracker.ingest.search import (
        QuotaExhausted,
        SearchError,
        build_provider,
        is_useful_host,
    )

    if provider is None:
        try:
            provider = build_provider(settings)
        except SearchError as exc:
            # Not configured is not a failure: every other harvester still runs.
            # The full message is kept rather than a one-line summary, because it
            # names each backend and its variable — this is the single most useful
            # thing the report can tell an operator whose enrich run came up short.
            return Harvest("search", skipped=str(exc).strip())

    queries = search_queries(project, gaps)
    if not queries:
        return Harvest("search", skipped="project has no company/locality to anchor a query")

    urls: list[str] = []
    ran = 0
    for query in queries:
        try:
            hits = provider.search(query, limit=settings.search_results_per_query)
        except QuotaExhausted as exc:
            log.warning("%s", exc)
            return Harvest("search", urls, note=f"quota exhausted after {ran} quer(ies): {exc}")
        except SearchError as exc:
            log.warning("query %r failed: %s", query, exc)
            continue
        ran += 1
        for hit in hits:
            if is_useful_host(hit.url) and hit.url not in urls:
                urls.append(hit.url)
    return Harvest("search", urls, note=f"{ran} quer(ies) run")


def harvest_refresh(session: Session, project_id: int) -> Harvest:
    """The project's own citations, to be re-read.

    Not redundant: articles are edited after publication, and a re-read under the
    current evidence gate can keep a value the gate at the time discarded.
    """
    from tracker.confidence import PLACEHOLDER_MARKER, SourceView, is_derived

    urls = []
    for source in session.scalars(select(Source).where(Source.project_id == project_id)):
        if PLACEHOLDER_MARKER in (source.url or ""):
            continue  # unfetchable by definition
        if is_derived(SourceView.from_row(source)):
            continue  # reference data, not an article
        urls.append(source.url)
    return Harvest("refresh", urls, note=f"{len(urls)} existing citation(s)")


def run(
    session: Session,
    project_id: int,
    *,
    settings: Settings | None = None,
    census_dir: Path | None = None,
    extractor: Extractor | None = None,
    fetcher: Fetcher | None = None,
    escalate: Fetcher | None = None,
    search_provider: object | None = None,
    cache_dir: Path | None = None,
    max_rounds: int = MAX_ROUNDS,
    max_articles: int = MAX_ARTICLES_PER_ROUND,
    skip_search: bool = False,
    skip_archive: bool = False,
    dry_run: bool = False,
) -> EnrichReport:
    """Recruit every method against one project until rounds stop paying."""
    from tracker.ingest import crawl

    settings = settings or get_settings()
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"no project with id {project_id}")

    report = EnrichReport(
        project_id=project_id,
        label=_label(project),
        before=for_project(project),
        sources_before=len(project_urls(session, project_id)),
        confidence_before=project.confidence or 0,
    )

    # Stage 1: derivation. Free and certain, so it runs before anything is fetched
    # and its fields are never searched for.
    report.derived = _derive(session, project_id, census_dir=census_dir, dry_run=dry_run)
    session.flush()
    project = session.get(Project, project_id)
    assert project is not None

    tried: set[str] = project_urls(session, project_id)
    for number in range(1, max_rounds + 1):
        gaps = for_project(project)
        if not any(s.is_gap for s in gaps):
            report.stopped_because = "every field is filled"
            break

        current = Round(number=number)
        current.harvests.append(harvest_queue(session, project_id))
        current.harvests.append(harvest_retry(session, project_id))
        if not skip_archive and number == 1:
            # The archive is a fixed corpus: sweeping it twice returns the same
            # URLs at the cost of thousands of fetches, so it runs once.
            current.harvests.append(
                harvest_archive(session, project_id, settings=settings, fetcher=fetcher)
            )
        if not skip_search:
            current.harvests.append(
                harvest_search(session, project, gaps, settings=settings, provider=search_provider)
            )
        if number == 1:
            current.harvests.append(harvest_refresh(session, project_id))

        for harvest in current.harvests:
            if harvest.skipped and not any(n == harvest.name for n, _ in report.skipped):
                report.skipped.append((harvest.name, harvest.skipped))

        # `refresh` URLs are deliberately re-read, so they bypass the tried set.
        refreshing = {u for h in current.harvests if h.name == "refresh" for u in h.urls}
        fresh = [u for u in current.urls if u in refreshing or u not in tried]
        if not fresh:
            report.stopped_because = "no harvester found an article we have not already read"
            report.rounds.append(current)
            break

        batch = fresh[:max_articles]
        tried.update(batch)
        before_state = {s.field for s in for_project(project) if s.status == FILLED}

        if dry_run:
            # Harvest only. `crawl.run(dry_run=True)` still fetches every page and
            # still pays for every LLM call -- it merely declines to commit. On the
            # most expensive command in the tool, a preview that bills you is a
            # trap, so extraction is skipped outright and the round reports what it
            # *would* have read.
            current.articles_read = len(batch)
            report.rounds.append(current)
            report.stopped_because = "dry run — harvested only, nothing fetched or extracted"
            break

        crawl.run(
            session,
            batch,
            fetcher=fetcher,
            escalate=escalate,
            extractor=extractor,
            settings=settings,
            cache_dir=cache_dir,
            dry_run=False,
            # Re-reading is the point: a URL already extracted may support a field
            # the gate dropped at the time, and refresh URLs are cited already.
            force=True,
        )
        current.articles_read = len(batch)

        session.flush()
        project = session.get(Project, project_id)
        assert project is not None
        after_state = {s.field for s in for_project(project) if s.status == FILLED}
        current.fields_filled = tuple(sorted(after_state - before_state))
        report.rounds.append(current)

        if not current.fields_filled:
            report.stopped_because = "a full round filled nothing new"
            break
    else:
        report.stopped_because = f"reached the {max_rounds}-round ceiling"

    project = session.get(Project, project_id)
    assert project is not None
    report.after = for_project(project)
    report.sources_after = len(project_urls(session, project_id))
    report.confidence_after = project.confidence or 0
    return report


def _derive(
    session: Session, project_id: int, *, census_dir: Path | None, dry_run: bool
) -> tuple[str, ...]:
    """Run the Census derivation, reporting which fields it filled.

    Absent reference data is not an error here: the command's job is to try every
    method, and a missing optional input means one method is unavailable.
    """
    from tracker.ingest.geo import GeoDataMissing
    from tracker.ingest.geo import run as run_geo

    if census_dir is None:
        return ()
    try:
        geo_report = run_geo(
            session, data_dir=census_dir, dry_run=dry_run, only_project_id=project_id
        )
    except GeoDataMissing as exc:
        log.info("skipping Census derivation: %s", exc)
        return ()

    # Read the counts rather than diffing the row: under --dry-run nothing is
    # written, so a diff would report that derivation found nothing when in fact it
    # found values it was told not to store.
    filled: list[str] = []
    if geo_report.county_filled:
        filled.append("county")
    if geo_report.coords_filled:
        filled += ["lat", "lon"]
    return tuple(filled)


__all__ = [
    "MAX_ARTICLES_PER_ROUND",
    "MAX_QUERIES",
    "MAX_ROUNDS",
    "EnrichReport",
    "Harvest",
    "Round",
    "harvest_archive",
    "harvest_queue",
    "harvest_refresh",
    "harvest_retry",
    "harvest_search",
    "project_urls",
    "run",
    "search_queries",
]
