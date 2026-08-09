"""The Wikipedia reference miner: URL recognition, unwrapping, and filtering.

Offline throughout — `links_for` is injected, so no MediaWiki API is called.
The link fixtures are the real external-links list the Hyperion Data Center
article returned on 2026-08-08, trimmed: they exercise every branch the live
data does (a map tool, an archive wrapper plus its direct twin, an IR press
release, opaque news slugs, a corporate registry).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tracker.ingest import wiki
from tracker.ingest.discover import load_config
from tracker.ingest.wiki import (
    external_links,
    is_wikipedia,
    mine,
    page_title,
    unwrap_archive,
)


@pytest.fixture
def spec():
    return load_config()[1]


# --- Recognition -------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://en.wikipedia.org/wiki/Hyperion_Data_Center", True),
        ("https://de.wikipedia.org/wiki/Rechenzentrum", True),
        ("https://en.wikipedia.org/w/index.php?title=X", True),
        ("https://wikipedia.org.evil.example/wiki/X", False),
        ("https://www.datacenterknowledge.com/article", False),
    ],
)
def test_is_wikipedia(url, expected):
    assert is_wikipedia(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://en.wikipedia.org/wiki/Hyperion_Data_Center", "Hyperion_Data_Center"),
        ("https://en.wikipedia.org/wiki/Stargate%20LLC", "Stargate LLC"),
        ("https://en.wikipedia.org/wiki/Category:Data_centers", None),
        ("https://en.wikipedia.org/wiki/File:Campus.jpg", None),
        ("https://en.wikipedia.org/w/index.php?title=X", None),
        ("https://en.wikipedia.org/", None),
    ],
)
def test_page_title(url, expected):
    assert page_title(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://web.archive.org/web/20251011004545/https://www.bloomberg.com/news/a",
            "https://www.bloomberg.com/news/a",
        ),
        # Not a wrapper: returned untouched.
        ("https://www.cnbc.com/2026/07/13/meta-louisiana.html", None),
        # An /details/ item is not a /web/ wrapper.
        ("https://archive.org/details/some-report", None),
    ],
)
def test_unwrap_archive(url, expected):
    assert unwrap_archive(url) == (expected or url)


# --- Mining ------------------------------------------------------------------

#: The Hyperion article's real reference list, trimmed to the shapes that matter.
HYPERION_LINKS = [
    "https://geohack.toolforge.org/geohack.php?pagename=Hyperion_Data_Center",
    "https://www.bbc.co.uk/news/articles/c1e02vx55wpo",
    "https://apnews.com/article/musk-meta-artificial-intelligence-data-center-1cf3",
    "https://web.archive.org/web/20251011004545/https://www.bloomberg.com/news/newsletters/2025-08-24/why-is-manhattan-being-crushed-by-this-giant-meta-data-center",
    "https://www.bloomberg.com/news/newsletters/2025-08-24/why-is-manhattan-being-crushed-by-this-giant-meta-data-center",
    "https://investor.atmeta.com/investor-news/press-release-details/2025/Meta-Announces-Joint-Venture-to-Develop-Hyperion-Data-Center/default.aspx",
    "https://www.cnbc.com/2026/07/13/meta-louisiana-data-center-investment-reaches-50-billion-amid-ai-push.html",
    "https://opencorporates.com/companies/us_la/46043373Q",
    "//protocol.relative.example/data-center-campus",
]


def links_for_fixture(url, *, settings=None):
    return list(HYPERION_LINKS)


def test_mine_keeps_the_primary_sources(spec):
    kept = mine(
        ["https://en.wikipedia.org/wiki/Hyperion_Data_Center"], spec, links_for=links_for_fixture
    )
    urls = [c.url for c in kept]
    # The one lead a search snippet can never surface: the operator's IR release.
    assert any("investor.atmeta.com" in u for u in urls)
    assert any("cnbc.com" in u for u in urls)
    # The apnews slug names the topic in its own words.
    assert any("apnews.com" in u for u in urls)


def test_mine_drops_the_plumbing_and_the_registry(spec):
    kept = mine(
        ["https://en.wikipedia.org/wiki/Hyperion_Data_Center"], spec, links_for=links_for_fixture
    )
    urls = [c.url for c in kept]
    assert not any("toolforge.org" in u for u in urls), "a map tool is not a source"
    assert not any("opencorporates.com" in u for u in urls), "no keyword in the slug"
    assert not any("bbc.co.uk" in u for u in urls), "an opaque slug cannot be judged"


def test_mine_unwraps_archive_links_and_dedupes_against_the_direct_twin(spec):
    kept = mine(
        ["https://en.wikipedia.org/wiki/Hyperion_Data_Center"], spec, links_for=links_for_fixture
    )
    bloomberg = [c.url for c in kept if "bloomberg.com" in c.url]
    assert bloomberg == [
        "https://www.bloomberg.com/news/newsletters/2025-08-24/"
        "why-is-manhattan-being-crushed-by-this-giant-meta-data-center"
    ], "the wrapper and the direct link are one article, keyed on the real URL"


def test_mine_normalizes_protocol_relative_links(spec):
    kept = mine(
        ["https://en.wikipedia.org/wiki/Hyperion_Data_Center"], spec, links_for=links_for_fixture
    )
    assert "https://protocol.relative.example/data-center-campus" in [c.url for c in kept]


def test_mine_labels_the_feed_with_the_article(spec):
    kept = mine(
        ["https://en.wikipedia.org/wiki/Hyperion_Data_Center"], spec, links_for=links_for_fixture
    )
    assert kept and all(c.feed == "wikipedia:Hyperion_Data_Center" for c in kept)


def test_mine_caps_links_per_page(spec, monkeypatch):
    monkeypatch.setattr(wiki, "MAX_LINKS_PER_PAGE", 2)
    many = [f"https://outlet{i}.example/meta-data-center-{i}-megawatt" for i in range(10)]
    kept = mine(
        ["https://en.wikipedia.org/wiki/X"], spec, links_for=lambda url, settings=None: many
    )
    assert len(kept) == 2


def test_mine_skips_non_article_pages(spec):
    calls: list[str] = []

    def spy(url, *, settings=None):
        calls.append(url)
        return []

    mine(["https://en.wikipedia.org/wiki/Category:Data_centers"], spec, links_for=spy)
    assert calls == [], "a Category: page has no references worth asking for"


# --- The API call ------------------------------------------------------------


@respx.mock
def test_external_links_reads_the_parse_api():
    respx.get("https://en.wikipedia.org/w/api.php").mock(
        return_value=httpx.Response(
            200,
            json={"parse": {"title": "X", "externallinks": ["https://a.example/one"]}},
        )
    )
    assert external_links("https://en.wikipedia.org/wiki/X") == ["https://a.example/one"]


@respx.mock
def test_external_links_swallows_api_failure():
    """Mining is a bonus on a search hit; a run must not die on a wiki hiccup."""
    respx.get("https://en.wikipedia.org/w/api.php").mock(return_value=httpx.Response(500))
    assert external_links("https://en.wikipedia.org/wiki/X") == []
