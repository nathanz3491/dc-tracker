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
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Protocol
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

    @property
    def sha1(self) -> str:
        return hashlib.sha1(self.markdown.encode("utf-8"), usedforsecurity=False).hexdigest()

    @property
    def looks_thin(self) -> bool:
        return len(self.markdown.strip()) < MIN_USEFUL_CHARS


class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...


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

        text = html_to_text(response.text)
        return FetchResult(
            url,
            bool(text.strip()),
            markdown=text,
            status=response.status_code,
            fetched_at=utcnow(),
            via="httpx",
        )


_MISSING_CRAWL4AI = (
    "crawl4ai is not installed. It is an optional extra:\n"
    '  python -m pip install -e ".[crawl]"\n'
    "  crawl4ai-setup            # downloads the Chromium build\n\n"
    "The default httpx fetcher needs neither."
)


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
                    excluded_tags=["nav", "footer", "aside", "form", "script"],
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
    """Whether a browser is worth trying after a plain HTTP attempt."""
    if result.via != "httpx":
        return False
    if result.status in BROWSER_WORTHY_STATUS:
        return True
    return result.ok and result.looks_thin


async def fetch_all(
    urls: list[str],
    *,
    fetcher: Fetcher | None = None,
    escalate: Fetcher | None = None,
    settings: Settings | None = None,
) -> list[FetchResult]:
    """Fetch concurrently, at most one request in flight per host.

    The per-host gate plus the politeness delay matter: fanning ten simultaneous
    requests at one newsroom is how a scraper earns a block.

    **This is also where the escalation fetcher is started and stopped.** A
    browser fetcher is an async context manager — it has to launch Chromium and
    tear it down — and only code already inside the event loop can enter it. Every
    caller here is synchronous (`crawl.run` owns the `asyncio.run`), so if this
    function did not do it, nobody could: `--browser` raised "must be used as an
    async context manager" the first time a page needed escalating, on every path
    that offered the flag.

    It is entered lazily, on the first page that actually needs it. Most runs
    never escalate, and launching a browser for them would cost several seconds
    and a Chromium process for nothing.
    """
    settings = settings or get_settings()
    primary = fetcher or HttpxFetcher(settings)
    gate = asyncio.Semaphore(settings.fetch_concurrency)
    host_gates: dict[str, asyncio.Semaphore] = {}

    #: The started escalation fetcher, once something has needed it. `False`
    #: means starting it failed and we are not going to keep retrying — a missing
    #: Chromium fails the same way for every URL, and twenty identical tracebacks
    #: help nobody.
    started: Fetcher | bool | None = None
    start_lock = asyncio.Lock()

    def host_gate(url: str) -> asyncio.Semaphore:
        host = urlsplit(url).netloc.lower()
        return host_gates.setdefault(host, asyncio.Semaphore(1))

    async def browser() -> Fetcher | None:
        """The escalation fetcher, started on first use. None if unavailable."""
        nonlocal started
        if started is not None:
            return started or None
        async with start_lock:
            if started is None:
                enter = getattr(escalate, "__aenter__", None)
                if enter is None:
                    started = escalate  # a plain fetcher, e.g. a test double
                else:
                    try:
                        started = await enter()
                    except Exception as exc:
                        # Loud, once. The pages still get their plain-HTTP result,
                        # and `tracker queue --failed` will show what could not be
                        # read, so this degrades visibly rather than silently.
                        log.error(
                            "could not start the browser fetcher, continuing without it: %s", exc
                        )
                        started = False
        return started or None

    async def one(url: str) -> FetchResult:
        async with gate, host_gate(url):
            result = await fetch_with_retry(primary, url)
            if escalate is not None and should_escalate(result):
                browser_fetcher = await browser()
                if browser_fetcher is not None:
                    log.info("escalating %s to a browser (status=%s)", url, result.status)
                    escalated = await fetch_with_retry(browser_fetcher, url, attempts=2)
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
        # command that started it.
        exit_ = getattr(escalate, "__aexit__", None)
        if started and exit_ is not None:
            with contextlib.suppress(Exception):
                await exit_(None, None, None)


__all__ = [
    "BROWSER_WORTHY_STATUS",
    "HARD_FAIL_STATUS",
    "MIN_USEFUL_CHARS",
    "RETRYABLE_STATUS",
    "Crawl4AIFetcher",
    "FetchResult",
    "Fetcher",
    "HttpxFetcher",
    "MissingDependency",
    "cache_path",
    "fetch_all",
    "fetch_with_retry",
    "html_to_text",
    "should_escalate",
]
