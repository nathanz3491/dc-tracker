"""The morning's account of how the news came in: `tracker news report`.

The nightly report counted what the models did to the database. Nothing said whether
the night had *heard* anything: a feed that had died five months earlier counted as
polled, a host reset before discovery left no line at all, and the Kuna go-live sat on
Meta's own site for six weeks without a single number moving. This module answers, for
one night:

* **per source** — feed, archive, news watch, the research's harvesters — how many
  entries it showed, how many the filter kept, how many were read, how many could
  not be read, how many were taken out of the queue unread, and how many of the reads
  changed something (`ingest_url.status = 'ok'`);
* **how late** the news was: from publication to the database, against a day;
* **what is wrong with the intake itself**: no discovery tonight, a feed whose newest
  entry is weeks old, fresh news still waiting in the queue, search credit running low.

Read-only. Every number comes from `seen_url`, `ingest_url` and the night's ledger.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from tracker.models import IngestUrl, SeenUrl, Source, utcnow

#: A feed whose newest entry is older than this is reported as quiet.
QUIET_AFTER: Final = dt.timedelta(days=45)

#: Search credit below this many nights at tonight's rate is worth a line.
CREDIT_NIGHTS: Final = 14

#: What a read that reached the model can end as.
READ: Final = ("ok", "no_project", "thin_content")

#: What a try that read nothing ends as: the page would not load, or — far more
#: rarely — the model's reply was unusable. A failed re-read of a page read before
#: keeps its old status and carries the error instead (`crawl.record_url`).
FAILED: Final = ("fetch_error", "parse_error", "llm_error")

#: What a queued article taken out of the queue unread ends as: another copy of a
#: story already read or queued, a page on a publisher closed to us or refusing every
#: read, a headline with no article behind it, a page not in English. Each carries its
#: reason in `error`.
SET_ASIDE: Final = "skipped"


@dataclass
class SourceLine:
    source: str
    seen: int = 0
    kept: int = 0
    read: int = 0
    #: Tried tonight and not read — counted because the reads alone hid them: on
    #: 2026-10-19 the news watch showed 146 reads and nothing else, while 115 more of
    #: its articles would not load and 410 were set aside.
    failed: int = 0
    #: Taken out of the queue tonight without being read. See `SET_ASIDE`.
    aside: int = 0
    found: int = 0


@dataclass
class Desk:
    since: dt.datetime
    sources: list[SourceLine] = field(default_factory=list)
    #: Days from publication to the read, for news first seen and read tonight.
    delays: list[float] = field(default_factory=list)
    discovery_ran: bool = False
    quiet_feeds: list[tuple[str, dt.datetime | None]] = field(default_factory=list)
    held_fresh: int = 0
    searches: int = 0
    credit: int | None = None
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        if not self.discovery_ran:
            out.append("    NO DISCOVERY RAN: no feed, archive or watch result was seen tonight")
        out.append(
            f"    {'source':34} {'seen':>6} {'kept':>6} {'read':>6} {'failed':>6} "
            f"{'aside':>6} {'found':>6}"
        )
        for row in self.sources:
            out.append(
                f"    {row.source[:34]:34} {row.seen:6d} {row.kept:6d} {row.read:6d} "
                f"{row.failed:6d} {row.aside:6d} {row.found:6d}"
            )
        read = sum(row.read for row in self.sources)
        failed = sum(row.failed for row in self.sources)
        aside = sum(row.aside for row in self.sources)
        if failed or aside:
            out.append(
                f"    {read:,} read; {failed:,} failed — the page would not load, or rarely "
                f"the model's reply was unusable; {aside:,} set aside unread — a second "
                "copy of a story, a publisher closed to us or refusing every read, a "
                "headline with no article"
            )
        if self.delays:
            ordered = sorted(self.delays)
            p90 = ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]
            within = sum(1 for d in ordered if d <= 1.0)
            out.append(
                f"    publication to database: median {statistics.median(ordered):.1f} days, "
                f"p90 {p90:.1f}; {within} of {len(ordered)} within a day"
            )
        if self.held_fresh:
            out.append(
                f"    {self.held_fresh} article(s) published in the last 3 days are still "
                "waiting in the queue"
            )
        for name, newest in self.quiet_feeds:
            when = newest.date().isoformat() if newest else "never"
            out.append(f"    quiet feed: {name} (newest entry {when})")
        if self.searches:
            from tracker.spend import SEARCH_CNY

            out.append(
                f"    {self.searches} web search(es) tonight, about "
                f"¥{self.searches * SEARCH_CNY:.2f} of prepaid credit"
            )
        if self.credit is not None:
            out.append(f"    search credit left: {self.credit:,}")
        out.extend(f"    {w}" for w in self.warnings)
        return out


def _origin(feed: str | None) -> str:
    """A queue row's origin, grouped: every watch label is "news watch"."""
    if not feed:
        return "(unlabelled)"
    if feed.startswith("watch:"):
        return "news watch"
    if feed.startswith("search:"):
        return "search templates"
    return feed


def build(
    session: Session,
    *,
    since: dt.datetime,
    feeds_path: Path | None = None,
    ledger: Path | None = None,
    credit: int | None = None,
    search_cap: int | None = None,
    now: dt.datetime | None = None,
) -> Desk:
    """The desk for everything since `since` (naive UTC, the database's clock)."""
    from tracker import spend
    from tracker.ingest.discover import load_config

    now = now or utcnow()
    desk = Desk(since=since)
    rows: dict[str, SourceLine] = defaultdict(lambda: SourceLine(""))

    for source, verdict, first in session.execute(
        select(SeenUrl.source, SeenUrl.verdict, SeenUrl.first_seen_at).where(
            SeenUrl.last_seen_at >= since
        )
    ):
        desk.discovery_ran = True
        if first < since:
            continue
        row = rows[_origin(source)]
        row.seen += 1
        if verdict == "queued":
            row.kept += 1

    # A read is a try tonight that reached the reader: a failed re-read keeps its old
    # status and carries an error (a thin page's error is its verdict, so it counts).
    # "found" is a URL first cited tonight: re-reading an article that was already
    # cited, or skipping it as unchanged, finds nothing.
    cited_before = exists(
        select(Source.id).where(Source.url == IngestUrl.url, Source.fetched_at < since)
    )
    reads = session.execute(
        select(
            IngestUrl.feed,
            IngestUrl.status,
            IngestUrl.published_at,
            IngestUrl.first_seen_at,
            IngestUrl.last_tried_at,
            cited_before,
        )
        .where(IngestUrl.last_tried_at >= since)
        .where(IngestUrl.status.in_(READ))
        .where(or_(IngestUrl.error.is_(None), IngestUrl.status == "thin_content"))
    ).all()
    for feed, status, published, first, tried, old in reads:
        row = rows[_origin(feed)]
        row.read += 1
        if status == "ok" and not old:
            row.found += 1
        # How late the news reached the database: publication to the read, for what
        # the news intake (feeds, archives, the watch) first saw tonight. Research
        # reads of old articles say nothing about that.
        if (
            published is not None
            and first is not None
            and first >= since
            and feed
            and not feed.startswith(("enrich:", "search:", "prospect:"))
            and published <= tried
        ):
            desk.delays.append((tried - published).total_seconds() / 86400)

    # What was tried tonight and not read, and what left the queue unread. Counted
    # beside the reads because the reads alone made a source look whole: the news
    # watch read 146 articles on 2026-10-19 and the table said nothing of the 115
    # that would not load (66 refused with HTTP 403, 32 whose robots.txt did not
    # answer) or the 410 set aside before reading.
    for feed, status in session.execute(
        select(IngestUrl.feed, IngestUrl.status).where(
            IngestUrl.last_tried_at >= since,
            or_(
                IngestUrl.status.in_((*FAILED, SET_ASIDE)),
                IngestUrl.status.in_(("ok", "no_project")) & IngestUrl.error.is_not(None),
            ),
        )
    ):
        row = rows[_origin(feed)]
        if status == SET_ASIDE:
            row.aside += 1
        else:
            row.failed += 1

    # The news watch keeps no `seen_url` rows (its results are counted by `tracker
    # news` itself); what it queued tonight is its "kept".
    watched = len(
        session.execute(
            select(IngestUrl.id).where(
                IngestUrl.first_seen_at >= since, IngestUrl.feed.like("watch:%")
            )
        ).all()
    )
    if watched:
        desk.discovery_ran = True
        rows["news watch"].kept += watched

    for name, line in rows.items():
        line.source = name
    desk.sources = sorted(
        rows.values(), key=lambda r: (-r.found, -r.read, -r.kept, -r.failed, -r.aside, r.source)
    )

    desk.held_fresh = len(
        session.execute(
            select(IngestUrl.id).where(
                IngestUrl.status == "discovered",
                IngestUrl.published_at >= now - dt.timedelta(days=3),
                IngestUrl.published_at <= now + dt.timedelta(days=1),
            )
        ).all()
    )

    try:
        feeds, _ = load_config(feeds_path)
    except Exception:  # a broken file is discover's to report, not the desk's
        feeds = []
    newest: dict[str, dt.datetime | None] = {}
    for source, when in session.execute(
        select(SeenUrl.source, SeenUrl.published_at).where(SeenUrl.published_at.is_not(None))
    ):
        if when <= now + dt.timedelta(days=1) and (
            newest.get(source) is None or when > newest[source]
        ):
            newest[source] = when
    seen_sources = set(session.scalars(select(SeenUrl.source).distinct()))
    for feed in feeds:
        if feed.closed or feed.searched or feed.headlines_of or feed.name not in seen_sources:
            continue
        latest = newest.get(feed.name)
        if latest is None or now - latest > QUIET_AFTER:
            desk.quiet_feeds.append((feed.name, latest))

    calls = spend.read(ledger) if ledger else []
    desk.searches = spend.searches(calls)
    desk.credit = credit
    if credit is not None and credit <= 0:
        desk.warnings.append(
            "SEARCH CREDIT EXHAUSTED: Serper has none left, so no news search can run — top up"
        )
    elif credit is not None:
        rate = desk.searches or search_cap or 0
        if rate and credit < rate * CREDIT_NIGHTS:
            desk.warnings.append(
                f"SEARCH CREDIT LOW: about {credit // rate} night(s) left at "
                f"{rate:,} searches a night — top up Serper"
            )
    return desk


def serper_credit(settings: Any = None) -> int | None:
    """Serper's remaining credit, or None when it cannot be asked.

    `GET https://google.serper.dev/account` answers `{"balance": N, "rateLimit": N}`
    (checked 2026-10-18). It is not a search and spends nothing.
    """
    import httpx

    from tracker.config import get_settings

    settings = settings or get_settings()
    if not settings.has_serper_key():
        return None
    try:
        response = httpx.get(
            "https://google.serper.dev/account",
            headers={"X-API-KEY": settings.serper_api_key.get_secret_value()},
            timeout=httpx.Timeout(20.0, connect=10.0),
        )
        response.raise_for_status()
        balance = response.json().get("balance")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return int(balance) if isinstance(balance, int | float) else None


__all__ = ["CREDIT_NIGHTS", "QUIET_AFTER", "Desk", "SourceLine", "build", "serper_credit"]
