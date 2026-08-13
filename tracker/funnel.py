"""What stage 1 costs and what it returns, per feed.

`discover` reports its own run — polled, filtered, queued — and then the number
disappears. Nothing has ever said what a feed produced *over time*: which ones
supply articles that become citations, and which spend LLM calls on articles that
turn out to describe no project at all.

That last number is the one worth surfacing. Across the live database **2,381 of
4,854 URLs that reached an LLM call produced no project — 49%**. The filter is two
tiers of keywords over a headline and a URL path, and it cannot tell an article
*about* a project from one that merely mentions the industry, so half the spend
goes on articles the extractor correctly finds nothing in. Sampled titles include
Meta newsroom posts about fact-checking and end-to-end encryption, which pass
because the feed is `topic_implied` and "expanding" is a signal term.

Reading it per feed is what makes it actionable: a `topic_implied` outlet that
covers a wider beat than the flag assumes shows up as a column of `no_project`.

Everything here is a read of `ingest_url`, which already records status, feed,
publication date and the run that found each URL. No new table, no counters to
keep in sync — the funnel is derived, so it cannot drift from what happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.models import IngestUrl, Source

#: A URL that reached the model. `discovered` has not been read yet and
#: `fetch_error` never got far enough to cost a call, so neither belongs in a
#: denominator about extraction.
REACHED_THE_MODEL: tuple[str, ...] = (
    "ok",
    "no_project",
    "thin_content",
    "parse_error",
    "llm_error",
)

#: Statuses that mean the URL is spent — read, and it will not be read again.
TERMINAL: tuple[str, ...] = ("ok", "no_project", "thin_content")


@dataclass
class FeedStat:
    """One feed's funnel: what it found, what was read, what stuck."""

    feed: str
    queued: int = 0
    #: Still waiting to be read.
    pending: int = 0
    #: Reached an LLM call.
    read: int = 0
    #: Produced at least one project.
    extracted: int = 0
    #: Reached the model and produced nothing.
    no_project: int = 0
    #: Refused before the call, as too thin to quote from.
    thin: int = 0
    #: Never fetched successfully.
    failed: int = 0
    #: Of the extracted URLs, how many back a stored citation today.
    cited: int = 0
    #: How many carry a publication date, which is what the merge tiebreak wants.
    dated: int = 0

    @property
    def waste(self) -> float:
        """Share of LLM calls that produced no project."""
        return self.no_project / self.read if self.read else 0.0

    @property
    def yield_rate(self) -> float:
        """Share of queued URLs that became a citation."""
        return self.cited / self.queued if self.queued else 0.0

    def as_json(self) -> dict[str, Any]:
        return {
            "feed": self.feed,
            "queued": self.queued,
            "pending": self.pending,
            "read": self.read,
            "extracted": self.extracted,
            "no_project": self.no_project,
            "thin": self.thin,
            "failed": self.failed,
            "cited": self.cited,
            "dated": self.dated,
            "waste": round(self.waste, 3),
            "yield_rate": round(self.yield_rate, 3),
        }


@dataclass
class Funnel:
    """The whole of stage 1, and the one number that pays for reading it."""

    feeds: list[FeedStat] = dc_field(default_factory=list)
    total_urls: int = 0
    reached_model: int = 0
    no_project: int = 0
    dated: int = 0

    @property
    def waste(self) -> float:
        return self.no_project / self.reached_model if self.reached_model else 0.0

    def ranked(self) -> list[FeedStat]:
        """Most wasteful first among feeds that have actually been read.

        Ordered by waste rather than volume, because the report exists to say
        which feed to reconsider. Ties break on `read` so a feed with two calls
        does not head the table, then on name for a total order.
        """
        return sorted(self.feeds, key=lambda f: (-f.waste, -f.read, f.feed))

    def as_json(self) -> dict[str, Any]:
        return {
            "total_urls": self.total_urls,
            "reached_model": self.reached_model,
            "no_project": self.no_project,
            "waste": round(self.waste, 3),
            "dated": self.dated,
            "feeds": [f.as_json() for f in self.ranked()],
        }


def fetch_failures(session: Session, *, limit: int = 8) -> list[tuple[str, int]]:
    """Why fetches fail, commonest first — the silent-timeout audit.

    The 2026-08-12 review warned about a specific failure mode: a default timeout
    swallowing most of a run and nothing saying so ("2,000 requests, 1,600 timed
    out, and it never told you"). Worth checking rather than assuming, because the
    remedy differs entirely — a timeout means raise `fetch_timeout_s`, a 403 means
    escalate the fetcher, and a 404 means the URL is gone.

    Measured on the live database it came back negative: of 1,081 failures, 625 are
    HTTP 403 and 198 are 429 — deliberate blocks, which is what the `curl_cffi`
    rung already exists for — plus ~23 TLS failures and a handful of 5xx. Timeouts
    are not the problem here. Kept as a standing check, since the answer is a
    property of the corpus and the corpus grows.

    Grouped on the error text rather than parsed into categories: the strings come
    from httpx and curl_cffi and inventing a taxonomy over them would be a second
    thing to keep in sync with two libraries.
    """
    rows = session.execute(
        select(IngestUrl.error, func.count())
        .where(IngestUrl.status == "fetch_error", IngestUrl.error.is_not(None))
        .group_by(IngestUrl.error)
        .order_by(func.count().desc())
    ).all()
    return [(str(error)[:70], count) for error, count in rows[:limit]]


def survey(session: Session) -> Funnel:
    """Build the funnel from `ingest_url`, one grouped query per column."""
    out = Funnel()

    rows = session.execute(
        select(
            func.coalesce(IngestUrl.feed, "(no feed)"),
            IngestUrl.status,
            func.count(),
            func.sum(func.iif(IngestUrl.published_at.is_not(None), 1, 0)),
        ).group_by(IngestUrl.feed, IngestUrl.status)
    ).all()

    stats: dict[str, FeedStat] = {}
    for feed, status, count, dated in rows:
        stat = stats.setdefault(feed, FeedStat(feed=feed))
        stat.queued += count
        stat.dated += dated or 0
        out.total_urls += count
        out.dated += dated or 0
        if status in REACHED_THE_MODEL:
            stat.read += count
            out.reached_model += count
        if status == "ok":
            stat.extracted += count
        elif status == "no_project":
            stat.no_project += count
            out.no_project += count
        elif status == "thin_content":
            stat.thin += count
        elif status == "fetch_error":
            stat.failed += count
        elif status not in TERMINAL:
            stat.pending += count

    # Which of them actually became a citation. Joined rather than assumed: a URL
    # can extract `ok` and still leave no source row behind, when the project it
    # described was later merged away or the claims were all refused.
    cited = session.execute(
        select(func.coalesce(IngestUrl.feed, "(no feed)"), func.count())
        .select_from(IngestUrl)
        .join(Source, Source.url == IngestUrl.url)
        .group_by(IngestUrl.feed)
    ).all()
    for feed, count in cited:
        stats.setdefault(feed, FeedStat(feed=feed)).cited = count

    out.feeds = list(stats.values())
    return out


__all__ = ["REACHED_THE_MODEL", "FeedStat", "Funnel", "fetch_failures", "survey"]
