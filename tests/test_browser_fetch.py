"""The Playwright rung: when it runs, what it asks first, and that it reads a page
that only exists once its JavaScript has run.

The properties, in the order a mistake would cost:

* it opens nothing `robots.txt` does not permit, and a `robots.txt` it cannot read
  counts as not permitting — the project's line on deliberate blocks;
* it is the top rung: nothing escalates past it, and it is on the ladder whenever
  it is installed, with Crawl4AI only a fallback;
* it reads a JavaScript-built page that the HTTP rungs see as an empty shell.
"""

from __future__ import annotations

import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tracker.ingest import fetch
from tracker.ingest.fetch import FetchResult, PlaywrightFetcher, should_escalate


def run(coro):
    return asyncio.run(coro)


# --- the ladder ------------------------------------------------------------------


def test_nothing_escalates_past_the_browser():
    shell = FetchResult("https://x.example/a", True, markdown="short", status=200, via="playwright")
    assert should_escalate(shell) is False
    assert (
        should_escalate(FetchResult("https://x.example/a", False, status=403, via="playwright"))
        is False
    )


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


def _robots(monkeypatch, status, body=""):
    calls = []

    async def fake(url, settings):
        calls.append(url)
        return status, body

    monkeypatch.setattr(fetch, "_get_text", fake)
    return calls


def test_robots_decides_page_by_page_and_is_read_once_per_site(monkeypatch):
    calls = _robots(monkeypatch, 200, "User-agent: *\nDisallow: /private/\n")
    rung = PlaywrightFetcher()
    assert run(rung.permitted("https://site.example/news/a")) is True
    assert run(rung.permitted("https://site.example/private/b")) is False
    assert calls == ["https://site.example/robots.txt"]


def test_no_robots_file_means_no_restriction(monkeypatch):
    _robots(monkeypatch, 404)
    assert run(PlaywrightFetcher().permitted("https://site.example/a")) is True


@pytest.mark.parametrize("status", [403, 429, 503, None])
def test_a_robots_file_that_cannot_be_read_counts_as_a_refusal(monkeypatch, status):
    """The DataCenterDynamics case: a firewall that refuses even robots.txt."""
    _robots(monkeypatch, status)
    assert run(PlaywrightFetcher().permitted("https://site.example/a")) is False


def test_a_refused_page_is_never_opened(monkeypatch):
    _robots(monkeypatch, 200, "User-agent: *\nDisallow: /\n")

    class NoBrowser:
        async def new_context(self, **kwargs):
            raise AssertionError("the browser opened a page robots.txt refused")

    rung = PlaywrightFetcher()
    rung._browser = NoBrowser()
    result = run(rung.fetch("https://site.example/a"))
    assert result.ok is False and "robots.txt" in result.error and result.via == "playwright"


def test_robots_is_read_for_this_crawlers_name(monkeypatch):
    """Rules addressed to our user agent apply to us, not only the `*` ones."""
    from tracker.config import get_settings

    agent = get_settings().user_agent.split("/")[0]
    _robots(monkeypatch, 200, f"User-agent: {agent}\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert run(PlaywrightFetcher().permitted("https://site.example/a")) is False


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
