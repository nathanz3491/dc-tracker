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

**Two rungs, cheapest first**, the same shape as the fetcher's own ladder:

1. **The URL path.** `/2026/07/13/slug`. Free, offline, deterministic, and it
   dates 422 queued URLs on the live database. A dated path is the publisher's
   own filing convention, not an inference.
2. **The page.** One GET each, no LLM. 7 of 10 sampled live pages carry a
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

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings
from tracker.ingest.fetch import Fetcher, date_from_url, fetch_all
from tracker.models import IngestUrl, Source
from tracker.vocab import PENDING_URL_STATUS

log = logging.getLogger(__name__)


@dataclass
class DateReport:
    """What a backfill found, and what it wrote."""

    #: Rows with no `published_at` when the run started.
    undated: int = 0
    #: Dated by the free offline pass over the URL path.
    from_url: int = 0
    #: Pages actually requested.
    fetched: int = 0
    #: Dated from the page's own metadata.
    from_page: int = 0
    #: Fetched and still undated — the page states none, or refused us.
    unanswered: int = 0
    #: Fetches that failed outright.
    failed: int = 0
    #: Rows written. Zero unless `apply`.
    written: int = 0
    #: Still undated and not fetched — either `--refetch` was off, or the run hit
    #: `--limit`. What is left for the next pass.
    remaining: int = 0
    examples: list[tuple[str, dt.datetime, str]] = dc_field(default_factory=list)

    @property
    def dated(self) -> int:
        return self.from_url + self.from_page

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("undated at start", self.undated),
            ("dated from the URL", self.from_url),
            ("pages fetched", self.fetched),
            ("dated from the page", self.from_page),
            ("fetched, no date stated", self.unanswered),
            ("fetch failed", self.failed),
            ("rows written", self.written),
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
        backs_a_citation = select(Source.id).where(Source.url == IngestUrl.url).exists()
        stmt = stmt.where(backs_a_citation | (IngestUrl.status == PENDING_URL_STATUS))
    return list(session.scalars(stmt.order_by(IngestUrl.first_seen_at.asc(), IngestUrl.id.asc())))


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
) -> DateReport:
    """Date what can be dated. Report-only unless `apply`.

    `refetch` is the expensive half: without it only the free URL-path rung runs,
    which is worth having on its own and costs nothing to try first.

    **`limit` bounds the fetching, not the run.** The URL-path rung always sweeps
    the whole backlog because it costs nothing — capping it the way
    `backfill blocks` caps LLM calls throttled a free pass to 25 of 5,552 rows and
    reported "5 dated" where the true offline answer is 422.
    """
    import asyncio

    settings = settings or get_settings()
    report = DateReport()

    pending = undated_urls(session, everything=everything)
    report.undated = len(pending)

    # Rung 1: the URL path. Free, so it runs over everything before anything is
    # requested, and it shrinks the fetch list for rung 2.
    still_undated: list[str] = []
    for url in pending:
        when = date_from_url(url)
        if when is None:
            still_undated.append(url)
            continue
        report.from_url += 1
        if _store(session, url, when, apply=apply):
            report.written += 1
        if len(report.examples) < 10:
            report.examples.append((url, when, "url"))

    if apply:
        session.flush()

    if not refetch or not still_undated:
        report.remaining = len(still_undated)
        return report

    if limit:
        report.remaining = max(0, len(still_undated) - limit)
        still_undated = still_undated[:limit]

    # Rung 2: ask the publisher. `fetch_all` rather than a bare loop, for the
    # per-host gate, the politeness delay and the escalation ladder — this is
    # thousands of requests across a couple of thousand third-party hosts, and a
    # WAF that fingerprints TLS answers 403 to httpx and 200 to curl_cffi.
    results = asyncio.run(
        fetch_all(still_undated, fetcher=fetcher, escalate=escalate, settings=settings)
    )
    report.fetched = len(results)
    for result in results:
        if not result.ok:
            report.failed += 1
            continue
        if result.published_at is None:
            report.unanswered += 1
            continue
        report.from_page += 1
        if _store(session, result.url, result.published_at, apply=apply):
            report.written += 1
        if len(report.examples) < 10:
            report.examples.append((result.url, result.published_at, result.via))

    if apply:
        session.flush()
    return report


__all__ = ["DateReport", "run", "undated_urls"]
