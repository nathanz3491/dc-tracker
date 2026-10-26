"""Filling `ingest_url.published_at` for URLs already in the database.

`fetch.published_date` reads the date out of a page at fetch time, which fixes
every article read from now on and none of the 2,432 already stored. Those were
fetched by a version that discarded the metadata before caching, so there is
nothing local left to read: of 585 cached articles, **zero** contain
`datePublished`, `article:published_time`, `<time` or a JSON-LD block. The text is
all that survived. Recovering their dates means going back to the publisher.

**Why it is worth a crawl.** `upsert` ranks competing claims on
`(confirmed, weight, recency)`, and `recency` falls back to `fetched_at` — crawl
order — whenever a date is missing. 506 stored values currently have a
same-weight rival that disagrees, so each is settled by which page the crawler
happened to visit second. Hyperion (#10) holds Meta's superseded $10B for exactly
that reason, and both sides of it come from the same publisher, so no authority
rule can separate them.

**Three rungs, cheapest first**, the same shape as the fetcher's own ladder:

1. **The URL path.** `/2026/07/13/slug`. Free, offline, deterministic, and it
   dates 422 queued URLs on the live database. A dated path is the publisher's
   own filing convention, not an inference.
2. **SEC's own record, for a filing.** A filing's page states no date — it is an
   inline-XBRL document with no metadata — so the rung below cannot find one in it
   however often it asks: on 2026-10-26, 172 of the 173 citations of sec.gov were
   undated, and a 2019 10-K was ranked as this month's evidence. SEC does say when each
   filing was filed, and one request to its submissions API dates every filing a company
   has made, so the cost is a request per company, not per page (90 filings, 24
   companies). A filing is therefore never fetched for its date. `edgar.FilingDates`.
3. **The page.** One GET each, no LLM. 7 of 10 sampled live pages carry a
   machine-readable date.

**One column, and only where it is NULL.** Nothing else on the row is touched —
not `status`, not `attempts`, not `last_tried_at`, and never the article text or
the cache. A backfill that quietly re-extracted would be the thing
`backfill.run`'s docstring warns about one table over: a large unrelated change
smuggled inside a repair.

**It does not go through `crawl.run`.** That path consults `_split_cached` first,
which serves any cached URL and removes it from the fetch list — so a date
backfill routed through it would read the local text file, find no metadata in it,
and report that the publisher states no date. Fetching directly is what makes this
work at all.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings
from tracker.ingest.fetch import Fetcher, date_from_url, fetch_all
from tracker.models import DateProbe, IngestUrl, Source, utcnow
from tracker.vocab import PENDING_URL_STATUS

if TYPE_CHECKING:
    from tracker.ingest.edgar import FilingDates

log = logging.getLogger(__name__)

#: Days a page that answered with no date is left alone before it is asked again.
#: Most never will state one — a Census data page, a facility directory — and
#: asking them nightly spent the whole `--limit` on the same pages. Migration 0035.
NO_DATE_RETRY_DAYS = 90

#: Days a failed request waits. Shorter: a timeout or a 503 is more often a bad
#: night at the publisher than a refusal.
FAILED_RETRY_DAYS = 14


@dataclass
class DateReport:
    """What a backfill found, and what it wrote."""

    #: Rows with no `published_at` when the run started: every one this run considers.
    undated: int = 0
    #: Of those, the ones behind a stored citation — the number to compare from one
    #: night to the next. The rest are articles still queued to be read, and they swing
    #: with what discovery queued: the news watch's first look took the total from
    #: 1,596 at the start of 2026-10-18 to 5,611 at the start of 10-19 (5,231 by the
    #: time the loop's dates step ran), while the citations went from 924 to 951.
    undated_cited: int = 0
    #: Of those, the articles still queued to be read and not yet behind a citation.
    #: With `everything`, the rest of `undated` is neither.
    undated_queued: int = 0
    #: Dated by the free offline pass over the URL path.
    from_url: int = 0
    #: Pages of SEC filings dated from SEC's own record of when they were filed.
    from_filing: int = 0
    #: Pages of SEC filings SEC gave no date for: a filing its record does not list, or
    #: a night it did not answer. Left undated and not fetched, since a filing's page
    #: states no date to be found.
    unfiled: int = 0
    #: Pages actually requested.
    fetched: int = 0
    #: Dated from the page's own metadata.
    from_page: int = 0
    #: Fetched and still undated — the page states none, or refused us.
    unanswered: int = 0
    #: Fetches that failed outright.
    failed: int = 0
    #: Rows written on `ingest_url`. Zero unless `apply`.
    written: int = 0
    #: Citations the date was copied onto. See `_propagate`.
    citations: int = 0
    #: Still undated and not fetched — either `--refetch` was off, or the run hit
    #: `--limit`. What is left for the next pass.
    remaining: int = 0
    #: Left out of this run's requests: asked recently (see `NO_DATE_RETRY_DAYS`),
    #: or on a publisher that challenges every client, where a request can only fail.
    skipped_recent: int = 0
    skipped_closed: int = 0
    examples: list[tuple[str, dt.datetime, str]] = dc_field(default_factory=list)

    @property
    def dated(self) -> int:
        return self.from_url + self.from_filing + self.from_page

    def as_rows(self) -> list[tuple[str, int]]:
        neither = self.undated - self.undated_cited - self.undated_queued
        return [
            ("undated citations at start", self.undated_cited),
            ("undated queued articles", self.undated_queued),
            *([("undated, neither (--all)", neither)] if neither else []),
            ("dated from the URL", self.from_url),
            ("dated from SEC's filing record", self.from_filing),
            *([("SEC filings it gave no date for", self.unfiled)] if self.unfiled else []),
            ("pages fetched", self.fetched),
            ("dated from the page", self.from_page),
            ("fetched, no date stated", self.unanswered),
            ("fetch failed", self.failed),
            ("asked recently, skipped", self.skipped_recent),
            ("closed publisher, skipped", self.skipped_closed),
            ("rows written", self.written),
            ("citations dated", self.citations),
        ]


def undated_urls(session: Session, *, everything: bool = False) -> list[str]:
    """URLs with no publication date that a date would actually change, oldest first.

    **Two things read this column, and only two.** `upsert._published_at` copies it
    onto `source.published_at`, where it breaks a merge tie — so it matters for a
    URL that backs a stored citation. And `crawl.published_dates` feeds it to the
    prompt as `ARTICLE_DATE`, which RULE 5 resolves "next year" against — so it
    matters for a URL still queued to be read.

    Everything else is a dead end. Measured on the live database, of 5,552 undated
    rows only **1,778** are one of those two: the remainder are 2,093 `no_project`,
    1,059 `fetch_error`, 439 extracted-but-orphaned, 177 `thin_content` and 6
    `parse_error`. None will ever feed a tiebreak or an extraction, and fetching
    them is 3,774 requests at third-party hosts to fill a column nothing reads.

    `everything=True` widens it anyway, for a run that wants the column complete
    rather than useful.

    Oldest first, so a capped run works through the backlog instead of
    reconsidering the same recent rows — the same ordering the crawl queue drains
    in.
    """
    stmt = select(IngestUrl.url).where(IngestUrl.published_at.is_(None))
    if not everything:
        stmt = stmt.where(_backs_a_citation() | (IngestUrl.status == PENDING_URL_STATUS))
    return list(session.scalars(stmt.order_by(IngestUrl.first_seen_at.asc(), IngestUrl.id.asc())))


def _backs_a_citation():
    return select(Source.id).where(Source.url == IngestUrl.url).exists()


def _undated_where(*conditions):
    return select(IngestUrl.url).where(IngestUrl.published_at.is_(None), *conditions)


def _split(session: Session, pending: list[str]) -> tuple[int, int]:
    """(behind a citation, queued and not) among `pending`. See `DateReport.undated_cited`."""
    cited = set(session.scalars(_undated_where(_backs_a_citation())))
    queued = set(session.scalars(_undated_where(IngestUrl.status == PENDING_URL_STATUS)))
    return (
        sum(1 for url in pending if url in cited),
        sum(1 for url in pending if url in queued and url not in cited),
    )


def _worth_asking(session: Session, urls: list[str], report: DateReport) -> list[str]:
    """The URLs a request might actually date, newest first.

    Two kinds are left out and counted. A page asked recently — it stated no date
    (`NO_DATE_RETRY_DAYS`) or the request failed (`FAILED_RETRY_DAYS`) — because
    asking again tomorrow gets the same answer. And a page on a publisher that
    challenges every client (`discover.closed_domains`), because the request can
    only fail.

    **Newest first**, against the oldest-first order the backlog is listed in. The
    memory above is what stops a capped run reconsidering the same pages, so the
    order is free to serve the email instead: a recent article's date decides
    whether it is news (`feed.reported_on`), and a 2019 one's decides only a tie.
    """
    from tracker.confidence import registrable_domain
    from tracker.ingest import discover

    now = utcnow()
    recent: set[str] = set()
    for url, outcome in session.execute(
        select(DateProbe.url, DateProbe.outcome).where(
            DateProbe.asked_at >= now - dt.timedelta(days=NO_DATE_RETRY_DAYS)
        )
    ):
        if outcome == "none":
            recent.add(url)
    recent |= set(
        session.scalars(
            select(DateProbe.url).where(
                DateProbe.outcome == "failed",
                DateProbe.asked_at >= now - dt.timedelta(days=FAILED_RETRY_DAYS),
            )
        )
    )
    closed = discover.closed_domains(reopened=discover.reopened_names(session))
    out: list[str] = []
    for url in reversed(urls):
        if url in recent:
            report.skipped_recent += 1
        elif registrable_domain(url) in closed:
            report.skipped_closed += 1
        else:
            out.append(url)
    return out


def _store(session: Session, url: str, when: dt.datetime, *, apply: bool) -> bool:
    """Fill the column if it is empty. Returns whether a row was written.

    Re-checked here rather than trusted from the earlier query: a long fetching
    run can overlap a `sync` that dated the same URL from a feed, and the
    publisher's own date is not ours to overwrite either way.
    """
    if not apply:
        return False
    row = session.scalar(select(IngestUrl).where(IngestUrl.url == url))
    if row is None or row.published_at is not None:
        return False
    row.published_at = when
    return True


#: Narrows the scan for citations on SEC's host; `parse_filing_url` decides what is one.
_SEC_LIKE = "%sec.gov/%"


def _undated_filings(session: Session, still_undated: list[str]) -> list[str]:
    """The pages of SEC filings with no date, whether queued or only cited.

    The queued ones are in `still_undated` already. A citation can also stand on a URL
    no queue row holds — the agent's gap-fill cites what a search found without
    crawling it, and 2 of the 98 SEC URLs cited on 2026-10-26 were of that kind — and
    `undated_urls` reads the queue, so it never sees one. Where a queue row exists and
    holds a date, `_propagate` copies it onto the citation, so only a citation with no
    dated row behind it is listed here.
    """
    from tracker.ingest.edgar import parse_filing_url

    urls = [url for url in still_undated if parse_filing_url(url) is not None]
    listed = set(urls)
    dated_in_queue = set(
        session.scalars(
            select(IngestUrl.url).where(
                IngestUrl.published_at.is_not(None), IngestUrl.url.like(_SEC_LIKE)
            )
        )
    )
    cited = session.scalars(
        select(Source.url)
        .where(Source.published_at.is_(None), Source.url.like(_SEC_LIKE))
        .distinct()
    )
    for url in cited:
        if url in listed or url in dated_in_queue or parse_filing_url(url) is None:
            continue
        listed.add(url)
        urls.append(url)
    return urls


def _ask_sec(
    session: Session,
    still_undated: list[str],
    found: dict[str, dt.datetime],
    report: DateReport,
    *,
    apply: bool,
    settings: Settings,
    filings: FilingDates | None,
) -> list[str]:
    """Date the pages of SEC filings from SEC's record, and return `still_undated` without them.

    **Every filing page leaves the list, dated or not.** The next rung fetches a page and
    reads its metadata, and a filing's page has none — so for a filing SEC has no date for
    that rung can only spend a download of up to two megabytes to say so, and write a
    `date_probe` row that would keep the *page* rung away for 90 days from a URL it was
    never going to date.

    **Not remembered, and not capped by `limit`.** The filing's date is not a page
    request that failed; the question is a record SEC keeps, and one request answers it
    for every filing a company has made. So a filing SEC did not answer for tonight is
    asked again tomorrow, which costs one request per company, and the whole backlog is
    asked at once: 172 citations were 24 companies. The rate limit is the client's, not
    this function's.
    """
    from tracker.ingest import edgar

    urls = _undated_filings(session, still_undated)
    if not urls:
        return still_undated
    filed = edgar.dates_of(urls, resolver=filings, settings=settings)
    for url in urls:
        when = filed.get(url)
        if when is None:
            report.unfiled += 1
            continue
        report.from_filing += 1
        found[url] = when
        if _store(session, url, when, apply=apply):
            report.written += 1
        if len(report.examples) < 10:
            report.examples.append((url, when, "sec"))
    taken = set(urls)
    return [url for url in still_undated if url not in taken]


def _propagate(session: Session, found: dict[str, dt.datetime], *, apply: bool) -> int:
    """Copy each URL's publication date onto the citations that quote it.

    **Without this the whole module changes nothing.** `upsert.claims_by_field`
    breaks a merge tie on `source.published_at`, not on `ingest_url.published_at` —
    the queue table is where the date is *learned* and the citation is where it is
    *read*. `upsert_record` bridges the two, but only for a URL it is ingesting, so
    a date discovered after a citation was written never reaches it. Filling one
    column and calling the tiebreak fixed would be the most expensive kind of
    no-op: 1,600 page requests spent on a value nothing consults.

    Never moves a date later — the same rule `upsert_record` follows. It may move one
    earlier: a citation dated by when the news watch found its URL is corrected to
    the publisher's date once the queue row has learned it.

    `found` is what *this* run resolved, and it is what makes the preview honest:
    without `apply` nothing has been written to `ingest_url` yet, so a join alone
    would report zero citations for every date the run just discovered — a preview
    of the wrong thing.
    """
    dates: dict[str, dt.datetime] = dict(
        session.execute(
            select(IngestUrl.url, IngestUrl.published_at).where(IngestUrl.published_at.is_not(None))
        ).all()
    )
    dates.update({url: when for url, when in found.items() if url not in dates})

    # Also a citation dated *later* than its URL: the news watch's sighting date,
    # kept before the page's own was learned (`upsert_record` explains). An earlier
    # date for the same URL is the publisher's; a citation is never moved later.
    rows = [
        row
        for row in session.scalars(select(Source)).all()
        if row.url in dates and (row.published_at is None or dates[row.url] < row.published_at)
    ]
    if not apply:
        return len(rows)
    for row in rows:
        row.published_at = dates[row.url]
    session.flush()
    return len(rows)


def run(
    session: Session,
    *,
    limit: int | None = None,
    refetch: bool = False,
    apply: bool = False,
    everything: bool = False,
    settings: Settings | None = None,
    fetcher: Fetcher | None = None,
    escalate: object | None = None,
    filings: FilingDates | None = None,
) -> DateReport:
    """Date what can be dated. Report-only unless `apply`.

    `refetch` is the expensive half: without it only the free URL-path rung runs,
    which is worth having on its own and costs nothing to try first. With it SEC is
    asked about the filings and the publishers about the rest. `filings` stands in
    for SEC's record, for tests.

    **`limit` bounds the page fetching, not the run.** The URL-path rung always sweeps
    the whole backlog because it costs nothing — capping it the way
    `backfill blocks` caps LLM calls throttled a free pass to 25 of 5,552 rows and
    reported "5 dated" where the true offline answer is 422. SEC's rung is not capped
    either, for the reason `_ask_sec` gives: it is a request a company, not a page.
    """
    import asyncio

    settings = settings or get_settings()
    report = DateReport()

    pending = undated_urls(session, everything=everything)
    report.undated = len(pending)
    report.undated_cited, report.undated_queued = _split(session, pending)
    #: What this run resolved, so `_propagate` can report honestly under --dry-run.
    found: dict[str, dt.datetime] = {}

    # Rung 1: the URL path. Free, so it runs over everything before anything is
    # requested, and it shrinks the fetch list for rung 2.
    still_undated: list[str] = []
    for url in pending:
        when = date_from_url(url)
        if when is None:
            still_undated.append(url)
            continue
        report.from_url += 1
        found[url] = when
        if _store(session, url, when, apply=apply):
            report.written += 1
        if len(report.examples) < 10:
            report.examples.append((url, when, "url"))

    if apply:
        session.flush()

    # Rung 2: SEC's own record, for the pages of filings. A request a company, so it
    # asks for the whole backlog; and it runs before the page rung so that no filing
    # is downloaded to be told it states no date.
    if refetch:
        still_undated = _ask_sec(
            session,
            still_undated,
            found,
            report,
            apply=apply,
            settings=settings,
            filings=filings,
        )
        if apply:
            session.flush()

    if not refetch or not still_undated:
        report.remaining = len(still_undated)
        report.citations = _propagate(session, found, apply=apply)
        return report

    still_undated = _worth_asking(session, still_undated, report)
    if limit:
        report.remaining = max(0, len(still_undated) - limit)
        still_undated = still_undated[:limit]

    # Rung 3: ask the publisher. `fetch_all` rather than a bare loop, for the
    # per-host gate, the politeness delay and the escalation ladder — this is
    # thousands of requests across a couple of thousand third-party hosts, and a
    # WAF that fingerprints TLS answers 403 to httpx and 200 to curl_cffi.
    results = asyncio.run(
        fetch_all(still_undated, fetcher=fetcher, escalate=escalate, settings=settings)
    )
    report.fetched = len(results)
    asked_at = utcnow()
    for result in results:
        outcome = "failed" if not result.ok else "none" if result.published_at is None else "dated"
        if apply:
            session.add(DateProbe(url=result.url, asked_at=asked_at, outcome=outcome))
        if not result.ok:
            report.failed += 1
            continue
        if result.published_at is None:
            report.unanswered += 1
            continue
        report.from_page += 1
        found[result.url] = result.published_at
        if _store(session, result.url, result.published_at, apply=apply):
            report.written += 1
        if len(report.examples) < 10:
            report.examples.append((result.url, result.published_at, result.via))

    if apply:
        session.flush()
    report.citations = _propagate(session, found, apply=apply)
    return report


__all__ = ["FAILED_RETRY_DAYS", "NO_DATE_RETRY_DAYS", "DateReport", "run", "undated_urls"]
