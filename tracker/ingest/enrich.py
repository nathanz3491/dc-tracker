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
5. **search** — the configured web-search backend (Serper recommended), with
   queries built from the project's *own* gaps, and Wikipedia hits mined for
   their references. Skipped with a clear message when no key is configured.
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

from sqlalchemy import literal, select
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
        """(filled, attemptable) over the 12 PRD fields, after the run."""
        return report_score(self.after)

    def score_before(self) -> tuple[int, int]:
        """The same, before the run — so a batch report can show movement."""
        return report_score(self.before)


def report_score(states: list[FieldState]) -> tuple[int, int]:
    """(filled, attemptable) over the 12 tracked fields for one project's states.

    Attemptable excludes fields a null is *correct* for, so a project with nothing
    built is not marked down for having no `mw_built`.
    """
    tracked = [s for s in states if s.field in TRACKED_FIELDS]
    attemptable = [s for s in tracked if s.status != NOT_APPLICABLE]
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
    from tracker.ingest.discover import (
        matches_known_project,
        newsroom_companies,
        project_identities,
    )

    implied = newsroom_companies()

    identities = [i for i in project_identities(session) if i.project_id == project_id]
    if not identities:
        return Harvest("queue", skipped="project has no company/locality to match on")

    rows = session.scalars(select(IngestUrl).where(IngestUrl.status == "discovered")).all()
    hits = [
        r.url
        for r in rows
        if matches_known_project(r.url, r.title, identities, implied_companies=implied)
        == project_id
    ]
    return Harvest("queue", hits, note=f"{len(rows)} candidate(s) in the queue")


def harvest_retry(session: Session, project_id: int) -> Harvest:
    """This project's URLs that previously failed to fetch.

    Worth retrying because a failure may have been transient, and because
    `--browser` can read pages plain HTTP cannot.
    """
    from tracker.ingest.discover import (
        RETRYABLE_STATUSES,
        matches_known_project,
        newsroom_companies,
        project_identities,
    )

    implied = newsroom_companies()
    identities = [i for i in project_identities(session) if i.project_id == project_id]
    if not identities:
        return Harvest("retry", skipped="project has no company/locality to match on")

    rows = session.scalars(select(IngestUrl).where(IngestUrl.status.in_(RETRYABLE_STATUSES))).all()
    hits = [
        r.url
        for r in rows
        if matches_known_project(r.url, r.title, identities, implied_companies=implied)
        == project_id
    ]
    return Harvest("retry", hits, note=f"{len(rows)} previously-failed URL(s)")


@dataclass
class ArchiveSweep:
    """One walk of every configured sitemap, reusable across projects.

    Sweeping is the expensive part -- ~1,700 matching URLs over a dozen sitemaps,
    each a fetch. The *matching* is free. So a multi-project run sweeps once and
    filters the same corpus per project; doing it inside the per-project loop
    would have re-fetched every archive thirty times to obtain identical bytes.
    """

    candidates: list = dc_field(default_factory=list)
    problems: list[str] = dc_field(default_factory=list)
    skipped: str | None = None

    @property
    def note(self) -> str:
        note = f"{len(self.candidates)} archived URL(s) swept"
        if self.problems:
            note += f"; {len(self.problems)} sitemap problem(s)"
        return note


def sweep_archives(settings: Settings, fetcher: Fetcher | None = None) -> ArchiveSweep:
    """Walk every configured sitemap once."""
    import asyncio

    from tracker.ingest.discover import (
        _RawFetcher,
        load_config,
        load_sitemaps,
        sweep_sitemaps,
    )

    specs = load_sitemaps()
    if not specs:
        return ArchiveSweep(skipped="no [[sitemap]] entries configured")

    _, spec = load_config()
    candidates, problems = asyncio.run(
        sweep_sitemaps(specs, fetcher or _RawFetcher(settings), spec)
    )
    return ArchiveSweep(candidates=candidates, problems=problems)


def harvest_archive(
    session: Session,
    project_id: int,
    *,
    settings: Settings,
    fetcher: Fetcher | None = None,
    sweep: ArchiveSweep | None = None,
) -> Harvest:
    """Sitemap archives, filtered to this project.

    This is the key-free search. The archives hold thousands of URLs going back
    years, and `matches_known_project` reduces them to the handful about this
    project — which is what a search engine would have done, without the API.

    `sweep` supplies an already-walked corpus so a multi-project run pays the
    fetch cost once.
    """
    from tracker.ingest.discover import (
        matches_known_project,
        newsroom_companies,
        project_identities,
    )

    if sweep is None:
        sweep = sweep_archives(settings, fetcher)
    if sweep.skipped:
        return Harvest("archive", skipped=sweep.skipped)

    identities = [i for i in project_identities(session) if i.project_id == project_id]
    if not identities:
        return Harvest("archive", skipped="project has no company/locality to match on")

    implied = newsroom_companies()
    hits = [
        c.url
        for c in sweep.candidates
        if matches_known_project(c.url, c.title, identities, implied_companies=implied)
        == project_id
    ]
    return Harvest("archive", hits, note=sweep.note)


def harvest_search(
    session: Session,
    project: Project,
    gaps: list[FieldState],
    *,
    settings: Settings,
    provider: object | None = None,
) -> Harvest:
    """The configured search backend (Serper/Google/Brave/Bocha), aimed at this
    project's gaps. Needs one key in .env.

    A Wikipedia article among the hits is mined for its references too — the
    campus article's bibliography names the operator's own announcements and
    the local coverage, which is precisely what a gap-filling read wants.
    """
    from tracker.ingest import wiki
    from tracker.ingest.discover import load_config
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
    note = ""
    for query in queries:
        try:
            hits = provider.search(query, limit=settings.search_results_per_query)
        except QuotaExhausted as exc:
            log.warning("%s", exc)
            note = f"quota exhausted after {ran} quer(ies): {exc}"
            break
        except SearchError as exc:
            log.warning("query %r failed: %s", query, exc)
            continue
        ran += 1
        for hit in hits:
            if is_useful_host(hit.url) and hit.url not in urls:
                urls.append(hit.url)

    wiki_urls = [u for u in urls if wiki.is_wikipedia(u)]
    mined = 0
    if wiki_urls:
        try:
            _, spec = load_config()
        except Exception as exc:  # a broken feeds.toml should not kill the harvest
            log.warning("skipping wikipedia mining: %s", exc)
        else:
            for candidate in wiki.mine(wiki_urls, spec, settings=settings):
                if candidate.url not in urls:
                    urls.append(candidate.url)
                    mined += 1

    if not note:
        note = f"{ran} quer(ies) run"
    if mined:
        note += f"; {mined} wikipedia reference(s)"
    return Harvest("search", urls, note=note)


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
    sweep: ArchiveSweep | None = None,
    target_fields: int | None = None,
) -> EnrichReport:
    """Recruit every method against one project until rounds stop paying.

    `target_fields` stops once this many of the 12 tracked fields are filled,
    leaving the rest of a shared budget for the next project. The PRD's bar is 9;
    pushing a project from 9 to 10 costs the same call as pushing another from 6
    to 7, and the second is worth more.
    """
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
        if target_fields is not None:
            filled, _ = report_score(gaps)
            if filled >= target_fields:
                report.stopped_because = f"reached the {target_fields}-field target"
                break

        current = Round(number=number)
        current.harvests.append(harvest_queue(session, project_id))
        current.harvests.append(harvest_retry(session, project_id))
        if not skip_archive and number == 1:
            # The archive is a fixed corpus: sweeping it twice returns the same
            # URLs at the cost of thousands of fetches, so it runs once.
            current.harvests.append(
                harvest_archive(
                    session, project_id, settings=settings, fetcher=fetcher, sweep=sweep
                )
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


#: The PRD's bar: nine of the twelve tracked fields populated.
DEFAULT_TARGET_FIELDS = 9


@dataclass
class BatchReport:
    """A multi-project run: what each project gained, and what the batch cost."""

    reports: list[EnrichReport] = dc_field(default_factory=list)
    sweep_note: str | None = None
    budget_exhausted: bool = False

    @property
    def articles_read(self) -> int:
        return sum(r.articles_read for r in self.reports)

    def reached(self, target: int) -> int:
        return sum(1 for r in self.reports if r.tracked_score()[0] >= target)

    def reached_before(self, target: int) -> int:
        return sum(1 for r in self.reports if r.score_before()[0] >= target)


def select_projects(
    session: Session, limit: int | None, *, target: int = DEFAULT_TARGET_FIELDS
) -> list[int]:
    """The projects worth spending a bounded budget on, best first.

    ``limit=None`` means every project below the target — the `--all` case. The
    ordering still matters there: the run shares one `--budget`, so whichever
    projects sort first get the articles, and closest-first is what converts the
    most projects before the budget runs dry.

    Ordered by how close each already is to `target`, then by planned capacity.
    Two reasons, and the first is the important one:

    * **Closest-first converts the most projects per call.** Taking a project from
      8 fields to 9 costs one article; taking one from 4 to 9 costs several and may
      not get there. The PRD asks for 20-30 projects done properly, so the metric
      is how many clear the bar, not how many fields move in total.
    * **Capacity breaks ties toward the projects that matter.** A 1 GW campus is
      worth completing before a 20 MW one.

    Projects already at or past the target are excluded — they need nothing.
    """
    from sqlalchemy import case, desc

    filled = sum(
        (case((getattr(Project, f).is_not(None), 1), else_=0) for f in TRACKED_FIELDS),
        start=literal(0),
    )
    stmt = (
        select(Project.id, filled.label("n"))
        .where(filled < target)
        .order_by(desc("n"), desc(Project.mw_planned.is_not(None)), desc(Project.mw_planned))
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = session.execute(stmt).all()
    return [row[0] for row in rows]


def run_many(
    session: Session,
    project_ids: list[int],
    *,
    settings: Settings | None = None,
    target_fields: int | None = DEFAULT_TARGET_FIELDS,
    max_articles: int = 200,
    max_articles_per_round: int = MAX_ARTICLES_PER_ROUND,
    max_rounds: int = MAX_ROUNDS,
    skip_archive: bool = False,
    dry_run: bool = False,
    **kwargs,
) -> BatchReport:
    """Enrich several projects under one shared article budget.

    The archive is swept **once** and the same corpus is filtered per project.
    Sweeping inside the per-project loop would re-fetch ~1,700 URLs across a dozen
    sitemaps for every project in the batch, to obtain identical bytes.

    `max_articles` is the budget for the whole batch, not per project, so one
    obscure project cannot consume a run aimed at thirty.
    """
    settings = settings or get_settings()
    batch = BatchReport()

    sweep = None
    if not skip_archive and not dry_run:
        sweep = sweep_archives(settings, kwargs.get("fetcher"))
        batch.sweep_note = sweep.skipped or sweep.note
    elif not skip_archive:
        # A dry run reports what it would harvest, and the sweep is read-only.
        sweep = sweep_archives(settings, kwargs.get("fetcher"))
        batch.sweep_note = sweep.skipped or sweep.note

    # Divide the budget rather than letting the first project take what it likes.
    # Measured: with a flat per-round cap of 25 and a budget of 120, five projects
    # consumed the lot and twenty-five never ran. A fair share means every selected
    # project gets a turn, which matters because the run is judged on how many
    # projects clear the target, not on how much any one of them moves.
    fair_share = max(1, max_articles // max(1, len(project_ids)))
    per_project = min(max_articles_per_round, fair_share)

    spent = 0
    unreached = list(project_ids)
    for project_id in project_ids:
        remaining = max_articles - spent
        if remaining <= 0:
            batch.budget_exhausted = True
            log.info(
                "article budget of %d spent; %d project(s) never ran",
                max_articles,
                len(unreached),
            )
            break
        unreached.remove(project_id)
        report = run(
            session,
            project_id,
            settings=settings,
            max_articles=min(per_project, remaining),
            max_rounds=max_rounds,
            skip_archive=skip_archive,
            dry_run=dry_run,
            sweep=sweep,
            target_fields=target_fields,
            **kwargs,
        )
        spent += report.articles_read
        batch.reports.append(report)
    return batch


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
    "DEFAULT_TARGET_FIELDS",
    "MAX_ARTICLES_PER_ROUND",
    "MAX_QUERIES",
    "MAX_ROUNDS",
    "ArchiveSweep",
    "BatchReport",
    "EnrichReport",
    "Harvest",
    "Round",
    "harvest_archive",
    "harvest_queue",
    "harvest_refresh",
    "harvest_retry",
    "harvest_search",
    "project_urls",
    "report_score",
    "run",
    "run_many",
    "search_queries",
    "select_projects",
    "sweep_archives",
]
