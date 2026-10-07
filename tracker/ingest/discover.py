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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypeVar
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings, seed_path
from tracker.ingest.fetch import (
    MIN_USEFUL_CHARS,
    Fetcher,
    FetchResult,
    cache_path,
    html_to_text,
    parse_timestamp,
)
from tracker.models import FeedProbe, IngestUrl, utcnow
from tracker.normalize import canonical_url, norm_text, url_identity, url_variants
from tracker.vocab import PENDING_URL_STATUS

if TYPE_CHECKING:
    import httpx

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
    #: Why this feed is not polled, when it is not. Set by `closed = "..."` in
    #: `seed/feeds.toml`, for a publisher that refuses every client this project is
    #: willing to be, so that a poll can only fail.
    #:
    #: Kept in the file rather than deleted, for two reasons. `tracker feeds`
    #: proposes any publisher whose citations decide stored values and that the
    #: file does not list (`probe.configured_hosts`), so a deleted
    #: datacenterfrontier would head its list of feeds to add for good. And a
    #: block can lift: the URL, the filter setting and the reasoning are still
    #: there when it does, and re-opening is deleting one line.
    closed: str | None = None
    #: The publisher whose headlines this feed lists, when it is a news search
    #: rather than the publisher's own feed — "datacenterdynamics.com" for Google
    #: News's results for that site. Its links are the search service's and lead
    #: back to a page we cannot read, so each headline is looked up elsewhere
    #: instead of queued. See :func:`follow_headlines`.
    headlines_of: str | None = None


#: The reason recorded for `closed = true`, which says that an entry is closed and
#: not why. The shipped file always says why; a test holds it to that.
UNEXPLAINED_CLOSURE = "closed in seed/feeds.toml, no reason given"


def _closed_reason(entry: dict[str, Any]) -> str | None:
    """Why a `[[feed]]` or `[[sitemap]]` entry is not polled, or None if it is.

    A string is the reason. `false` means open, like an absent key, so an entry can
    be re-opened by flipping it as well as by deleting the line.
    """
    value = entry.get("closed")
    if value is None or value is False:
        return None
    reason = "" if value is True else str(value).strip()
    return reason or UNEXPLAINED_CLOSURE


@dataclass(frozen=True)
class FilterSpec:
    topic: tuple[str, ...]
    signal: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    #: Obstacle vocabulary, and the second way to satisfy the signal tier.
    #:
    #: Every `signal` term is announcement-shaped -- announce, expand, invest,
    #: build, campus, megawatt. That is right for finding a project but it silently
    #: discarded every article about one going wrong: measured against the real
    #: filter, "Loudoun supervisors reject data center rezoning application" and
    #: "Georgia Power says transmission upgrades delay data center energization"
    #: were both dropped for having "no project signal". So the corpus the extractor
    #: ever saw was announcements only, and no schema change can recover a risk from
    #: an article that was never queued.
    #:
    #: A risk term satisfies the signal tier ALONE, but the `topic` tier still has
    #: to match, so a transformer-shortage story about a steel mill is still
    #: dropped. This is deliberately not about raising `blocker` coverage -- see
    #: tracker/gaps.py, where absence is usually the truth. It is about not throwing
    #: away the articles where an obstacle genuinely IS reported.
    risk_signal: tuple[str, ...] = ()

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
        if signal:
            return True, f"{found} + {signal!r}"
        risk = next((t for t in self.risk_signal if t in haystack), None)
        if risk:
            return True, f"{found} + risk {risk!r}"
        return False, f"topic {found} but no project or risk signal"

    def risk_term(self, text: str) -> str | None:
        """The obstacle term this text carries, if any. Ignores the other tiers.

        Used to prioritise the queue: an article about a tracked project going
        wrong is the most valuable LLM call available, because no press release
        names its own blocker.
        """
        haystack = normalize_haystack(text)
        return next((t for t in self.risk_signal if t in haystack), None)


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str
    feed: str
    published_at: dt.datetime | None = None
    source_type: str = "general_media"
    topic_implied: bool = False
    #: The article body, when the feed syndicates it in full (RSS
    #: `content:encoded`, Atom `<content>`). Empty for the common case of a
    #: summary-only feed and always empty for a sitemap.
    #:
    #: This is the difference between reading a source and only seeing its
    #: headlines. See :func:`cache_feed_text`.
    content: str = ""


@dataclass
class DiscoverReport:
    feeds_polled: int = 0
    feeds_failed: int = 0
    entries_seen: int = 0
    filtered: int = 0
    already_known: int = 0
    queued: int = 0
    #: Bodies taken straight from the feeds, so the crawl path never has to
    #: request the article page. See :func:`cache_feed_text`.
    bodies_cached: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    #: `(name, reason)` for each feed not polled because it is marked closed.
    #: Counted apart from `feeds_failed`, so that line keeps meaning "something
    #: broke since the file was last edited" — thirteen expected failures a night
    #: would teach a reader to stop looking at it.
    closed: list[tuple[str, str]] = field(default_factory=list)
    #: Closed entries polled anyway because their latest weekly check answered.
    reopened: list[str] = field(default_factory=list)
    #: The weekly checks made this run. See :func:`probe_closed`.
    probes: list[ProbeResult] = field(default_factory=list)
    #: Headlines-only feeds: headlines not seen before, how many were looked up,
    #: and how many readable copies that put in the queue (counted in `queued`
    #: too). See :func:`follow_headlines`.
    headlines_new: int = 0
    headlines_looked_up: int = 0
    leads_queued: int = 0

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("feeds polled", self.feeds_polled),
            ("feeds closed", len(self.closed)),
            ("feeds reopened", len(self.reopened)),
            ("feeds failed", self.feeds_failed),
            ("entries seen", self.entries_seen),
            ("filtered out", self.filtered),
            ("already known", self.already_known),
            ("queued", self.queued),
            ("bodies from feed", self.bodies_cached),
            ("headlines, new", self.headlines_new),
            ("headlines looked up", self.headlines_looked_up),
            ("readable copies queued", self.leads_queued),
        ]


# --- Configuration ----------------------------------------------------------


def default_feeds_path() -> Path:
    return seed_path("feeds.toml")


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
            closed=_closed_reason(entry),
            headlines_of=str(entry["headlines_of"]) if entry.get("headlines_of") else None,
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
        # Optional, unlike topic/signal above: an operator's existing feeds.toml
        # predates this tier and must keep working. Absent means the filter behaves
        # exactly as it did before, which is a narrower filter, never a broader one.
        risk_signal=tuple(str(t).lower() for t in raw_filter.get("risk_signal") or ()),
    )


# --- Parsing ----------------------------------------------------------------


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join((element.text or "").split())


def _parse_date(raw: str) -> dt.datetime | None:
    """Feed dates arrive in RFC 2822 (RSS) or ISO 8601 (Atom).

    Delegates to `fetch.parse_timestamp`, which handles the same formats plus the
    ones page metadata uses. One definition, because a feed date and a date read
    out of the article's own HTML land in the *same* column and get sorted against
    each other — two conventions there would be a silently unorderable tiebreak.
    """
    return parse_timestamp(raw)


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
                # `content:encoded` carries the whole article; `description` is a
                # teaser. Only the former is worth treating as the body, so no
                # fallback to `description` here — a 350-character summary read as
                # if it were the article would let the evidence gate verify quotes
                # against a fragment and call the rest unsupported.
                content=_text(item.find("content:encoded", _NS)),
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
                content=_text(entry.find(f"{{{_NS['atom']}}}content")),
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
    #: The operator whose own newsroom this is, when it is one.
    #:
    #: Everything on stackinfra.com is published by STACK, so the domain already
    #: proves the company and `matches_known_project` need not find it in the slug
    #: as well. Measured across eight newsrooms, dropping that redundant
    #: requirement took the yield from 15 articles over 8 projects to 28 over 13 —
    #: "new-hillsboro-campus-announced" matches once the operator is implied.
    company: str | None = None
    #: Why this archive is not walked, when it is not. See `FeedSpec.closed`.
    closed: str | None = None

    def as_feed(self) -> FeedSpec:
        return FeedSpec(
            self.name, self.url, self.source_type, self.topic_implied, closed=self.closed
        )


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
            company=(str(entry["company"]) if entry.get("company") else None),
            closed=_closed_reason(entry),
        )
        for entry in (data.get("sitemap") or [])
        if entry.get("url")
    ]


async def sweep_sitemaps(
    specs: list[SitemapSpec], fetcher: Fetcher, filter_spec: FilterSpec
) -> tuple[list[Candidate], list[str]]:
    """Walk every configured sitemap. One failing site never stops the others.

    A sitemap marked closed is skipped without a request, and is not a problem:
    the file already says why it cannot be read.
    """
    found: list[Candidate] = []
    problems: list[str] = []
    for spec in specs:
        if spec.closed:
            log.info("not walking %s, marked closed: %s", spec.name, spec.closed)
            continue
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

    **Known means known under any spelling.** A candidate is stored in its
    `canonical_url` form — a search hit carries a click-tracking `srsltid` on every
    result — and counts as known when a stored row shares its `url_identity`: the
    same page with or without `www.`, a trailing slash or `https`. On a copy of
    production 69 queued URLs had another spelling already in the table, each a
    second fetch and most a second model call on the same text. The candidates
    handed back carry the stored spelling, so a caller caching their bodies files
    them under the URL the crawl will ask for.
    """
    from dataclasses import replace

    if not candidates:
        return []
    candidates = [replace(c, url=canonical_url(c.url)) for c in candidates]
    spellings = list(dict.fromkeys(v for c in candidates for v in url_variants(c.url)))
    known: set[str] = set()
    for start in range(0, len(spellings), 500):  # SQLite caps bound parameters
        known.update(
            url_identity(url)
            for url in session.scalars(
                select(IngestUrl.url).where(IngestUrl.url.in_(spellings[start : start + 500]))
            )
        )
    now = utcnow()
    queued: list[Candidate] = []
    for candidate in candidates:
        identity = url_identity(candidate.url)
        if identity in known:
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
        known.add(identity)  # a feed can list the same URL twice, or two spellings of it
        queued.append(candidate)
        report.queued += 1
    session.flush()
    return queued


def cache_feed_text(candidates: list[Candidate], cache_dir: Path | None) -> int:
    """Save syndicated article bodies into the article cache. Returns how many.

    **This is what makes a whole class of source readable at all.** Several
    outlets serve their feed freely and then answer 403 to any non-browser
    request for the article itself — measured, every article from the state
    nonprofit newsrooms failed to fetch, so the queue filled with headlines whose
    bodies we could never read.

    Those same feeds carry the complete article in `content:encoded`, 4,000 to
    12,000 characters of it. The body was already in a file we had downloaded; we
    were re-requesting it through a door that was shut. Writing it here means the
    crawl path serves it from disk via `crawl._split_cached` and never issues the
    request that would have been refused.

    Deliberately not a bypass, and worth being precise about the difference: no
    access control is circumvented, no fingerprint is spoofed, and nothing is
    fetched that the publisher did not hand over. It is the syndication feed used
    for the purpose a syndication feed exists for.

    Two guards:

    * **Short bodies are skipped.** A summary-only feed that puts 300 characters in
      `content:encoded` would otherwise cache a teaser as though it were the
      article, and the evidence gate would then verify a couple of quotes and
      declare every other value unsupported. `MIN_USEFUL_CHARS` is the same floor
      the fetcher uses to decide it got a JS shell rather than a page.
    * **An existing cache entry is never overwritten.** A real fetch is the more
      complete artefact — feeds truncate, drop tables and omit figure captions —
      so syndicated text fills a gap rather than replacing what we already have.

    The text goes through `html_to_text`, the same reduction the fetch path
    applies, because the evidence gate matches quotes against exactly this string.
    Producing it any other way would let a quote fail verification purely for
    having been whitespaced differently.
    """
    if not cache_dir:
        return 0
    written = 0
    for candidate in candidates:
        if not candidate.content:
            continue
        path = cache_path(candidate.url, cache_dir)
        if path.exists():
            continue
        # The headline leads, as it would in the fetched page: the extractor is
        # told the title is the strongest hint about which project an article is
        # about, and `content:encoded` does not repeat it.
        body = html_to_text(f"<h1>{candidate.title}</h1>\n{candidate.content}")
        if len(body) < MIN_USEFUL_CHARS:
            continue
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        written += 1
    if written:
        log.info("cached %d article body/bodies straight from the feeds", written)
    return written


# --- Prioritising the queue toward depth ------------------------------------
#
# The queue drains oldest-first, which grows the database *sideways*: every run
# creates more single-source projects. But a queued article that covers a project
# already tracked is worth far more, because it becomes a SECOND source -- filling
# fields one article could not, and lifting confidence from 2 to 3.
#
# Measured on a real archive: 259 of the queued articles covered 18 of 29 existing
# projects. Crawling those first is the difference between 29 shallow rows and 18
# corroborated ones.


#: Words that clear a length bar but appear in nearly every data-center headline,
#: so they are no evidence that an article concerns one particular project.
_GENERIC_NAME_TOKENS = frozenset(
    {
        "campus",
        "center",
        "centre",
        "datacenter",
        "facility",
        "project",
        "expansion",
        "building",
        "phase",
        "north",
        "south",
        "east",
        "west",
    }
)


@dataclass(frozen=True)
class ProjectIdentity:
    """What a queued URL has to mention to be about this project."""

    project_id: int
    company: str
    locality: str
    name_tokens: tuple[str, ...]


def _company_words(company: str | None) -> frozenset[str]:
    """Every word that belongs to the operator's name, raw and normalized.

    Both forms are needed: the raw name carries suffixes `company_key` strips, and
    the key carries alias substitutions the raw name does not (Facebook -> meta).
    """
    from tracker.dedup import company_key

    raw = re.split(r"[^a-z0-9]+", (company or "").lower())
    return frozenset(w for w in [*raw, *company_key(company).split()] if w)


def project_identities(session: Session) -> list[ProjectIdentity]:
    from sqlalchemy import select

    from tracker.dedup import city_key, company_key, county_key
    from tracker.models import Project

    out: list[ProjectIdentity] = []
    for row in session.scalars(select(Project)):
        out.append(
            ProjectIdentity(
                project_id=row.id,
                company=company_key(row.company),
                locality=city_key(row.city) or county_key(row.county),
                # Only distinctive tokens count as evidence of a *specific*
                # project. Length alone is not enough: "campus" and "center"
                # clear any length bar and appear in nearly every data-center
                # headline, so "Sabey Ashburn Campus" would match every Sabey
                # article mentioning a campus — including genuinely different
                # sites in other cities.
                #
                # Tokens already in the company name are excluded too. Project
                # names usually repeat the operator ("Sabey Ashburn Campus"), and
                # such a token adds no discriminating power: the company check has
                # already passed, so re-matching it would accept any article by
                # that operator anywhere.
                #
                # Excluded against the RAW company name, not only the normalized
                # key. `company_key` strips corporate suffixes, so "STACK
                # Infrastructure" keys to "stack" and left "infrastructure" looking
                # distinctive in "STACK Infrastructure Hillsboro Campus" — while
                # every STACK article's slug contains "stack-infrastructure". That
                # made a company-wide piece ("raises $400 million", "expansion into
                # Asia-Pacific") match one Hillsboro project. Any operator whose
                # name ends in a stripped word — Infrastructure, Data Centers,
                # Energy, Systems, Realty — leaked the same way.
                name_tokens=tuple(
                    t
                    for t in re.split(r"[^a-z0-9]+", (row.name or "").lower())
                    if len(t) > 4
                    and t not in _GENERIC_NAME_TOKENS
                    and t not in _company_words(row.company)
                ),
            )
        )
    return out


def newsroom_companies(path: Path | None = None) -> dict[str, str]:
    """host -> company key, for the operator newsrooms in the sitemap config.

    Lets `matches_known_project` know that everything on this host is published by
    that operator, so the company need not also appear in the slug.
    """
    from tracker.dedup import company_key

    out: dict[str, str] = {}
    for spec in load_sitemaps(path):
        if not spec.company:
            continue
        host = urlsplit(spec.url).netloc.lower().removeprefix("www.")
        out[host] = company_key(spec.company)
    return out


def matches_known_project(
    url: str,
    title: str | None,
    identities: list[ProjectIdentity],
    *,
    implied_companies: dict[str, str] | None = None,
) -> int | None:
    """The id of the project this URL appears to cover, if any.

    Requires the **full** company key plus either the locality or a distinctive
    name token. Matching on a single company token is far too loose: "digital" and
    "ashburn" together hit every Ashburn article by any operator, which in testing
    inflated one project's apparent coverage from a handful to 154.

    `implied_companies` maps a host to the operator that publishes it. On an
    operator's own newsroom the domain already establishes the company, so
    requiring it in the slug too only loses matches — a STACK release titled
    "New Hillsboro campus announced" names the city and not the company. The
    locality-or-name-token requirement still stands, so precision is unchanged.
    """
    haystack = normalize_haystack(f"{title or ''} {urlsplit(url).path}")
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    implied = (implied_companies or {}).get(host)

    for identity in identities:
        if not identity.company:
            continue
        # Either the slug names the operator, or the domain does.
        if identity.company not in haystack and identity.company != implied:
            continue
        if identity.locality and identity.locality in haystack:
            return identity.project_id
        if any(token in haystack for token in identity.name_tokens):
            return identity.project_id
    return None


# --- Pages nothing can read -------------------------------------------------
#
# A publisher marked `closed` in `seed/feeds.toml` refuses every client this
# project runs, and its articles answer the same way its feed does. So a queued or
# cited page on one of those domains can be read only from a body cached before
# the block began — on 2026-10-02 that was 22 of the 650 queued there.
#
# Every crawl that cuts a list to a limit puts the rest last: the queue crawls,
# `sync`'s extract, retry and refresh, and each round of enrich. Nothing is
# dropped. The queue still holds them, `tracker queue` still lists them, and they
# are tried when nothing readable is left, which is also how a block that lifts
# gets noticed. What stops is a page that will answer a challenge taking a slot
# from one that would not: measured that day, six of the nightly crawl's next ten
# slots, and all fifteen of a `tracker sync` extract, which reads `priority`
# publishers first — and datacenterfrontier and datacenterdynamics are two.

T = TypeVar("T")


def closed_domains(
    path: Path | None = None, *, reopened: frozenset[str] = frozenset()
) -> frozenset[str]:
    """Every publisher with a feed or archive marked closed, by registrable domain.

    `reopened` names entries a weekly check found open (:func:`reopened_names`);
    those count as open. A headlines-only feed is never a closed publisher: its own
    host is the search service.

    Empty when the config cannot be read, so a broken file leaves the crawl in its
    old order rather than stopping it; `tracker discover` is where that is reported.
    """
    from tracker.confidence import registrable_domain

    try:
        feeds, _ = load_config(path)
        sitemaps = load_sitemaps(path)
    except (DiscoverError, tomllib.TOMLDecodeError):
        return frozenset()
    feeds = without_reopened(feeds, reopened)
    sitemaps = without_reopened(sitemaps, reopened)
    urls = [f.url for f in feeds if f.closed] + [s.url for s in sitemaps if s.closed]
    return frozenset(domain for domain in map(registrable_domain, urls) if domain)


def unreadable_test(
    cache_dir: Path | None,
    *,
    path: Path | None = None,
    reopened: frozenset[str] = frozenset(),
) -> Callable[[str], bool]:
    """A test for pages no fetch will read: on a closed publisher, nothing cached.

    `cache_dir` is the cache the crawl will serve from. None means it will not
    serve from one — `--no-cache`, and the refresh phase, which re-reads on purpose
    — and then every page on a closed publisher is unreadable.
    """
    from tracker.confidence import registrable_domain

    closed = closed_domains(path, reopened=reopened)

    def unreadable(url: str) -> bool:
        if not closed or registrable_domain(url) not in closed:
            return False
        return cache_dir is None or not cache_path(url, cache_dir).is_file()

    return unreadable


def readable_first(
    items: list[T],
    unreadable: Callable[[str], bool] | None,
    *,
    url: Callable[[T], str] | None = None,
) -> list[T]:
    """`items` in their order, except the ones `unreadable` flags, which go last.

    Stable on both sides, so the ordering the caller chose — depth first, newest
    first, priority publishers first — survives among the readable pages and among
    the rest. `url` reads an item's URL; without it the items are URLs.
    """
    if unreadable is None:
        return items
    get: Callable[[Any], str] = url or (lambda item: item)
    readable: list[T] = []
    last: list[T] = []
    for item in items:
        (last if unreadable(get(item)) else readable).append(item)
    return readable + last


def pending(
    session: Session,
    limit: int | None = None,
    *,
    known_first: bool = False,
    new_first: bool = False,
    spec: FilterSpec | None = None,
    unreadable: Callable[[str], bool] | None = None,
) -> list[IngestUrl]:
    """Queued candidates.

    Ordered oldest-published-first so a backlog drains predictably. With
    ``known_first`` the ones covering an already-tracked project come first, which
    spends each LLM call on depth rather than on another single-source row.

    ``new_first`` is the nightly loop's order, and it starts with the **news**:
    every article published within the email's window (`feed.REPORT_WINDOW_DAYS`),
    newest first, whatever campus it names. Then the articles that name **no**
    tracked campus, newest published first, then the rest in the usual order —
    those are where a campus the database has never heard of can turn up. Each is
    read by the normal crawl, whose identity check decides whether a record is a new
    campus or an existing one under another name before anything is inserted.

    The news goes first because what the crawl learns tonight is mailed tomorrow.
    An update about a *tracked* campus used to sort behind every untracked article
    in a backlog of 1,600: a 2026-09-21 report of a lawsuit against Project
    Camellia was queued the next day, read on 10-03, and mailed on 10-04 as news.

    Passing ``spec`` splits that first group again, putting the articles that also
    carry an obstacle term ahead of the rest. Those are the highest-value calls in
    the queue: a project's own press release never names its blocker, so an
    adversarial second source is the only way that fact is ever recorded.

    ``unreadable`` (:func:`unreadable_test`) flags the pages no fetch will read,
    which then go after every other row, whichever order was asked for, before the
    limit is cut.
    """
    stmt = (
        select(IngestUrl)
        .where(IngestUrl.status == PENDING_URL_STATUS)
        .order_by(IngestUrl.published_at.asc().nullslast(), IngestUrl.id.asc())
    )
    if new_first:
        from tracker.feed import REPORT_WINDOW_DAYS

        rows = list(session.scalars(stmt))
        cutoff = utcnow() - dt.timedelta(days=REPORT_WINDOW_DAYS)
        news = [row for row in rows if row.published_at is not None and row.published_at >= cutoff]
        news.sort(key=lambda row: (row.published_at, row.id), reverse=True)
        taken = {row.id for row in news}
        backlog = [row for row in rows if row.id not in taken]
        identities = project_identities(session)
        implied = newsroom_companies()
        fresh = [
            row
            for row in backlog
            if not matches_known_project(row.url, row.title, identities, implied_companies=implied)
        ]
        # Newest first; an undated article last, since nothing says it is recent.
        dated = [row for row in fresh if row.published_at is not None]
        undated = [row for row in fresh if row.published_at is None]
        dated.sort(key=lambda row: (row.published_at, row.id), reverse=True)
        taken |= {row.id for row in fresh}
        ordered = news + dated + undated + [row for row in rows if row.id not in taken]
    elif known_first:
        # Scored in Python: the match needs slug normalization that SQL cannot do,
        # and the queue is thousands of rows, not millions.
        rows = list(session.scalars(stmt))
        identities = project_identities(session)
        implied = newsroom_companies()
        risky, enriching, fresh = [], [], []
        for row in rows:
            if not matches_known_project(row.url, row.title, identities, implied_companies=implied):
                fresh.append(row)
            elif spec is not None and spec.risk_term(f"{row.title or ''} {urlsplit(row.url).path}"):
                risky.append(row)
            else:
                enriching.append(row)
        if risky or enriching:
            log.info(
                "%d queued candidate(s) cover a tracked project (%d of them report an "
                "obstacle); crawling those first",
                len(risky) + len(enriching),
                len(risky),
            )
        ordered = risky + enriching + fresh
    elif limit and unreadable is None:
        return list(session.scalars(stmt.limit(limit)))
    else:
        ordered = list(session.scalars(stmt))
    ordered = readable_first(ordered, unreadable, url=lambda row: row.url)
    return ordered[:limit] if limit else ordered


def pending_split(session: Session) -> tuple[int, int]:
    """(deepens an existing project, would create a new one) over the whole queue."""
    identities = project_identities(session)
    rows = list(session.scalars(select(IngestUrl).where(IngestUrl.status == PENDING_URL_STATUS)))
    implied = newsroom_companies()
    deep = sum(
        1
        for r in rows
        if matches_known_project(r.url, r.title, identities, implied_companies=implied)
    )
    return deep, len(rows) - deep


def pending_risk_count(session: Session, spec: FilterSpec) -> int:
    """How many queued candidates for a tracked project report an obstacle.

    Reported separately from `pending_split` so the run summary can say what the
    ordering actually did, rather than claiming depth-first and leaving the
    operator to guess which articles that put first.
    """
    if not spec.risk_signal:
        return 0
    identities = project_identities(session)
    implied = newsroom_companies()
    rows = list(session.scalars(select(IngestUrl).where(IngestUrl.status == PENDING_URL_STATUS)))
    return sum(
        1
        for r in rows
        if matches_known_project(r.url, r.title, identities, implied_companies=implied)
        and spec.risk_term(f"{r.title or ''} {urlsplit(r.url).path}")
    )


#: Outcomes worth another attempt: the URL was never successfully read, and the
#: reason might be transient (a rate limit, a timeout) or fixable (a site that
#: needs a browser, or one that served a teaser card where the article should
#: have been). `no_project` and `ok` are settled and are never retried —
#: `thin_content` is not settled, because a model never read the page.
RETRYABLE_STATUSES = ("fetch_error", "parse_error", "llm_error", "thin_content")

#: Failed tries in a row, the same way each time, after which a URL is no longer
#: retried automatically (`ingest_url.failures`, migration 0027).
#:
#: A transient failure — a rate limit, an outage, a flaky handshake — rarely repeats
#: identically on three separate runs; a structural one repeats every time: a page
#: that 404s, a host whose TLS our client cannot complete, an article whose
#: extraction always runs out of room. Measured on a copy of production, the second
#: kind was being paid for without end: 9 URLs failing "reply truncated at the token
#: limit" had been tried 66 times, at up to ~98,000 output tokens a try, and 14
#: failing with one SSL "EOF" error 190 times, still retried on 2026-09-22. Three is
#: the smallest streak that tells the two apart while giving a transient failure two
#: more chances. It stops only the *automatic* retries — `sync --retry-failed`,
#: enrich's retry harvester — and `tracker ingest crawl --url` still reads one.
MAX_SAME_FAILURES = 3


def _worth_retrying():
    """The SQL test for a failed URL an automatic retry may still spend a try on."""
    return and_(IngestUrl.status.in_(RETRYABLE_STATUSES), IngestUrl.failures < MAX_SAME_FAILURES)


def failed(session: Session, limit: int | None = None) -> list[IngestUrl]:
    """URLs a previous run could not turn into a project.

    These are otherwise invisible: `pending()` only returns `discovered`, and
    discovery deliberately never re-queues a URL it has already seen. Without this
    they accumulate silently — a run can report "queue is empty, 0 failed" while a
    dozen articles sit unread.

    Every one, including those no longer retried automatically: giving up on
    retrying a URL is not the same as having read it. See `retryable`.
    """
    stmt = (
        select(IngestUrl)
        .where(IngestUrl.status.in_(RETRYABLE_STATUSES))
        .order_by(IngestUrl.last_tried_at.asc(), IngestUrl.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def retryable(
    session: Session,
    limit: int | None = None,
    *,
    unreadable: Callable[[str], bool] | None = None,
) -> list[IngestUrl]:
    """The failed URLs an automatic retry should still spend a try on.

    `failed` less the ones that have failed the same way :data:`MAX_SAME_FAILURES`
    times running. Longest-untried first, like `failed`, except that the pages
    `unreadable` flags go last — see :func:`pending`.
    """
    stmt = (
        select(IngestUrl)
        .where(_worth_retrying())
        .order_by(IngestUrl.last_tried_at.asc(), IngestUrl.id.asc())
    )
    if limit and unreadable is None:
        stmt = stmt.limit(limit)
    rows = readable_first(list(session.scalars(stmt)), unreadable, url=lambda row: row.url)
    return rows[:limit] if limit else rows


def given_up(session: Session) -> list[IngestUrl]:
    """Failed URLs no longer retried automatically, for a run summary to name."""
    return list(
        session.scalars(
            select(IngestUrl)
            .where(
                IngestUrl.status.in_(RETRYABLE_STATUSES),
                IngestUrl.failures >= MAX_SAME_FAILURES,
            )
            .order_by(IngestUrl.last_tried_at.asc(), IngestUrl.id.asc())
        )
    )


def failure_summary(session: Session) -> list[tuple[str, int]]:
    """(host, count) for unread URLs, so the report can name the blocker."""
    from collections import Counter

    hosts = Counter(
        urlsplit(row.url).netloc.lower().removeprefix("www.") for row in failed(session)
    )
    return sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0]))


def drop_pending(
    session: Session,
    urls: list[str] | None = None,
    *,
    ids: list[int] | None = None,
    feeds: list[str] | None = None,
) -> int:
    """Remove queued candidates the operator judged not worth crawling.

    By URL, by row id, or by feed. The id was added because the URL was
    unusable as a handle: `tracker queue` printed `row.url[:60]`, so the string
    on screen was a *prefix* of the real URL. Pasting it into `--drop --url`
    matched nothing, and pasting it into a browser produced a 404 — which is what
    a queue full of dead links looked like from the outside.

    **A feed ending in a colon matches every label beneath it.** Web-search rows
    carry `search:<template>:<place>`, one label per place, so a template that
    turns out to be worthless has no single feed name to drop — and the rolled-up
    name the funnel reports (`search:rezoning`) is a group that no row holds, so
    an exact match would report dropping nothing and read as a bug. Writing
    `search:rezoning:` clears the whole template. The trailing colon is what makes
    this safe to add: no existing feed name ends in one, so nothing that used to
    match exactly can silently start matching more.
    """
    stmt = select(IngestUrl).where(IngestUrl.status == PENDING_URL_STATUS)
    if urls:
        stmt = stmt.where(IngestUrl.url.in_(urls))
    if ids:
        stmt = stmt.where(IngestUrl.id.in_(ids))
    if feeds:
        exact = [f for f in feeds if not f.endswith(":")]
        prefixes = [f for f in feeds if f.endswith(":")]
        clauses = [IngestUrl.feed.in_(exact)] if exact else []
        clauses += [IngestUrl.feed.startswith(p) for p in prefixes]
        stmt = stmt.where(or_(*clauses)) if clauses else stmt
    rows = list(session.scalars(stmt))
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


# --- Keeping the queue honest -------------------------------------------------
#
# A queue is a promise: everything in it is worth an LLM call. Two things break
# that promise quietly, and both had, measured on the live database of 1,241
# queued candidates.
#
# **Links that are no longer there.** A sitemap is a snapshot; articles get
# unpublished, and a queued URL is never re-checked between discovery and the
# crawl that spends a call on it.
#
# **Articles that would not be queued today.** The filter in `seed/feeds.toml` is
# data and it gets edited — a term added, an exclusion tightened. Nothing ever
# re-applied it to what was already queued, so the queue held every candidate that
# passed every *past* version of the filter: NTT case studies, DataBank compliance
# blogs, and Meta's announcement of the winners of an AR effects contest.


#: HTTP answers that mean the page is gone rather than defended. 403 and 429 are
#: deliberately absent: a newsroom answering 403 to a non-browser is exactly the
#: case `--browser` exists for, and dropping those would delete the queue's most
#: valuable rows because they were the best defended.
DEAD_STATUS: tuple[int, ...] = (404, 410)


@dataclass
class UrlVerdict:
    """What one queued URL answered when asked."""

    row_id: int
    url: str
    title: str
    feed: str
    status: int | None
    #: "ok" | "dead" | "blocked" | "error"
    verdict: str
    detail: str = ""


#: Error text a failed name lookup produces, on Linux, macOS and Windows.
_NO_SUCH_NAME: tuple[str, ...] = (
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo",
    "no address",
)


def _lookup_failed(status: int | None, error: str) -> bool:
    return status is None and any(term in error.lower() for term in _NO_SUCH_NAME)


def classify_status(status: int | None, error: str = "") -> str:
    """Reachable, gone, defended, or something else — from this one answer alone.

    A name that does not resolve reads as dead here, but one answer cannot tell a
    domain that is gone from a resolver that is down; `verify_urls` is what decides
    between them, from the rest of the batch.
    """
    if status in DEAD_STATUS:
        return "dead"
    if status is not None and 200 <= status < 400:
        return "ok"
    if status in (401, 403, 429):
        return "blocked"
    if status is None and error:
        # A name that does not resolve is as dead as a 404 and stays dead; a
        # timeout is a bad afternoon. Only the first is worth deleting.
        return "dead" if _lookup_failed(status, error) else "error"
    return "error"


def verify_urls(rows: list[IngestUrl], *, settings: Settings | None = None) -> list[UrlVerdict]:
    """Fetch every URL and say which are gone. Read-only against the database.

    Uses the project's own fetch stack — same user agent, same per-host
    politeness — so a site that answers this differently from a crawl is telling
    us something real rather than reacting to a different client.

    **A failed name lookup counts as gone only when other names in the same check
    resolved.** On this machine a DNS outage and every host having vanished produce
    the same error for every URL, and `queue check --drop` deleted whatever a check
    during a hiccup asked about. A 404 or 410 is the server positively saying so; a
    lookup failure is evidence against a name only once some other URL in the batch
    got an HTTP answer, which proves the resolver and the network were up. Until then
    it is "could not tell", which is never dropped.
    """
    import asyncio

    from tracker.ingest.fetch import fetch_all

    if not rows:
        return []
    by_url = {row.url: row for row in rows}
    results = asyncio.run(fetch_all(list(by_url), settings=settings))
    resolver_worked = any(result.status is not None for result in results)
    out: list[UrlVerdict] = []
    for result in results:
        row = by_url.get(result.url)
        if row is None:
            continue
        verdict = classify_status(result.status, result.error or "")
        if (
            verdict == "dead"
            and not resolver_worked
            and _lookup_failed(result.status, result.error or "")
        ):
            verdict = "error"
        out.append(
            UrlVerdict(
                row_id=row.id,
                url=row.url,
                title=row.title or "",
                feed=row.feed or "",
                status=result.status,
                verdict=verdict,
                detail=(result.error or "")[:120],
            )
        )
    return out


@dataclass
class PruneCandidate:
    """A queued row the current filter would not have queued, and why."""

    row_id: int
    url: str
    title: str
    feed: str
    reason: str


def refilter_pending(
    session: Session, *, feeds_path: Path | None = None
) -> tuple[list[PruneCandidate], int]:
    """Re-apply the configured filter to everything already queued.

    Returns `(no longer matching, total examined)`. Judges each row exactly as
    discovery would today, including `topic_implied` for the feed it came from —
    without that, every newsroom sitemap entry would be re-judged as though it
    came from a general outlet and the whole queue would look like noise.

    A row whose feed is no longer in `feeds.toml` is left alone rather than
    dropped. Deleting somebody's queue because they commented out a feed would be
    a surprising thing for a maintenance command to do.
    """
    feeds, spec = load_config(feeds_path)
    implied = {f.name: f.topic_implied for f in feeds}
    implied.update({s.name: s.topic_implied for s in load_sitemaps(feeds_path)})

    rows = list(session.scalars(select(IngestUrl).where(IngestUrl.status == PENDING_URL_STATUS)))
    out: list[PruneCandidate] = []
    for row in rows:
        if row.feed and row.feed not in implied and not row.feed.startswith("search:"):
            continue
        path = urlsplit(row.url).path
        keep, reason = spec.matches(
            f"{row.title or ''} {path}", topic_implied=implied.get(row.feed or "", False)
        )
        if not keep:
            out.append(
                PruneCandidate(
                    row_id=row.id,
                    url=row.url,
                    title=row.title or "",
                    feed=row.feed or "",
                    reason=reason,
                )
            )
    return out, len(rows)


def drop_ids(session: Session, ids: list[int]) -> int:
    """Delete queued rows by id. The handle `tracker queue` now prints."""
    return drop_pending(session, ids=ids)


# --- Run --------------------------------------------------------------------


# --- Closed publishers: asked again once a week --------------------------------

#: How often a closed feed or archive is asked whether it has reopened.
PROBE_EVERY: Final = dt.timedelta(days=7)


@dataclass(frozen=True)
class ProbeResult:
    """One closed entry, asked again."""

    name: str
    url: str
    open: bool
    status: int | None
    detail: str


def _closed_entries(path: Path | None = None) -> list[FeedSpec]:
    """Every [[feed]] and [[sitemap]] marked closed in the file, as feed specs."""
    feeds, _ = load_config(path)
    return [f for f in feeds if f.closed] + [s.as_feed() for s in load_sitemaps(path) if s.closed]


def _latest_probes(session: Session) -> dict[str, FeedProbe]:
    """The newest check per entry name."""
    latest: dict[str, FeedProbe] = {}
    for row in session.scalars(select(FeedProbe).order_by(FeedProbe.checked_at.asc())):
        latest[row.name] = row
    return latest


def reopened_names(session: Session) -> frozenset[str]:
    """Closed entries whose latest weekly check answered: polled again until one fails.

    Empty when the table does not exist yet, so a command run against a database
    one migration behind still starts.
    """
    from sqlalchemy.exc import OperationalError

    try:
        return frozenset(name for name, row in _latest_probes(session).items() if row.open)
    except OperationalError:
        return frozenset()


def _answer(result: FetchResult | None, spec: FeedSpec) -> tuple[bool, str]:
    """Whether a fetch came back as a real feed or sitemap, and what was seen."""
    if result is None:
        return False, "no result"
    if not result.ok:
        return False, result.error or f"HTTP {result.status}"
    text = result.markdown or ""
    if is_sitemap_index(text):
        children = index_children(text)
        return bool(children), f"sitemap index, {len(children)} child sitemap(s)"
    try:
        entries = parse_feed(text, spec)
    except DiscoverError as exc:
        return False, str(exc)
    return bool(entries), f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"


def record_probe(
    session: Session, spec: FeedSpec, result: FetchResult | None, *, now: dt.datetime
) -> ProbeResult:
    """Write one check of `spec` and return it."""
    open_, detail = _answer(result, spec)
    status = result.status if result is not None else None
    session.add(
        FeedProbe(
            name=spec.name,
            url=spec.url,
            checked_at=now,
            open=open_,
            status=status,
            detail=detail[:500],
        )
    )
    session.flush()
    return ProbeResult(spec.name, spec.url, open_, status, detail)


def probe_closed(
    session: Session,
    *,
    feeds_path: Path | None = None,
    fetcher: Any = None,
    settings: Settings | None = None,
    now: dt.datetime | None = None,
    force: bool = False,
) -> list[ProbeResult]:
    """Ask every closed feed and archive whether it answers, once per `PROBE_EVERY`.

    One request each, to the entry's own URL, with the client discovery always
    uses — the project's own name and nothing that pretends to be a browser. A
    publisher that serves a feed to that client has reopened to us; one that still
    answers with a challenge has not, and nothing here tries to get past it.

    `force` asks every closed entry now, whatever its last check said.
    """
    import asyncio

    from tracker.ingest.fetch import fetch_all

    settings = settings or get_settings()
    now = now or utcnow()
    latest = _latest_probes(session)
    due = [
        spec
        for spec in _closed_entries(feeds_path)
        if force or spec.name not in latest or now - latest[spec.name].checked_at >= PROBE_EVERY
    ]
    if not due:
        return []
    results = asyncio.run(
        fetch_all(
            [spec.url for spec in due],
            fetcher=fetcher or _RawFetcher(settings),
            settings=settings,
        )
    )
    by_url = {r.url: r for r in results}
    return [record_probe(session, spec, by_url.get(spec.url), now=now) for spec in due]


def without_reopened(specs: list[T], reopened: frozenset[str]) -> list[T]:
    """`specs` with `closed` cleared on every entry a weekly check found open."""
    from dataclasses import replace

    return [
        replace(spec, closed=None) if spec.closed and spec.name in reopened else spec
        for spec in specs
    ]


# --- Headlines only: another site's articles, found and read elsewhere ----------

#: Most headlines looked up per run. A search costs about $0.001; this bounds the
#: first night, when a week of headlines arrives at once, and nothing else.
MAX_HEADLINE_LOOKUPS = 40

#: Results kept per headline: the same story, from somebody we can read.
LEADS_PER_HEADLINE = 2

#: Share of a headline's words a search result must share to count as the same
#: story. Measured on DCD headlines: the operator's own release and the local
#: paper's piece share well over half; a different story about the same company
#: shares a third or less.
SAME_STORY_OVERLAP = 0.5

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "is",
        "are",
        "be",
        "its",
        "it",
        "this",
        "that",
        "new",
        "data",
        "center",
        "centre",
        "centers",
        "campus",
        "says",
        "will",
    ]
)


def _story_words(text: str) -> frozenset[str]:
    """Distinctive words, cut to five letters so "acquire" meets "acquired"."""
    return frozenset(
        w[:5] for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in _STOPWORDS
    )


def same_story(headline: str, title: str, snippet: str = "") -> bool:
    """Whether a search result is plausibly the headline's own story."""
    wanted = _story_words(headline)
    if not wanted:
        return False
    found = _story_words(f"{title} {snippet}")
    return len(wanted & found) / len(wanted) >= SAME_STORY_OVERLAP


#: Words a result's title must run to before matching the headline word for word
#: marks it as a copy. Short titles collide by accident ("AWS expands in Ohio").
COPY_MIN_WORDS = 5


def copies_headline(headline: str, title: str) -> bool:
    """Whether a result is the blocked publisher's own article, republished.

    A title that repeats the headline word for word — cut short by the search
    engine's "..." or not, with or without a " - Site" suffix — is a reposting
    site carrying the article we are not reading, and reading it there is reading
    it anyway. Measured on the first six DCD headlines looked up: two such copies
    (voltcal.com, goalfore.com) beside five pieces in other outlets' own words.
    """
    cut = re.split(r"\s*(?:\.\.\.|…)", title, maxsplit=1)[0]
    cut = re.sub(r"\s+[-|\u2013]\s+[^-|\u2013]+$", "", cut)
    theirs = _WORD.findall(cut.lower())
    ours = _WORD.findall(headline.lower())
    return len(theirs) >= COPY_MIN_WORDS and ours[: len(theirs)] == theirs


#: A headline that names a capacity — "1GW", "75 MW".
_CAPACITY = re.compile(r"\b\d+(?:\.\d+)?\s?[GM]W\b", re.I)

#: The publisher's own surveys, polls, podcasts and sponsored pieces. They pass the
#: topic test and are never a project: on 2026-10-07 "DCD Survey: Data center
#: construction" led to a 2023 report and "DCF Poll: ..." to an opinion piece.
_NOT_NEWS = re.compile(
    r"^(?:DCD|DCF)\s+(?:Survey|Poll|Podcast|Webinar|Broadcast|Awards?)\b"
    r"|\b(?:sponsored|webinar|whitepaper|podcast)\b",
    re.I,
)


def parse_headlines(xml: str, feed: FeedSpec) -> list[Candidate]:
    """Items of a news-search feed that the configured publisher wrote.

    Google News's RSS lists each article with a `<source url="...">` naming the
    outlet and a title ending " - <outlet>". Items from anybody else are dropped,
    and the suffix is removed so the headline can be searched for as written.
    The publisher's surveys and polls are dropped (`_NOT_NEWS`), and a headline
    stating a capacity counts as on topic without saying "data center". The
    link stays the news service's own: it is unreadable to us, and is kept only so
    the same headline is recognised tomorrow.
    """
    from tracker.confidence import registrable_domain

    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError as exc:
        raise DiscoverError(f"{feed.name}: not parseable XML ({exc})") from exc
    wanted = registrable_domain(f"https://{feed.headlines_of}")
    out: list[Candidate] = []
    for item in root.iter("item"):
        source = item.find("source")
        link = _text(item.find("link"))
        if source is None or not link:
            continue
        if registrable_domain(source.get("url") or "") != wanted:
            continue
        title = _text(item.find("title"))
        outlet = _text(source)
        if outlet and title.endswith(f" - {outlet}"):
            title = title[: -len(outlet) - 3].rstrip()
        if _NOT_NEWS.search(title):
            continue
        out.append(
            Candidate(
                url=link,
                title=title,
                feed=feed.name,
                published_at=_parse_date(_text(item.find("pubDate"))),
                source_type=feed.source_type,
                # A stated capacity stands in for the topic word: "TeraWulf's
                # Kentucky campus to reach 1GW" is the story we want and never says
                # "data center". Everything else has to say it.
                topic_implied=feed.topic_implied or bool(_CAPACITY.search(title)),
            )
        )
    return out[:MAX_PER_FEED]


def follow_headlines(
    session: Session,
    headlines: list[Candidate],
    spec: FilterSpec,
    *,
    run_id: str,
    report: DiscoverReport,
    closed: frozenset[str],
    provider: Any = None,
    dry_run: bool = False,
) -> list[Candidate]:
    """Look each new headline up once, and queue the copies we are allowed to read.

    A headline is recorded in `ingest_url` under the news service's link with
    status `skipped`, so it is looked up exactly once and never handed to the
    crawl, which could not read it. Its search excludes every closed publisher,
    and a result has to share most of the headline's words to count as the same
    story — the operator's own release, the local paper, another trade outlet.
    The leads are queued with the headline's publish date, so the nightly crawl's
    news-first order treats them as the news they are.

    A search that fails leaves the headline unrecorded, to be tried tomorrow.
    """
    from dataclasses import replace

    from tracker.confidence import registrable_domain
    from tracker.ingest.search import SearchError, SearchReport, build_provider, hits_to_candidates

    known = {
        url_identity(u)
        for u in session.scalars(
            select(IngestUrl.url).where(IngestUrl.url.in_([h.url for h in headlines]))
        )
    }
    fresh = [h for h in headlines if url_identity(h.url) not in known]
    report.headlines_new += len(fresh)
    if dry_run or not fresh:
        return []
    if provider is None:
        try:
            provider = build_provider()
        except SearchError as exc:
            report.failures.append(("headlines", f"no search backend: {exc}"))
            return []
    exclude = " ".join(f"-site:{d}" for d in sorted(closed))
    leads: list[Candidate] = []
    now = utcnow()
    for headline in fresh[:MAX_HEADLINE_LOOKUPS]:
        query = f"{headline.title} {exclude}".strip()
        try:
            hits = provider.search(query, limit=6)
        except SearchError as exc:
            report.failures.append((headline.feed, f"search failed: {exc}"))
            continue
        report.headlines_looked_up += 1
        label = f"lead:{headline.feed}"
        found = hits_to_candidates(hits, spec, report=SearchReport(), labels={query: label})
        snippets = {h.url: h.snippet for h in hits}
        same = [
            replace(c, published_at=headline.published_at)
            for c in found
            if registrable_domain(c.url) not in closed
            and same_story(headline.title, c.title, snippets.get(c.url, ""))
            and not copies_headline(headline.title, c.title)
        ][:LEADS_PER_HEADLINE]
        leads.extend(same)
        session.add(
            IngestUrl(
                url=canonical_url(headline.url),
                run_id=run_id,
                status="skipped",
                title=norm_text(headline.title, max_len=300),
                feed=headline.feed,
                published_at=headline.published_at,
                error=f"headline only: {len(same)} readable cop(y/ies) queued",
                attempts=0,
                first_seen_at=now,
                last_tried_at=now,
            )
        )
    session.flush()
    queued = queue_candidates(session, leads, run_id=run_id, report=report)
    report.leads_queued += len(queued)
    return queued


def run(
    session: Session,
    *,
    feeds_path: Path | None = None,
    fetcher: Fetcher | None = None,
    settings: Settings | None = None,
    since_days: int | None = 60,
    run_id: str | None = None,
    dry_run: bool = False,
    cache_dir: Path | None = None,
    probe: bool = True,
    probe_force: bool = False,
    search_provider: Any = None,
) -> tuple[DiscoverReport, list[Candidate]]:
    """Poll every configured feed and queue the matching articles.

    A feed that fails is recorded and the run continues: one outlet changing its
    URL must not stop discovery from the other six. A feed marked closed is not
    requested at all, and is reported as closed rather than as failed — unless its
    weekly check (`probe`, :func:`probe_closed`) found it answering again, when it
    is polled like any other, and closed again the moment a poll fails.

    A headlines-only feed's matches are looked up rather than queued; see
    :func:`follow_headlines`.
    """
    import asyncio

    settings = settings or get_settings()
    feeds, spec = load_config(feeds_path)
    report = DiscoverReport()
    run_id = run_id or utcnow().strftime("discover-%Y%m%dT%H%M%S")
    since = utcnow() - dt.timedelta(days=since_days) if since_days else None

    if probe:
        # On a dry run too: the checks are rolled back with everything else, and
        # they are what `--probe-closed --dry-run` is asked for.
        report.probes = probe_closed(
            session, feeds_path=feeds_path, fetcher=fetcher, settings=settings, force=probe_force
        )
    reopened = reopened_names(session)
    report.reopened = [f.name for f in feeds if f.closed and f.name in reopened]
    feeds = without_reopened(feeds, reopened)
    report.closed = [(f.name, f.closed) for f in feeds if f.closed]
    feeds = [f for f in feeds if not f.closed]
    closed = closed_domains(feeds_path, reopened=reopened)

    from tracker.ingest.fetch import fetch_all

    results = asyncio.run(
        fetch_all(
            [f.url for f in feeds], fetcher=fetcher or _RawFetcher(settings), settings=settings
        )
    )
    by_url = {r.url: r for r in results}

    all_kept: list[Candidate] = []
    headlines: list[Candidate] = []
    for feed in feeds:
        result = by_url.get(feed.url)
        report.feeds_polled += 1
        if feed.name in report.reopened and not dry_run:
            # A reopened publisher is held to every poll: one that fails is closed
            # again tonight, not after another week of failures.
            record_probe(session, feed, result, now=utcnow())
        if result is None or not result.ok:
            report.feeds_failed += 1
            reason = (result.error if result else "no result") or "unknown error"
            report.failures.append((feed.name, reason))
            log.warning("feed %s failed: %s", feed.name, reason)
            continue
        try:
            entries = (
                parse_headlines(result.markdown, feed)
                if feed.headlines_of
                else parse_feed(result.markdown, feed)
            )
        except DiscoverError as exc:
            report.feeds_failed += 1
            report.failures.append((feed.name, str(exc)))
            log.warning("%s", exc)
            continue
        if not entries:
            report.failures.append((feed.name, "parsed but contained no entries"))
        kept = select_candidates(entries, spec, since=since, report=report)
        (headlines if feed.headlines_of else all_kept).extend(kept)

    queued = queue_candidates(session, all_kept, run_id=run_id, report=report)
    queued += follow_headlines(
        session,
        headlines,
        spec,
        run_id=run_id,
        report=report,
        closed=closed,
        provider=search_provider,
        dry_run=dry_run,
    )
    if dry_run:
        session.rollback()
        # The report still describes what would have happened. No bodies are
        # written either: a dry run must not leave files behind any more than it
        # leaves rows behind.
        return report, all_kept + headlines
    # Only for the newly queued. A candidate already in `ingest_url` has had its
    # turn, and rewriting its body would resurrect an article the operator dropped
    # from the queue on purpose.
    report.bodies_cached = cache_feed_text(queued, cache_dir)
    session.commit()
    return report, queued


#: Appended to a failure Cloudflare marks `cf-mitigated: challenge`.
#:
#: A bare "HTTP 403" reads as a header problem worth an afternoon of trying
#: User-Agents. This one is not: the "Just a moment..." page lets through only a
#: client that runs the publisher's script, so no header and no TLS fingerprint
#: changes the answer — measured on thirteen feeds on 2026-10-02, see the
#: `closed` notes in `seed/feeds.toml`. Saying so in the failure line is what
#: makes the next one recognisable from the nightly log alone.
CHALLENGE_NOTE = "Cloudflare challenge: only a client that runs the site's script gets past it"


class _RawFetcher:
    """Fetches a feed as raw XML.

    Distinct from `HttpxFetcher`, which runs `html_to_text` on the body — that
    would strip the very tags a feed parser needs.

    It sends `settings.user_agent`, the project's own name and contact. Measured on
    the feeds that answer 403, a browser's User-Agent changed nothing, so borrowing
    one would buy nothing either.

    `transport` stands in for the network, for tests.
    """

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self._transport = transport

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
                transport=self._transport,
            ) as client:
                response = await client.get(url)
        # `InvalidURL` is not a `RequestError`. A mistyped feed URL raised it before
        # any request, and that exception stopped discovery for every other feed.
        except (httpx.RequestError, httpx.InvalidURL) as exc:
            return FetchResult(url, False, error=str(exc), fetched_at=utcnow(), via="feed")

        if response.status_code >= 400:
            error = f"HTTP {response.status_code}"
            if response.headers.get("cf-mitigated", "").lower() == "challenge":
                error = f"{error} ({CHALLENGE_NOTE})"
            return FetchResult(
                url,
                False,
                status=response.status_code,
                error=error,
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
    "CHALLENGE_NOTE",
    "COPY_MIN_WORDS",
    "DEAD_STATUS",
    "MAX_HEADLINE_LOOKUPS",
    "MAX_PER_FEED",
    "MAX_SAME_FAILURES",
    "PROBE_EVERY",
    "RETRYABLE_STATUSES",
    "UNEXPLAINED_CLOSURE",
    "Candidate",
    "DiscoverError",
    "DiscoverReport",
    "FeedSpec",
    "FilterSpec",
    "ProbeResult",
    "ProjectIdentity",
    "PruneCandidate",
    "SitemapSpec",
    "UrlVerdict",
    "classify_status",
    "closed_domains",
    "copies_headline",
    "crawl_sitemap",
    "default_feeds_path",
    "drop_ids",
    "drop_pending",
    "failed",
    "failure_summary",
    "follow_headlines",
    "given_up",
    "load_config",
    "load_sitemaps",
    "matches_known_project",
    "parse_feed",
    "parse_headlines",
    "pending",
    "pending_risk_count",
    "pending_split",
    "probe_closed",
    "project_identities",
    "queue_candidates",
    "readable_first",
    "record_probe",
    "refilter_pending",
    "reopened_names",
    "retryable",
    "run",
    "same_story",
    "select_candidates",
    "sweep_sitemaps",
    "unreadable_test",
    "verify_urls",
    "without_reopened",
]
