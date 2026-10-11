"""Whether a site's `robots.txt` lets this project read a page, asked before every fetch.

Only the headless-browser rung used to ask. The plain clients, which make almost
every request, relied on the hand-kept list of sites in `seed/feeds.toml`, and the
crawler's public page (`docs/crawler.md`) had to say so. Now every fetcher asks
here first: the article clients, the feed and sitemap client, the console's reader
view and the company-logo fetch. The browser rung keeps its own stricter reading.

**How an answer is read follows RFC 9309**, which is how the major crawlers read it:

- **2xx** — parsed, and its rules for our User-Agent apply. The token matched is the
  part before the first `/` (`dc-tracker`), so `User-agent: dc-tracker` names us
  and `User-agent: *` covers us.
- **4xx** — there is no usable file, so nothing is disallowed. A 403 here is a
  firewall answering, not a policy written for crawlers; the page request that
  follows meets the same firewall and fails on its own terms.
- **5xx, a timeout, or no answer** — the site cannot say, so everything is
  disallowed until it can. Remembered for `UNREACHABLE_TTL_S`, so one bad minute
  does not close a site for a day.

A file that was read is kept for `TTL_S`, per origin and per process, so a night
asks each site once rather than once per page. `Crawl-delay` is honoured too, up to
`MAX_CRAWL_DELAY_S`: the batch fetcher waits that long between two requests to the
same host when it is longer than the project's own one-second gap.

`settings.respect_robots` turns this off for the test suite, whose fake transports
answer every path with an article; tests about this module turn it back on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

#: How long a `robots.txt` that was read is believed.
TTL_S: Final[float] = 24 * 3600

#: How long "the site could not say" is believed before asking again.
UNREACHABLE_TTL_S: Final[float] = 30 * 60

#: Ceiling on an honoured `Crawl-delay`. A site asking for an hour between pages
#: gets one page a minute from us at most, which on a night's few pages per site
#: is the same thing.
MAX_CRAWL_DELAY_S: Final[float] = 60.0

#: The `error` a refused fetch carries, and the `via` that marks it, so the retry
#: and escalation logic can tell a refusal from a network failure.
REFUSED: Final[str] = "robots.txt does not permit this crawler to read this page"
VIA: Final[str] = "robots"

#: `(status, body)` for one GET; status None when nothing answered.
Getter = Callable[[str], tuple[int | None, str]]
AsyncGetter = Callable[[str], Awaitable[tuple[int | None, str]]]


@dataclass(frozen=True, slots=True)
class _Rules:
    #: None means everything is disallowed (the site could not say).
    parser: RobotFileParser | None
    expires: float


_cache: dict[str, _Rules] = {}
_lock = threading.Lock()


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def robots_url(url: str) -> str:
    return f"{origin(url)}/robots.txt"


def interpret(status: int | None, body: str) -> _Rules:
    """One answer for `/robots.txt`, read the RFC 9309 way (see the module docstring)."""
    now = time.monotonic()
    if status is not None and 200 <= status < 300:
        parser = RobotFileParser()
        parser.parse(body.splitlines())
        return _Rules(parser, now + TTL_S)
    if status is not None and 400 <= status < 500:
        parser = RobotFileParser()
        parser.parse([])
        return _Rules(parser, now + TTL_S)
    return _Rules(None, now + UNREACHABLE_TTL_S)


def _cached(url: str) -> _Rules | None:
    with _lock:
        rules = _cache.get(origin(url))
    if rules is not None and rules.expires > time.monotonic():
        return rules
    return None


def _store(url: str, rules: _Rules) -> _Rules:
    with _lock:
        _cache[origin(url)] = rules
    return rules


def _verdict(rules: _Rules, url: str, user_agent: str) -> bool:
    if rules.parser is None:
        return False
    return bool(rules.parser.can_fetch(user_agent, url))


def _is_robots_file(url: str) -> bool:
    return urlsplit(url).path == "/robots.txt"


def allowed(url: str, user_agent: str, get: Getter) -> bool:
    """Whether `url` may be fetched, reading the site's file through `get` if needed."""
    if _is_robots_file(url):
        return True
    rules = _cached(url) or _store(url, interpret(*get(robots_url(url))))
    return _verdict(rules, url, user_agent)


async def allowed_async(url: str, user_agent: str, get: AsyncGetter) -> bool:
    """`allowed`, for the async fetchers."""
    if _is_robots_file(url):
        return True
    rules = _cached(url)
    if rules is None:
        rules = _store(url, interpret(*await get(robots_url(url))))
    return _verdict(rules, url, user_agent)


def crawl_delay(url: str, user_agent: str) -> float:
    """The site's `Crawl-delay` for us, from a file already read; 0 when none."""
    rules = _cached(url)
    if rules is None or rules.parser is None:
        return 0.0
    try:
        delay = rules.parser.crawl_delay(user_agent)
    except (TypeError, ValueError):
        return 0.0
    if delay is None:
        return 0.0
    return min(float(delay), MAX_CRAWL_DELAY_S)


def clear() -> None:
    """Forget every file read, for tests."""
    with _lock:
        _cache.clear()


__all__ = [
    "MAX_CRAWL_DELAY_S",
    "REFUSED",
    "TTL_S",
    "UNREACHABLE_TTL_S",
    "VIA",
    "allowed",
    "allowed_async",
    "clear",
    "crawl_delay",
    "interpret",
    "robots_url",
]
