"""Finding feeds a publisher already serves, for hosts that have proved useful.

`seed/feeds.toml` is hand-maintained: 28 feeds and 22 sitemaps, each added because
somebody noticed the outlet. That is the wrong way round. The database already
knows which publishers decide stored values — `tracker sources` counts it — and
some of the best ones are not configured at all, so they are only ever reached
when a search or an archive sweep happens to surface them.

So candidates come from the record rather than from a model:
**hosts whose claims decide values, that `feeds.toml` does not list.** No LLM.
`docs/plan-scale-with-sources.md` measured a plain normaliser settling 21 of 23
naming groups and concluded "one LLM call per project is 126 calls to do nothing
on 123 of them"; asking a model to name good data-centre outlets has the same
shape, and the answer is already in the database.

Three rungs per host, cheapest and most authoritative first:

1. **`robots.txt`.** `Sitemap:` lines are the publisher telling machines where the
   index is. Free, and it is the one place a site *declares* the answer.
2. **Well-known paths.** `/feed`, `/rss.xml`, `/sitemap.xml` and friends.
3. **The homepage `<link rel="alternate">`.** What a browser reads to offer a
   subscription.

**Every hit is validated by parsing it and running the real filter over it**, so
the report says how many entries would actually have been *queued* rather than
that a URL responded. A feed that parses and matches nothing is not a find.

**Proposes, never writes.** It prints TOML to paste, the shape `tracker
duplicates` and `tracker blocks` already use for a judgement that wants a person.
An automatically-added feed is an automatically-added corpus, and the filter is
the only thing standing between a bad one and paid extraction.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings
from tracker.ingest.discover import (
    DiscoverError,
    FeedSpec,
    FilterSpec,
    index_children,
    is_sitemap_index,
    load_config,
    load_sitemaps,
    parse_feed,
)

log = logging.getLogger(__name__)

#: Paths a publisher commonly serves a feed or an index on. Ordered by how likely
#: they are to be the real thing, because the first parseable hit wins and a
#: `/sitemap.xml` covering the whole site is a worse buy than a news feed.
CANDIDATE_PATHS: tuple[str, ...] = (
    "/feed",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
    "/feeds/posts/default",
    "/sitemap.xml",
)

#: `Sitemap:` lines in robots.txt.
_ROBOTS_SITEMAP = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)

#: A subscribable document linked from an HTML page.
_ALTERNATE = re.compile(
    r"<link[^>]+rel=[\"']alternate[\"'][^>]*>",
    re.IGNORECASE,
)
_HREF = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
_FEED_TYPE = re.compile(r"type=[\"']application/(rss|atom)\+xml[\"']", re.IGNORECASE)

#: Entries read from any one probed document. A cap, not a sample: this is only
#: sizing whether the feed is worth adding, and some sitemaps carry 50,000 URLs.
MAX_ENTRIES = 200

#: Child sitemaps followed from an index. `index_children` puts the article files
#: first, so a small number reaches the useful ones without walking a whole site.
MAX_INDEX_CHILDREN = 2

#: How deep to follow nested indexes. One level is what real sites use; deeper is
#: a loop or a site large enough that the archive sweep is the right tool anyway.
MAX_INDEX_DEPTH = 1


@dataclass
class FeedHit:
    """One document that parsed, and what it would have contributed."""

    url: str
    entries: int
    would_queue: int
    found_via: str
    sample: list[str] = dc_field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return self.would_queue / self.entries if self.entries else 0.0


@dataclass
class HostProbe:
    """What one publisher turned out to serve."""

    host: str
    cited: int = 0
    decisive: int = 0
    hits: list[FeedHit] = dc_field(default_factory=list)
    tried: int = 0
    note: str | None = None

    @property
    def best(self) -> FeedHit | None:
        """The hit that would have queued the most. Ties break on fewer entries.

        Preferring the smaller document on a tie matters: a news feed and a
        whole-site sitemap often queue the same articles, and the feed costs one
        request per poll where the sitemap costs a walk.
        """
        if not self.hits:
            return None
        return sorted(self.hits, key=lambda h: (-h.would_queue, h.entries, h.url))[0]

    def as_toml(self) -> str:
        """A `[[feed]]` block to paste into `seed/feeds.toml`."""
        hit = self.best
        if hit is None:
            return ""
        name = self.host.split(".")[0]
        # The record line is omitted when the host was named explicitly rather than
        # proposed, because "0 value(s) decided" would then be a fact about the
        # command line rather than about the publisher.
        record = (
            f"# {self.decisive} value(s) decided from {self.cited} citation(s).\n"
            if self.cited
            else ""
        )
        return (
            f"[[feed]]\n"
            f'name = "{name}"\n'
            f"{record}"
            f"# {hit.would_queue} of {hit.entries} entries would queue. "
            f"Found via {hit.found_via}.\n"
            f"# Verify the hit rate before trusting it: every queued entry costs a call.\n"
            f'url = "{hit.url}"\n'
            f'source_type = "general_media"\n'
        )


def configured_hosts() -> set[str]:
    """Registrable domains already in `seed/feeds.toml`, feeds and sitemaps alike.

    Both lists, because a host reachable through its archive is already reachable
    — proposing it as a feed would be proposing work already done.
    """
    from tracker.sources import host_of

    hosts: set[str] = set()
    try:
        feeds, _ = load_config()
    except DiscoverError:
        feeds = []
    for spec in feeds:
        hosts.add(host_of(spec.url))
    try:
        for spec in load_sitemaps():
            hosts.add(host_of(spec.url))
    except DiscoverError:
        pass
    return hosts


def candidates(session: Session, *, limit: int = 15, min_decisive: int = 2) -> list:
    """Publishers worth probing: proven by the record, absent from the config.

    Ordered by how many values they decide, which is the same ordering
    `tracker sources` defaults to and for the same reason — it is "how much do we
    actually use this", and a small sample cannot inflate it.
    """
    from tracker.sources import survey

    known = configured_hosts()
    ranked = survey(session).ranked(by="decisive")
    return [h for h in ranked if h.host not in known and h.decisive >= min_decisive][:limit]


async def _get(fetcher, url: str) -> str | None:
    result = await fetcher.fetch(url)
    return result.markdown if result.ok and result.markdown.strip() else None


def _score(document: str, url: str, spec: FilterSpec, *, found_via: str) -> FeedHit | None:
    """Parse a candidate document and run the real filter over its entries.

    Returns None when it does not parse as a feed or sitemap at all, which is the
    common case: `/feed` on a site that has none usually answers 200 with the
    homepage.
    """
    probe_spec = FeedSpec(name="probe", url=url, topic_implied=False)
    try:
        entries = parse_feed(document, probe_spec, cap=MAX_ENTRIES)
    except DiscoverError:
        return None
    if not entries:
        return None

    kept = [
        candidate
        for candidate in entries
        if spec.matches(f"{candidate.title} {urlsplit(candidate.url).path}")[0]
    ]
    return FeedHit(
        url=url,
        entries=len(entries),
        would_queue=len(kept),
        found_via=found_via,
        sample=[c.title[:90] for c in kept[:3]],
    )


async def probe_host(host: str, *, fetcher, spec: FilterSpec) -> HostProbe:
    """Walk the three rungs for one publisher, stopping when a rung answers."""
    probe = HostProbe(host=host)
    seen: set[str] = set()

    async def get(url: str) -> str | None:
        """Every request goes through here, so `tried` is the real request count.

        It counted only candidate documents at first, which reported "nothing
        parseable in 8 requests" for a host that had actually been asked ten
        times — the robots.txt and homepage probes were invisible.
        """
        probe.tried += 1
        return await _get(fetcher, url)

    async def consider(url: str, found_via: str, *, depth: int = 0) -> None:
        if url in seen:
            return
        seen.add(url)
        document = await get(url)
        if document is None:
            return
        # A `<sitemapindex>` lists other sitemaps, so it parses to zero entries and
        # would read as "not a feed". Following it one level is what makes the
        # probe find what `feeds.toml` already carries: this site's own configured
        # entry points at `sitemap/Article.xml` precisely because `/sitemap.xml` is
        # an index. `index_children` prefers the article file for the same reason.
        if is_sitemap_index(document):
            if depth >= MAX_INDEX_DEPTH:
                return
            for child in index_children(document)[:MAX_INDEX_CHILDREN]:
                await consider(child, f"{found_via} → index", depth=depth + 1)
            return
        hit = _score(document, url, spec, found_via=found_via)
        if hit is not None and hit.entries:
            probe.hits.append(hit)

    base = f"https://{host}"

    # Rung 1: the publisher's own declaration.
    robots = await get(f"{base}/robots.txt")
    for match in _ROBOTS_SITEMAP.finditer(robots or ""):
        await consider(match.group(1).strip(), "robots.txt")

    # Rung 2: the conventional paths.
    if not probe.hits:
        for path in CANDIDATE_PATHS:
            await consider(f"{base}{path}", f"well-known {path}")
            if probe.hits:
                break

    # Rung 3: what a browser would offer to subscribe to.
    if not probe.hits:
        home = await get(base)
        for tag in _ALTERNATE.finditer(home or ""):
            if not _FEED_TYPE.search(tag.group(0)):
                continue
            href = _HREF.search(tag.group(0))
            if href is None:
                continue
            target = href.group(1)
            if target.startswith("/"):
                target = f"{base}{target}"
            await consider(target, "<link rel=alternate>")

    if not probe.hits:
        probe.note = f"nothing parseable in {probe.tried} request(s)"
    return probe


def run(
    session: Session,
    *,
    hosts: list[str] | None = None,
    limit: int = 15,
    min_decisive: int = 2,
    settings: Settings | None = None,
    fetcher=None,
) -> list[HostProbe]:
    """Probe the most-used publishers that `feeds.toml` does not carry."""
    settings = settings or get_settings()
    _, spec = load_config()

    if hosts:
        targets = [HostProbe(host=h) for h in hosts]
    else:
        targets = [
            HostProbe(host=stat.host, cited=stat.cited, decisive=stat.decisive)
            for stat in candidates(session, limit=limit, min_decisive=min_decisive)
        ]
    if not targets:
        return []

    if fetcher is None:
        from tracker.ingest.discover import _RawFetcher

        fetcher = _RawFetcher(settings)

    async def walk() -> list[HostProbe]:
        out = []
        # Sequential across hosts on purpose. This is speculative traffic against
        # publishers who have not asked for it, and the whole run is a few dozen
        # requests — there is nothing to gain by making it fast.
        for target in targets:
            probed = await probe_host(target.host, fetcher=fetcher, spec=spec)
            probed.cited, probed.decisive = target.cited, target.decisive
            out.append(probed)
        return out

    return asyncio.run(walk())


__all__ = [
    "CANDIDATE_PATHS",
    "FeedHit",
    "HostProbe",
    "candidates",
    "configured_hosts",
    "probe_host",
    "run",
]
