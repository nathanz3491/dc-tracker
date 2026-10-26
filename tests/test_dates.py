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


def test_undated_citations_are_counted_apart_from_undated_queued_articles(session):
    """The total mixes two populations with different lives. On 2026-10-19 it went
    from 1,596 to 5,611 in a day — the news watch's first look had queued thousands
    of articles with no date — while the citations moved from 924 to 951. Only the
    citations are comparable night to night."""
    cited(session, "https://x.test/cited-a")
    cited(session, "https://x.test/cited-b", status="discovered")  # cited, and queued again
    queued(session, "https://x.test/queued-a")
    queued(session, "https://x.test/queued-b")
    queued(session, "https://x.test/queued-c")
    queued(session, "https://x.test/dead-end", status="no_project")

    report = dates.run(session)
    assert (report.undated, report.undated_cited, report.undated_queued) == (5, 2, 3)
    rows = dict(report.as_rows())
    assert rows["undated citations at start"] == 2
    assert rows["undated queued articles"] == 3
    assert "undated, neither (--all)" not in rows

    everything = dates.run(session, everything=True)
    assert dict(everything.as_rows())["undated, neither (--all)"] == 1


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


def test_a_citation_dated_later_than_its_url_takes_the_earlier_date(session):
    """The news watch's sighting date, kept before the page's own was learned.

    DCK's 2017 Digital Realty article was found by a search on 2026-10-12, and the
    citation kept that date; read as 2026, it won a capacity merge on recency.
    """
    cited(session, "https://x.test/2017/10/05/slug")
    source = session.scalar(select(Source))
    source.published_at = dt.datetime(2026, 10, 12)
    session.flush()

    report = dates.run(session, apply=True)

    assert report.citations == 1
    assert session.scalar(select(Source)).published_at == dt.datetime(2017, 10, 5)


def test_a_preview_copies_nothing(session):
    cited(session, "https://x.test/2026/07/13/slug")
    report = dates.run(session, apply=False)
    assert report.citations == 1
    assert session.scalar(select(Source)).published_at is None


# --- asking a publisher once, not every night (migration 0035) --------------------


def test_a_page_that_stated_no_date_is_not_asked_again_for_ninety_days(session):
    """500 of the undated citations were Census data pages that will never state a
    date; a nightly loop with --refetch would spend its whole limit on them."""
    from tracker.models import DateProbe

    cited(session, "https://www.census.gov/table/one")
    fetcher = FakeFetcher(
        {
            "https://www.census.gov/table/one": FetchResult(
                "https://www.census.gov/table/one", True, markdown="t"
            )
        }
    )
    first = dates.run(session, refetch=True, apply=True, fetcher=fetcher)
    assert first.unanswered == 1
    [probe] = session.scalars(select(DateProbe)).all()
    assert probe.outcome == "none"

    second = dates.run(session, refetch=True, apply=True, fetcher=fetcher)
    assert second.skipped_recent == 1
    assert fetcher.calls == ["https://www.census.gov/table/one"], "asked once"

    probe.asked_at = probe.asked_at - dt.timedelta(days=dates.NO_DATE_RETRY_DAYS + 1)
    session.flush()
    dates.run(session, refetch=True, apply=True, fetcher=fetcher)
    assert len(fetcher.calls) == 2, "asked again once the wait is over"


def test_a_failed_request_waits_a_fortnight_not_a_season(session):
    from tracker.models import DateProbe

    cited(session, "https://y.test/opaque")
    fetcher = FakeFetcher({})
    dates.run(session, refetch=True, apply=True, fetcher=fetcher)
    [probe] = session.scalars(select(DateProbe)).all()
    assert probe.outcome == "failed"
    asked = len(fetcher.calls)  # the fetch layer retries a failure; count requests per run
    assert dates.run(session, refetch=True, apply=True, fetcher=fetcher).skipped_recent == 1
    assert len(fetcher.calls) == asked

    probe.asked_at = probe.asked_at - dt.timedelta(days=dates.FAILED_RETRY_DAYS + 1)
    session.flush()
    dates.run(session, refetch=True, apply=True, fetcher=fetcher)
    assert len(fetcher.calls) > asked


def test_the_newest_pages_are_asked_first(session):
    """The memory stops a capped run re-asking the same pages, so the order can
    serve the email: a new article's date decides whether it is news."""
    queued(session, "https://y.test/old", seen=NOW - dt.timedelta(days=30))
    queued(session, "https://y.test/new", seen=NOW)
    fetcher = FakeFetcher({})
    dates.run(session, refetch=True, limit=1, apply=True, fetcher=fetcher)
    assert set(fetcher.calls) == {"https://y.test/new"}


def test_a_preview_remembers_nothing(session):
    from tracker.models import DateProbe

    cited(session, "https://y.test/opaque")
    dates.run(session, refetch=True, apply=False, fetcher=FakeFetcher({}))
    assert session.scalars(select(DateProbe)).all() == []


# --- SEC filings: dated from SEC's record, never fetched for it --------------------
#
# A filing's page states no date, so the page rung cannot find one however often it
# asks, and 172 of the 173 citations of sec.gov were undated on 2026-10-26. SEC's own
# record of when each was filed does have it, one request a company.

COMPANY = "https://www.sec.gov/Archives/edgar/data/1326801"
Q2 = f"{COMPANY}/000162828026050705/meta-20260630.htm"  # 0001628280-26-050705
Q1 = f"{COMPANY}/000162828026000012/meta-20260331.htm"  # 0001628280-26-000012
FILED = dt.datetime(2026, 7, 30)


class FakeSEC:
    """SEC's submissions record for Meta, counting what is asked of it."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.urls: list[str] = []

    def get(self, url: str, **_params):
        import httpx

        self.urls.append(url)
        if self.down:
            return httpx.Response(503)
        if not url.endswith("/CIK0001326801.json"):
            return httpx.Response(404)
        filed = {"0001628280-26-050705": "2026-07-30", "0001628280-26-000012": "2026-04-30"}
        recent = {"accessionNumber": list(filed), "filingDate": list(filed.values())}
        return httpx.Response(200, json={"filings": {"recent": recent, "files": []}})

    def close(self) -> None:
        pass


def sec_run(session, sec=None, **kwargs):
    """`dates.run` as the nightly loop calls it, with SEC's record standing in."""
    from tracker.ingest.edgar import FilingDates

    kwargs = {"refetch": True, "apply": True, "fetcher": FakeFetcher({}), **kwargs}
    return dates.run(session, filings=FilingDates(client=sec or FakeSEC()), **kwargs)


def citation(session, url) -> Source:
    return session.scalar(select(Source).where(Source.url == url))


def queue_row(session, url) -> IngestUrl:
    return session.scalar(select(IngestUrl).where(IngestUrl.url == url))


def test_a_filing_is_dated_from_secs_record_and_never_fetched(session):
    cited(session, Q2)
    cited(session, "https://y.test/opaque")
    fetcher = FakeFetcher({})

    report = sec_run(session, fetcher=fetcher)

    assert (report.from_filing, report.unfiled, report.written, report.citations) == (1, 0, 1, 1)
    assert set(fetcher.calls) == {"https://y.test/opaque"}, (
        "a filing is not downloaded to be told it states no date"
    )
    assert queue_row(session, Q2).published_at == FILED
    assert citation(session, Q2).published_at == FILED
    assert citation(session, "https://y.test/opaque").published_at is None
    assert (report.fetched, report.failed) == (1, 1), "the page rung still serves everything else"


def test_only_the_filings_that_lack_a_date_are_asked_about(session):
    """Fill-only, like every rung: a filing the queue already dates is not asked
    about, and what it holds is not touched."""
    dated = "https://www.sec.gov/Archives/edgar/data/1494877/000155837025009991/R13.htm"
    cited(session, dated)
    queue_row(session, dated).published_at = dt.datetime(2020, 1, 1)
    cited(session, Q1)
    cited(session, "https://y.test/2026/07/13/dated-in-path")
    sec = FakeSEC()

    report = sec_run(session, sec)

    assert report.from_filing == 1
    assert queue_row(session, dated).published_at == dt.datetime(2020, 1, 1)
    assert queue_row(session, Q1).published_at == dt.datetime(2026, 4, 30)
    assert [u.rsplit("/", 1)[1] for u in sec.urls] == ["CIK0001326801.json"], (
        "the company with nothing undated is not asked about"
    )


def test_a_filing_sec_does_not_list_is_left_undated_and_is_not_fetched(session):
    """The page rung's only answer for it would be a download and a `date_probe` row
    keeping the *page* rung away for 90 days — from a URL it was never going to date."""
    from tracker.models import DateProbe

    cited(session, f"{COMPANY}/000162828026099999/meta-20261231.htm")
    fetcher = FakeFetcher({})

    report = sec_run(session, fetcher=fetcher)

    assert (report.from_filing, report.unfiled, report.fetched) == (0, 1, 0)
    assert fetcher.calls == []
    assert session.scalars(select(DateProbe)).all() == []
    assert dict(report.as_rows())["SEC filings it gave no date for"] == 1
    assert session.scalar(select(Source)).published_at is None


def test_the_report_says_nothing_of_filings_unless_some_were_left_undated(session):
    cited(session, Q2)
    assert "SEC filings it gave no date for" not in dict(sec_run(session).as_rows())


def test_sec_being_down_leaves_the_filings_for_the_next_night(session):
    """The date is a record SEC keeps, not a page that failed: nothing is remembered
    against it, so tomorrow asks again at the cost of a request a company."""
    cited(session, Q2)

    report = sec_run(session, FakeSEC(down=True))
    assert (report.from_filing, report.unfiled) == (0, 1)
    assert citation(session, Q2).published_at is None

    report = sec_run(session)
    assert report.from_filing == 1
    assert citation(session, Q2).published_at == FILED


def test_without_a_contact_in_the_user_agent_nothing_is_asked_and_nothing_fails(session):
    """The shipped placeholder is refused by SEC for the whole run, so the lookup says
    so once and the run goes on. No resolver is injected, which is the nightly's path;
    the suite's network guard fails this test if a request goes out."""
    cited(session, Q2)

    report = dates.run(session, refetch=True, apply=True, fetcher=FakeFetcher({}))

    assert (report.from_filing, report.unfiled) == (0, 1)
    assert citation(session, Q2).published_at is None


def test_without_refetch_sec_is_not_asked(session):
    """The request is the expensive half, here as for a page."""
    cited(session, Q2)
    sec = FakeSEC()

    report = sec_run(session, sec, refetch=False)

    assert sec.urls == []
    assert (report.from_filing, report.remaining) == (0, 1)


def test_a_preview_reports_the_filing_dates_and_writes_none(session):
    cited(session, Q2)

    report = sec_run(session, apply=False)

    assert (report.from_filing, report.written, report.citations) == (1, 0, 1)
    assert queue_row(session, Q2).published_at is None
    assert citation(session, Q2).published_at is None
    assert report.dated == 1
    assert report.examples == [(Q2, FILED, "sec")]


def test_a_citation_no_queue_row_holds_is_dated_too(session):
    """2 of the 98 SEC pages cited on 2026-10-26 had no queue row — the agent's
    gap-fill cites what a search found without crawling it — and `undated_urls` reads
    the queue, so it never saw them."""
    cited(session, "https://y.test/anything")
    session.add(
        Source(
            project_id=session.scalar(select(Project)).id,
            url=Q2,
            source_type="company_filing",
            fetched_at=NOW,
        )
    )
    session.flush()

    report = sec_run(session)

    assert (report.from_filing, report.written, report.citations) == (1, 0, 1)
    assert citation(session, Q2).published_at == FILED
    assert queue_row(session, Q2) is None, "no queue row is made to hold it"


def test_limit_does_not_cap_the_filings(session):
    """It bounds the page fetching. SEC's record is a request a company, not a page."""
    cited(session, Q2)
    cited(session, Q1)
    cited(session, "https://y.test/opaque-1")
    cited(session, "https://y.test/opaque-2")

    report = sec_run(session, limit=1)

    assert report.from_filing == 2
    assert report.fetched == 1


def test_a_second_run_asks_sec_nothing(session):
    cited(session, Q2)
    sec_run(session)
    sec = FakeSEC()

    second = sec_run(session, sec)

    assert sec.urls == []
    assert (second.undated, second.from_filing) == (0, 0)


def test_the_dated_total_counts_every_rung(session):
    cited(session, "https://x.test/2026/07/13/dated-in-path")
    cited(session, Q2)
    cited(session, "https://y.test/opaque")
    fetcher = FakeFetcher(
        {
            "https://y.test/opaque": FetchResult(
                "https://y.test/opaque", True, markdown="t", published_at=dt.datetime(2025, 5, 5)
            )
        }
    )

    report = sec_run(session, fetcher=fetcher)

    assert (report.from_url, report.from_filing, report.from_page) == (1, 1, 1)
    assert report.dated == 3 and report.written == 3
