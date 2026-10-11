"""A company's website and logo, for its page — no network: the fetch is a fake."""

from __future__ import annotations

import json

from tracker import company_logo
from tracker.company_logo import logo, website


def test_the_cited_domain_named_after_the_company_is_its_website():
    urls = [
        "https://www.datacenterdynamics.com/en/news/compass/",
        "https://www.compassdatacenters.com/news/red-oak/",
        "https://www.compassdatacenters.com/news/statesville/",
    ]
    assert website("compass datacenters", urls) == "compassdatacenters.com"


def test_a_first_word_of_four_letters_is_enough_when_nothing_matches_whole():
    assert website("crusoe energy", ["https://crusoe.ai/newsroom/abilene"]) == "crusoe.ai"


def test_no_matching_domain_is_no_website_rather_than_a_guess():
    """A publisher's domain is never mistaken for the company's own."""
    urls = ["https://www.datacenterdynamics.com/x", "https://example.com/y"]
    assert website("tract", urls) is None
    assert website("", urls) is None


def test_dot_com_wins_over_a_country_domain_cited_more():
    urls = ["https://acme.cn/a", "https://acme.cn/b", "https://acme.com/c"]
    assert website("acme", urls) == "acme.com"


def test_the_big_tenants_come_from_the_known_list():
    """Meta is cited through its landlords' releases, almost never its own site."""
    assert website("meta", []) == company_logo.KNOWN_SITES["meta"]


def test_a_logo_is_fetched_once_and_then_served_from_disk(tmp_path):
    calls = []

    def fetch(domain, user_agent, timeout):
        calls.append((domain, user_agent))
        return b"\x89PNG fake", "image/png"

    first = logo("acme", "acme.com", cache=tmp_path, user_agent="ua", fetch=fetch)
    second = logo("acme", "acme.com", cache=tmp_path, user_agent="ua", fetch=fetch)
    assert first == second == (b"\x89PNG fake", "image/png")
    assert calls == [("acme.com", "ua")]


def test_no_logo_is_remembered_too(tmp_path):
    """A site with no findable icon is not asked again on every page view."""
    calls = []

    def fetch(domain, user_agent, timeout):
        calls.append(domain)

    assert logo("acme", "acme.com", cache=tmp_path, user_agent="ua", fetch=fetch) is None
    assert logo("acme", "acme.com", cache=tmp_path, user_agent="ua", fetch=fetch) is None
    assert calls == ["acme.com"]


def test_a_new_website_asks_again(tmp_path):
    calls = []

    def fetch(domain, user_agent, timeout):
        calls.append(domain)
        return b"<svg/>", "image/svg+xml"

    logo("acme", "acme.cn", cache=tmp_path, user_agent="ua", fetch=fetch)
    logo("acme", "acme.com", cache=tmp_path, user_agent="ua", fetch=fetch)
    assert calls == ["acme.cn", "acme.com"]


def test_an_expired_entry_asks_again(tmp_path):
    calls = []

    def fetch(domain, user_agent, timeout):
        calls.append(domain)

    logo("acme", "acme.com", cache=tmp_path, user_agent="ua", fetch=fetch)
    meta = tmp_path / "acme.json"
    record = json.loads(meta.read_text(encoding="utf-8"))
    record["at"] -= (company_logo.TTL_DAYS + 1) * 86_400
    meta.write_text(json.dumps(record), encoding="utf-8")
    logo("acme", "acme.com", cache=tmp_path, user_agent="ua", fetch=fetch)
    assert calls == ["acme.com", "acme.com"]


def test_the_touch_icon_is_tried_before_the_favicon():
    html = """<head>
      <link rel="icon" href="/small.png">
      <link rel="apple-touch-icon" href="/big.png">
    </head>"""
    found = company_logo._icon_candidates(html, "https://acme.com/")
    assert found == [
        "https://acme.com/big.png",
        "https://acme.com/small.png",
        "https://acme.com/favicon.ico",
    ]
