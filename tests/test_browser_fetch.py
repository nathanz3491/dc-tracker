"""The Playwright rung: when it runs, what it asks first, and that it reads a page
that only exists once its JavaScript has run.

The properties, in the order a mistake would cost:

* it opens nothing `robots.txt` does not permit, and it reads that file exactly as
  every other rung does (`tracker.robots`, RFC 9309): once per site per run, a 4xx
  as no file, a 5xx, a 429 or no answer as "cannot say";
* its refusal is a robots refusal, never retried and never escalated;
* it is on the ladder whenever it is installed, with Crawl4AI only a fallback;
* it reads a JavaScript-built page that the HTTP rungs see as an empty shell.
"""

from __future__ import annotations

import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tracker import robots
from tracker.config import get_settings
from tracker.ingest import fetch
from tracker.ingest.fetch import FetchResult, PlaywrightFetcher, should_escalate


def run(coro):
    return asyncio.run(coro)


# --- the ladder ------------------------------------------------------------------


def test_the_browser_escalates_to_deepseeks_fetch_and_nothing_past_that():
    """Since 2026-10-19 DeepSeek's Fetch tool is the rung after the browser, for a page
    the browser was refused or got only a shell of."""
    shell = FetchResult("https://x.example/a", True, markdown="short", status=200, via="playwright")
    assert should_escalate(shell) is True
    blocked = FetchResult("https://x.example/a", False, status=403, via="playwright")
    assert should_escalate(blocked) is True
    fetched = FetchResult(
        "https://x.example/a", True, markdown="short", status=200, via="deepseek-fetch"
    )
    assert should_escalate(fetched) is False


def test_the_browser_is_on_the_ladder_whenever_it_is_installed(monkeypatch):
    monkeypatch.setattr(fetch.CurlCffiFetcher, "available", staticmethod(lambda: True))
    monkeypatch.setattr(PlaywrightFetcher, "available", staticmethod(lambda: True))
    kinds = [type(r).__name__ for r in fetch.escalation_ladder()]
    assert kinds == ["CurlCffiFetcher", "PlaywrightFetcher"], "no flag needed, no Crawl4AI"
    assert [type(r).__name__ for r in fetch.escalation_ladder(browser=True)] == kinds


def test_crawl4ai_is_only_a_fallback_when_asked_for(monkeypatch):
    monkeypatch.setattr(fetch.CurlCffiFetcher, "available", staticmethod(lambda: False))
    monkeypatch.setattr(PlaywrightFetcher, "available", staticmethod(lambda: False))
    assert fetch.escalation_ladder() == []
    assert [type(r).__name__ for r in fetch.escalation_ladder(browser=True)] == ["Crawl4AIFetcher"]


# --- robots.txt ------------------------------------------------------------------


@pytest.fixture
def respect(monkeypatch):
    """The suite turns robots.txt off for its fake sites; these tests are about it."""
    monkeypatch.setenv("TRACKER_RESPECT_ROBOTS", "1")
    get_settings.cache_clear()
    robots.clear()
    yield
    robots.clear()
    get_settings.cache_clear()


def _robots(monkeypatch, status, body=""):
    calls = []

    async def fake(url, settings):
        calls.append(url)
        return status, body

    monkeypatch.setattr(fetch, "_get_text", fake)
    return calls


def test_robots_decides_page_by_page_and_is_read_once_per_site(monkeypatch, respect):
    calls = _robots(monkeypatch, 200, "User-agent: *\nDisallow: /private/\n")
    rung = PlaywrightFetcher()
    assert run(rung.permitted("https://site.example/news/a")) is True
    assert run(rung.permitted("https://site.example/private/b")) is False
    assert calls == ["https://site.example/robots.txt"]


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_a_4xx_robots_file_means_no_restriction_as_for_every_rung(monkeypatch, respect, status):
    """RFC 9309, the reading the plain clients and DeepSeek's fetch always had. The
    browser alone used to refuse here, so a page it would not open was then sent to
    DeepSeek, which asked the shared rules and was allowed."""
    _robots(monkeypatch, status)
    assert run(PlaywrightFetcher().permitted("https://site.example/a")) is True


@pytest.mark.parametrize("status", [429, 500, 503, None])
def test_a_site_that_cannot_say_or_asks_for_fewer_requests_is_left_alone(
    monkeypatch, respect, status
):
    _robots(monkeypatch, status)
    assert run(PlaywrightFetcher().permitted("https://site.example/a")) is False


def test_one_reading_per_site_serves_every_rung(monkeypatch, respect):
    """What a rung below read is what the browser goes by: no second request."""
    calls = _robots(monkeypatch, 200, "User-agent: *\nDisallow: /private/\n")
    settings = get_settings()

    async def a_lower_rung_asks() -> bool:
        return await fetch.permitted(
            "https://site.example/news/a", settings, lambda u: fetch._get_text(u, settings)
        )

    assert run(a_lower_rung_asks()) is True
    assert run(PlaywrightFetcher().permitted("https://site.example/private/b")) is False
    assert calls == ["https://site.example/robots.txt"]


def test_a_refused_page_is_never_opened_and_the_refusal_is_not_retried(monkeypatch, respect):
    """On 2026-10-19 each of the browser's refusals was retried after a backoff, as
    though robots.txt might say something else a few seconds later."""
    _robots(monkeypatch, 200, "User-agent: *\nDisallow: /\n")

    class NoBrowser:
        async def new_context(self, **kwargs):
            raise AssertionError("the browser opened a page robots.txt refused")

    rung = PlaywrightFetcher()
    rung._browser = NoBrowser()
    result = run(fetch.fetch_with_retry(rung, "https://site.example/a", attempts=2))
    assert result.ok is False and result.error == robots.REFUSED
    assert result.via == robots.VIA and result.attempts == 0
    assert should_escalate(result) is False


def test_robots_is_read_for_this_crawlers_name(monkeypatch, respect):
    """Rules addressed to our user agent apply to us, not only the `*` ones."""
    agent = get_settings().user_agent.split("/")[0]
    _robots(monkeypatch, 200, f"User-agent: {agent}\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert run(PlaywrightFetcher().permitted("https://site.example/a")) is False


def test_one_rung_serves_one_event_loop_after_another(monkeypatch, respect):
    """`enrich` builds the ladder once and every row's crawl is its own `asyncio.run`.

    The robots lock used to be made in `__init__`, so it belonged to the first loop
    that waited on it, and the next row's concurrent pages died with "bound to a
    different event loop" — five pages on the 2026-10-13 night, read by nothing. The
    rung keeps no lock of its own now: the reading is the run's, in `tracker.robots`,
    behind a thread lock no event loop owns.
    """

    async def slow(url, settings):
        await asyncio.sleep(0)  # a real read yields, so the second page waits on the lock
        return 200, "User-agent: *\nAllow: /\n"

    monkeypatch.setattr(fetch, "_get_text", slow)
    rung = PlaywrightFetcher()

    async def two_at_once(site):
        return await asyncio.gather(
            rung.permitted(f"https://{site}.example/a"), rung.permitted(f"https://{site}.example/b")
        )

    assert run(two_at_once("first")) == [True, True]
    assert run(two_at_once("second")) == [True, True]


# --- a real browser, when one is installed ------------------------------------------

_SHELL = b"""<!doctype html><html><head><title>t</title></head><body>
<nav>Home | About</nav>
<div id="root"></div>
<script>
  document.getElementById("root").innerHTML =
    "<p>" + "Acme Data Centers announced a 300 MW campus in Racine County, Wisconsin, "
    + "with the first building expected online in 2027 and a total investment of "
    + "$2.5 billion across the site, according to the company. </p>".repeat(3);
</script>
</body></html>"""


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/robots.txt":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_SHELL)

    def log_message(self, *args):
        pass


#: Where a developer's Chrome shell is, decided here rather than by `home()`,
#: which every test points at a temporary directory of its own.
_BROWSERS = Path(
    os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    or Path(__file__).resolve().parents[1] / ".cache" / "ms-playwright"
)


def _installed() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return any(_BROWSERS.glob("chromium_headless_shell-*"))


@pytest.mark.skipif(not _installed(), reason="the [browser] extra is not installed")
def test_a_javascript_page_is_read_in_full(monkeypatch):
    """What the HTTP rungs see is `<div id="root"></div>`; the browser sees the article."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(_BROWSERS))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/release"
    try:

        async def read():
            async with PlaywrightFetcher() as rung:
                return await rung.fetch(url)

        result = run(read())
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert result.ok and result.via == "playwright", result.error
    assert "300 MW campus in Racine County" in result.markdown
    assert "Home | About" not in result.markdown, "navigation is dropped before reading"
