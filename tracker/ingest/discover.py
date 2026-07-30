"""Find candidate articles from RSS/Atom feeds and sitemaps.

This is the piece that turns the tracker from "documents what you point it at"
into "finds things to document". `ingest crawl` needs URLs; nothing produced them
until now, so the database only ever held what an operator typed in by hand.

Design notes:

* **Feeds live in `seed/feeds.toml`**, so adding an outlet is an edit to a data
  file rather than a code change. Parsed with stdlib `tomllib`.
* **XML is parsed with stdlib `xml.etree`**, not `feedparser`. RSS 2.0 and Atom
  are simple enough that a dependency is not worth it, and the crawl path already
  carries the only heavy optional dep this project has.
* **Filtering is two-tier and both tiers must match.** A `topic` term proves the
  article is about data centers; a `signal` term proves it concerns a specific
  *project*. Commentary about AI power demand passes the first and fails the
  second, which is exactly right — there is nothing in it to extract, and an LLM
  call on it is wasted money.
* **Candidates are queued, not crawled.** They land in `ingest_url` with status
  `discovered` and their headline, so `tracker queue` can show what was found and
  an operator can drop the noise before paying for extraction.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings, install_root
from tracker.ingest.fetch import Fetcher, FetchResult
from tracker.models import IngestUrl, utcnow
from tracker.normalize import norm_text
from tracker.vocab import PENDING_URL_STATUS

log = logging.getLogger(__name__)

#: Namespaces that appear in the feeds we poll.
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

#: Cap on entries taken from any single feed per run, so one prolific outlet
#: cannot crowd out the others.
MAX_PER_FEED = 60


class DiscoverError(RuntimeError):
    """The feed configuration is missing or unusable."""


def normalize_haystack(text: str) -> str:
    """Prepare a title-plus-URL string for plain substring matching.

    Two normalizations, both load-bearing:

    * **Separators to spaces.** URL slugs are hyphenated, so ``data-center`` has
      to match the term ``data cent``. Matching the URL is the only way to filter
      a sitemap entry or a feed with empty titles, and without this it never
      matched anything.
    * **A space between a digit and a letter.** ``900MW`` becomes ``900 mw`` so
      the ``mw`` signal fires. A capacity figure in a headline is the single
      strongest indicator that an article is about a specific project, and it is
      almost always written closed-up.

    Terms stay plain substrings rather than regexes so `seed/feeds.toml` remains
    editable by someone who does not write regular expressions.
    """
    lowered = text.lower()
    lowered = re.sub(r"[-_/.:,;()\[\]]+", " ", lowered)
    lowered = re.sub(r"(\d)\s*([a-z])", r"\1 \2", lowered)
    collapsed = re.sub(r"\s+", " ", lowered).strip()
    # Padded with spaces so a term like " mw" cannot match inside a longer word.
    return f" {collapsed} "


@dataclass(frozen=True)
class FeedSpec:
    name: str
    url: str
    source_type: str = "general_media"
    #: True for an outlet that only ever covers data centers. The topic tier is
    #: then presumed satisfied and the signal tier alone decides.
    #:
    #: Declared rather than inferred on purpose. Without it, "datacenterfrontier"
    #: and "datacenterdynamics" satisfy the topic tier from their *domain name*,
    #: which is an accident — and the accident cuts the other way too, dropping a
    #: real headline like "Crusoe expands Abilene campus to 1.2GW" that never says
    #: "data center" because the whole publication is about them.
    topic_implied: bool = False


@dataclass(frozen=True)
class FilterSpec:
    topic: tuple[str, ...]
    signal: tuple[str, ...]
    exclude: tuple[str, ...] = ()

    def matches(self, text: str, *, topic_implied: bool = False) -> tuple[bool, str]:
        """Two-tier keyword test. Returns ``(keep, reason)``."""
        haystack = normalize_haystack(text)
        hit_exclude = next((t for t in self.exclude if t in haystack), None)
        if hit_exclude:
            return False, f"excluded by {hit_exclude!r}"

        topic = next((t for t in self.topic if t in haystack), None)
        if not topic and not topic_implied:
            return False, "no data-center topic term"

        found = repr(topic) if topic else "topic implied by the feed"
        signal = next((t for t in self.signal if t in haystack), None)
        if not signal:
            return False, f"topic {found} but no project signal"
        return True, f"{found} + {signal!r}"


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str
    feed: str
    published_at: dt.datetime | None = None
    source_type: str = "general_media"
    topic_implied: bool = False


@dataclass
class DiscoverReport:
    feeds_polled: int = 0
    feeds_failed: int = 0
    entries_seen: int = 0
    filtered: int = 0
    already_known: int = 0
    queued: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("feeds polled", self.feeds_polled),
            ("feeds failed", self.feeds_failed),
            ("entries seen", self.entries_seen),
            ("filtered out", self.filtered),
            ("already known", self.already_known),
            ("queued", self.queued),
        ]


# --- Configuration ----------------------------------------------------------


def default_feeds_path() -> Path:
    return install_root() / "seed" / "feeds.toml"


def load_config(path: Path | None = None) -> tuple[list[FeedSpec], FilterSpec]:
    path = path or default_feeds_path()
    if not path.is_file():
        raise DiscoverError(
            f"no feed configuration at {path}.\n"
            "Expected a TOML file with [[feed]] entries and a [filter] table; see "
            "seed/feeds.toml in the repository for the format."
        )
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise DiscoverError(f"{path.name} is not valid TOML: {exc}") from exc

    raw_feeds = data.get("feed") or []
    if not raw_feeds:
        raise DiscoverError(f"{path.name} defines no [[feed]] entries")
    feeds = [
        FeedSpec(
            name=str(entry.get("name") or entry.get("url", "?")),
            url=str(entry["url"]),
            source_type=str(entry.get("source_type") or "general_media"),
            topic_implied=bool(entry.get("topic_implied", False)),
        )
        for entry in raw_feeds
        if entry.get("url")
    ]

    raw_filter = data.get("filter") or {}
    topic = tuple(str(t).lower() for t in raw_filter.get("topic") or ())
    signal = tuple(str(t).lower() for t in raw_filter.get("signal") or ())
    if not topic or not signal:
        raise DiscoverError(
            f"{path.name} needs both `topic` and `signal` term lists under [filter]. "
            "Both tiers must match, so an empty list would discard everything."
        )
    return feeds, FilterSpec(
        topic=topic,
        signal=signal,
        exclude=tuple(str(t).lower() for t in raw_filter.get("exclude") or ()),
    )


# --- Parsing ----------------------------------------------------------------


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join((element.text or "").split())


def _parse_date(raw: str) -> dt.datetime | None:
    """Feed dates arrive in RFC 2822 (RSS) or ISO 8601 (Atom)."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    # Naive UTC, matching every other timestamp in the schema.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.UTC).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def parse_feed(xml: str, feed: FeedSpec, *, cap: int | None = None) -> list[Candidate]:
    """Extract entries from an RSS 2.0, Atom, or sitemap document.

    All three are handled in one function because they differ only in element
    names, and a feed silently switching format should not stop discovery.
    """
    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError as exc:
        raise DiscoverError(f"{feed.name}: not parseable XML ({exc})") from exc

    candidates: list[Candidate] = []

    # RSS 2.0 / RDF: <item><title/><link/><pubDate/>
    for item in root.iter("item"):
        link = _text(item.find("link"))
        if not link:
            continue
        candidates.append(
            Candidate(
                url=link,
                title=_text(item.find("title")),
                feed=feed.name,
                published_at=_parse_date(
                    _text(item.find("pubDate")) or _text(item.find("dc:date", _NS))
                ),
                source_type=feed.source_type,
                topic_implied=feed.topic_implied,
            )
        )

    # Atom: <entry><title/><link href=""/><published/>
    for entry in root.iter(f"{{{_NS['atom']}}}entry"):
        link = ""
        for link_el in entry.findall(f"{{{_NS['atom']}}}link"):
            rel = link_el.get("rel") or "alternate"
            if rel == "alternate" and link_el.get("href"):
                link = link_el.get("href", "")
                break
        if not link:
            continue
        candidates.append(
            Candidate(
                url=link,
                title=_text(entry.find(f"{{{_NS['atom']}}}title")),
                feed=feed.name,
                published_at=_parse_date(
                    _text(entry.find(f"{{{_NS['atom']}}}published"))
                    or _text(entry.find(f"{{{_NS['atom']}}}updated"))
                ),
                source_type=feed.source_type,
                topic_implied=feed.topic_implied,
            )
        )

    # Sitemap: <url><loc/><lastmod/>. No titles, so filtering falls back to the
    # URL slug -- workable because news slugs are usually the headline.
    for url_el in root.iter(f"{{{_NS['sitemap']}}}url"):
        loc = _text(url_el.find(f"{{{_NS['sitemap']}}}loc"))
        if not loc:
            continue
        candidates.append(
            Candidate(
                url=loc,
                title=_slug_to_title(loc),
                feed=feed.name,
                published_at=_parse_date(_text(url_el.find(f"{{{_NS['sitemap']}}}lastmod"))),
                source_type=feed.source_type,
                topic_implied=feed.topic_implied,
            )
        )

    return candidates[: cap or MAX_PER_FEED]


def _slug_to_title(url: str) -> str:
    """Recover a rough headline from a URL slug, for sitemaps with no titles."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
    return re.sub(r"[-_]+", " ", slug).strip()


# --- Sitemaps ---------------------------------------------------------------
#
# Sitemaps are the key-free answer to "find projects announced before today".
# A feed shows only what published recently; datacenterfrontier's article
# sitemaps hold 3,395 URLs going back to 2015. They are also published expressly
# for machines to read, so using them needs no API key and circumvents nothing.


@dataclass(frozen=True)
class SitemapSpec:
    name: str
    url: str
    source_type: str = "general_media"
    topic_implied: bool = False
    #: Child sitemaps to fetch when the URL is an index. Newest first.
    max_children: int = 4
    #: Ceiling on URLs examined per child, so one enormous sitemap cannot stall a run.
    max_urls: int = 5000

    def as_feed(self) -> FeedSpec:
        return FeedSpec(self.name, self.url, self.source_type, self.topic_implied)


def is_sitemap_index(xml: str) -> bool:
    return "<sitemapindex" in xml[:2000]


def index_children(xml: str) -> list[str]:
    """Child sitemap URLs from a <sitemapindex>, article sitemaps first.

    Most sites split by content type, and only the article files are useful --
    fetching Company.xml or Event.xml spends a request on pages that can never
    describe a project.
    """
    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError:
        return []
    locs = [
        _text(el.find(f"{{{_NS['sitemap']}}}loc"))
        for el in root.iter(f"{{{_NS['sitemap']}}}sitemap")
    ]
    locs = [u for u in locs if u]
    preferred = [u for u in locs if re.search(r"article|news|post|story", u, re.I)]
    return preferred or locs


async def crawl_sitemap(
    spec: SitemapSpec, fetcher: Fetcher, filter_spec: FilterSpec
) -> tuple[list[Candidate], list[str]]:
    """Walk one sitemap (following an index one level) and return matches.

    Filtering happens here rather than in the caller because a sitemap can yield
    thousands of URLs and only the matches are worth carrying further.
    """
    problems: list[str] = []
    root_result = await fetcher.fetch(spec.url)
    if not root_result.ok:
        return [], [f"{spec.name}: {root_result.error or 'fetch failed'}"]

    targets = [spec.url]
    if is_sitemap_index(root_result.markdown):
        children = index_children(root_result.markdown)
        if not children:
            return [], [f"{spec.name}: sitemap index listed no children"]
        targets = children[: spec.max_children]
        log.info("%s is an index; fetching %d child sitemap(s)", spec.name, len(targets))
    else:
        # Already a urlset; reuse the body we have.
        entries = parse_feed(root_result.markdown, spec.as_feed(), cap=spec.max_urls)
        return _match_sitemap(entries, filter_spec, spec), problems

    kept: list[Candidate] = []
    for child in targets:
        result = await fetcher.fetch(child)
        if not result.ok:
            problems.append(f"{spec.name}: child {child} {result.error or 'failed'}")
            continue
        try:
            entries = parse_feed(result.markdown, spec.as_feed(), cap=spec.max_urls)
        except DiscoverError as exc:
            problems.append(f"{spec.name}: {exc}")
            continue
        kept.extend(_match_sitemap(entries, filter_spec, spec))
    return kept, problems


def _match_sitemap(
    entries: list[Candidate], filter_spec: FilterSpec, spec: SitemapSpec
) -> list[Candidate]:
    out: list[Candidate] = []
    for candidate in entries:
        path = urlsplit(candidate.url).path
        keep, _ = filter_spec.matches(f"{candidate.title} {path}", topic_implied=spec.topic_implied)
        if keep:
            out.append(candidate)
    return out


def load_sitemaps(path: Path | None = None) -> list[SitemapSpec]:
    """[[sitemap]] entries from the feed config. Optional; absent means none."""
    path = path or default_feeds_path()
    if not path.is_file():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [
        SitemapSpec(
            name=str(entry.get("name") or entry["url"]),
            url=str(entry["url"]),
            source_type=str(entry.get("source_type") or "general_media"),
            topic_implied=bool(entry.get("topic_implied", False)),
            max_children=int(entry.get("max_children", 4)),
            max_urls=int(entry.get("max_urls", 5000)),
        )
        for entry in (data.get("sitemap") or [])
        if entry.get("url")
    ]


async def sweep_sitemaps(
    specs: list[SitemapSpec], fetcher: Fetcher, filter_spec: FilterSpec
) -> tuple[list[Candidate], list[str]]:
    """Walk every configured sitemap. One failing site never stops the others."""
    found: list[Candidate] = []
    problems: list[str] = []
    for spec in specs:
        try:
            kept, issues = await crawl_sitemap(spec, fetcher, filter_spec)
        except Exception as exc:
            problems.append(f"{spec.name}: {exc}")
            continue
        log.info("%s -> %d matching URL(s)", spec.name, len(kept))
        found.extend(kept)
        problems.extend(issues)
    return found, problems


# --- Filtering and queueing -------------------------------------------------


def select_candidates(
    candidates: list[Candidate],
    spec: FilterSpec,
    *,
    since: dt.datetime | None = None,
    report: DiscoverReport | None = None,
) -> list[Candidate]:
    """Apply the keyword tiers and the age cutoff."""
    kept: list[Candidate] = []
    for candidate in candidates:
        if report is not None:
            report.entries_seen += 1
        if since and candidate.published_at and candidate.published_at < since:
            if report is not None:
                report.filtered += 1
            continue
        # The URL PATH participates in matching: slugs carry the headline, and
        # some feeds ship empty or truncated titles. The host is deliberately
        # excluded -- "datacenterfrontier.com" says nothing about one article.
        path = urlsplit(candidate.url).path
        keep, reason = spec.matches(
            f"{candidate.title} {path}", topic_implied=candidate.topic_implied
        )
        if not keep:
            log.debug("skip %s (%s)", candidate.url, reason)
            if report is not None:
                report.filtered += 1
            continue
        log.debug("match %s (%s)", candidate.url, reason)
        kept.append(candidate)
    return kept


def queue_candidates(
    session: Session, candidates: list[Candidate], *, run_id: str, report: DiscoverReport
) -> list[Candidate]:
    """Insert unseen candidates as `discovered`. Returns the newly queued ones.

    A URL already in `ingest_url` is left completely alone — whether it was
    crawled successfully, failed, or is still pending. Re-queueing a processed URL
    would make discovery undo the crawl path's bookkeeping.
    """
    urls = [c.url for c in candidates]
    if not urls:
        return []
    known = {
        row.url for row in session.scalars(select(IngestUrl).where(IngestUrl.url.in_(urls))).all()
    }
    now = utcnow()
    queued: list[Candidate] = []
    for candidate in candidates:
        if candidate.url in known:
            report.already_known += 1
            continue
        session.add(
            IngestUrl(
                url=candidate.url,
                run_id=run_id,
                status=PENDING_URL_STATUS,
                title=norm_text(candidate.title, max_len=300),
                feed=candidate.feed,
                published_at=candidate.published_at,
                attempts=0,
                first_seen_at=now,
                last_tried_at=now,
            )
        )
        known.add(candidate.url)  # a feed can list the same URL twice
        queued.append(candidate)
        report.queued += 1
    session.flush()
    return queued


def pending(session: Session, limit: int | None = None) -> list[IngestUrl]:
    """Queued candidates, oldest published first so backlogs drain in order."""
    stmt = (
        select(IngestUrl)
        .where(IngestUrl.status == PENDING_URL_STATUS)
        .order_by(IngestUrl.published_at.asc().nullslast(), IngestUrl.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


#: Outcomes worth another attempt: the URL was never successfully read, and the
#: reason might be transient (a rate limit, a timeout) or fixable (a site that
#: needs a browser). `no_project` and `ok` are settled and are never retried.
RETRYABLE_STATUSES = ("fetch_error", "parse_error", "llm_error")


def failed(session: Session, limit: int | None = None) -> list[IngestUrl]:
    """URLs a previous run could not turn into a project.

    These are otherwise invisible: `pending()` only returns `discovered`, and
    discovery deliberately never re-queues a URL it has already seen. Without this
    they accumulate silently — a run can report "queue is empty, 0 failed" while a
    dozen articles sit unread.
    """
    stmt = (
        select(IngestUrl)
        .where(IngestUrl.status.in_(RETRYABLE_STATUSES))
        .order_by(IngestUrl.last_tried_at.asc(), IngestUrl.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def failure_summary(session: Session) -> list[tuple[str, int]]:
    """(host, count) for unread URLs, so the report can name the blocker."""
    from collections import Counter

    hosts = Counter(
        urlsplit(row.url).netloc.lower().removeprefix("www.") for row in failed(session)
    )
    return sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0]))


def drop_pending(session: Session, urls: list[str] | None = None) -> int:
    """Remove queued candidates the operator judged not worth crawling."""
    stmt = select(IngestUrl).where(IngestUrl.status == PENDING_URL_STATUS)
    if urls:
        stmt = stmt.where(IngestUrl.url.in_(urls))
    rows = list(session.scalars(stmt))
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


# --- Run --------------------------------------------------------------------


def run(
    session: Session,
    *,
    feeds_path: Path | None = None,
    fetcher: Fetcher | None = None,
    settings: Settings | None = None,
    since_days: int | None = 60,
    run_id: str | None = None,
    dry_run: bool = False,
) -> tuple[DiscoverReport, list[Candidate]]:
    """Poll every configured feed and queue the matching articles.

    A feed that fails is recorded and the run continues: one outlet changing its
    URL must not stop discovery from the other six.
    """
    import asyncio

    settings = settings or get_settings()
    feeds, spec = load_config(feeds_path)
    report = DiscoverReport()
    run_id = run_id or utcnow().strftime("discover-%Y%m%dT%H%M%S")
    since = utcnow() - dt.timedelta(days=since_days) if since_days else None

    from tracker.ingest.fetch import fetch_all

    results = asyncio.run(
        fetch_all(
            [f.url for f in feeds], fetcher=fetcher or _RawFetcher(settings), settings=settings
        )
    )
    by_url = {r.url: r for r in results}

    all_kept: list[Candidate] = []
    for feed in feeds:
        result = by_url.get(feed.url)
        report.feeds_polled += 1
        if result is None or not result.ok:
            report.feeds_failed += 1
            reason = (result.error if result else "no result") or "unknown error"
            report.failures.append((feed.name, reason))
            log.warning("feed %s failed: %s", feed.name, reason)
            continue
        try:
            entries = parse_feed(result.markdown, feed)
        except DiscoverError as exc:
            report.feeds_failed += 1
            report.failures.append((feed.name, str(exc)))
            log.warning("%s", exc)
            continue
        if not entries:
            report.failures.append((feed.name, "parsed but contained no entries"))
        all_kept.extend(select_candidates(entries, spec, since=since, report=report))

    queued = queue_candidates(session, all_kept, run_id=run_id, report=report)
    if dry_run:
        session.rollback()
        # The report still describes what would have happened.
        return report, all_kept
    session.commit()
    return report, queued


class _RawFetcher:
    """Fetches a feed as raw XML.

    Distinct from `HttpxFetcher`, which runs `html_to_text` on the body — that
    would strip the very tags a feed parser needs.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def fetch(self, url: str) -> FetchResult:
        import httpx

        headers = {
            "User-Agent": self.settings.user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.fetch_timeout_s, connect=10.0),
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(url)
        except httpx.RequestError as exc:
            return FetchResult(url, False, error=str(exc), fetched_at=utcnow(), via="feed")

        if response.status_code >= 400:
            return FetchResult(
                url,
                False,
                status=response.status_code,
                error=f"HTTP {response.status_code}",
                fetched_at=utcnow(),
                via="feed",
            )
        return FetchResult(
            url,
            bool(response.text.strip()),
            markdown=response.text,
            status=response.status_code,
            fetched_at=utcnow(),
            via="feed",
        )


__all__ = [
    "MAX_PER_FEED",
    "RETRYABLE_STATUSES",
    "Candidate",
    "DiscoverError",
    "DiscoverReport",
    "FeedSpec",
    "FilterSpec",
    "SitemapSpec",
    "crawl_sitemap",
    "default_feeds_path",
    "drop_pending",
    "failed",
    "failure_summary",
    "load_config",
    "load_sitemaps",
    "parse_feed",
    "pending",
    "queue_candidates",
    "run",
    "select_candidates",
    "sweep_sitemaps",
]
