"""Wikipedia as a lead generator: mine a campus article's references.

Google's first result for a tracked campus is routinely its Wikipedia article
("Hyperion Data Center"), and that article's References section is a curated
bibliography of exactly the primary sources this tracker wants — the operator's
own press release, the investor-relations announcement, the local-news coverage.
Measured on Hyperion: 50 external links, among them the investor.atmeta.com
release behind the Blue Owl joint venture, two CNBC pieces, AP and BBC coverage.

Two distinct uses of a Wikipedia hit, and this module is only the second:

* **The article itself** is fetched and extracted like any page — the evidence
  gate applies unchanged, so a value still needs a verbatim quote. What keeps
  that honest is `confidence.TERTIARY_DOMAINS`: a wikipedia.org citation never
  counts as an independent domain, because Wikipedia summarizes the same press
  coverage the row already cites, and letting it corroborate would launder
  aggregation into independence.

* **Its references** are read through the MediaWiki API (`action=parse`,
  `prop=externallinks`) rather than from the crawl cache, because
  `fetch.html_to_text` strips hrefs — the cached text has no links left in it.
  The API is the official, structured way to ask; no HTML parsing, no scraping.

Mined links carry no title or snippet, so filtering runs on the URL alone:
the same host blocklist as search hits, the wikimedia tool family dropped, and
a keyword pass over the slug in which **any** tier (topic, signal, or risk)
keeps the link. Requiring topic AND signal — the rule for unsolicited feed
entries — would drop the operator's own campus page (`/richland-parish-data-
center/` carries a topic term and no signal term), and a reference cited by a
data-center article has already had its relevance judged by an editor; the
keyword pass only exists to shed the citations that are *generically* about
something else — the census table, the corporate registry, the map tool.

Links wrapped by the Wayback Machine (`web.archive.org/web/<ts>/<url>`) are
unwrapped to the original URL first, so an archived copy and a live link to the
same article dedupe to one candidate keyed on the real address.
"""

from __future__ import annotations

import logging
from urllib.parse import unquote, urlsplit

import httpx

from tracker.config import Settings, get_settings
from tracker.ingest.discover import Candidate, FilterSpec, normalize_haystack

log = logging.getLogger(__name__)

#: References kept per article, after filtering. A flagship campus article cites
#: fifty-plus sources; the first N (roughly citation order, so the load-bearing
#: ones) are plenty, and a cap keeps one article from flooding the queue.
MAX_LINKS_PER_PAGE = 25

#: Hosts that are Wikipedia's own plumbing rather than sources: sister projects,
#: map and citation tools. Matched on label boundaries like the search blocklist.
_WIKIMEDIA_FAMILY = (
    "wikipedia.org",
    "wikimedia.org",
    "wikidata.org",
    "wiktionary.org",
    "wikisource.org",
    "mediawiki.org",
    "toolforge.org",
    "wmflabs.org",
    "archive.org",  # bare archive.org items; web.archive.org wrappers are unwrapped instead
)

#: Title namespaces that are never articles about anything.
_NON_ARTICLE_PREFIXES = ("Category:", "File:", "Talk:", "Template:", "Portal:", "Help:")


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().split("@")[-1].split(":")[0].removeprefix("www.")


def is_wikipedia(url: str) -> bool:
    """True for any language edition's article URL."""
    host = _host(url)
    return host == "wikipedia.org" or host.endswith(".wikipedia.org")


def page_title(url: str) -> str | None:
    """The article title a /wiki/ URL names, or None for non-article pages."""
    parts = urlsplit(url)
    if not parts.path.startswith("/wiki/"):
        return None
    title = unquote(parts.path.removeprefix("/wiki/")).strip()
    if not title or title.startswith(_NON_ARTICLE_PREFIXES):
        return None
    return title


def unwrap_archive(url: str) -> str:
    """A Wayback Machine wrapper reduced to the URL it archived.

    ``https://web.archive.org/web/20260524235358/https://example.com/x`` names
    example.com's page, and that is the address the queue should carry: it
    dedupes against a direct link to the same article, and it is the URL a
    citation should point at.
    """
    parts = urlsplit(url)
    if _host(url) != "web.archive.org" or not parts.path.startswith("/web/"):
        return url
    rest = parts.path.removeprefix("/web/")
    for scheme in ("https://", "http://"):
        index = rest.find(scheme)
        if index != -1:
            return rest[index:] + (f"?{parts.query}" if parts.query else "")
    return url


def _is_family(url: str) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in _WIKIMEDIA_FAMILY)


def external_links(
    url: str, *, settings: Settings | None = None, client: httpx.Client | None = None
) -> list[str]:
    """The article's external links, via the MediaWiki API. [] on any failure.

    Failure is deliberately non-fatal: mining is a bonus on top of a search hit,
    and a run should not die because one wiki page moved or the API hiccuped.
    """
    settings = settings or get_settings()
    title = page_title(url)
    if title is None:
        return []
    endpoint = f"https://{urlsplit(url).netloc}/w/api.php"
    params = {
        "action": "parse",
        "page": title,
        "prop": "externallinks",
        "format": "json",
        "redirects": "1",
    }
    try:
        if client is not None:
            response = client.get(endpoint, params=params)
        else:
            response = httpx.get(
                endpoint,
                params=params,
                headers={"User-Agent": settings.user_agent},
                timeout=httpx.Timeout(settings.fetch_timeout_s, connect=10.0),
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("could not read references of %s: %s", url, exc)
        return []
    links = (payload.get("parse") or {}).get("externallinks") or []
    return [str(link) for link in links if isinstance(link, str)]


def _keyword_pass(url: str, spec: FilterSpec) -> bool:
    """Any tier keeps a reference; the exclude tier still drops one."""
    parts = urlsplit(url)
    haystack = normalize_haystack(f"{parts.netloc.lower().removeprefix('www.')} {parts.path}")
    if any(term in haystack for term in spec.exclude):
        return False
    return any(
        term in haystack for tier in (spec.topic, spec.signal, spec.risk_signal) for term in tier
    )


def mine(
    wiki_urls: list[str],
    spec: FilterSpec,
    *,
    settings: Settings | None = None,
    links_for=None,
) -> list[Candidate]:
    """References of the given Wikipedia articles, as queueable candidates.

    `links_for` is injectable for tests; the default asks the live API. Resolved
    at call time rather than as a parameter default so a monkeypatched
    `external_links` is honoured.
    """
    from tracker.ingest.search import is_useful_host

    if links_for is None:
        links_for = external_links
    settings = settings or get_settings()
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for wiki_url in dict.fromkeys(wiki_urls):
        title = page_title(wiki_url)
        if title is None:
            continue
        kept = 0
        raw = links_for(wiki_url, settings=settings)
        for position, link in enumerate(raw):
            if kept >= MAX_LINKS_PER_PAGE:
                log.info(
                    "%s: reference cap reached, %d link(s) not considered",
                    title,
                    len(raw) - position,
                )
                break
            if link.startswith("//"):
                link = "https:" + link
            if not link.startswith(("http://", "https://")):
                continue
            link = unwrap_archive(link)
            if link in seen or _is_family(link) or not is_useful_host(link):
                continue
            if not _keyword_pass(link, spec):
                continue
            seen.add(link)
            kept += 1
            candidates.append(
                Candidate(
                    url=link,
                    title=link,
                    feed=f"wikipedia:{title}"[:120],
                    published_at=None,
                    source_type="general_media",
                )
            )
        if raw:
            log.info("%s: %d reference(s), %d kept", title, len(raw), kept)
    return candidates


__all__ = [
    "MAX_LINKS_PER_PAGE",
    "external_links",
    "is_wikipedia",
    "mine",
    "page_title",
    "unwrap_archive",
]
