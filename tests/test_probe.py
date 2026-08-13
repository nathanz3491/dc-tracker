"""Finding feeds for publishers the record already says are worth reading.

No network anywhere here: every rung is driven by a fake fetcher, because the
thing worth testing is the ladder and the validation, not httpx.
"""

from __future__ import annotations

import asyncio

import pytest

from tracker.ingest import probe
from tracker.ingest.discover import FilterSpec
from tracker.ingest.fetch import FetchResult

SPEC = FilterSpec(topic=("data cent",), signal=("megawatt", " mw ", "campus"))

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Meta expands Richland Parish data center campus</title>
        <link>https://x.test/a</link></item>
  <item><title>A recipe for sourdough</title><link>https://x.test/b</link></item>
</channel></rss>"""

INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://x.test/sitemap/Article.xml</loc></sitemap>
  <sitemap><loc>https://x.test/sitemap/Company.xml</loc></sitemap>
</sitemapindex>"""

ARTICLES = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://x.test/meta-data-center-campus-900mw</loc></url>
  <url><loc>https://x.test/about-us</loc></url>
</urlset>"""


class FakeFetcher:
    """Serves canned documents. Everything else 404s."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        body = self.mapping.get(url)
        if body is None:
            return FetchResult(url, False, status=404, error="HTTP 404")
        return FetchResult(url, True, markdown=body)


def run_probe(mapping, host="x.test"):
    fetcher = FakeFetcher(mapping)
    result = asyncio.run(probe.probe_host(host, fetcher=fetcher, spec=SPEC))
    return result, fetcher


# --- the three rungs --------------------------------------------------------


def test_robots_txt_is_asked_first():
    """A `Sitemap:` line is the publisher declaring the answer."""
    result, fetcher = run_probe(
        {
            "https://x.test/robots.txt": "User-agent: *\nSitemap: https://x.test/news.xml\n",
            "https://x.test/news.xml": RSS,
        }
    )
    assert result.best is not None
    assert result.best.url == "https://x.test/news.xml"
    assert result.best.found_via == "robots.txt"
    assert fetcher.calls[0] == "https://x.test/robots.txt"


def test_well_known_paths_are_tried_when_robots_says_nothing():
    result, _ = run_probe({"https://x.test/rss.xml": RSS})
    assert result.best is not None
    assert result.best.url == "https://x.test/rss.xml"
    assert "well-known" in result.best.found_via


def test_the_homepage_link_is_the_last_rung():
    home = '<html><head><link rel="alternate" type="application/rss+xml" href="/f.xml">'
    result, _ = run_probe({"https://x.test": home, "https://x.test/f.xml": RSS})
    assert result.best is not None
    assert result.best.url == "https://x.test/f.xml"
    assert result.best.found_via == "<link rel=alternate>"


def test_a_relative_alternate_href_is_resolved():
    home = '<html><link rel="alternate" type="application/atom+xml" href="/deep/feed.xml">'
    result, _ = run_probe({"https://x.test": home, "https://x.test/deep/feed.xml": RSS})
    assert result.best is not None
    assert result.best.url == "https://x.test/deep/feed.xml"


def test_an_alternate_that_is_not_a_feed_is_ignored():
    """`rel=alternate` also marks translations and AMP pages."""
    home = '<html><link rel="alternate" hreflang="fr" href="/fr/">'
    result, fetcher = run_probe({"https://x.test": home})
    assert result.best is None
    assert "https://x.test/fr/" not in fetcher.calls


# --- sitemap indexes --------------------------------------------------------


def test_a_sitemap_index_is_followed_one_level():
    """The case that made this necessary.

    `datacenterfrontier.com/sitemap.xml` is a `<sitemapindex>`, which parses to
    zero entries and reads as "not a feed". Following it is what let the probe
    rediscover `sitemap/Article.xml` — the entry `seed/feeds.toml` already
    carries, and for exactly this reason.
    """
    result, _ = run_probe(
        {
            "https://x.test/sitemap.xml": INDEX,
            "https://x.test/sitemap/Article.xml": ARTICLES,
        }
    )
    assert result.best is not None
    assert result.best.url == "https://x.test/sitemap/Article.xml"
    assert "index" in result.best.found_via


def test_article_children_are_preferred_over_the_rest():
    """`index_children` puts article files first, so Company.xml is not fetched."""
    _, fetcher = run_probe(
        {
            "https://x.test/sitemap.xml": INDEX,
            "https://x.test/sitemap/Article.xml": ARTICLES,
        }
    )
    assert "https://x.test/sitemap/Company.xml" not in fetcher.calls


def test_a_nested_index_does_not_recurse_forever():
    self_referential = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://x.test/sitemap.xml</loc></sitemap>
      <sitemap><loc>https://x.test/deeper.xml</loc></sitemap>
    </sitemapindex>"""
    result, fetcher = run_probe(
        {"https://x.test/sitemap.xml": self_referential, "https://x.test/deeper.xml": INDEX}
    )
    assert result.best is None
    assert len(fetcher.calls) < 20


# --- validation -------------------------------------------------------------


def test_entries_are_scored_with_the_real_filter():
    """Two entries, one about a campus. Only that one would queue."""
    result, _ = run_probe({"https://x.test/rss.xml": RSS})
    assert result.best.entries == 2
    assert result.best.would_queue == 1
    assert result.best.hit_rate == pytest.approx(0.5)


def test_a_page_that_is_not_a_feed_is_not_a_find():
    """`/feed` on a site with none usually answers 200 with the homepage."""
    result, _ = run_probe({"https://x.test/feed": "<html><body>Welcome</body></html>"})
    assert result.hits == []
    assert result.note and "nothing parseable" in result.note


def test_a_host_serving_nothing_reports_how_hard_it_looked():
    result, _ = run_probe({})
    assert result.best is None
    assert result.tried > len(probe.CANDIDATE_PATHS)


def test_the_smaller_document_wins_a_tie():
    """A feed and a whole-site sitemap often queue the same articles.

    The feed costs one request per poll where the sitemap costs a walk, so on an
    equal `would_queue` the smaller document is the better buy.
    """
    big = probe.FeedHit(
        url="https://x.test/sitemap.xml", entries=5000, would_queue=3, found_via="a"
    )
    small = probe.FeedHit(url="https://x.test/rss.xml", entries=20, would_queue=3, found_via="b")
    result = probe.HostProbe(host="x.test", hits=[big, small])
    assert result.best is small


# --- candidate selection ----------------------------------------------------


def test_configured_hosts_covers_feeds_and_sitemaps():
    """A host reachable through its archive is already reachable."""
    known = probe.configured_hosts()
    assert "datacenterfrontier.com" in known


def test_the_toml_omits_the_record_line_for_an_explicitly_named_host():
    """`0 value(s) decided` would be a fact about the command line, not the site."""
    hit = probe.FeedHit(url="https://x.test/f.xml", entries=10, would_queue=4, found_via="robots")
    named = probe.HostProbe(host="x.test", hits=[hit])
    proposed = probe.HostProbe(host="x.test", hits=[hit], cited=29, decisive=15)
    assert "decided" not in named.as_toml()
    assert "15 value(s) decided from 29 citation(s)" in proposed.as_toml()
    assert 'url = "https://x.test/f.xml"' in named.as_toml()
