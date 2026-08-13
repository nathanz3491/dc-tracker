"""Fetching article text, behind a protocol with two implementations.

**httpx is the default and Crawl4AI is the escalation**, which is the reverse of
the PRD's framing. The reasons:

* The definition-of-done articles (a Microsoft release, a trade-press piece) are
  ordinary server-rendered pages. A headless Chromium adds seconds per page and
  hundreds of megabytes of install to fetch HTML that `httpx` already gets.
* Crawl4AI hard-pins a third-party fork of litellm. Keeping it out of the default
  path keeps that out of the dependency graph for everyone who does not need it.
* It is still genuinely useful where it earns its weight: Cloudflare interstitials
  and JS-rendered shells. So it stays, as an opt-in `[crawl]` extra reached
  automatically when httpx comes back with a 403 or a suspiciously empty body.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlsplit

import httpx

from tracker.config import Settings, get_settings
from tracker.models import utcnow

log = logging.getLogger(__name__)

#: Below this many characters of extracted text, assume we got a JS shell or a
#: consent wall rather than an article, and escalate to a real browser.
MIN_USEFUL_CHARS = 400

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Statuses no amount of retrying will fix, but a browser sometimes will.
BROWSER_WORTHY_STATUS = frozenset({403, 429, 503})
HARD_FAIL_STATUS = frozenset({401, 402, 404, 410, 451})


class MissingDependency(RuntimeError):
    """An optional extra is needed. Message names the exact install command."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    ok: bool
    markdown: str = ""
    status: int | None = None
    error: str | None = None
    fetched_at: datetime | None = None
    via: str = "httpx"
    attempts: int = 1
    #: When the publisher says it published this, read out of the page's own
    #: metadata by :func:`published_date`. None when the page states none —
    #: never a guess, because this feeds a merge tiebreak.
    published_at: datetime | None = None

    @property
    def sha1(self) -> str:
        return hashlib.sha1(self.markdown.encode("utf-8"), usedforsecurity=False).hexdigest()

    @property
    def looks_thin(self) -> bool:
        return len(self.markdown.strip()) < MIN_USEFUL_CHARS


class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...


# --- Publication date -------------------------------------------------------
#
# Read here, in the fetcher, because it is the only place the raw HTML exists.
# `html_to_text` strips every tag a few lines later and the article cache stores
# its output, so by the time anything downstream sees the page the metadata is
# gone: measured across 585 cached articles, zero contain `datePublished`,
# `article:published_time`, `<time` or a JSON-LD block.
#
# What it buys: `upsert` ranks competing claims on `(confirmed, weight, recency)`
# and `recency` falls back to `fetched_at` — crawl order — whenever a date is
# missing, which it is for 88% of citations. Hyperion (#10) holds Meta's
# superseded $10B for exactly that reason: two `government_doc` pages from the
# SAME publisher (opportunitylouisiana.gov), both quote-backed, so no authority
# rule can separate them. $50B was published 2026-07-13 and $10B on 2024-12-04,
# but $10B was crawled eighteen hours later and won.
#
# Sampled on ten live Hyperion URLs: 7 carry a machine-readable date (5 JSON-LD,
# 2 `<time>`).

#: Earliest date treated as real. A `<time>` element or a stray meta tag often
#: carries a template default or a copyright year; nothing in this dataset was
#: published before the modern data centre industry existed, so anything below
#: this is evidence the selector matched furniture rather than a byline.
_PLAUSIBLE_FROM: Final = datetime(2000, 1, 1)

#: How far ahead of now a date may sit and still be believed. Not zero: a
#: publisher in UTC+13 legitimately stamps "tomorrow" by our clock, and some
#: newsrooms post-date a release by a day. Beyond that it is a scheduled-content
#: placeholder, not a publication date.
_PLAUSIBLE_AHEAD_DAYS: Final = 2

#: JSON-LD. Matched with a regex rather than parsed, for the same reason
#: `html_to_text` is a regex pass: the blocks are frequently invalid JSON, are
#: often nested inside an `@graph`, and there is nothing to gain from a parser
#: that refuses the whole document over a trailing comma.
#: Case-insensitive on the key: schema.org says `datePublished`, and real pages
#: emit `datepublished` and `DatePublished` too.
_LD_DATE = re.compile(r'"datepublished"\s*:\s*"([^"]{4,40})"', re.IGNORECASE)

#: OpenGraph and friends, in both attribute orders — `content` may precede or
#: follow the name, and real pages do both.
_META_NAMES = r"(?:article:published_time|og:published_time|publish[-_]date|pubdate|date)"
_META_DATE = (
    re.compile(
        rf'<meta[^>]+(?:property|name)\s*=\s*["\']{_META_NAMES}["\'][^>]*?'
        rf'content\s*=\s*["\']([^"\']{{4,40}})["\']',
        re.IGNORECASE,
    ),
    re.compile(
        rf'<meta[^>]+content\s*=\s*["\']([^"\']{{4,40}})["\'][^>]*?'
        rf'(?:property|name)\s*=\s*["\']{_META_NAMES}["\']',
        re.IGNORECASE,
    ),
)

#: A date in the URL path: `/2026/07/13/slug` or `/20260713-slug`. Anchored on a
#: separator at both ends so a bare run of digits — an article id, a product
#: number — cannot be read as a date.
_URL_DATE = (
    re.compile(r"/(20[12]\d)/(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])(?=[/\-_]|$)"),
    re.compile(r"/(20[12]\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?=[/\-_]|$)"),
    re.compile(r"/(20[12]\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?=[/\-_]|$)"),
)

#: A `<time datetime="...">` element. Last rung: the tag marks *a* date, not
#: necessarily the publication date — it is also used for event dates and comment
#: timestamps — so it only answers when nothing more explicit did.
_TIME_DATE = re.compile(r'<time[^>]+datetime\s*=\s*["\']([^"\']{4,40})["\']', re.IGNORECASE)


def parse_timestamp(raw: str) -> datetime | None:
    """A date from a feed or a page, as naive UTC, or None.

    Naive UTC with `microsecond=0`, matching `models.utcnow` and every other
    timestamp in the schema — mixing aware and naive values gives SQLite a
    silently unorderable column, and this one is sorted on.

    Handles RFC 2822 (RSS), ISO 8601 (Atom, JSON-LD, OpenGraph) and a bare
    `YYYY-MM-DD`. `discover._parse_date` delegates here so a feed date and a page
    date cannot end up in two different conventions in the same column.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    parsed: datetime | None = None
    # RFC 2822 first only when it looks like it: `parsedate_to_datetime` accepts
    # some ISO strings and mangles them.
    if "," in raw or raw.endswith(("GMT", "UTC")):
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return None
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def date_from_url(url: str) -> datetime | None:
    """A publication date embedded in the URL path, or None.

    `…/2026/07/13/slug` and `…/20260713-slug`. Free, offline, and deterministic,
    which is why it is worth having even though it only reaches part of the corpus:
    measured over the live database it dates 175 citations that nothing else does,
    taking coverage from 11.8% to 18.2%.

    A dated path is a publisher's own filing convention, not an inference — CMSes
    put it there — so this is not the guess the module docstring rules out. It is
    still the *weakest* rung, because a URL can be re-slugged; it answers only when
    the page's own metadata did not.
    """
    for pattern in _URL_DATE:
        match = pattern.search(urlsplit(url or "").path)
        if match is None:
            continue
        year, month, day = match.groups()
        try:
            return datetime(int(year), int(month), int(day))
        except ValueError:
            continue  # 2026/02/31 — a slug that only looks like a date
    return None


def published_date(html: str, url: str | None = None) -> datetime | None:
    """The publication date the page states, or None.

    Cheapest and most explicit rung first, matching the escalation ladder the
    fetchers themselves use, and falling back to the URL path last. **Returns None
    rather than guessing.** A fabricated timestamp here is worse than no timestamp:
    `upsert._published_at` already refuses to invent one on the same grounds,
    because a wrong date does not degrade the merge tiebreak, it inverts it.
    """
    if not html:
        return date_from_url(url) if url else None
    ceiling = utcnow() + timedelta(days=_PLAUSIBLE_AHEAD_DAYS)

    candidates = (
        (_LD_DATE.search(html), "json-ld"),
        *((pattern.search(html), "meta") for pattern in _META_DATE),
        (_TIME_DATE.search(html), "time"),
    )
    for match, where in candidates:
        if match is None:
            continue
        when = parse_timestamp(unescape(match.group(1)))
        if when is None:
            continue
        if not (_PLAUSIBLE_FROM <= when <= ceiling):
            # Worth a line: a page whose only date is out of range is usually a
            # template default, and silently returning None makes that look like
            # a page that carries no date at all.
            log.debug("ignoring implausible %s date %s", where, when)
            continue
        return when
    return date_from_url(url) if url else None


# --- HTML -> text -----------------------------------------------------------

_DROP_TAGS = re.compile(
    r"<(script|style|noscript|nav|footer|aside|form|svg|iframe|template)\b.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_END = re.compile(
    r"</(p|div|section|article|h[1-6]|li|tr|blockquote|figcaption)\s*>", re.IGNORECASE
)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    """Reduce HTML to readable text, preserving paragraph breaks.

    Deliberately a small regex pass rather than a parser dependency. Its only job
    is to give the model prose to quote from; the evidence gate later checks each
    quote against this same text, so both sides see identical input.

    **Entities are decoded with `html.unescape`, not a hand-written table.** The
    table this replaces held nine *named* entities and no numeric ones, and the
    docstring justified that by saying both sides of the gate see the same text so
    markup precision cannot affect correctness. That reasoning holds for matching a
    quote and fails for reading a value out of one: `crawl._stated_in` re-parses
    quantities from the quote with `_MONEY_EXPR` and `norm_money_detail`, and those
    patterns require the unit to sit next to its number.

    Measured on SEC filings, where `&#160;` appears 17,469 times across 39
    documents and routinely separates a figure from its unit:

        "$ 13.5 &#160;billion"  ->  $13              (a 10^9 error)
        "393 &#160;MW"          ->  no match at all, the value silently lost

    News HTML mostly emits `&nbsp;`, which the old table did cover, so this stayed
    invisible until a numeric-entity-heavy source arrived. The `\\xa0` that
    unescaping produces is folded to a space by the whitespace pass below.
    """
    text = _COMMENTS.sub("", html)
    text = _DROP_TAGS.sub(" ", text)
    text = _BR.sub("\n", text)
    text = _BLOCK_END.sub("\n\n", text)
    text = _TAG.sub(" ", text)
    text = unescape(text)
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


# --- Implementations --------------------------------------------------------


class HttpxFetcher:
    """Plain HTTP. The default: fast, free, no browser."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def fetch(self, url: str) -> FetchResult:
        headers = {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        timeout = httpx.Timeout(self.settings.fetch_timeout_s, connect=10.0)
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, headers=headers
            ) as client:
                response = await client.get(url)
        except httpx.RequestError as exc:
            return FetchResult(url, False, error=str(exc), fetched_at=utcnow(), via="httpx")

        if response.status_code >= 400:
            return FetchResult(
                url,
                False,
                status=response.status_code,
                error=f"HTTP {response.status_code}",
                fetched_at=utcnow(),
                via="httpx",
            )

        # The date is read from the raw HTML, before `html_to_text` discards it.
        published = published_date(response.text, url)
        text = html_to_text(response.text)
        return FetchResult(
            url,
            bool(text.strip()),
            markdown=text,
            status=response.status_code,
            fetched_at=utcnow(),
            via="httpx",
            published_at=published,
        )


_MISSING_CURL_CFFI = (
    "curl_cffi is not installed. It is an optional extra:\n"
    '  python -m pip install -e ".[impersonate]"\n\n'
    "It is what reaches sites whose WAF fingerprints the TLS handshake rather "
    "than reading the User-Agent. The default httpx fetcher needs nothing."
)


class CurlCffiFetcher:
    """HTTP presenting a real browser's TLS fingerprint.

    **Why this exists, measured.** A growing share of hosts answer 403 to
    `httpx` and 200 to `curl` *for the same URL and the same User-Agent*. The
    block is on the TLS ClientHello — cipher and extension ordering, a JA3/JA4
    fingerprint — so it cannot be argued with by setting headers, and it is not
    a statement about who we are.

    On the five 403s from one live `enrich` run, every one returned 200 here:

        investor.atmeta.com   403 -> 200      (Meta/Blue Owl JV press release)
        entergy.com           403 -> 200      10,266 characters of prose
        lailluminator.com     403 -> 200
        bloomberg.com         403 -> 200      (paywall teaser, ~1.1k prose)
        electricchoice.com    403 -> 200

    **This is not defeating an access control, and the distinction is the whole
    justification.** Each of those hosts' `robots.txt` permits us — investor.
    atmeta.com says `Allow: /` with `Crawl-delay: 10`, entergy.com disallows only
    `/wp-admin/`. An over-broad WAF rule is not a policy, which is the same
    reasoning that already sanctions `--browser` for the operator sitemaps that
    serve curl and refuse httpx. Where a site genuinely *does* refuse crawlers —
    DataCenterDynamics' bot management — it stays discovery-only, and this
    changes nothing about that.

    It sits *below* the browser on the ladder because it is far cheaper: one
    request, no Chromium. It cannot render JavaScript, which is why the rung
    above it still earns its place — the Blue Owl release returns 200 here and
    only 106 characters of text, being an ASP.NET shell that assembles itself in
    the browser.
    """

    VIA = "curl_cffi"

    #: Which browser to impersonate. "chrome" tracks curl_cffi's current stable
    #: Chrome profile rather than pinning a version that ages out of relevance.
    IMPERSONATE = "chrome"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @staticmethod
    def available() -> bool:
        """Whether the optional extra is importable, for building the ladder."""
        try:
            import curl_cffi  # noqa: F401
        except ImportError:
            return False
        return True

    @staticmethod
    def ensure_available() -> None:
        """Raise now, naming the install command, rather than at first use."""
        try:
            import curl_cffi  # noqa: F401
        except ImportError as exc:
            raise MissingDependency(_MISSING_CURL_CFFI) from exc

    async def fetch(self, url: str) -> FetchResult:
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError as exc:
            raise MissingDependency(_MISSING_CURL_CFFI) from exc

        headers = {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            async with AsyncSession(impersonate=self.IMPERSONATE) as session:
                response = await session.get(
                    url,
                    headers=headers,
                    timeout=self.settings.fetch_timeout_s,
                    allow_redirects=True,
                )
        except Exception as exc:  # curl_cffi raises its own error hierarchy
            return FetchResult(url, False, error=str(exc), fetched_at=utcnow(), via=self.VIA)

        status = response.status_code
        if status >= 400:
            return FetchResult(
                url,
                False,
                status=status,
                error=f"HTTP {status}",
                fetched_at=utcnow(),
                via=self.VIA,
            )

        # Decode once: `_decode` is not free, and reading the date from a
        # differently-decoded copy could disagree with the text the gate checks.
        raw = _decode(response)
        text = html_to_text(raw)
        return FetchResult(
            url,
            bool(text.strip()),
            markdown=text,
            status=status,
            fetched_at=utcnow(),
            via=self.VIA,
            published_at=published_date(raw, url),
        )


def _decode(response: object) -> str:
    """Response body as text, without mangling smart quotes.

    curl_cffi's `.text` guessed wrong on entergy.com and turned the apostrophe in
    "Meta's" into replacement characters, which then reach the model and the
    evidence gate as literal mojibake. Trying the declared charset first and
    UTF-8 before falling back keeps the article's own punctuation intact.
    """
    body = getattr(response, "content", None)
    if not isinstance(body, bytes):
        return str(getattr(response, "text", "") or "")
    declared = (getattr(response, "encoding", None) or "").strip()
    for encoding in (declared, "utf-8", "cp1252", "latin-1"):
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


_MISSING_CRAWL4AI = (
    "crawl4ai is not installed. It is an optional extra:\n"
    '  python -m pip install -e ".[crawl]"\n'
    "  crawl4ai-setup            # downloads the Chromium build\n\n"
    "The default httpx fetcher needs neither."
)

#: Seconds to let a page finish assembling itself after load, before reading it.
#:
#: **Why this is not zero.** Without it the browser rung was worthless on exactly
#: the pages it exists for. Meta's investor-relations release announcing the Blue
#: Owl joint venture — the primary source for Hyperion's $27B, which the row has
#: been holding the superseded $10B against — returned HTTP 200 and **one
#: character** of text: a Q4 Inc. shell that fetches its own body after load. At
#: 3 seconds the same page yields 15,546 characters containing both "Blue Owl"
#: and the figure.
#:
#: Investor-relations pages are the worst case and the most valuable one, because
#: `investment_usd` is the field this database is thinnest on and a press release
#: states it in the first sentence. The cost is bounded: this rung only runs after
#: httpx *and* curl_cffi have both fallen short, which is a few pages per run.
JS_SETTLE_S: Final = 3.0

#: Tags the browser rung drops as furniture before reading a page.
#:
#: **`form` is deliberately NOT here, and that is the whole point of the list
#: existing.** ASP.NET WebForms wraps the entire document body in a single
#: `<form runat="server">` — which is what every Q4 Inc. investor-relations site
#: is built on — so excluding `form` deleted the article along with the search
#: box. Measured on Meta's Blue Owl press release, same page, same 3-second
#: settle, the only difference being this list:
#:
#:     excluded_tags with "form"     ->      1 character
#:     excluded_tags without "form"  ->  9,180 characters, "Blue Owl" present
#:
#: A stray search box costs a few tokens. Losing the body costs the citation.
_BOILERPLATE_TAGS: Final = ("nav", "footer", "aside", "script")


class Crawl4AIFetcher:
    """Headless-browser fetch via Crawl4AI. Optional, for pages httpx cannot get."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._crawler = None

    @staticmethod
    def ensure_available() -> None:
        """Raise now if the optional extra is missing.

        Constructing the fetcher cannot tell you this — the import lives in
        `__aenter__`, which does not run until the first page needs escalating,
        by which point the command has already fetched half its work. Callers
        offering `--browser` check this before starting so the failure lands on
        the flag rather than twenty pages in.
        """
        try:
            import crawl4ai  # noqa: F401
        except ImportError as exc:
            raise MissingDependency(_MISSING_CRAWL4AI) from exc

    async def __aenter__(self) -> Crawl4AIFetcher:
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig
        except ImportError as exc:
            raise MissingDependency(_MISSING_CRAWL4AI) from exc

        _assert_proactor_loop()
        self._crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
        await self._crawler.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._crawler is not None:
            await self._crawler.__aexit__(*exc_info)
            self._crawler = None

    async def fetch(self, url: str) -> FetchResult:
        if self._crawler is None:
            raise RuntimeError("Crawl4AIFetcher must be used as an async context manager")
        from crawl4ai import CacheMode, CrawlerRunConfig

        try:
            result = await self._crawler.arun(
                url,
                config=CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS,
                    page_timeout=int(self.settings.fetch_timeout_s * 1000),
                    excluded_tags=list(_BOILERPLATE_TAGS),
                    delay_before_return_html=JS_SETTLE_S,
                ),
            )
        except Exception as exc:
            return FetchResult(url, False, error=str(exc), fetched_at=utcnow(), via="crawl4ai")

        text = ""
        markdown = getattr(result, "markdown", None)
        if markdown is not None:
            # `fit_markdown` is the pruned, boilerplate-stripped variant, which
            # directly reduces LLM input cost. Fall back to the raw form.
            text = (
                getattr(markdown, "fit_markdown", "")
                or getattr(markdown, "raw_markdown", "")
                or (markdown if isinstance(markdown, str) else "")
            )
        if not text and getattr(result, "html", None):
            text = html_to_text(result.html)

        return FetchResult(
            url,
            bool(getattr(result, "success", False) and text.strip()),
            markdown=text,
            status=getattr(result, "status_code", None),
            error=getattr(result, "error_message", None),
            fetched_at=utcnow(),
            via="crawl4ai",
        )


def _assert_proactor_loop() -> None:
    """Playwright on Windows needs the Proactor loop to spawn a browser.

    It is the 3.13 default, but a stray `WindowsSelectorEventLoopPolicy` anywhere
    in the process turns this into an opaque `NotImplementedError` from deep
    inside asyncio's subprocess machinery.
    """
    if sys.platform != "win32":  # pragma: no cover - platform specific
        return
    loop = asyncio.get_event_loop_policy().get_event_loop()
    if type(loop).__name__ == "SelectorEventLoop":  # pragma: no cover
        raise RuntimeError(
            "Windows requires the Proactor event loop to launch a browser, but a "
            "SelectorEventLoop is active. Remove any "
            "asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy()) call."
        )


# --- Escalation and caching -------------------------------------------------


def cache_path(url: str, root: Path) -> Path:
    digest = hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()
    return root / f"{digest}.txt"


async def fetch_with_retry(fetcher: Fetcher, url: str, *, attempts: int = 3) -> FetchResult:
    """Retry a transient failure with exponential backoff."""
    settings = get_settings()
    last = FetchResult(url, False, error="not attempted", fetched_at=utcnow())
    for attempt in range(1, attempts + 1):
        last = await fetcher.fetch(url)
        if last.ok:
            return FetchResult(**{**last.__dict__, "attempts": attempt})
        if last.status in HARD_FAIL_STATUS or last.status in BROWSER_WORTHY_STATUS:
            break  # retrying the same tool will not help; escalate instead
        if last.status is not None and last.status not in RETRYABLE_STATUS:
            break
        if attempt < attempts:
            # Exponential with a little jitter, so a batch of URLs failing against
            # one host does not retry in lockstep.
            delay = min(
                settings.retry_backoff_base_s * 2**attempt, settings.retry_backoff_max_s
            ) * (1.0 + 0.25 * (attempt % 2))
            if delay > 0:
                log.warning("fetch failed for %s (%s); retrying in %.1fs", url, last.error, delay)
                await asyncio.sleep(delay)
    return FetchResult(**{**last.__dict__, "attempts": attempts})


def should_escalate(result: FetchResult) -> bool:
    """Whether a stronger fetcher is worth trying after this attempt.

    True for a status a different client might get past, and for an ok-but-thin
    body — which is what a JavaScript shell looks like. Any rung below the top
    may escalate, so the check is "did this rung fall short", not "was this
    httpx": the Blue Owl press release returns 200 and 106 characters through
    `curl_cffi` and needs the browser above it.
    """
    if result.via == "crawl4ai":
        return False  # nothing left to escalate to
    if result.status in BROWSER_WORTHY_STATUS:
        return True
    return result.ok and result.looks_thin


def escalation_ladder(settings: Settings | None = None, *, browser: bool = False) -> list[Fetcher]:
    """Escalation rungs available in this install, cheapest first.

    `curl_cffi` is included whenever it is installed, without a flag: it costs
    one ordinary request and recovers a whole class of 403s that are a WAF's TLS
    fingerprinting rather than anybody's policy. The browser stays behind
    `--browser` because Chromium is seconds and hundreds of megabytes, which is
    the same cost-proportionate ordering `enrich` uses for its harvesters.
    """
    settings = settings or get_settings()
    rungs: list[Fetcher] = []
    if CurlCffiFetcher.available():
        rungs.append(CurlCffiFetcher(settings))
    if browser:
        rungs.append(Crawl4AIFetcher(settings))
    return rungs


async def fetch_all(
    urls: list[str],
    *,
    fetcher: Fetcher | None = None,
    escalate: Fetcher | Sequence[Fetcher] | None = None,
    settings: Settings | None = None,
) -> list[FetchResult]:
    """Fetch concurrently, at most one request in flight per host.

    The per-host gate plus the politeness delay matter: fanning ten simultaneous
    requests at one newsroom is how a scraper earns a block.

    **`escalate` is a ladder, cheapest rung first**, and a single fetcher is still
    accepted so existing callers and test doubles are unaffected. Each rung is
    tried only because the one below it fell short, which is the same
    cost-proportionate ordering `enrich` uses for its harvesters:
    `curl_cffi` costs one ordinary request and clears a WAF that fingerprints
    TLS; Chromium costs seconds and renders JavaScript. The Blue Owl press
    release needs both — 403 from httpx, 200-but-106-characters from curl_cffi,
    real text only from the browser.

    **This is also where each rung is started and stopped.** A browser fetcher is
    an async context manager — it has to launch Chromium and tear it down — and
    only code already inside the event loop can enter it. Every caller here is
    synchronous (`crawl.run` owns the `asyncio.run`), so if this function did not
    do it, nobody could: `--browser` raised "must be used as an async context
    manager" the first time a page needed escalating, on every path that offered
    the flag.

    Rungs are entered lazily, on the first page that actually needs each one.
    Most runs never escalate, and launching a browser for them would cost several
    seconds and a Chromium process for nothing.
    """
    settings = settings or get_settings()
    primary = fetcher or HttpxFetcher(settings)
    gate = asyncio.Semaphore(settings.fetch_concurrency)
    host_gates: dict[str, asyncio.Semaphore] = {}

    if escalate is None:
        ladder: list[Fetcher] = []
    elif isinstance(escalate, (list, tuple)):
        ladder = list(escalate)
    else:
        ladder = [escalate]

    #: Started rungs, by index. `False` means starting that rung failed and we are
    #: not going to keep retrying — a missing Chromium fails the same way for
    #: every URL, and twenty identical tracebacks help nobody.
    started: dict[int, Fetcher | bool] = {}
    start_lock = asyncio.Lock()

    def host_gate(url: str) -> asyncio.Semaphore:
        host = urlsplit(url).netloc.lower()
        return host_gates.setdefault(host, asyncio.Semaphore(1))

    async def rung(index: int) -> Fetcher | None:
        """One escalation rung, started on first use. None if unavailable."""
        if index in started:
            return started[index] or None
        async with start_lock:
            if index not in started:
                candidate = ladder[index]
                enter = getattr(candidate, "__aenter__", None)
                if enter is None:
                    started[index] = candidate  # a plain fetcher, e.g. a test double
                else:
                    try:
                        started[index] = await enter()
                    except Exception as exc:
                        # Loud, once. The pages still get their plain-HTTP result,
                        # and `tracker queue --failed` will show what could not be
                        # read, so this degrades visibly rather than silently.
                        log.error(
                            "could not start the %s fetcher, continuing without it: %s",
                            type(candidate).__name__,
                            exc,
                        )
                        started[index] = False
        return started[index] or None

    async def one(url: str) -> FetchResult:
        async with gate, host_gate(url):
            result = await fetch_with_retry(primary, url)
            for index in range(len(ladder)):
                if not should_escalate(result):
                    break
                stronger = await rung(index)
                if stronger is None:
                    continue
                log.info(
                    "escalating %s to %s (status=%s)",
                    url,
                    getattr(stronger, "VIA", type(stronger).__name__),
                    result.status,
                )
                escalated = await fetch_with_retry(stronger, url, attempts=2)
                if escalated.ok:
                    result = escalated
            if settings.politeness_delay_s:
                await asyncio.sleep(settings.politeness_delay_s)
            return result

    # dict.fromkeys dedupes while preserving order, so a repeated URL in the
    # input file is fetched once.
    unique = list(dict.fromkeys(urls))
    try:
        return await asyncio.gather(*(one(u) for u in unique))
    finally:
        # Chromium does not exit on its own, and a leaked one survives the
        # command that started it. Every rung that was actually entered is closed,
        # not just the last one.
        for index, instance in started.items():
            if not instance:
                continue
            exit_ = getattr(ladder[index], "__aexit__", None)
            if exit_ is not None:
                with contextlib.suppress(Exception):
                    await exit_(None, None, None)


__all__ = [
    "BROWSER_WORTHY_STATUS",
    "HARD_FAIL_STATUS",
    "MIN_USEFUL_CHARS",
    "RETRYABLE_STATUS",
    "Crawl4AIFetcher",
    "CurlCffiFetcher",
    "FetchResult",
    "Fetcher",
    "HttpxFetcher",
    "MissingDependency",
    "cache_path",
    "escalation_ladder",
    "fetch_all",
    "fetch_with_retry",
    "html_to_text",
    "should_escalate",
]
