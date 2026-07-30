"""Feed discovery: parsing, two-tier filtering, and queueing.

Entirely offline — feeds come from fixture XML through an injected fetcher.
"""

from __future__ import annotations

import datetime as dt
import tomllib
from pathlib import Path

import pytest
from sqlalchemy import select

from tracker.ingest import discover
from tracker.ingest.discover import (
    Candidate,
    DiscoverError,
    DiscoverReport,
    FeedSpec,
    FilterSpec,
    default_feeds_path,
    drop_pending,
    load_config,
    parse_feed,
    pending,
    queue_candidates,
    select_candidates,
)
from tracker.ingest.fetch import FetchResult
from tracker.models import IngestUrl
from tracker.vocab import PENDING_URL_STATUS

FIXTURES = Path(__file__).parent / "fixtures"
NOW = dt.datetime(2026, 7, 25, 12, 0, 0)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFeedFetcher:
    """Serves fixture XML for whatever URL is asked for."""

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        body = self.mapping.get(url)
        if body is None:
            return FetchResult(url, False, error="HTTP 404", status=404, fetched_at=NOW, via="feed")
        return FetchResult(url, True, markdown=body, status=200, fetched_at=NOW, via="feed")


# --- Shipped configuration --------------------------------------------------


def test_shipped_feeds_toml_is_valid():
    feeds, spec = load_config()
    assert feeds, "seed/feeds.toml defines no feeds"
    assert all(f.url.startswith("https://") for f in feeds)
    assert spec.topic and spec.signal


def test_shipped_feeds_have_unique_names():
    feeds, _ = load_config()
    names = [f.name for f in feeds]
    assert len(names) == len(set(names))


def test_only_specialist_feeds_imply_the_topic():
    """A general outlet must still prove an article is about data centers."""
    feeds, _ = load_config()
    implied = {f.name for f in feeds if f.topic_implied}
    assert implied == {"datacenterdynamics", "datacenterfrontier"}


def test_shipped_feeds_cover_both_trade_press_and_company_sources():
    """Corroboration needs independent domains, so one kind of feed is not enough."""
    feeds, _ = load_config()
    kinds = {f.source_type for f in feeds}
    assert "trade_press" in kinds
    assert "company_filing" in kinds


def test_feeds_path_is_independent_of_cwd(tmp_path, monkeypatch):
    before = default_feeds_path()
    monkeypatch.chdir(tmp_path)
    assert default_feeds_path() == before


def test_missing_config_says_what_is_expected(tmp_path: Path):
    with pytest.raises(DiscoverError, match="\\[\\[feed\\]\\]"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml_is_reported(tmp_path: Path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[[feed]\nurl = ", encoding="utf-8")
    with pytest.raises(DiscoverError, match="not valid TOML"):
        load_config(bad)


def test_config_without_both_filter_tiers_is_refused(tmp_path: Path):
    """An empty tier would discard everything, since both must match."""
    partial = tmp_path / "partial.toml"
    partial.write_text(
        '[[feed]]\nurl = "https://a.test/rss"\n[filter]\ntopic = ["data cent"]\n',
        encoding="utf-8",
    )
    with pytest.raises(DiscoverError, match="both `topic` and `signal`"):
        load_config(partial)


# --- Parsing ----------------------------------------------------------------


def test_parses_rss():
    entries = parse_feed(fixture("feed_rss.xml"), FeedSpec("dcd", "https://a.test/rss"))
    assert len(entries) == 6
    first = entries[0]
    assert first.url.endswith("/microsoft-fairwater-wisconsin/")
    assert "Fairwater" in first.title
    assert first.published_at == dt.datetime(2026, 7, 20, 9, 15, 0)
    assert first.feed == "dcd"


def test_parses_atom_and_picks_the_alternate_link():
    entries = parse_feed(
        fixture("feed_atom.xml"), FeedSpec("ms", "https://a.test/atom", "company_filing")
    )
    assert len(entries) == 2
    assert entries[0].url == "https://news.microsoft.com/2026/07/mount-pleasant-investment/"
    assert entries[0].published_at == dt.datetime(2026, 7, 19, 11, 0, 0)
    assert entries[0].source_type == "company_filing"


def test_parses_sitemap_and_recovers_a_title_from_the_slug():
    """Sitemaps carry no titles, so the slug has to serve as the headline."""
    entries = parse_feed(fixture("feed_sitemap.xml"), FeedSpec("dcf", "https://a.test/sitemap.xml"))
    assert len(entries) == 2
    assert entries[0].title == "crusoe abilene campus expansion 1200mw"
    assert entries[0].published_at == dt.datetime(2026, 7, 22, 0, 0, 0)


def test_unparseable_xml_raises_a_named_error():
    with pytest.raises(DiscoverError, match="not parseable XML"):
        parse_feed(fixture("feed_broken.xml"), FeedSpec("x", "https://a.test/rss"))


def test_feed_dates_are_stored_naive_utc():
    """Every timestamp in this schema is naive UTC; feeds arrive with offsets."""
    entries = parse_feed(fixture("feed_rss.xml"), FeedSpec("dcd", "https://a.test/rss"))
    assert all(e.published_at is None or e.published_at.tzinfo is None for e in entries)


def test_per_feed_entry_cap(monkeypatch):
    """One prolific outlet must not crowd out the others."""
    monkeypatch.setattr(discover, "MAX_PER_FEED", 2)
    entries = parse_feed(fixture("feed_rss.xml"), FeedSpec("dcd", "https://a.test/rss"))
    assert len(entries) == 2


# --- Two-tier filtering -----------------------------------------------------


@pytest.fixture
def spec() -> FilterSpec:
    return load_config()[1]


def test_a_project_announcement_matches(spec):
    keep, reason = spec.matches(
        "Microsoft breaks ground on 900MW Fairwater data center campus in Wisconsin"
    )
    assert keep
    assert "data cent" in reason


def test_industry_commentary_is_rejected_for_lacking_a_project(spec):
    """Passes the topic tier, fails the signal tier -- nothing to extract."""
    keep, reason = spec.matches("The data center industry faces a reckoning")
    assert not keep
    assert "no project signal" in reason


def test_unrelated_news_is_rejected_for_lacking_the_topic(spec):
    keep, reason = spec.matches("Microsoft Teams adds new meeting features")
    assert not keep
    assert "topic" in reason


@pytest.mark.parametrize(
    "headline",
    [
        "Opinion: why the data center boom will not last",
        "Podcast: building a 500MW data center campus",
        "Quarterly results beat analyst expectations at data center REIT campus",
    ],
)
def test_excluded_headline_shapes_are_dropped(spec, headline):
    keep, reason = spec.matches(headline)
    assert not keep
    assert "excluded" in reason


def test_selection_applies_the_age_cutoff(spec):
    # topic_implied mirrors the shipped config for this outlet.
    entries = parse_feed(
        fixture("feed_rss.xml"), FeedSpec("dcd", "https://a.test/rss", topic_implied=True)
    )
    report = DiscoverReport()
    kept = select_candidates(entries, spec, since=NOW - dt.timedelta(days=30), report=report)
    urls = [c.url for c in kept]
    assert any("microsoft-fairwater" in u for u in urls)
    assert not any("ancient-campus" in u for u in urls), "2020 article is outside the window"
    assert report.entries_seen == 6
    assert report.filtered == 4


def test_selection_matches_against_the_url_slug(spec):
    """Slugs carry the headline, and some feeds ship empty titles.

    Hyphenated, so this only works because the haystack normalizes separators.
    """
    kept = select_candidates(
        [Candidate(url="https://a.test/news/meta-1000mw-data-center-campus/", title="", feed="f")],
        spec,
    )
    assert len(kept) == 1


def test_the_host_is_not_evidence_about_an_article(spec):
    """ "datacenterfrontier.com" would otherwise satisfy the topic tier for free."""
    kept = select_candidates(
        [
            Candidate(
                url="https://www.datacenterfrontier.com/careers/apply/",
                title="We are hiring",
                feed="dcf",
            )
        ],
        spec,
    )
    assert kept == []


def test_a_specialist_feed_keeps_a_headline_that_never_says_data_center(spec):
    """A publication about nothing else does not repeat itself in every headline."""
    headline = "Crusoe expands Abilene campus to 1.2GW"
    assert spec.matches(headline)[0] is False
    assert spec.matches(headline, topic_implied=True)[0] is True


def test_topic_implied_still_requires_a_project_signal(spec):
    """The relaxation is one tier, not both."""
    assert spec.matches("Our editorial team is growing", topic_implied=True)[0] is False


def test_closed_up_capacity_figures_match(spec):
    """ "900MW" is the strongest project signal and is almost always written closed-up."""
    assert spec.matches("New 900MW data center campus announced")[0] is True


# --- Queueing ---------------------------------------------------------------


def candidates() -> list[Candidate]:
    return [
        Candidate(
            url="https://a.test/one/",
            title="A 900MW data center campus",
            feed="dcd",
            published_at=dt.datetime(2026, 7, 20, 9, 0, 0),
        ),
        Candidate(
            url="https://a.test/two/",
            title="Another data center campus investment",
            feed="dcd",
            published_at=dt.datetime(2026, 7, 21, 9, 0, 0),
        ),
    ]


def test_queue_inserts_candidates_as_discovered(session):
    report = DiscoverReport()
    queued = queue_candidates(session, candidates(), run_id="r1", report=report)
    assert len(queued) == 2
    assert report.queued == 2

    rows = list(session.scalars(select(IngestUrl)))
    assert {r.status for r in rows} == {PENDING_URL_STATUS}
    assert {r.feed for r in rows} == {"dcd"}
    assert all(r.title for r in rows), "the title is what makes the queue triageable"
    assert all(r.attempts == 0 for r in rows)


def test_queueing_is_idempotent(session):
    queue_candidates(session, candidates(), run_id="r1", report=DiscoverReport())
    report = DiscoverReport()
    queued = queue_candidates(session, candidates(), run_id="r2", report=report)
    assert queued == []
    assert report.already_known == 2
    assert len(list(session.scalars(select(IngestUrl)))) == 2


def test_a_duplicate_within_one_run_is_queued_once(session):
    duplicated = candidates() + candidates()[:1]
    report = DiscoverReport()
    queue_candidates(session, duplicated, run_id="r1", report=report)
    assert report.queued == 2
    assert report.already_known == 1


def test_an_already_crawled_url_is_never_requeued(session):
    """Discovery must not undo the crawl path's bookkeeping."""
    session.add(IngestUrl(url="https://a.test/one/", run_id="crawl-1", status="ok", attempts=1))
    session.flush()

    report = DiscoverReport()
    queue_candidates(session, candidates(), run_id="r2", report=report)
    assert report.queued == 1
    assert report.already_known == 1
    row = session.scalar(select(IngestUrl).where(IngestUrl.url == "https://a.test/one/"))
    assert row.status == "ok", "the crawled row must be left exactly as it was"


def test_a_failed_url_is_not_requeued_either(session):
    session.add(
        IngestUrl(url="https://a.test/one/", run_id="crawl-1", status="fetch_error", attempts=3)
    )
    session.flush()
    report = DiscoverReport()
    queue_candidates(session, candidates(), run_id="r2", report=report)
    assert report.queued == 1
    assert report.already_known == 1


# --- Queue inspection -------------------------------------------------------


def test_pending_returns_oldest_published_first(session):
    queue_candidates(session, candidates(), run_id="r1", report=DiscoverReport())
    rows = pending(session)
    assert [r.url for r in rows] == ["https://a.test/one/", "https://a.test/two/"]


def test_pending_excludes_processed_urls(session):
    queue_candidates(session, candidates(), run_id="r1", report=DiscoverReport())
    row = session.scalar(select(IngestUrl).where(IngestUrl.url == "https://a.test/one/"))
    row.status = "ok"
    session.flush()
    assert [r.url for r in pending(session)] == ["https://a.test/two/"]


def test_pending_respects_a_limit(session):
    queue_candidates(session, candidates(), run_id="r1", report=DiscoverReport())
    assert len(pending(session, limit=1)) == 1


def test_drop_pending_removes_selected_urls(session):
    queue_candidates(session, candidates(), run_id="r1", report=DiscoverReport())
    assert drop_pending(session, ["https://a.test/one/"]) == 1
    assert [r.url for r in pending(session)] == ["https://a.test/two/"]


def test_drop_pending_without_urls_clears_the_queue(session):
    queue_candidates(session, candidates(), run_id="r1", report=DiscoverReport())
    assert drop_pending(session) == 2
    assert pending(session) == []


def test_drop_pending_leaves_processed_rows_alone(session):
    session.add(IngestUrl(url="https://a.test/done/", run_id="c", status="ok"))
    queue_candidates(session, candidates(), run_id="r1", report=DiscoverReport())
    drop_pending(session)
    remaining = list(session.scalars(select(IngestUrl)))
    assert [r.url for r in remaining] == ["https://a.test/done/"]


# --- Full run ---------------------------------------------------------------


def feed_mapping(tmp_path: Path) -> tuple[Path, dict[str, str | None]]:
    """A config pointing at three fixture feeds, one of which 404s."""
    config = tmp_path / "feeds.toml"
    shipped = tomllib.loads(default_feeds_path().read_text(encoding="utf-8"))
    lines = [
        # topic_implied mirrors the shipped config: DCD covers only data centers.
        '[[feed]]\nname = "dcd"\nurl = "https://a.test/rss"\n'
        'source_type = "trade_press"\ntopic_implied = true\n',
        '[[feed]]\nname = "ms"\nurl = "https://a.test/atom"\nsource_type = "company_filing"\n',
        '[[feed]]\nname = "gone"\nurl = "https://a.test/missing"\n',
        "[filter]\n",
        f"topic = {shipped['filter']['topic']!r}\n",
        f"signal = {shipped['filter']['signal']!r}\n",
        f"exclude = {shipped['filter']['exclude']!r}\n",
    ]
    config.write_text("".join(lines), encoding="utf-8")
    return config, {
        "https://a.test/rss": fixture("feed_rss.xml"),
        "https://a.test/atom": fixture("feed_atom.xml"),
        "https://a.test/missing": None,
    }


def test_run_queues_matches_and_survives_a_dead_feed(session, tmp_path: Path):
    config, mapping = feed_mapping(tmp_path)
    fetcher = FakeFeedFetcher(mapping)

    report, queued = discover.run(
        session, feeds_path=config, fetcher=fetcher, since_days=None, run_id="r1"
    )

    assert report.feeds_polled == 3
    assert report.feeds_failed == 1, "the 404 feed must be reported, not fatal"
    assert ("gone", "HTTP 404") in report.failures
    assert report.queued >= 3, "matches from the two live feeds"

    urls = {c.url for c in queued}
    assert any("microsoft-fairwater" in u for u in urls)
    assert any("xai-colossus" in u for u in urls)
    assert any("mount-pleasant-investment" in u for u in urls)
    assert not any("teams-features" in u for u in urls)
    assert not any("boom-will-not-last" in u for u in urls)


def test_run_is_idempotent(session, tmp_path: Path):
    config, mapping = feed_mapping(tmp_path)
    fetcher = FakeFeedFetcher(mapping)
    first, _ = discover.run(session, feeds_path=config, fetcher=fetcher, since_days=None)
    second, queued = discover.run(session, feeds_path=config, fetcher=fetcher, since_days=None)
    assert queued == []
    assert second.queued == 0
    assert second.already_known == first.queued


def test_run_dry_run_writes_nothing(session, tmp_path: Path):
    config, mapping = feed_mapping(tmp_path)
    report, would_queue = discover.run(
        session,
        feeds_path=config,
        fetcher=FakeFeedFetcher(mapping),
        since_days=None,
        dry_run=True,
    )
    assert report.queued > 0, "the report still describes what would happen"
    assert would_queue, "and the candidates are returned for inspection"
    assert list(session.scalars(select(IngestUrl))) == []


def test_run_honours_the_age_window(session, tmp_path: Path):
    config, mapping = feed_mapping(tmp_path)
    report, queued = discover.run(
        session, feeds_path=config, fetcher=FakeFeedFetcher(mapping), since_days=1
    )
    del report
    # Fixture articles are dated July 2026; "1 day" from the real clock excludes
    # all of them, which proves the cutoff is applied rather than ignored.
    assert queued == []
