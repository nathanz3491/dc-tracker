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
topic = ["data cent"]
signal = ["campus", "megawatt"]
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
