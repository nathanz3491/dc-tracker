"""Keeping up with the news: the keyword matcher, what discovery remembers, the
operator-newsroom walk, the campus news watch, and the morning's account of it.

The failures these pin, in the order they cost (measured 2026-10-18):

* Meta's "Kuna, we are online!" sat on Meta's own site for six weeks: the keyword
  filter dropped it on every walk of the sitemap and left no trace, and nothing
  searched the news for a campus whose fields were already filled.
* Plain-substring terms threw news away both ways: "jobs" cut "creating 100 jobs",
  "stock" cut Stockton.
* A watch result or a newsroom post sorted behind a backlog of a thousand.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import select

from tracker.ingest import desk, discover, watch
from tracker.ingest.discover import (
    Candidate,
    DiscoverReport,
    FeedSpec,
    FilterSpec,
    SitemapSpec,
    first_term,
    normalize_haystack,
)
from tracker.ingest.fetch import FetchResult
from tracker.ingest.search import QuotaExhausted, SearchHit
from tracker.models import CompanyWatch, IngestUrl, NewsWatch, Project, SeenUrl, Watch
from tracker.vocab import PENDING_URL_STATUS

NOW = dt.datetime(2026, 10, 18, 10, 0)


def _hay(text: str) -> str:
    return normalize_haystack(text)


# --- the matcher -------------------------------------------------------------------


def test_a_term_matches_whole_words_and_a_star_the_start_of_one():
    assert first_term(["stock"], _hay("Stockton data center approved")) is None
    assert first_term(["stock"], _hay("Data center stock falls")) == "stock"
    assert first_term(["announc*"], _hay("Meta announced a campus")) == "announc*"
    assert first_term(["data cent*"], _hay("the data-centre in Slough")) == "data cent*"
    assert first_term(["jobs"], _hay("creating 100 new jobs")) == "jobs", "a word is a word"
    assert first_term(["ebook"], _hay("Facebook campus")) is None, "no longer inside a word"


def test_quotes_and_closed_up_figures_are_normalized_like_the_terms():
    assert first_term(["mw"], _hay("a 900MW campus")) == "mw"
    assert first_term(["meta"], _hay("Meta\u2019s campus")) == "meta"
    assert first_term(["were growing"], _hay("/2025/09/montgomery-were-growing/"))
    assert first_term(["we're growing"], _hay("Montgomery, we\u2019re growing!"))


SPEC = FilterSpec(
    topic=("data cent*",),
    signal=("campus*", "online", "operational"),
    exclude=("careers", "podcast"),
    risk_signal=("withdraw*",),
)


def test_a_headline_naming_a_tracked_campus_is_kept_without_a_keyword():
    entries = [
        Candidate("https://a.test/kuna-update", "Kuna update from Meta", "f"),
        Candidate("https://a.test/kuna-podcast", "Kuna podcast with Meta", "f"),
        Candidate("https://a.test/other", "Something else entirely", "f"),
    ]
    kept, dropped = discover.judge_candidates(
        entries, SPEC, known=lambda c: 1543 if "kuna" in c.url else None
    )
    assert [c.url for c in kept] == ["https://a.test/kuna-update"]
    reasons = {c.url: why for c, why in dropped}
    assert reasons["https://a.test/kuna-podcast"].startswith("excluded"), "exclusion still wins"
    assert "no data-center topic" in reasons["https://a.test/other"]


def test_a_county_is_not_a_distinctive_name():
    """ "Newton County Data Center" made every Meta post naming any county about it."""
    assert "county" in discover._GENERIC_NAME_TOKENS


# --- what discovery remembers ---------------------------------------------------------


def test_every_entry_is_remembered_once_and_a_later_queueing_upgrades_it(session):
    entry = Candidate("https://a.test/x?utm_source=feed", "X", "feed-a")
    discover.record_seen(session, [(entry, discover.DROPPED, "no signal")], now=NOW)
    discover.record_seen(
        session,
        [(Candidate("https://a.test/x", "X", "feed-b"), discover.QUEUED, None)],
        now=NOW + dt.timedelta(days=1),
    )
    [row] = session.scalars(select(SeenUrl)).all()
    assert row.source == "feed-a", "where it was first seen"
    assert row.verdict == discover.QUEUED
    assert row.last_seen_at == NOW + dt.timedelta(days=1)


def test_a_revised_filter_queues_what_the_old_one_dropped(session, tmp_path: Path):
    config = tmp_path / "feeds.toml"
    config.write_text(
        '[[feed]]\nname = "f"\nurl = "https://f.test/rss"\n\n'
        '[filter]\ntopic = ["data cent*"]\nsignal = ["online"]\n',
        encoding="utf-8",
    )
    discover.record_seen(
        session,
        [
            (Candidate("https://a.test/1", "Data center goes online", "f"), discover.DROPPED, "x"),
            (Candidate("https://a.test/2", "Data center", "f"), discover.DROPPED, "x"),
            (
                Candidate("https://a.test/3", "Data center online", "f"),
                discover.DROPPED,
                discover.TOO_OLD,
            ),
        ],
        now=NOW,
    )
    queued = discover.rejudge(session, feeds_path=config)
    assert [c.url for c in queued] == ["https://a.test/1"]
    assert session.scalar(select(IngestUrl.url)) == "https://a.test/1"
    assert {r.url: r.verdict for r in session.scalars(select(SeenUrl))}["https://a.test/3"] == (
        discover.DROPPED
    ), "too old is not a filter's verdict"


# --- the operator-newsroom walk --------------------------------------------------------

_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{rows}
</urlset>"""


def _urlset(*rows: tuple[str, str]) -> str:
    return _SITEMAP.format(
        rows="\n".join(f"<url><loc>{u}</loc><lastmod>{m}</lastmod></url>" for u, m in rows)
    )


class Fetcher:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    async def fetch(self, url: str) -> FetchResult:
        body = self.mapping.get(url)
        if body is None:
            return FetchResult(url, False, error="HTTP 404", status=404, fetched_at=NOW, via="t")
        return FetchResult(url, True, markdown=body, status=200, fetched_at=NOW, via="t")


OWN = SitemapSpec(
    "meta-datacenters",
    "https://dc.test/sitemap.xml",
    source_type="company_filing",
    topic_implied=True,
    company="Meta",
)
NEWSROOM_SPEC = FilterSpec(
    topic=("data cent*",), signal=("hello",), exclude=("careers",), risk_signal=()
)


def test_an_operator_newsroom_is_caught_up_once_then_read_whole(session):
    first = _urlset(
        ("https://dc.test/2026/04/hello-tulsa/", "2025-11-01T00:00:00+00:00"),
        ("https://dc.test/2021/03/sarpy-update/", "2025-11-25T00:00:00+00:00"),
    )
    report = DiscoverReport()
    queued, seen = discover.watch_newsrooms(
        session, [OWN], Fetcher({OWN.url: first}), NEWSROOM_SPEC, report=report
    )
    # The first walk is a catch-up: what passes the filter, and nothing else.
    assert [c.url for c in queued] == ["https://dc.test/2026/04/hello-tulsa/"]
    assert queued[0].published_at == dt.datetime(2026, 4, 1), "the path's month, not lastmod"
    verdicts = {c.url: (v, why) for c, v, why in seen}
    assert verdicts["https://dc.test/2021/03/sarpy-update/"][0] == discover.LISTED
    tulsa = verdicts["https://dc.test/2026/04/hello-tulsa/"]
    assert tulsa[0] == discover.QUEUED
    assert tulsa[1].startswith("catch-up"), "not read ahead of the backlog as news"
    discover.queue_candidates(session, queued, run_id="t", report=DiscoverReport())
    discover.record_seen(session, seen, now=NOW)

    second = _urlset(
        ("https://dc.test/2026/04/hello-tulsa/", "2025-11-01T00:00:00+00:00"),
        ("https://dc.test/2021/03/sarpy-update/", "2025-11-25T00:00:00+00:00"),
        ("https://dc.test/kuna-we-are-online/", "2026-09-03T16:07:32+00:00"),
        ("https://dc.test/careers/", "2026-09-04T00:00:00+00:00"),
    )
    queued, seen = discover.watch_newsrooms(
        session,
        [OWN],
        Fetcher({OWN.url: second}),
        NEWSROOM_SPEC,
        report=DiscoverReport(),
        now=dt.datetime(2026, 9, 4, 10),
    )
    # From then on everything new on the operator's own site is read, no keyword
    # needed, dated by its lastmod; an exclusion still keeps a careers page out.
    assert [c.url for c in queued] == ["https://dc.test/kuna-we-are-online/"]
    assert queued[0].published_at == dt.datetime(2026, 9, 3, 16, 7, 32)
    assert {(c.url, v, why) for c, v, why in seen} == {
        ("https://dc.test/kuna-we-are-online/", discover.QUEUED, discover.NEWSROOM_NEW),
        ("https://dc.test/careers/", discover.DROPPED, "excluded by 'careers'"),
    }


def _walk(session, spec, body, **kw):
    kw.setdefault("now", NOW)
    queued, seen = discover.watch_newsrooms(
        session, [spec], Fetcher({spec.url: body}), NEWSROOM_SPEC, report=DiscoverReport(), **kw
    )
    discover.queue_candidates(session, queued, run_id="t", report=DiscoverReport())
    discover.record_seen(session, seen, now=NOW)
    return queued, seen


def test_a_surge_of_unseen_posts_is_read_as_a_catch_up_not_as_news(session):
    """A child sitemap that failed on the first walk answers the next night: its
    hundreds of old posts are not tonight's news, and lastmod is no publish date."""
    _walk(session, OWN, _urlset(("https://dc.test/first/", "2026-01-01")))
    rows = [(f"https://dc.test/old-post-{i}/", "2026-09-15") for i in range(40)]
    rows.append(("https://dc.test/2026/10/hello-tulsa/", "2026-10-17"))
    queued, seen = _walk(session, OWN, _urlset(*rows))
    assert [c.url for c in queued] == ["https://dc.test/2026/10/hello-tulsa/"]
    assert queued[0].published_at == dt.datetime(2026, 10, 1), "the path's month, not lastmod"
    assert sum(1 for _, v, _ in seen if v == discover.LISTED) == 40


THIRD = SitemapSpec("utilitydive-archive", "https://ud.test/sitemap.xml")


def test_an_entry_listed_late_is_a_catch_up_not_news(session):
    """Utility Dive answered six monthly child sitemaps with a challenge on the first
    walk (2026-10-19). The night they answer, their 2024 articles are unseen."""
    _walk(session, THIRD, _urlset(("https://ud.test/news/first/", "2026-10-18")), now=NOW)
    _walk(session, OWN, _urlset(("https://dc.test/first/", "2026-10-18")), now=NOW)
    rows = [
        ("https://ud.test/news/data-center-campus-hello-2024/", "2024-11-20"),
        ("https://ud.test/news/data-center-campus-hello-today/", "2026-10-17"),
    ]
    queued, seen = _walk(session, THIRD, _urlset(*rows), now=NOW)
    assert [c.url for c in queued] == ["https://ud.test/news/data-center-campus-hello-today/"]
    late = {c.url: (v, why) for c, v, why in seen}[
        "https://ud.test/news/data-center-campus-hello-2024/"
    ]
    assert late == (discover.LISTED, "listed late")

    # On an operator's own site a late post is judged by the filter, not read whole.
    rows = [("https://dc.test/2024/05/a-community-grant/", "2024-05-02")]
    rows += [("https://dc.test/2024/06/hello-tulsa/", "2024-06-02")]
    queued, seen = _walk(session, OWN, _urlset(*rows), now=NOW)
    assert [c.url for c in queued] == ["https://dc.test/2024/06/hello-tulsa/"]
    assert {why for _, _, why in seen} >= {"catch-up: topic implied by the feed + 'hello'"}


def test_a_renamed_newsroom_is_not_walked_for_the_first_time_again(session):
    from dataclasses import replace

    _walk(session, OWN, _urlset(*[(f"https://dc.test/p{i}/", "2026-01-01") for i in range(10)]))
    renamed = replace(OWN, name="meta-dc-renamed")
    rows = [(f"https://dc.test/p{i}/", "2026-01-01") for i in range(10)]
    rows.append(("https://dc.test/kuna-we-are-online/", "2026-10-17"))
    queued, _ = _walk(session, renamed, _urlset(*rows))
    assert [c.url for c in queued] == ["https://dc.test/kuna-we-are-online/"]


def test_the_sighting_that_queued_a_url_owns_its_record(session):
    archive = Candidate("https://a.test/story", "story", "utilitydive-archive")
    feed = Candidate("https://a.test/story", "Data center story", "utilitydive")
    discover.record_seen(
        session, [(archive, discover.DROPPED, "no signal"), (feed, discover.QUEUED, None)], now=NOW
    )
    [row] = session.scalars(select(SeenUrl)).all()
    assert (row.verdict, row.source) == (discover.QUEUED, "utilitydive")


def test_a_relative_link_is_resolved_and_found_again(session):
    rss = (
        "<rss><channel><item><title>Data center campus</title><link>/news/x</link></item>"
        "<item><title>Mail</title><link>mailto:a@b.test</link></item></channel></rss>"
    )
    [entry] = discover.parse_feed(rss, FeedSpec("f", "https://pub.test/feed/"))
    assert entry.url == "https://pub.test/news/x"
    discover.record_seen(session, [(Candidate("/odd", "x", "f"), discover.DROPPED, "x")], now=NOW)
    discover.record_seen(session, [(Candidate("/odd", "x", "f"), discover.DROPPED, "x")], now=NOW)
    assert len(session.scalars(select(SeenUrl)).all()) == 1


def test_a_term_with_no_letter_or_digit_is_refused(tmp_path):
    config = tmp_path / "feeds.toml"
    config.write_text(
        '[[feed]]\nname = "f"\nurl = "https://f.test/rss"\n\n'
        '[filter]\ntopic = ["data cent*"]\nsignal = ["campus*"]\nexclude = ["&"]\n',
        encoding="utf-8",
    )
    with pytest.raises(discover.DiscoverError, match="no letter or digit"):
        discover.load_config(config)


def test_media_child_sitemaps_are_not_walked():
    index = (
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://dc.test/sitemap-1.xml</loc></sitemap>"
        "<sitemap><loc>https://dc.test/image-sitemap-1.xml</loc></sitemap>"
        "<sitemap><loc>https://dc.test/video-sitemap-1.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    assert discover.index_children(index) == ["https://dc.test/sitemap-1.xml"]
    for kept in ("media-releases-sitemap.xml", "media/post-sitemap.xml", "port-authority-news.xml"):
        assert not discover._MEDIA_SITEMAP.search(kept.rsplit("/", 1)[-1]), kept
    for dropped in ("db-authors-sitemap.xml", "post_tag-sitemap.xml", "sitemap-image-1.xml"):
        assert discover._MEDIA_SITEMAP.search(dropped), dropped


def test_a_sitemap_read_as_a_feed_keeps_its_newest_entries():
    rows = [(f"https://m.test/p{i}", f"2026-01-{i % 28 + 1:02d}") for i in range(80)]
    rows.append(("https://m.test/changed", "2026-10-14"))
    entries = discover.parse_feed(_urlset(*rows), FeedSpec("m", "https://m.test/s.xml"))
    assert len(entries) == discover.MAX_PER_FEED
    assert entries[0].url == "https://m.test/changed"


def test_a_site_wide_edit_is_not_news():
    """morethanjustparks re-dated 1,234 pages on 2026-10-14, its index pages among
    them, and dated that day they were read first as news. Pages changed one by one
    keep their date and come first; the edit's keep only a date their address holds."""
    rows = [(f"https://m.test/state/s{i}", "2026-10-14") for i in range(40)]
    rows += [("https://m.test/2026/09/posted", "2026-10-14")]
    rows += [("https://m.test/campus/new-one", "2026-10-16"), ("https://m.test/old", "2026-03-02")]
    entries = discover.parse_feed(_urlset(*rows), FeedSpec("m", "https://m.test/s.xml"))
    dated = {e.url: e.published_at for e in entries}
    assert [e.url for e in entries[:3]] == [
        "https://m.test/campus/new-one",
        "https://m.test/2026/09/posted",
        "https://m.test/old",
    ]
    assert dated["https://m.test/2026/09/posted"] == dt.datetime(2026, 9, 1)
    assert dated["https://m.test/state/s0"] is None


# --- the reading order ---------------------------------------------------------------


def _queue(session, url, feed, published):
    session.add(
        IngestUrl(
            url=url,
            run_id="t",
            status=PENDING_URL_STATUS,
            feed=feed,
            published_at=published,
            attempts=0,
            first_seen_at=NOW,
            last_tried_at=NOW,
        )
    )


def test_watch_results_come_after_the_news_and_before_the_backlog(session, monkeypatch):
    monkeypatch.setattr(discover, "utcnow", lambda: NOW)
    _queue(session, "https://a.test/old-backlog", "prospect:x", NOW - dt.timedelta(days=300))
    _queue(session, "https://a.test/watch-old", "watch:#1543", NOW - dt.timedelta(days=200))
    _queue(session, "https://a.test/news", "lead", NOW - dt.timedelta(days=2))
    _queue(session, "https://a.test/future", "lead", NOW + dt.timedelta(days=40))
    # A newsroom post queued as new tonight goes with the watch's finds; one an old
    # --deep sweep queued under the same feed name does not.
    _queue(session, "https://dc.test/new-post", "meta-datacenters", NOW - dt.timedelta(days=90))
    _queue(session, "https://dc.test/deep-post", "meta-datacenters", NOW - dt.timedelta(days=90))
    discover.record_seen(
        session,
        [
            (
                Candidate("https://dc.test/new-post", "x", "meta-datacenters"),
                discover.QUEUED,
                discover.NEWSROOM_NEW,
            )
        ],
        now=NOW,
    )
    session.flush()
    order = [row.url for row in discover.pending(session, new_first=True)]
    assert order.index("https://a.test/news") < order.index("https://a.test/watch-old")
    assert order.index("https://a.test/watch-old") < order.index("https://a.test/old-backlog")
    assert order.index("https://dc.test/new-post") < order.index("https://dc.test/deep-post")
    assert order[0] != "https://a.test/future", "a date in the future is no date"


def test_a_builders_post_about_an_unknown_campus_takes_turns_with_the_watch(session, monkeypatch):
    """The watch's first look queued about 5,000 results; read first, they would have
    kept Meta's "Hello Tulsa" -- a campus the database never held -- waiting weeks."""
    monkeypatch.setattr(discover, "utcnow", lambda: NOW)
    days = dt.timedelta(days=1)
    _queue(session, "https://a.test/news", "lead", NOW - 2 * days)
    _queue(session, "https://a.test/w-undated", "watch:#1", None)
    for n in (210, 220, 230):
        _queue(session, f"https://a.test/w{n}", "watch:#1", NOW - n * days)
    meta = "https://datacenters.atmeta.com"
    _queue(session, f"{meta}/2026/04/hello-tulsa/", "meta-datacenters", dt.datetime(2026, 4, 1))
    _queue(session, f"{meta}/2024/05/hello-x/", "meta-datacenters", dt.datetime(2024, 5, 1))
    _queue(session, "https://dir.test/a-campus", "servercountry-archive", NOW - 100 * days)
    # Recent, but the watch's: on 2026-10-19 about 350 of its first-look results fell
    # inside the email's window, and counted as news they took every slot.
    _queue(session, "https://a.test/w3", "watch:#1", NOW - 3 * days)
    _queue(session, "https://a.test/w5", "watch:#1", NOW - 5 * days)
    session.flush()
    order = [row.url for row in discover.pending(session, new_first=True)]
    assert order == [
        "https://a.test/news",
        "https://a.test/w3",
        f"{meta}/2026/04/hello-tulsa/",
        "https://a.test/w5",
        f"{meta}/2024/05/hello-x/",
        "https://a.test/w210",
        "https://a.test/w220",
        "https://a.test/w230",
        "https://a.test/w-undated",
        "https://dir.test/a-campus",
    ]


# --- the campus news watch ------------------------------------------------------------


def _campus(session, n: int, **kw) -> Project:
    project = Project(
        name=kw.pop("name", "Meta Kuna"),
        company=kw.pop("company", "Meta"),
        state=kw.pop("state", "ID"),
        city=kw.pop("city", "Kuna"),
        county=kw.pop("county", "Ada County"),
        dedup_key=f"key-{n}",
        phase=kw.pop("phase", "construction"),
        **kw,
    )
    session.add(project)
    session.flush()
    return project


def test_the_query_names_the_company_as_the_press_does_and_the_place():
    kuna = Project(name="Meta Kuna", company="Meta", city="Kuna", county="Ada County")
    assert watch.anchor(kuna) == '"Meta" ("Kuna" OR "Ada County")'
    qts = Project(name="Digital Gateway", company="QTS Data Centers", county="Prince William")
    assert watch.anchor(qts) == '"QTS" ("Prince William County" OR "Digital Gateway")'
    old = Project(name="Eagle Mountain Data Center", company="Facebook", city="Eagle Mountain")
    assert watch.anchor(old) == '("Facebook" OR "Meta") "Eagle Mountain"'
    code = Project(name="Stargate (Project Jupiter)", company="Oracle", city="Las Cruces")
    assert watch.anchor(code) == '"Oracle" ("Las Cruces" OR "Project Jupiter")'
    assert watch.anchor(Project(name="Data Center Campus", company="Acme")) is None


def test_a_follow_up_asks_for_the_dates_since_the_last_look_newest_first():
    """Google's fixed windows rank a page of ten by relevance; a busy place's older
    stories could fill it and push out the one published since the last look."""
    assert watch.window_for(NOW - dt.timedelta(hours=20), NOW) == (
        "cdr:1,cd_min:10/16/2026,cd_max:10/18/2026,sbd:1"
    )
    assert watch.window_for(NOW - dt.timedelta(days=90), NOW) == "qdr:y"


@pytest.mark.parametrize(
    ("company", "term"),
    [
        ("QTS Data Centers", '"QTS"'),
        ("Amazon Web Services (AWS)", '("Amazon Web Services" OR "AWS")'),
        ("TA Realty / EdgeConneX", '("TA Realty" OR "EdgeConneX")'),
        ("Yondr Group and Cerberus", '("Yondr Group and Cerberus" OR "Yondr Group" OR "Cerberus")'),
        ("Prologis, L.P.", '"Prologis"'),
        ("The Data Centers LLC", '"The Data Centers"'),
    ],
)
def test_a_company_is_searched_under_every_name_the_press_uses(company, term):
    assert watch._company_term(Project(company=company)) == term


def test_two_counties_are_two_places_and_various_is_none():
    row = Project(
        name="Amazon campuses",
        company="Amazon",
        city="Various",
        county="Caddo and Bossier parishes",
        state="LA",
    )
    assert watch.anchor(row) == '"Amazon" ("Caddo Parish" OR "Bossier Parish")'
    vague = Project(name="Calvert Technology Center", company="AWS", county="Calvert County")
    assert watch.anchor(vague) == '("AWS" OR "Amazon") "Calvert County"'


class FakeNews:
    def __init__(self, news=(), web=(), fail_after: int | None = None):
        self.news_hits, self.web_hits = list(news), list(web)
        self.calls: list[tuple[str, str, str]] = []
        self.fail_after = fail_after

    def _spend(self):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise QuotaExhausted("cap")

    def news(self, query, *, page=1, window="qdr:w"):
        self._spend()
        self.calls.append(("news", query, window))
        return self.news_hits

    def recent(self, query, *, page=1, window="qdr:w"):
        self._spend()
        self.calls.append(("web", query, window))
        return self.web_hits


def _hit(url, title, snippet="", date="2 days ago"):
    return SearchHit(url=url, title=title, snippet=snippet, query="q", date=date)


def test_a_first_look_reaches_back_a_year_and_a_later_one_to_the_last(session):
    kuna = _campus(session, 1)
    provider = FakeNews(
        news=[
            _hit("https://news.test/kuna-online", "Meta's Kuna data center is now online"),
            _hit("https://news.test/old", "Meta Kuna data center plans", date="Jan 3, 2024"),
            _hit("https://www.facebook.com/kuna", "Kuna data center online"),
            _hit("https://news.test/weather", "Kuna weather this weekend"),
        ],
        web=[_hit("https://news.test/kuna-expands", "Meta expands Kuna campus", date="")],
    )
    report = watch.run(session, provider=provider, now=NOW, feeds_path=_feeds(session))
    assert [c[0] for c in provider.calls] == ["news", "web"]
    assert all(c[2] == "qdr:y" for c in provider.calls)
    assert '"online" OR "operational"' in provider.calls[1][1], "the words its stage waits on"
    urls = set(session.scalars(select(IngestUrl.url)))
    assert urls == {"https://news.test/kuna-online", "https://news.test/kuna-expands"}
    assert {r.feed for r in session.scalars(select(IngestUrl))} == {f"watch:#{kuna.id}"}
    look = session.get(NewsWatch, kuna.id)
    assert look.baseline_at == NOW and look.queries == 2 and look.queued == 2
    assert report.queued == 2

    later = FakeNews()
    watch.run(session, provider=later, now=NOW + dt.timedelta(hours=12), feeds_path=_feeds(session))
    assert later.calls == [], "not due: construction is looked at daily"
    # A night that starts a little earlier than the last is still the next day.
    watch.run(
        session,
        provider=later,
        now=NOW + dt.timedelta(hours=23, minutes=59),
        feeds_path=_feeds(session),
    )
    assert [(c[0], c[2]) for c in later.calls] == [
        ("news", "cdr:1,cd_min:10/17/2026,cd_max:10/19/2026,sbd:1")
    ]


def test_a_failed_follow_up_leaves_the_window_open(session):
    """A missed night is late news, not lost news: the clock moves only when the
    search went through."""
    from tracker.ingest.search import SearchError

    kuna = _campus(session, 9)
    session.add(NewsWatch(project_id=kuna.id, baseline_at=NOW, checked_at=NOW))
    session.flush()

    class Broken(FakeNews):
        def news(self, query, *, page=1, window="qdr:w"):
            raise SearchError("proxy 502")

    watch.run(
        session, provider=Broken(), now=NOW + dt.timedelta(days=1), feeds_path=_feeds(session)
    )
    assert session.get(NewsWatch, kuna.id).checked_at == NOW


def test_a_date_given_in_weeks_is_not_stored_as_a_day(session):
    kuna = _campus(session, 10)
    provider = FakeNews(
        news=[
            _hit("https://news.test/a", "Kuna data center online", date="3 weeks ago"),
            _hit("https://news.test/b", "Kuna data center campus", date="2 days ago"),
        ]
    )
    watch.run(session, provider=provider, now=NOW, feeds_path=_feeds(session))
    dates = {r.url: r.published_at for r in session.scalars(select(IngestUrl))}
    assert dates["https://news.test/a"] is None, "the page's own date fills it when read"
    assert dates["https://news.test/b"] == NOW - dt.timedelta(days=2)
    assert kuna.id


def test_a_look_taken_after_midnight_is_due_at_that_evenings_run(session):
    """The first look at every campus ran by hand from 00:11 on 2026-10-19; at six
    hours' slack every daily campus fell due at 18:11, after that night's watch."""
    campus = _campus(session, 2, phase="construction")
    took = dt.datetime(2026, 10, 18, 16, 11)  # 00:11 in Beijing
    session.add(NewsWatch(project_id=campus.id, baseline_at=took, checked_at=took))
    session.flush()
    evening = dt.datetime(2026, 10, 19, 10, 4)  # 18:04: the loop's watch
    assert [p.id for p, _ in watch.due(session, now=evening)[0]] == [campus.id]
    assert watch.due(session, now=took + dt.timedelta(hours=5))[0] == [], "not twice a day"


def test_a_watched_companys_operating_campus_is_looked_at_daily(session):
    from tracker import accounts

    campus = _campus(session, 2, phase="operational")
    session.add(NewsWatch(project_id=campus.id, baseline_at=NOW, checked_at=NOW))
    session.flush()
    assert watch.due(session, now=NOW + dt.timedelta(days=1, hours=1))[0] == []
    account = accounts.create(session, "reader@example.com", "a long enough password 1")
    session.add(Watch(account_id=account.id, entry="Meta", company_key="meta"))
    session.flush()
    followups, _ = watch.due(session, now=NOW + dt.timedelta(days=1, hours=1))
    assert [p.id for p, _ in followups] == [campus.id]


def test_the_search_cap_stops_the_watch_and_keeps_what_it_did(session):
    a = _campus(session, 3, mw_planned=900.0)
    b = _campus(
        session, 4, name="Hyperion", city="Rayville", county="Richland Parish", mw_planned=100.0
    )
    provider = FakeNews(fail_after=2)
    report = watch.run(session, provider=provider, now=NOW, feeds_path=_feeds(session))
    assert report.stopped == "cap"
    assert session.get(NewsWatch, a.id).baseline_at == NOW, "the largest went first, whole"
    assert session.get(NewsWatch, b.id) is None, "untouched, so first in line next night"


def _feeds(session) -> Path:
    """A feeds.toml with a small filter, written beside the test database."""
    path = Path(session.get_bind().url.database).with_name("feeds.toml")
    path.write_text(
        '[[feed]]\nname = "f"\nurl = "https://f.test/rss"\n\n'
        '[filter]\ntopic = ["data cent*"]\nsignal = ["online", "campus*", "plans"]\n',
        encoding="utf-8",
    )
    return path


# --- the company watch ------------------------------------------------------------------


def test_a_company_with_three_campuses_or_a_watched_one_is_searched_as_a_company(session):
    """The campus watch asks only about campuses held; a company's next one is
    likely somewhere the database holds nothing."""
    from tracker import accounts

    for n in range(3):
        _campus(session, 100 + n, name=f"Meta site {n}", city=f"Town{n}")
    _campus(session, 110, company="Foo Corp", name="Foo One", city="Elsewhere")
    account = accounts.create(session, "reader@example.com", "a long enough password 1")
    session.add(Watch(account_id=account.id, entry="xAI", company_key="xai"))
    session.flush()
    due = watch.companies_due(session, now=NOW)
    assert [(key, label) for key, label, _ in due] == [("xai", "xAI"), ("meta", "Meta")]


def test_a_company_search_reaches_back_a_month_then_to_its_last_look():
    first = watch.company_query("Meta", None, now=NOW)
    assert first.text == '"Meta" "data center"' and first.window == "qdr:m"
    look = CompanyWatch(company_key="meta", baseline_at=NOW, checked_at=NOW)
    later = watch.company_query("Meta", look, now=NOW + dt.timedelta(days=7))
    assert later.window.startswith("cdr:1,") and later.window.endswith(",sbd:1")


def test_a_companys_news_is_queued_and_its_next_search_waits_a_week(session):
    for n in range(3):
        _campus(session, 120 + n, name=f"Meta site {n}", city=f"Town{n}")
    provider = FakeNews(
        news=[
            _hit(
                "https://news.test/meta-lea-county",
                "Meta plans $10 billion data center campus in Lea County",
            ),
            _hit("https://news.test/meta-earnings", "Meta earnings beat on ad revenue"),
        ]
    )
    report = watch.run_companies(session, provider=provider, now=NOW, feeds_path=_feeds(session))
    assert provider.calls == [("news", '"Meta" "data center"', "qdr:m")]
    queued = {row.url: row.feed for row in session.scalars(select(IngestUrl))}
    assert queued == {"https://news.test/meta-lea-county": "watch:company:meta"}
    assert report.queued == 1
    assert watch.companies_due(session, now=NOW + dt.timedelta(days=1)) == []
    later = watch.companies_due(session, now=NOW + dt.timedelta(days=7))
    assert [key for key, _, _ in later] == ["meta"]


# --- the morning's account --------------------------------------------------------------


def test_the_desk_says_when_no_discovery_ran_and_when_credit_is_low(session, tmp_path):
    report = desk.build(session, since=NOW, now=NOW + dt.timedelta(hours=10))
    assert report.lines()[0].startswith("    NO DISCOVERY RAN")

    discover.record_seen(
        session,
        [(Candidate("https://a.test/1", "Data center", "lead", NOW), discover.QUEUED, None)],
        now=NOW + dt.timedelta(hours=1),
    )
    ledger = tmp_path / "night.spend"
    ledger.write_text("2026-10-18T10:00:00Z\t1\tnews\tserper\t0\t0\t0\t0\tnews\n" * 100)
    report = desk.build(
        session, since=NOW, now=NOW + dt.timedelta(hours=10), ledger=ledger, credit=900
    )
    text = "\n".join(report.lines())
    assert "NO DISCOVERY RAN" not in text
    assert "SEARCH CREDIT LOW: about 9 night(s) left" in text


# --- where a URL came from -----------------------------------------------------------------


def test_a_url_the_queue_did_not_hold_is_labelled_with_how_it_was_found(session):
    from tracker.ingest import crawl
    from tracker.ingest.crawl import ExtractionOutcome

    crawl.record_url(
        session, "r", ExtractionOutcome("https://a.test/n", "ok"), origin="enrich:search"
    )
    _queue(session, "https://a.test/q", "lead", None)
    session.flush()
    crawl.record_url(
        session, "r", ExtractionOutcome("https://a.test/q", "ok"), origin="enrich:archive"
    )
    labels = {row.url: row.feed for row in session.scalars(select(IngestUrl))}
    assert labels == {"https://a.test/n": "enrich:search", "https://a.test/q": "lead"}


def test_the_desk_says_when_search_credit_is_gone_and_counts_only_real_reads(session):
    report = desk.build(session, since=NOW, now=NOW + dt.timedelta(hours=10), credit=0)
    assert any("SEARCH CREDIT EXHAUSTED" in w for w in report.warnings)

    failed = IngestUrl(
        url="https://a.test/reread",
        run_id="t",
        status="ok",
        feed="lead",
        error="HTTP 503",
        attempts=2,
        first_seen_at=NOW - dt.timedelta(days=30),
        last_tried_at=NOW + dt.timedelta(hours=1),
    )
    session.add(failed)
    session.flush()
    report = desk.build(session, since=NOW, now=NOW + dt.timedelta(hours=10))
    assert all(row.read == 0 for row in report.sources), "a failed re-read is not a read"
    assert [(row.source, row.failed) for row in report.sources] == [("lead", 1)], "it failed"


def test_the_desk_counts_what_would_not_load_and_what_was_set_aside(session):
    """The reads alone made a source look whole. On 2026-10-19 the news watch showed
    146 reads and nothing else, while 115 more of its articles would not load and 410
    left the queue unread, as copies of a story or on a closed publisher."""

    def tried(url, status, error=None, *, feed="watch:#7", when=NOW + dt.timedelta(hours=1)):
        session.add(
            IngestUrl(
                url=url,
                run_id="t",
                status=status,
                feed=feed,
                error=error,
                attempts=1,
                first_seen_at=NOW - dt.timedelta(days=1),
                last_tried_at=when,
            )
        )

    tried("https://a.test/read", "ok")
    tried("https://a.test/403", "fetch_error", "HTTP 403")
    tried("https://a.test/robots", "fetch_error", "robots.txt could not be read")
    tried("https://a.test/reply", "parse_error", "model did not return a JSON object")
    tried("https://a.test/copy", "skipped", f"{discover.SAME_STORY}https://a.test/read")
    tried("https://a.test/closed", "skipped", discover.DISCARDED_UNREADABLE)
    tried("https://a.test/last-week", "fetch_error", "HTTP 403", when=NOW - dt.timedelta(days=7))
    tried("https://b.test/other", "fetch_error", "HTTP 429", feed="enrich:search")
    session.flush()

    report = desk.build(session, since=NOW, now=NOW + dt.timedelta(hours=10))
    rows = {row.source: (row.read, row.failed, row.aside, row.found) for row in report.sources}
    assert rows == {"news watch": (1, 3, 2, 1), "enrich:search": (0, 1, 0, 0)}

    text = "\n".join(report.lines())
    assert "read failed  aside  found" in text
    assert "1 read; 4 failed" in text and "2 set aside unread" in text


def test_a_companys_other_name_is_not_a_campus_name(session):
    """ "Facebook Prineville Campus" is filed under Meta. "facebook" is the company's
    other name, not the campus's: it must not make every Facebook post news of it."""
    prineville = _campus(
        session,
        11,
        name="Facebook Prineville Campus",
        city="Prineville",
        county="Crook County",
        state="OR",
    )
    identities = [i for i in discover.project_identities(session) if i.project_id == prineville.id]
    implied = {"about.fb.com": "meta"}
    groups = "https://about.fb.com/news/2026/01/new-facebook-groups-features/"
    assert discover.matches_known_project(groups, "", identities, implied_companies=implied) is None
    local = "https://about.fb.com/news/2026/01/prineville-data-center-expansion/"
    assert discover.matches_known_project(local, "", identities, implied_companies=implied) == (
        prineville.id
    )
