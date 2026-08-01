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
    """A general outlet must still prove an article is about data centers.

    Deliberately an exact allowlist rather than a rule. `topic_implied` switches
    off half the filter, so every addition should be an argued decision that fails
    this test until someone writes the argument down — which is what the comments
    below are. A rule-shaped assertion would let a general outlet in silently.
    """
    feeds, _ = load_config()
    implied = {f.name for f in feeds if f.topic_implied}
    assert implied == {
        # Publications that cover nothing but data centers.
        "datacenterdynamics",
        "datacenterfrontier",
        "datacenterknowledge",
        # Bisnow's data-center vertical, not its national feed. Every article in
        # this one is about a data center; `bisnow-national` is not here.
        "bisnow-datacenter",
        # A pure-play operator's own newsroom, like the [[sitemap]] entries.
        "qts-newsroom",
    }


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


# --- Syndicated article bodies ----------------------------------------------
#
# Several outlets serve the feed freely and answer 403 to any non-browser request
# for the article, while syndicating the whole article inside the feed. Caching
# that body is what makes them readable; see discover.cache_feed_text.

_FULL_TEXT_RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <item>
      <title>County rejects the Fairwater rezoning</title>
      <link>https://blocked.test/2026/07/fairwater-rezoning/</link>
      <description>A 300-word teaser that is not the article.</description>
      <content:encoded><![CDATA[<p>%s</p>]]></content:encoded>
    </item>
    <item>
      <title>Summary only, no body</title>
      <link>https://blocked.test/2026/07/summary-only/</link>
      <description>Teaser.</description>
    </item>
  </channel>
</rss>
"""


def _full_text_feed(body: str) -> str:
    return _FULL_TEXT_RSS % body


def test_rss_content_encoded_is_captured_as_the_body():
    entries = parse_feed(_full_text_feed("The board voted 5-2."), FeedSpec("x", "https://a.test/f"))
    assert "The board voted 5-2." in entries[0].content
    assert entries[1].content == "", "an item without content:encoded has no body"


def test_description_is_never_used_as_the_body():
    """A teaser read as the article would make the gate reject real values.

    `description` holds a summary. If it were cached as the body, the evidence
    gate would verify the handful of quotes that happen to appear in the teaser
    and mark everything else unsupported — worse than having no body at all,
    because the result looks like a successful extraction.
    """
    entries = parse_feed(_full_text_feed("Body."), FeedSpec("x", "https://a.test/f"))
    assert "teaser" not in entries[0].content.lower()


def test_cache_feed_text_writes_a_body_the_crawl_path_will_find(tmp_path):
    from tracker.ingest.fetch import cache_path

    body = "The county board voted to reject the rezoning application. " * 12
    entries = parse_feed(_full_text_feed(body), FeedSpec("x", "https://a.test/f"))
    assert discover.cache_feed_text(entries, tmp_path) == 1

    written = cache_path(entries[0].url, tmp_path)
    assert written.is_file()
    text = written.read_text(encoding="utf-8")
    assert "voted to reject the rezoning" in text
    # The headline leads, as it would in the fetched page.
    assert text.startswith("County rejects the Fairwater rezoning")


def test_cache_feed_text_skips_bodies_too_short_to_be_an_article(tmp_path):
    entries = parse_feed(_full_text_feed("Three words here."), FeedSpec("x", "https://a.test/f"))
    assert discover.cache_feed_text(entries, tmp_path) == 0
    assert not list(tmp_path.iterdir())


def test_cache_feed_text_never_overwrites_a_real_fetch(tmp_path):
    """A fetched page is the more complete artefact; feeds truncate."""
    from tracker.ingest.fetch import cache_path

    body = "The county board voted to reject the rezoning application. " * 12
    entries = parse_feed(_full_text_feed(body), FeedSpec("x", "https://a.test/f"))
    existing = cache_path(entries[0].url, tmp_path)
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("the genuinely fetched article", encoding="utf-8")

    assert discover.cache_feed_text(entries, tmp_path) == 0
    assert existing.read_text(encoding="utf-8") == "the genuinely fetched article"


def test_cache_feed_text_is_a_no_op_without_a_cache_dir():
    body = "The county board voted to reject the rezoning application. " * 12
    entries = parse_feed(_full_text_feed(body), FeedSpec("x", "https://a.test/f"))
    assert discover.cache_feed_text(entries, None) == 0


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
    assert "no project or risk signal" in reason


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


# --- The risk tier ----------------------------------------------------------
#
# Every headline below was measured as REJECTED by the two-tier filter, all with
# "no project signal", because the signal vocabulary is announcement-shaped. That
# meant the extractor only ever saw articles about projects going well, so an
# obstacle could not be recorded however good the schema.


@pytest.mark.parametrize(
    "headline",
    [
        "Loudoun supervisors reject data center rezoning application",
        "Residents sue to block Prince William data center project",
        "Georgia Power says transmission upgrades delay data center energization",
        "Transformer shortage pushes back hyperscale data center timelines",
        "Moratorium halts new data center development in Fayetteville",
        "Water use concerns stall Tucson data center vote",
        "Ohio regulators approve tariff for large data center loads",
    ],
)
def test_obstacle_headlines_are_kept(spec, headline):
    keep, reason = spec.matches(headline)
    assert keep, f"{headline!r} was dropped: {reason}"
    assert "risk" in reason


def test_the_topic_tier_still_gates_an_obstacle_headline(spec):
    """A risk term alone is not enough: the article must be about data centers.

    Otherwise every transformer shortage and every utility lawsuit in the country
    enters the queue.
    """
    keep, reason = spec.matches("Transformer shortage delays Ohio steel mill expansion")
    assert not keep
    assert "topic" in reason


def test_exclusions_win_over_a_risk_term(spec):
    """The exclude tier runs first, so commentary and finance coverage stay out."""
    for headline in (
        "Opinion: the data center backlash is overdue",
        "Analyst pegs data center transformer shortage as 2027 risk",
        "Nvidia shares fall on data center delay fears",
    ):
        keep, reason = spec.matches(headline)
        assert not keep, headline
        assert "excluded" in reason


def test_an_announcement_signal_is_still_reported_as_such(spec):
    """Precedence: a plain signal term is preferred, so reasons stay comparable."""
    keep, reason = spec.matches("Microsoft breaks ground on 900MW data center campus")
    assert keep
    assert "risk" not in reason


def test_risk_tier_is_optional_in_the_config(tmp_path: Path):
    """A feeds.toml predating this tier must keep working, unchanged."""
    older = tmp_path / "older.toml"
    older.write_text(
        '[[feed]]\nurl = "https://a.test/rss"\n'
        '[filter]\ntopic = ["data cent"]\nsignal = ["campus"]\n',
        encoding="utf-8",
    )
    _, spec = load_config(older)
    assert spec.risk_signal == ()
    assert not spec.matches("Loudoun rejects data center rezoning")[0]


def test_risk_term_ignores_the_other_tiers(spec):
    """`risk_term` is for ordering the queue, not for deciding what enters it."""
    assert spec.risk_term("data center construction delayed again") == "delay"
    assert spec.risk_term("Microsoft breaks ground on a new campus") is None


@pytest.mark.parametrize(
    "headline",
    [
        "PJM Issued First Backup-Generator Warnings During Heat Wave",
        "The tissue of the argument",
        "Utilities will reissue the notice",
    ],
)
def test_short_risk_terms_do_not_match_inside_a_word(spec, headline):
    """Terms are plain substrings, so "sue" hits "issued" unless it is padded.

    Found on a live run: the PJM headline below was queued as litigation news. Same
    class of bug the signal tier already guards with " mw".
    """
    assert spec.risk_term(headline) is None


@pytest.mark.parametrize(
    "headline",
    [
        "Residents sue to block the data center",
        "Town sues data center developer",
        "Neighbors sued the county over the approval",
    ],
)
def test_the_padded_litigation_terms_still_match(spec, headline):
    assert spec.risk_term(headline) is not None


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


# --- Prioritising the queue toward depth ------------------------------------


def tracked(session, company: str, name: str, city: str, state: str = "VA"):
    from tracker.ingest.records import IngestRecord, SourceRecord
    from tracker.models import utcnow
    from tracker.upsert import upsert_record

    return upsert_record(
        session,
        IngestRecord(
            project={"company": company, "name": name, "city": city, "state": state},
            sources=[
                SourceRecord(
                    url=f"https://seed.test/{name.replace(' ', '-')}",
                    source_type="trade_press",
                    fetched_at=utcnow(),
                    claims={"company": company, "city": city, "state": state},
                )
            ],
        ),
    ).project_id


def test_an_article_about_a_tracked_project_is_recognised(session):
    from tracker.ingest.discover import matches_known_project, project_identities

    pid = tracked(session, "Sabey Data Centers", "Sabey Ashburn Campus", "Ashburn")
    ids = project_identities(session)
    assert (
        matches_known_project(
            "https://x.test/news/sabey-data-centers-expands-ashburn-campus-70mw/", None, ids
        )
        == pid
    )


def test_generic_name_words_do_not_count_as_a_match(session):
    """ "Campus" and "center" appear in nearly every data-center headline.

    Treating them as distinctive matched every Sabey article to the Ashburn
    project, including genuinely different sites in other cities.
    """
    from tracker.ingest.discover import matches_known_project, project_identities

    tracked(session, "Sabey Data Centers", "Sabey Ashburn Campus", "Ashburn")
    ids = project_identities(session)
    assert (
        matches_known_project(
            "https://x.test/news/sabey-data-centers-plans-70mw-campus-in-quincy/", None, ids
        )
        is None
    ), "a Quincy campus is not the Ashburn project"


def test_a_single_company_token_is_not_enough(session):
    """ "digital" + "ashburn" hit every Ashburn article by any operator, which
    inflated one project's apparent coverage from a handful to 154."""
    from tracker.ingest.discover import matches_known_project, project_identities

    tracked(session, "Digital Realty", "Digital Ashburn Campus", "Ashburn")
    ids = project_identities(session)
    assert (
        matches_known_project("https://x.test/news/cologix-pre-leases-120mw-in-ashburn/", None, ids)
        is None
    )


def test_an_unrelated_article_is_not_matched(session):
    from tracker.ingest.discover import matches_known_project, project_identities

    tracked(session, "Sabey Data Centers", "Sabey Ashburn Campus", "Ashburn")
    ids = project_identities(session)
    assert matches_known_project("https://x.test/news/meta-hyperion-louisiana/", None, ids) is None


def test_known_first_puts_depth_before_breadth(session):
    from tracker.ingest.discover import pending, pending_split

    tracked(session, "Sabey Data Centers", "Sabey Ashburn Campus", "Ashburn")
    queue_candidates(
        session,
        [
            Candidate(
                "https://x.test/brand-new-data-center-campus-500mw/", "A new 500MW campus", "f"
            ),
            Candidate(
                "https://x.test/sabey-data-centers-ashburn-expansion-70mw/",
                "Sabey expands its Ashburn data center campus by 70MW",
                "f",
            ),
        ],
        run_id="r",
        report=DiscoverReport(),
    )

    assert pending_split(session) == (1, 1)
    ordered = pending(session, known_first=True)
    assert "sabey" in ordered[0].url, "the article that deepens a project must come first"
    # Without the flag the queue keeps its oldest-first order.
    assert "brand-new" in pending(session)[0].url


def test_an_obstacle_article_outranks_another_article_on_the_same_project(session):
    """Both deepen a tracked project, so the tiebreak is which one can add a risk."""
    from tracker.ingest.discover import pending, pending_risk_count

    spec = load_config()[1]
    tracked(session, "Sabey Data Centers", "Sabey Ashburn Campus", "Ashburn")
    queue_candidates(
        session,
        [
            Candidate(
                "https://x.test/sabey-data-centers-ashburn-expansion-70mw/",
                "Sabey expands its Ashburn data center campus by 70MW",
                "f",
            ),
            Candidate(
                "https://x.test/sabey-data-centers-ashburn-rezoning-denied/",
                "Ashburn board denies Sabey data center rezoning",
                "f",
            ),
        ],
        run_id="r",
        report=DiscoverReport(),
    )

    assert pending_risk_count(session, spec) == 1
    ordered = pending(session, known_first=True, spec=spec)
    assert "rezoning" in ordered[0].url, "the article that can record an obstacle must come first"
    # Without a spec the two are indistinguishable and oldest-first wins.
    assert "expansion" in pending(session, known_first=True)[0].url


def test_an_obstacle_article_about_an_untracked_project_does_not_jump_the_queue(session):
    """The risk bucket refines depth-first; it does not override it.

    An obstacle article about a project we do not track still only creates another
    single-source row, so it has no claim on going first.
    """
    from tracker.ingest.discover import pending

    spec = load_config()[1]
    tracked(session, "Sabey Data Centers", "Sabey Ashburn Campus", "Ashburn")
    queue_candidates(
        session,
        [
            Candidate(
                "https://x.test/stranger-corp-data-center-lawsuit/",
                "Residents sue over Stranger Corp data center",
                "f",
            ),
            Candidate(
                "https://x.test/sabey-data-centers-ashburn-expansion-70mw/",
                "Sabey expands its Ashburn data center campus by 70MW",
                "f",
            ),
        ],
        run_id="r",
        report=DiscoverReport(),
    )

    ordered = pending(session, known_first=True, spec=spec)
    assert "sabey" in ordered[0].url


def test_known_first_still_honours_the_limit(session):
    from tracker.ingest.discover import pending

    tracked(session, "Sabey Data Centers", "Sabey Ashburn Campus", "Ashburn")
    queue_candidates(
        session,
        [
            Candidate(f"https://x.test/a{i}-data-center-campus-100mw/", f"New campus {i}", "f")
            for i in range(5)
        ],
        run_id="r",
        report=DiscoverReport(),
    )
    assert len(pending(session, limit=2, known_first=True)) == 2


# --- Operator newsrooms -----------------------------------------------------


def newsroom_config(tmp_path):
    """A feeds file with one operator newsroom and one trade-press archive."""
    path = tmp_path / "feeds.toml"
    path.write_text(
        """
[[feed]]
name = "x"
url = "https://x.test/rss"

[filter]
topic = ["data cent"]
signal = ["campus"]

[[sitemap]]
name = "stack-newsroom"
url = "https://www.stackinfra.com/sitemap_index.xml"
source_type = "company_filing"
company = "STACK Infrastructure"
topic_implied = true

[[sitemap]]
name = "trade-archive"
url = "https://www.datacenterfrontier.com/sitemap.xml"
source_type = "trade_press"
""",
        encoding="utf-8",
    )
    return path


def test_a_newsroom_entry_records_its_operator(tmp_path):
    specs = {s.name: s for s in discover.load_sitemaps(newsroom_config(tmp_path))}
    assert specs["stack-newsroom"].company == "STACK Infrastructure"
    assert specs["trade-archive"].company is None, "trade press belongs to no operator"


def test_newsroom_companies_maps_host_to_company_key(tmp_path):
    mapping = discover.newsroom_companies(newsroom_config(tmp_path))
    assert mapping == {"stackinfra.com": "stack"}, "www. is stripped; the key is normalized"


def test_the_domain_can_stand_in_for_the_company(tmp_path):
    """The measured win: 15 articles over 8 projects became 28 over 13.

    A release on the operator's own site is titled "New Hillsboro campus
    announced" — it names the city, not the company, because the reader already
    knows whose site they are on.
    """
    identity = discover.ProjectIdentity(
        project_id=7, company="stack", locality="hillsboro", name_tokens=()
    )
    url = "https://www.stackinfra.com/news/new-hillsboro-campus-announced/"

    assert discover.matches_known_project(url, None, [identity]) is None
    implied = discover.newsroom_companies(newsroom_config(tmp_path))
    assert discover.matches_known_project(url, None, [identity], implied_companies=implied) == 7


def test_an_implied_company_still_needs_a_locality_or_name_token(tmp_path):
    """Precision is unchanged: the domain replaces one requirement, not both.

    Otherwise every corporate page — careers, leadership, an ESG report — would
    match every project that operator owns.
    """
    identity = discover.ProjectIdentity(
        project_id=7, company="stack", locality="hillsboro", name_tokens=()
    )
    implied = discover.newsroom_companies(newsroom_config(tmp_path))
    for path in ("/news/our-commitment-to-sustainability/", "/company/leadership/"):
        assert (
            discover.matches_known_project(
                f"https://www.stackinfra.com{path}", None, [identity], implied_companies=implied
            )
            is None
        )


def test_one_operators_domain_does_not_imply_another_operators_projects(tmp_path):
    mine = discover.ProjectIdentity(
        project_id=7, company="stack", locality="hillsboro", name_tokens=()
    )
    theirs = discover.ProjectIdentity(
        project_id=8, company="vantage", locality="hillsboro", name_tokens=()
    )
    implied = discover.newsroom_companies(newsroom_config(tmp_path))
    url = "https://www.stackinfra.com/news/new-hillsboro-campus/"
    assert discover.matches_known_project(url, None, [theirs, mine], implied_companies=implied) == 7
