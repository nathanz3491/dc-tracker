"""Backfilling `ingest_url.published_at` for URLs already stored.

Two things carry this module and both are asserted below: it only considers URLs
where a date changes something, and it never overwrites one.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from tracker import dates
from tracker.ingest.fetch import FetchResult
from tracker.models import IngestUrl, Project, Source

NOW = dt.datetime(2026, 8, 13, 12, 0, 0)


class FakeFetcher:
    """Returns canned FetchResults. Records what it was asked for."""

    def __init__(self, mapping: dict[str, FetchResult]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        return self.mapping.get(
            url, FetchResult(url, False, error="not in fixture", fetched_at=NOW)
        )


def queued(session, url, *, status="discovered", published_at=None, seen=NOW):
    session.add(
        IngestUrl(
            url=url,
            run_id="r",
            status=status,
            published_at=published_at,
            first_seen_at=seen,
            last_tried_at=seen,
        )
    )
    session.flush()


def cited(session, url, *, status="ok"):
    """An `ingest_url` row that also backs a stored citation."""
    project = session.scalar(select(Project))
    if project is None:
        project = Project(
            name="Hyperion",
            company="Meta",
            county="Richland Parish",
            state="LA",
            dedup_key="meta|county:richland|LA",
        )
        session.add(project)
        session.flush()
    queued(session, url, status=status)
    session.add(Source(project_id=project.id, url=url, source_type="trade_press", fetched_at=NOW))
    session.flush()


# --- which URLs are worth dating --------------------------------------------


def test_only_urls_a_date_would_change_are_considered(session):
    """The 68% saving.

    Only two things read this column: `upsert._published_at`, for a URL backing a
    citation, and `crawl.published_dates`, for a URL still queued. On the live
    database 3,774 of 5,552 undated rows are neither — `no_project`,
    `fetch_error`, orphans — so fetching them is thousands of requests to fill a
    column nothing reads.
    """
    cited(session, "https://a.test/cited")
    queued(session, "https://b.test/queued", status="discovered")
    queued(session, "https://c.test/dead-end", status="no_project")
    queued(session, "https://d.test/failed", status="fetch_error")
    queued(session, "https://e.test/orphan", status="ok")

    assert set(dates.undated_urls(session)) == {
        "https://a.test/cited",
        "https://b.test/queued",
    }


def test_everything_widens_the_net(session):
    cited(session, "https://a.test/cited")
    queued(session, "https://c.test/dead-end", status="no_project")
    assert len(dates.undated_urls(session, everything=True)) == 2


def test_a_url_that_already_has_a_date_is_not_reconsidered(session):
    cited(session, "https://a.test/cited")
    queued(session, "https://b.test/dated", published_at=dt.datetime(2026, 1, 1))
    assert dates.undated_urls(session) == ["https://a.test/cited"]


def test_oldest_first(session):
    queued(session, "https://a.test/new", seen=dt.datetime(2026, 8, 1))
    queued(session, "https://b.test/old", seen=dt.datetime(2024, 1, 1))
    assert dates.undated_urls(session) == ["https://b.test/old", "https://a.test/new"]


# --- the free rung ----------------------------------------------------------


def test_the_url_rung_dates_without_a_request(session):
    queued(session, "https://x.test/2026/07/13/meta-hyperion")
    report = dates.run(session, apply=True)
    assert (report.from_url, report.fetched, report.written) == (1, 0, 1)
    row = session.scalar(select(IngestUrl))
    assert row.published_at == dt.datetime(2026, 7, 13)


def test_without_apply_nothing_is_written(session):
    queued(session, "https://x.test/2026/07/13/meta-hyperion")
    report = dates.run(session)
    assert report.from_url == 1
    assert report.written == 0
    assert session.scalar(select(IngestUrl)).published_at is None


def test_running_twice_writes_nothing_the_second_time(session):
    queued(session, "https://x.test/2026/07/13/slug")
    first = dates.run(session, apply=True)
    second = dates.run(session, apply=True)
    assert first.written == 1
    assert (second.undated, second.written) == (0, 0)


# --- the fetching rung ------------------------------------------------------


def test_the_page_rung_reads_the_date_the_fetch_carried(session):
    cited(session, "https://x.test/no-date-in-path")
    fetcher = FakeFetcher(
        {
            "https://x.test/no-date-in-path": FetchResult(
                "https://x.test/no-date-in-path",
                True,
                markdown="text",
                fetched_at=NOW,
                published_at=dt.datetime(2024, 12, 4),
            )
        }
    )
    report = dates.run(session, refetch=True, apply=True, fetcher=fetcher)
    assert (report.fetched, report.from_page, report.written) == (1, 1, 1)
    assert session.scalar(select(IngestUrl)).published_at == dt.datetime(2024, 12, 4)


def test_a_page_that_states_no_date_is_counted_not_guessed(session):
    cited(session, "https://x.test/undated")
    fetcher = FakeFetcher(
        {"https://x.test/undated": FetchResult("https://x.test/undated", True, markdown="t")}
    )
    report = dates.run(session, refetch=True, apply=True, fetcher=fetcher)
    assert (report.unanswered, report.written) == (1, 0)
    assert session.scalar(select(IngestUrl)).published_at is None


def test_a_failed_fetch_is_reported_separately(session):
    cited(session, "https://x.test/gone")
    report = dates.run(session, refetch=True, apply=True, fetcher=FakeFetcher({}))
    assert (report.failed, report.written) == (1, 0)


def test_the_free_rung_shrinks_the_fetch_list(session):
    """A URL the path already dated is never requested.

    Asserted on distinct URLs: `fetch_with_retry` legitimately re-attempts a
    network-level failure, so the raw call list carries duplicates.
    """
    cited(session, "https://x.test/2026/07/13/dated-in-path")
    cited(session, "https://y.test/opaque")
    fetcher = FakeFetcher({})
    dates.run(session, refetch=True, apply=True, fetcher=fetcher)
    assert set(fetcher.calls) == {"https://y.test/opaque"}


def test_limit_bounds_the_fetching_not_the_free_pass(session):
    """The bug this asserts against.

    `--limit` defaults to 25, inherited from `backfill blocks` where it caps LLM
    calls. Applying it to the whole run throttled a free offline pass to 25 of
    5,552 rows and reported 5 dated where the true answer is 422.
    """
    for n in range(4):
        cited(session, f"https://x.test/2026/07/1{n}/dated")
    for n in range(4):
        cited(session, f"https://y.test/opaque-{n}")

    fetcher = FakeFetcher({})
    report = dates.run(session, refetch=True, limit=1, apply=True, fetcher=fetcher)
    assert report.from_url == 4, "the free rung must not be capped"
    assert len(set(fetcher.calls)) == 1, "the fetching must be capped"
    assert report.remaining == 3


def test_without_refetch_nothing_is_requested(session):
    cited(session, "https://y.test/opaque")
    fetcher = FakeFetcher({})
    report = dates.run(session, apply=True, fetcher=fetcher)
    assert fetcher.calls == []
    assert report.remaining == 1


# --- the guarantee that makes it safe to re-run -----------------------------


def test_an_existing_date_is_never_overwritten(session):
    """A publisher's date is not ours to move, and a long run can race a sync."""
    queued(
        session,
        "https://x.test/2026/07/13/slug",
        published_at=dt.datetime(2020, 1, 1),
    )
    report = dates.run(session, apply=True, everything=True)
    assert report.written == 0
    assert session.scalar(select(IngestUrl)).published_at == dt.datetime(2020, 1, 1)


def test_nothing_but_the_date_column_moves(session):
    cited(session, "https://x.test/2026/07/13/slug", status="ok")
    before = session.scalar(select(IngestUrl))
    snapshot = (before.status, before.attempts, before.run_id, before.last_tried_at)

    dates.run(session, apply=True)

    after = session.scalar(select(IngestUrl))
    assert (after.status, after.attempts, after.run_id, after.last_tried_at) == snapshot
    assert after.published_at == dt.datetime(2026, 7, 13)


@pytest.mark.parametrize("apply", [True, False])
def test_the_report_adds_up(session, apply):
    cited(session, "https://x.test/2026/07/13/dated")
    cited(session, "https://y.test/opaque")
    fetcher = FakeFetcher(
        {
            "https://y.test/opaque": FetchResult(
                "https://y.test/opaque", True, markdown="t", published_at=dt.datetime(2025, 5, 5)
            )
        }
    )
    report = dates.run(session, refetch=True, apply=apply, fetcher=fetcher)
    assert report.undated == 2
    assert report.dated == report.from_url + report.from_page == 2
    assert report.fetched == report.from_page + report.unanswered + report.failed
    assert report.written == (2 if apply else 0)


# --- the date has to reach where the merge reads it -------------------------


def test_the_date_is_copied_onto_the_citations_that_quote_the_url(session):
    """Without this the whole command changes nothing.

    `upsert.claims_by_field` breaks its tie on `source.published_at`. The queue
    table is where a date is *learned*; the citation is where it is *read*.
    `upsert_record` bridges the two, but only for a URL it is ingesting — so a date
    discovered afterwards never arrived, and 1,600 page requests would have filled
    a column nothing consults.
    """
    cited(session, "https://x.test/2026/07/13/slug")
    assert session.scalar(select(Source)).published_at is None

    report = dates.run(session, apply=True)

    assert report.citations == 1
    assert session.scalar(select(Source)).published_at == dt.datetime(2026, 7, 13)


def test_a_citation_that_already_has_a_date_keeps_it(session):
    """Fill-only, the same rule `upsert_record` follows.

    A date already on a citation came from the publisher when the article was read.
    A later pass has no better claim to it.
    """
    cited(session, "https://x.test/2026/07/13/slug")
    source = session.scalar(select(Source))
    source.published_at = dt.datetime(2019, 3, 3)
    session.flush()

    report = dates.run(session, apply=True)

    assert report.citations == 0
    assert session.scalar(select(Source)).published_at == dt.datetime(2019, 3, 3)


def test_a_preview_copies_nothing(session):
    cited(session, "https://x.test/2026/07/13/slug")
    report = dates.run(session, apply=False)
    assert report.citations == 1
    assert session.scalar(select(Source)).published_at is None
