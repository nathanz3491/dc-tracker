"""Keeping the crawl queue honest: dead links, and links that no longer qualify.

A queue is a promise that everything in it is worth an LLM call. Two things broke
that promise quietly on the live database, where 1,241 candidates had piled up:

* **Links that are no longer there.** A sitemap is a snapshot and a queued URL is
  never re-checked between discovery and the crawl that spends a call on it.
* **Articles that would not be queued today.** The filter in `seed/feeds.toml` is
  data and it gets edited; nothing ever re-applied it to what was already queued.

There is a third failure these tests cannot see, and it is the one that made the
queue unusable: the listing printed `url[:60]`, so every link on screen was a
*prefix* of a real one — a 404 in a browser and a no-op in `--drop --url`. That is
fixed in `cli.queue` by printing the whole URL and an id beside it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker.ingest import discover
from tracker.ingest.fetch import FetchResult
from tracker.models import IngestUrl
from tracker.vocab import PENDING_URL_STATUS

NOW = dt.datetime(2026, 7, 25, 12, 0, 0)


def _queued(session, url, *, title="", feed="trade-archive"):
    row = IngestUrl(
        url=url,
        run_id="discover-1",
        status=PENDING_URL_STATUS,
        title=title,
        feed=feed,
        published_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def _config(tmp_path):
    path = tmp_path / "feeds.toml"
    path.write_text(
        """
[[feed]]
name = "wire"
url = "https://x.test/rss"

[filter]
topic = ["data cent*"]
signal = ["campus*", "megawatt*"]
exclude = ["resources blogs"]

[[sitemap]]
name = "op-newsroom"
url = "https://op.test/sitemap.xml"
topic_implied = true
""",
        encoding="utf-8",
    )
    return path


# --- what "dead" means --------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error", "expect"),
    [
        (200, "", "ok"),
        (301, "", "ok"),
        (404, "", "dead"),
        (410, "", "dead"),
        # Defended, not gone. On the live queue this was 55 URLs across seven
        # publishers — the best-defended sources, which is often to say the good
        # ones, and exactly what `ingest crawl --browser` exists for.
        (403, "", "blocked"),
        (401, "", "blocked"),
        (429, "", "blocked"),
        (500, "", "error"),
        (None, "timed out", "error"),
        (None, "getaddrinfo failed", "dead"),
    ],
)
def test_only_gone_counts_as_gone(status, error, expect):
    assert discover.classify_status(status, error) == expect


def test_verify_reports_each_queued_url_without_touching_the_database(session, monkeypatch):
    alive = _queued(session, "https://a.test/live/")
    gone = _queued(session, "https://a.test/gone/")

    async def fake_fetch_all(urls, **kwargs):
        return [
            FetchResult(u, u.endswith("live/"), status=200 if u.endswith("live/") else 404)
            for u in urls
        ]

    monkeypatch.setattr("tracker.ingest.fetch.fetch_all", fake_fetch_all)
    verdicts = {v.row_id: v.verdict for v in discover.verify_urls([alive, gone])}

    assert verdicts == {alive.id: "ok", gone.id: "dead"}
    assert session.get(IngestUrl, gone.id) is not None, "verifying never deletes"


_NO_SUCH_NAME = "[Errno 8] nodename nor servname provided, or not known"


def test_a_name_that_did_not_resolve_is_not_dead_when_no_name_did(session, monkeypatch):
    """A DNS outage on this machine looks exactly like every host having vanished.

    `classify_status` reads a single answer and cannot tell them apart, so
    `queue check --drop` during a network hiccup deleted every queued row it
    asked about. A failed lookup is only evidence against a name when other names
    in the same check resolved — that is the positive signal that the resolver
    works."""
    rows = [_queued(session, f"https://site{i}.test/a/") for i in range(3)]

    async def fake_fetch_all(urls, **kwargs):
        return [FetchResult(u, False, error=_NO_SUCH_NAME) for u in urls]

    monkeypatch.setattr("tracker.ingest.fetch.fetch_all", fake_fetch_all)
    verdicts = [v.verdict for v in discover.verify_urls(rows)]
    assert verdicts == ["error", "error", "error"]


def test_a_name_that_did_not_resolve_while_others_did_is_dead(session, monkeypatch):
    alive = _queued(session, "https://alive.test/a/")
    gone = _queued(session, "https://gone.test/a/")

    async def fake_fetch_all(urls, **kwargs):
        return [
            FetchResult(u, True, status=200)
            if "alive" in u
            else FetchResult(u, False, error=_NO_SUCH_NAME)
            for u in urls
        ]

    monkeypatch.setattr("tracker.ingest.fetch.fetch_all", fake_fetch_all)
    verdicts = {v.row_id: v.verdict for v in discover.verify_urls([alive, gone])}
    assert verdicts == {alive.id: "ok", gone.id: "dead"}


# --- re-applying the filter ---------------------------------------------------


def test_a_row_the_filter_no_longer_wants_is_named_with_its_reason(session, tmp_path):
    keep = _queued(
        session,
        "https://op.test/news/new-hillsboro-campus/",
        title="new hillsboro campus",
        feed="op-newsroom",
    )
    drop = _queued(
        session,
        "https://op.test/resources/blogs/nist-cloud-compliance/",
        title="navigating nist cloud compliance",
        feed="op-newsroom",
    )

    stale, examined = discover.refilter_pending(session, feeds_path=_config(tmp_path))

    assert examined == 2
    assert [c.row_id for c in stale] == [drop.id]
    assert "resources blogs" in stale[0].reason
    assert session.get(IngestUrl, keep.id) is not None


def test_a_newsroom_row_is_judged_with_its_topic_implied(session, tmp_path):
    """Without this every sitemap entry is re-judged as though it came from a
    general outlet, and the whole queue looks like noise."""
    row = _queued(
        session, "https://op.test/news/500-megawatt-expansion/", title="", feed="op-newsroom"
    )
    stale, _ = discover.refilter_pending(session, feeds_path=_config(tmp_path))
    assert [c.row_id for c in stale] == []
    assert session.get(IngestUrl, row.id) is not None


def test_a_row_from_a_feed_no_longer_configured_is_left_alone(session, tmp_path):
    """Commenting out a feed should not delete somebody's queue."""
    _queued(session, "https://gone.test/something/", title="something", feed="retired-feed")
    stale, _ = discover.refilter_pending(session, feeds_path=_config(tmp_path))
    assert stale == []


def test_pruning_deletes_only_what_it_named(session, tmp_path):
    keep = _queued(
        session, "https://op.test/news/new-campus/", title="new campus", feed="op-newsroom"
    )
    drop = _queued(
        session,
        "https://op.test/resources/blogs/compliance/",
        title="compliance",
        feed="op-newsroom",
    )
    stale, _ = discover.refilter_pending(session, feeds_path=_config(tmp_path))

    assert discover.drop_ids(session, [c.row_id for c in stale]) == 1
    assert session.get(IngestUrl, drop.id) is None
    assert session.get(IngestUrl, keep.id) is not None


# --- the handles the listing prints ------------------------------------------


def test_dropping_by_id_works_where_a_truncated_url_could_not(session):
    row = _queued(session, "https://a.test/a-very-long-slug-that-used-to-be-cut-at-sixty-chars/")
    assert discover.drop_pending(session, ids=[row.id]) == 1
    assert session.get(IngestUrl, row.id) is None


def test_dropping_by_feed_clears_one_source_and_no_other(session):
    _queued(session, "https://a.test/one/", feed="noisy")
    _queued(session, "https://a.test/two/", feed="noisy")
    keep = _queued(session, "https://b.test/three/", feed="useful")

    assert discover.drop_pending(session, feeds=["noisy"]) == 2
    assert session.get(IngestUrl, keep.id) is not None


def test_dropping_never_reaches_a_url_that_was_already_crawled(session):
    """`--drop` is about the pending queue; a crawled row is bookkeeping."""
    done = IngestUrl(url="https://a.test/done/", run_id="crawl-1", status="ok")
    session.add(done)
    session.flush()

    assert discover.drop_pending(session, ids=[done.id]) == 0
    assert session.get(IngestUrl, done.id) is not None


# --- one copy of a story -------------------------------------------------------------


def _story(session, url, title, *, status=PENDING_URL_STATUS, minute=0):
    row = IngestUrl(
        url=url,
        run_id="t",
        status=status,
        title=title,
        feed="f",
        first_seen_at=NOW + dt.timedelta(minutes=minute),
    )
    session.add(row)
    session.flush()
    return row


def test_a_press_release_filed_twice_on_one_site_is_read_once(session):
    """CyrusOne files every release under two paths, and both were queued on 2026-10-19."""
    title = "cyrusone closes 1 175 billion abs issuance"
    first = _story(session, "https://www.cyrusone.com/resources/press-releases/abs", title)
    second = _story(session, "https://www.cyrusone.com/media-coverage/abs", title, minute=1)
    copies, back = discover.drop_same_story(session)
    assert [(row.url, kept) for row, kept in copies] == [(second.url, first.url)]
    assert second.status == "skipped" and second.error == discover.SAME_STORY + first.url
    assert first.status == PENDING_URL_STATUS and back == 0


def test_a_story_already_read_on_another_site_is_not_read_again(session):
    title = "Quantum Loophole plans 2100-acre data center campus in Maryland"
    read = _story(session, "https://www.datacenterfrontier.com/x/ql", title, status="ok")
    queued = _story(session, "https://quantumloophole.com/2021/06/28/ql/", title)
    copies, _ = discover.drop_same_story(session)
    assert [(row.url, kept) for row, kept in copies] == [(queued.url, read.url)]


def test_a_headline_cut_short_is_the_story_it_begins(session):
    full = _story(
        session,
        "https://a.test/news/layoffs",
        "Prineville data center with connection to Meta will lay off 66 workers",
        minute=5,
    )
    cut = _story(
        session,
        "https://b.test/s/1",
        "Prineville data center with connection to Meta will lay off ...",
    )
    copies, _ = discover.drop_same_story(session)
    assert [(row.url, kept) for row, kept in copies] == [(cut.url, full.url)], "the full one kept"


def test_a_short_headline_or_a_longer_one_is_another_story(session):
    """Short headlines collide by accident across sites; a longer headline that merely
    begins like another may be the next day's news."""
    _story(session, "https://a.test/1", "Third quarter 2026 earnings conference call")
    _story(session, "https://b.test/2", "Third quarter 2026 earnings conference call")
    _story(session, "https://c.test/3", "CyrusOne raises 9.7 billion for its campuses")
    _story(
        session,
        "https://c.test/4",
        "CyrusOne raises 9.7 billion for its campuses in Texas and Ohio",
    )
    assert discover.drop_same_story(session)[0] == []


def test_a_mobile_or_amp_copy_is_the_same_page(session):
    page = _story(session, "https://www.news.test/story/data-center", "a")
    amp = _story(session, "https://m.news.test/story/data-center/amp", "b", minute=1)
    copies, _ = discover.drop_same_story(session)
    assert [(row.url, kept) for row, kept in copies] == [(amp.url, page.url)]


def test_the_readable_copy_is_kept(session):
    title = "Developer files plans for a 300 MW data center campus in Ohio"
    closed = _story(session, "https://closed.test/a", title)
    open_ = _story(session, "https://open.test/a", title, minute=1)
    copies, _ = discover.drop_same_story(session, unreadable=lambda url: "closed.test" in url)
    assert [(row.url, kept) for row, kept in copies] == [(closed.url, open_.url)]


def test_a_copy_set_aside_comes_back_when_the_one_kept_cannot_be_read(session):
    """A paywall on the first site is not a reason to lose the story."""
    title = "Developer files plans for a 300 MW data center campus in Ohio"
    first = _story(session, "https://paywall.test/a", title)
    second = _story(session, "https://free.test/a", title, minute=1)
    discover.drop_same_story(session)
    assert second.status == "skipped"
    first.status = "thin_content"
    session.flush()
    copies, back = discover.drop_same_story(session)
    assert back == 1 and copies == [] and second.status == PENDING_URL_STATUS


def test_a_report_writes_nothing(session):
    title = "Developer files plans for a 300 MW data center campus in Ohio"
    _story(session, "https://one.test/a", title)
    _story(session, "https://two.test/a", title, minute=1)
    copies, _ = discover.drop_same_story(session, apply=False)
    assert len(copies) == 1 and copies[0][0].status == PENDING_URL_STATUS
