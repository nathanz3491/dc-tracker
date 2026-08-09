"""All-out single-project enrichment, entirely offline.

No network and no LLM: the harvesters take injected fetchers and the extractor is a
`FakeLLM`, so a fresh clone runs this without an API key.

The assertions that carry the design:

* :func:`test_the_loop_stops_when_a_round_fills_nothing` — the actual stop
  condition. "Cost no object" has to mean *diminishing returns*, not unbounded.
* :func:`test_dry_run_does_not_fetch_or_extract` — a preview of the most expensive
  command in the tool must not bill you.
* :func:`test_queries_are_anchored_on_the_project` — an unanchored gap query
  returns the industry, not the project.
* :func:`test_a_field_a_null_is_correct_for_is_not_counted_as_failure`.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from tracker.dedup import dedup_key
from tracker.gaps import MISSING, NOT_APPLICABLE
from tracker.ingest import enrich
from tracker.ingest.fetch import FetchResult
from tracker.llm import LLMReply
from tracker.models import IngestUrl, Project, Source, utcnow

NOW = dt.datetime(2026, 3, 1, 12, 0, 0)
ARTICLE = (
    "STACK Infrastructure has begun construction on its Hillsboro campus in Oregon. "
    "The company said the site will draw 230 megawatts at full buildout and represents "
    "an investment of $1.2 billion. The first phase is expected online in 2027. "
    "STACK announced the project on March 4, 2024."
)


class FakeFetcher:
    """Returns the same article for any URL, and records what was asked for."""

    def __init__(self, markdown: str = ARTICLE) -> None:
        self.markdown = markdown
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        return FetchResult(
            url=url, ok=True, markdown=self.markdown, status=200, fetched_at=NOW, via="httpx"
        )


class FakeLLM:
    """Returns a canned extraction. Records how many times it was called."""

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload if payload is not None else _extraction()
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply:
        self.calls += 1
        return LLMReply(json.dumps(self.payload), "stop", 900, 300, "fake-model")


class EmptyLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__({"projects": []})


def _extraction() -> dict:
    return {
        "projects": [
            {
                "name": "STACK Infrastructure Hillsboro Campus",
                "company": "STACK Infrastructure",
                "city": "Hillsboro",
                "state": "OR",
                "country": "US",
                "mw_planned": 230,
                "investment_usd": 1_200_000_000,
                "phase": "construction",
                "expected_online": "2027-01-01",
                "first_announced": "2024-03-04",
                "evidence": [
                    {"field": "mw_planned", "quote": "the site will draw 230 megawatts"},
                    {"field": "investment_usd", "quote": "an investment of $1.2 billion"},
                    {"field": "phase", "quote": "has begun construction on its Hillsboro campus"},
                    {"field": "expected_online", "quote": "expected online in 2027"},
                    {"field": "first_announced", "quote": "announced the project on March 4, 2024"},
                ],
            }
        ]
    }


class FakeSearch:
    """A SearchProvider that returns a fixed URL list and logs the queries."""

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 10):
        from tracker.ingest.search import SearchHit

        self.queries.append(query)
        return [SearchHit(url=u, title="STACK Hillsboro", snippet="") for u in self.urls[:limit]]


def add_project(session, **kwargs) -> Project:
    defaults = {
        "name": "STACK Infrastructure Hillsboro Campus",
        "company": "STACK Infrastructure",
        "city": "Hillsboro",
        "state": "OR",
        "country": "US",
        "phase": "announced",
        "confidence": 1,
    }
    merged = {**defaults, **kwargs}
    merged.setdefault(
        "dedup_key",
        dedup_key(merged["company"], merged.get("city"), merged.get("county"), merged["state"]),
    )
    project = Project(**merged)
    session.add(project)
    session.flush()
    return project


def add_source(session, project_id: int, url: str, **kwargs) -> Source:
    source = Source(
        project_id=project_id,
        url=url,
        source_type=kwargs.pop("source_type", "trade_press"),
        fetched_at=kwargs.pop("fetched_at", utcnow()),
        **kwargs,
    )
    session.add(source)
    session.flush()
    return source


def add_queued(session, url: str, title: str, status: str = "discovered") -> IngestUrl:
    row = IngestUrl(url=url, title=title, status=status, run_id="test")
    session.add(row)
    session.flush()
    return row


def run(session, project_id, **kwargs):
    """`enrich.run` with offline defaults."""
    kwargs.setdefault("fetcher", FakeFetcher())
    kwargs.setdefault("extractor", FakeLLM())
    kwargs.setdefault("skip_archive", True)
    kwargs.setdefault("skip_search", True)
    return enrich.run(session, project_id, **kwargs)


# --- query construction -----------------------------------------------------


def test_queries_are_anchored_on_the_project(session):
    """An unanchored gap query returns the industry, not this project."""
    project = add_project(session)
    queries = enrich.search_queries(project, enrich.for_project(project))

    assert queries, "a project with gaps must produce queries"
    for query in queries:
        assert '"STACK Infrastructure"' in query
        assert '"Hillsboro"' in query


def test_queries_target_the_missing_fields_only(session):
    project = add_project(session, mw_planned=230.0, investment_usd=1)
    queries = " ".join(enrich.search_queries(project, enrich.for_project(project)))

    assert "megawatts capacity" not in queries, "mw_planned is filled; do not search for it"
    assert "investment billion" not in queries, "investment is filled; do not search for it"
    assert "expected online date" in queries, "expected_online is missing; search for it"


def test_a_county_only_project_still_anchors_its_queries(session):
    project = add_project(session, city=None, county="Washington County")
    queries = enrich.search_queries(project, enrich.for_project(project))
    assert queries and all('"Washington County"' in q for q in queries)


def test_a_project_with_no_locality_yields_no_queries():
    """Guards the contract, not a real row.

    `ck_project_locality` makes a city-less, county-less project impossible to
    store, so this builds a detached object: `search_queries` is pure and must not
    emit an unanchored query if it is ever handed one.
    """
    detached = Project(name="Nowhere", company="Acme", state="OR", country="US", phase="announced")
    assert enrich.search_queries(detached, enrich.for_project(detached)) == []


def test_queries_are_capped(session):
    project = add_project(session)
    assert len(enrich.search_queries(project, enrich.for_project(project), limit=3)) == 3


# --- harvesters -------------------------------------------------------------


def test_the_queue_harvester_finds_only_this_projects_candidates(session):
    project = add_project(session)
    add_queued(session, "https://x.com/stack-hillsboro-expansion", "STACK Hillsboro expansion")
    add_queued(session, "https://x.com/google-council-bluffs", "Google Council Bluffs")

    harvest = enrich.harvest_queue(session, project.id)
    assert harvest.urls == ["https://x.com/stack-hillsboro-expansion"]


def test_the_queue_harvester_ignores_already_crawled_rows(session):
    project = add_project(session)
    add_queued(session, "https://x.com/stack-hillsboro-a", "STACK Hillsboro", status="ok")
    assert enrich.harvest_queue(session, project.id).urls == []


def test_the_retry_harvester_picks_up_this_projects_failures(session):
    project = add_project(session)
    add_queued(session, "https://x.com/stack-hillsboro-b", "STACK Hillsboro", status="fetch_error")
    add_queued(session, "https://x.com/other-project", "Somebody Else", status="fetch_error")

    assert enrich.harvest_retry(session, project.id).urls == ["https://x.com/stack-hillsboro-b"]


def test_refresh_excludes_placeholder_and_derived_citations(session):
    """Neither is a fetchable article, so re-reading them is wasted budget."""
    project = add_project(session)
    add_source(session, project.id, "https://real.example/article")
    add_source(session, project.id, "https://news.example/PLACEHOLDER-replace-me/")
    add_source(
        session,
        project.id,
        "https://www2.census.gov/geo/reference.txt",
        source_type="government_doc",
        extractor="derived:census-place-2020",
    )

    assert enrich.harvest_refresh(session, project.id).urls == ["https://real.example/article"]


def test_search_is_skipped_with_an_actionable_message(session, monkeypatch):
    from tracker.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    settings = Settings(minimax_api_key="x")
    project = add_project(session)

    harvest = enrich.harvest_search(
        session, project, enrich.for_project(project), settings=settings
    )
    assert harvest.urls == []
    assert harvest.skipped and "TRACKER_GOOGLE_API_KEY" in harvest.skipped


def test_search_harvester_uses_the_provider(session, monkeypatch):
    from tracker.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    settings = Settings(minimax_api_key="x")
    project = add_project(session)
    provider = FakeSearch(["https://trade.example/stack-hillsboro"])

    harvest = enrich.harvest_search(
        session, project, enrich.for_project(project), settings=settings, provider=provider
    )
    assert harvest.urls == ["https://trade.example/stack-hillsboro"]
    assert provider.queries, "the provider must actually be queried"


def test_the_search_harvest_names_the_backend_it_used(session, monkeypatch):
    """Which engine answered is load-bearing, not trivia.

    This project ran `enrich` against Bocha's Chinese-web index for weeks while
    looking for US trade press, and nothing on screen said so — the harvest read
    as a method that found nothing rather than one pointed at the wrong index.
    """
    from tracker.config import Settings
    from tracker.ingest.search import provider_name

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    settings = Settings(minimax_api_key="x")
    project = add_project(session)

    class NamedSearch(FakeSearch):
        NAME = "serper"

    harvest = enrich.harvest_search(
        session,
        project,
        enrich.for_project(project),
        settings=settings,
        provider=NamedSearch(["https://trade.example/stack-hillsboro"]),
    )
    assert harvest.note and "via serper" in harvest.note
    assert provider_name(NamedSearch([])) == "serper"


def test_every_real_backend_can_name_itself(session):
    """A provider with no NAME would report as a class name in the run log."""
    from tracker.ingest.search import PROVIDERS, provider_name

    for expected, cls in PROVIDERS.items():
        assert expected == cls.NAME, f"{cls.__name__} must call itself {expected!r}"
        assert expected == provider_name(cls.__new__(cls))


def test_search_harvester_mines_wikipedia_hits(session, monkeypatch):
    """A Wikipedia hit is worth more than its own page: its references name the
    primary sources — the operator's IR release above all."""
    from tracker.config import Settings
    from tracker.ingest import wiki

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr(
        wiki,
        "external_links",
        lambda url, settings=None: [
            "https://investor.example/press/stack-hillsboro-data-center-expansion",
            "https://opencorporates.example/companies/123",
        ],
    )
    settings = Settings(minimax_api_key="x")
    project = add_project(session)
    provider = FakeSearch(["https://en.wikipedia.org/wiki/STACK_Hillsboro"])

    harvest = enrich.harvest_search(
        session, project, enrich.for_project(project), settings=settings, provider=provider
    )
    assert "https://investor.example/press/stack-hillsboro-data-center-expansion" in harvest.urls
    assert not any("opencorporates" in u for u in harvest.urls), "no keyword in the slug"
    assert harvest.note and "wikipedia reference" in harvest.note


# --- the loop ---------------------------------------------------------------


def test_it_fills_fields_from_a_harvested_article(session):
    project = add_project(session)
    add_queued(session, "https://x.com/stack-hillsboro-c", "STACK Hillsboro campus")

    report = run(session, project.id)

    assert "mw_planned" in report.gained
    assert "investment_usd" in report.gained
    assert "expected_online" in report.gained
    row = session.get(Project, project.id)
    assert row.mw_planned == 230.0
    assert row.investment_usd == 1_200_000_000


def test_the_loop_stops_when_a_round_fills_nothing(session):
    """The real stop condition. An LLM that extracts nothing must end the run."""
    project = add_project(session)
    add_queued(session, "https://x.com/stack-hillsboro-d", "STACK Hillsboro")

    report = enrich.run(
        session,
        project.id,
        fetcher=FakeFetcher(),
        extractor=EmptyLLM(),
        skip_archive=True,
        skip_search=True,
        max_rounds=6,
    )
    assert len(report.rounds) == 1, "one barren round is enough to stop"
    assert report.stopped_because == "a full round filled nothing new"


def test_it_stops_when_no_harvester_has_anything_new(session):
    project = add_project(session)
    report = run(session, project.id)
    assert report.articles_read == 0
    assert "not already read" in report.stopped_because


def test_it_stops_once_every_field_is_filled(session):
    project = add_project(
        session,
        customer="A Tenant",
        county="Washington County",
        lat=45.5,
        lon=-122.9,
        mw_planned=230.0,
        mw_built=10.0,
        investment_usd=1,
        phase="construction",
        first_announced=dt.date(2024, 3, 4),
        expected_online=dt.date(2027, 1, 1),
        blocker="something",
    )
    report = run(session, project.id)
    assert report.stopped_because == "every field is filled"
    assert report.articles_read == 0


def test_the_round_ceiling_is_honoured(session):
    """A harvester that keeps yielding new URLs must still terminate."""
    project = add_project(session)
    for i in range(30):
        add_queued(session, f"https://x.com/stack-hillsboro-{i}", "STACK Hillsboro")

    report = enrich.run(
        session,
        project.id,
        fetcher=FakeFetcher(),
        extractor=FakeLLM(),
        skip_archive=True,
        skip_search=True,
        max_rounds=2,
        max_articles=1,
    )
    assert len(report.rounds) <= 2


def test_dry_run_does_not_fetch_or_extract(session):
    """A preview of the most expensive command must cost nothing."""
    project = add_project(session)
    add_queued(session, "https://x.com/stack-hillsboro-e", "STACK Hillsboro")
    fetcher, llm = FakeFetcher(), FakeLLM()

    report = enrich.run(
        session,
        project.id,
        fetcher=fetcher,
        extractor=llm,
        skip_archive=True,
        skip_search=True,
        dry_run=True,
    )

    assert fetcher.calls == [], "dry run must not fetch"
    assert llm.calls == 0, "dry run must not pay for extraction"
    assert report.rounds[0].articles_read == 1, "it still reports what it would read"
    assert "dry run" in report.stopped_because
    assert session.get(Project, project.id).mw_planned is None


def test_an_unknown_project_id_is_an_error(session):
    with pytest.raises(LookupError):
        run(session, 9999)


# --- reporting --------------------------------------------------------------


def test_a_field_a_null_is_correct_for_is_not_counted_as_failure(session):
    """`mw_built` on an announced project is right, so it leaves the denominator."""
    project = add_project(session, phase="announced")
    report = run(session, project.id)

    states = {s.field: s for s in report.after}
    assert states["mw_built"].status == NOT_APPLICABLE
    assert states["mw_built"].reason and "nothing is built" in states["mw_built"].reason

    _, attemptable = report.tracked_score()
    assert attemptable == 11, "mw_built is excluded from the 12 while nothing is built"


def test_mw_built_counts_once_construction_starts(session):
    project = add_project(session, phase="construction")
    report = run(session, project.id)
    states = {s.field: s for s in report.after}
    assert states["mw_built"].status == MISSING
    _, attemptable = report.tracked_score()
    assert attemptable == 12


def test_often_absent_fields_are_flagged_rather_than_failed(session):
    project = add_project(session)
    report = run(session, project.id)
    states = {s.field: s for s in report.after}
    for name in ("blocker", "customer"):
        assert states[name].status == MISSING
        assert states[name].reason, f"{name} must explain that a null may be correct"


def test_the_report_records_citation_and_confidence_movement(session):
    project = add_project(session)
    add_queued(session, "https://x.com/stack-hillsboro-f", "STACK Hillsboro")

    report = run(session, project.id)
    assert report.sources_before == 0
    assert report.sources_after >= 1
    assert report.confidence_after >= report.confidence_before


def test_gained_lists_only_newly_filled_fields(session):
    project = add_project(session, mw_planned=230.0)
    add_queued(session, "https://x.com/stack-hillsboro-g", "STACK Hillsboro")

    report = run(session, project.id)
    assert "mw_planned" not in report.gained, "it was already filled"
    assert "investment_usd" in report.gained


def test_skipped_harvesters_are_reported_once(session, monkeypatch):
    from tracker.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    project = add_project(session)
    for i in range(3):
        add_queued(session, f"https://x.com/stack-hillsboro-s{i}", "STACK Hillsboro")

    report = enrich.run(
        session,
        project.id,
        settings=Settings(minimax_api_key="x"),
        fetcher=FakeFetcher(),
        extractor=FakeLLM(),
        skip_archive=True,
        skip_search=False,
        max_articles=1,
    )
    names = [n for n, _ in report.skipped]
    assert names.count("search") == 1, "a skip reason must not repeat per round"


# --- derivation -------------------------------------------------------------


def test_derivation_runs_before_any_fetch_and_targets_one_project(session, tmp_path):
    """Free and certain, so it must never be a search target."""
    import zipfile

    from tests.test_geo import COUNTY_ROWS, GAZ_ROWS
    from tracker.ingest import geo

    root = tmp_path / "census"
    root.mkdir()
    (root / geo.COUNTY_FILE).write_text(COUNTY_ROWS, encoding="utf-8")
    with zipfile.ZipFile(root / geo.GAZETTEER_FILE, "w") as archive:
        archive.writestr("2024_Gaz_place_national.txt", GAZ_ROWS)

    target = add_project(session, city="Memphis", state="TN", company="xAI", name="Colossus")
    other = add_project(session, city="Mount Pleasant", state="WI", company="Microsoft")

    report = run(session, target.id, census_dir=root)

    assert "county" in report.derived
    assert session.get(Project, target.id).county == "Shelby County"
    assert session.get(Project, other.id).county is None, "only the named project is touched"


def test_a_missing_census_directory_is_not_an_error(session, tmp_path):
    project = add_project(session)
    report = run(session, project.id, census_dir=tmp_path / "absent")
    assert report.derived == ()


# --- the token-leak regression ---------------------------------------------


def test_a_company_suffix_is_not_a_distinctive_project_token(session):
    """Regression: "STACK Infrastructure Hillsboro Campus" matched every STACK story.

    `company_key` strips corporate suffixes, so "STACK Infrastructure" keys to
    "stack" and left "infrastructure" looking distinctive — while every STACK slug
    contains "stack-infrastructure". Company-wide pieces ("raises $400 million")
    were harvested as evidence about one Hillsboro project.
    """
    from tracker.ingest.discover import matches_known_project, project_identities

    project = add_project(session)
    identity = next(i for i in project_identities(session) if i.project_id == project.id)
    assert "infrastructure" not in identity.name_tokens
    assert identity.name_tokens == ("hillsboro",)

    company_wide = "https://dcf.example/stack-infrastructure-raises-400-million-to-fund-growth"
    assert matches_known_project(company_wide, None, [identity]) is None

    specific = "https://dcf.example/stack-infrastructure-expands-in-hillsboro-oregon"
    assert matches_known_project(specific, None, [identity]) == project.id


def test_an_alias_company_still_matches(session):
    """The raw-name exclusion must not break Facebook -> meta aliasing."""
    from tracker.ingest.discover import matches_known_project, project_identities

    project = add_project(
        session, company="Facebook", name="Facebook Altoona Campus", city="Altoona", state="IA"
    )
    identity = next(i for i in project_identities(session) if i.project_id == project.id)
    url = "https://dcf.example/meta-expands-its-altoona-iowa-data-center"
    assert matches_known_project(url, None, [identity]) == project.id


def test_project_urls_reports_existing_citations(session):
    project = add_project(session)
    add_source(session, project.id, "https://a.example/one")
    add_source(session, project.id, "https://b.example/two")
    assert enrich.project_urls(session, project.id) == {
        "https://a.example/one",
        "https://b.example/two",
    }


def test_harvested_urls_are_deduplicated_across_harvesters(session):
    """The same URL in the queue and the archive must be read once."""
    shared = "https://x.com/stack-hillsboro-shared"
    rnd = enrich.Round(
        number=1,
        harvests=[
            enrich.Harvest("queue", [shared, "https://x.com/a"]),
            enrich.Harvest("archive", [shared, "https://x.com/b"]),
        ],
    )
    assert rnd.urls == [shared, "https://x.com/a", "https://x.com/b"]


def test_source_count_is_distinct_urls(session):
    project = add_project(session)
    add_source(session, project.id, "https://a.example/one")
    assert len(enrich.project_urls(session, project.id)) == 1


def test_report_survives_a_project_with_no_gaps_and_no_sources(session):
    project = add_project(session)
    report = run(session, project.id)
    assert report.project_id == project.id
    assert report.label.startswith("STACK Infrastructure")
    assert Path is not None  # import is used by the module under test
    assert isinstance(report.before, list) and isinstance(report.after, list)
    assert session.scalars(select(Source)).all() == []


# --- Batch enrichment -------------------------------------------------------


def test_the_archive_is_swept_once_for_the_whole_batch(session, monkeypatch):
    """The efficiency that makes a 30-project run possible at all.

    Sweeping inside the per-project loop re-fetches ~1,700 URLs across a dozen
    sitemaps for every project, to obtain identical bytes.
    """
    sweeps = []

    def fake_sweep(settings, fetcher=None):
        sweeps.append(1)
        return enrich.ArchiveSweep(candidates=[], problems=[])

    monkeypatch.setattr(enrich, "sweep_archives", fake_sweep)
    ids = [add_project(session, city=c, company=f"Op {c}").id for c in ("Reno", "Mesa", "Plano")]

    enrich.run_many(
        session, ids, fetcher=FakeFetcher(), extractor=FakeLLM(), skip_search=True, dry_run=True
    )
    assert len(sweeps) == 1, f"swept {len(sweeps)} times for 3 projects"


def test_the_budget_is_shared_across_projects(session):
    """One obscure project must not consume a run aimed at thirty."""
    ids = []
    for city in ("Reno", "Mesa", "Plano"):
        project = add_project(session, city=city, company=f"Op {city}")
        ids.append(project.id)
        for i in range(6):
            add_queued(session, f"https://x.com/op-{city.lower()}-{i}", f"Op {city} {city} campus")

    batch = enrich.run_many(
        session,
        ids,
        fetcher=FakeFetcher(),
        extractor=FakeLLM(),
        skip_search=True,
        skip_archive=True,
        max_articles=4,
        max_articles_per_round=2,
        target_fields=None,
    )
    assert batch.articles_read <= 4, f"read {batch.articles_read}, budget was 4"


def test_projects_beyond_the_budget_are_reported_as_never_run(session):
    """With fewer articles than projects, the shortfall must be visible."""
    ids = []
    for city in ("Reno", "Mesa", "Plano", "Ames", "Provo"):
        project = add_project(session, city=city, company=f"Op {city}")
        ids.append(project.id)
        for i in range(4):
            add_queued(session, f"https://x.com/nb-{city.lower()}-{i}", f"Op {city} {city} campus")

    batch = enrich.run_many(
        session,
        ids,
        fetcher=FakeFetcher(),
        extractor=FakeLLM(),
        skip_search=True,
        skip_archive=True,
        max_articles=2,
        target_fields=None,
    )
    assert batch.articles_read <= 2
    assert batch.budget_exhausted, "three projects never ran; the report must say so"
    assert len(batch.reports) < len(ids)


def test_a_project_stops_at_the_target_leaving_budget_for_the_next(session):
    """Taking a project from 9 to 10 costs the same call as another from 6 to 7."""
    project = add_project(
        session,
        city="Hillsboro",
        customer="A Tenant",
        mw_planned=230.0,
        mw_built=1.0,
        investment_usd=1,
        phase="construction",
        first_announced=dt.date(2024, 3, 4),
        expected_online=dt.date(2027, 1, 1),
    )
    add_queued(session, "https://x.com/stack-hillsboro-t", "STACK Hillsboro campus")

    # name, company, customer, city, state, mw_planned, mw_built, investment_usd,
    # phase, first_announced, expected_online = 11 filled already.
    batch = enrich.run_many(
        session,
        [project.id],
        fetcher=FakeFetcher(),
        extractor=FakeLLM(),
        skip_search=True,
        skip_archive=True,
        target_fields=9,
    )
    assert batch.articles_read == 0, "already past the target; must not spend"
    assert "target" in batch.reports[0].stopped_because


def test_select_prefers_projects_closest_to_the_target(session):
    # name, company, city, state, phase, mw_planned, investment_usd,
    # first_announced = 8 of 12, so one field short of the target.
    near = add_project(
        session,
        city="Reno",
        company="Near Co",
        mw_planned=10.0,
        investment_usd=1,
        phase="construction",
        first_announced=dt.date(2024, 1, 1),
    )
    far = add_project(session, city="Mesa", company="Far Co")  # 5 of 12

    chosen = enrich.select_projects(session, 2, target=9)
    assert chosen[0] == near.id, "the project needing one more field comes first"
    assert far.id in chosen


def test_select_skips_projects_already_at_the_target(session):
    done = add_project(
        session,
        city="Reno",
        company="Done Co",
        customer="T",
        mw_planned=1.0,
        mw_built=1.0,
        investment_usd=1,
        phase="construction",
        first_announced=dt.date(2024, 1, 1),
        expected_online=dt.date(2027, 1, 1),
        blocker="x",
    )
    todo = add_project(session, city="Mesa", company="Todo Co")

    chosen = enrich.select_projects(session, 10, target=9)
    assert done.id not in chosen, "nothing to gain, so it must not be selected"
    assert todo.id in chosen


def test_select_breaks_ties_toward_larger_projects(session):
    small = add_project(session, city="Reno", company="Small Co", mw_planned=20.0)
    big = add_project(session, city="Mesa", company="Big Co", mw_planned=1000.0)
    chosen = enrich.select_projects(session, 2, target=9)
    assert chosen.index(big.id) < chosen.index(small.id)


def test_select_with_no_limit_returns_every_project_below_target(session):
    """The `--all` case: no cap, same ordering, finished projects still excluded.

    The ordering is not decoration even when everything is selected — the run
    shares one budget, so whoever sorts first gets the articles before it runs
    dry, and closest-first is what converts the most projects.
    """
    ids = [add_project(session, city=c, company=f"Op {c}").id for c in ("Reno", "Mesa", "Ames")]
    done = add_project(
        session,
        city="Provo",
        company="Done Co",
        customer="T",
        mw_planned=1.0,
        mw_built=1.0,
        investment_usd=1,
        phase="construction",
        first_announced=dt.date(2024, 1, 1),
        expected_online=dt.date(2027, 1, 1),
        blocker="x",
    )

    chosen = enrich.select_projects(session, None, target=9)
    assert set(chosen) == set(ids), "everything below the target, nothing above it"
    assert done.id not in chosen
    assert chosen == enrich.select_projects(session, 10, target=9), (
        "None must mean 'no cap', not a different ordering"
    )


def test_the_batch_report_counts_projects_over_the_bar(session):
    ids = [add_project(session, city=c, company=f"Op {c}").id for c in ("Reno", "Mesa")]
    batch = enrich.run_many(
        session,
        ids,
        fetcher=FakeFetcher(),
        extractor=FakeLLM(),
        skip_search=True,
        skip_archive=True,
    )
    assert len(batch.reports) == 2
    assert batch.reached_before(9) == 0
    assert batch.reached(9) >= 0


def test_the_budget_is_divided_fairly_not_first_come_first_served(session):
    """Measured failure: a flat per-round cap let five projects eat a 120 budget.

    Twenty-five selected projects never ran at all. The run is judged on how many
    projects clear the target, so every one selected has to get a turn.
    """
    ids = []
    for city in ("Reno", "Mesa", "Plano", "Ames"):
        project = add_project(session, city=city, company=f"Op {city}")
        ids.append(project.id)
        for i in range(10):
            add_queued(session, f"https://x.com/op-{city.lower()}-{i}", f"Op {city} {city} campus")

    batch = enrich.run_many(
        session,
        ids,
        fetcher=FakeFetcher(),
        extractor=FakeLLM(),
        skip_search=True,
        skip_archive=True,
        max_articles=8,
        max_articles_per_round=25,
        target_fields=None,
    )
    assert len(batch.reports) == 4, "every selected project must get a turn"
    assert all(r.articles_read <= 2 for r in batch.reports), (
        f"one project took more than its share: {[r.articles_read for r in batch.reports]}"
    )
