#!/usr/bin/env python
"""Re-run the survey behind `docs/government-sources.md`.

Four routes to bulk government data were tested and all four failed. Portals
change, so the finding has a shelf life — this script is how you check whether it
still holds instead of taking a document's word for it.

Nothing here writes to the database. It makes read-only requests to public
endpoints and prints what came back.

    python scripts/probe_government_sources.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = {
    "User-Agent": "dc-tracker/0.1 (+contact: set TRACKER_USER_AGENT)",
    "Accept": "application/json",
}
TIMEOUT = 35

#: Where the tracked capacity actually is, so the survey is aimed at the places
#: that matter rather than at whichever jurisdiction happens to publish most.
#: Measured 2 August 2026 over 221 projects and 96,532 MW.
MARKETS = "TX 34%, CO 12%, NM 9%, OH 8%, GA 8%, VA 8%"


def _get(url: str, timeout: int = TIMEOUT):
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _try(label: str, fn) -> None:
    started = time.time()
    try:
        detail = fn()
        print(f"  ok   {label:34} {time.time() - started:5.1f}s  {detail}")
    except urllib.error.HTTPError as exc:
        print(f"  {exc.code:<4} {label:34} {time.time() - started:5.1f}s")
    except Exception as exc:
        print(f"  err  {label:34} {time.time() - started:5.1f}s  {type(exc).__name__}")


def socrata() -> None:
    """Route 1: municipal open-data portals. One uniform API, wrong jurisdictions."""
    print("\n1. Socrata building permits (uniform SODA API across ~460 portals)")

    def catalog(query: str):
        url = "http://api.us.socrata.com/api/catalog/v1?only=dataset&limit=3&q="
        found = _get(url + urllib.parse.quote(query))
        domains = [r["metadata"]["domain"] for r in found["results"]]
        return f"{found['resultSetSize']:>4} datasets  {domains}"

    for query in (
        "Loudoun permits",
        "Prince William permits",
        "Virginia building permits",
        "Texas building permits",
        "Georgia building permits",
        "Arizona building permits",
    ):
        _try(query, lambda q=query: catalog(q))

    print("   and the hits that do exist, on a portal that has them:")

    def dallas():
        url = (
            "https://www.dallasopendata.com/resource/e7gq-4sah.json?$q="
            + urllib.parse.quote("data center")
            + "&$limit=3"
        )
        for row in _get(url):
            print(
                f"       ${row.get('value', '?')!s:>10}  "
                f"{str(row.get('work_description', ''))[:64]}"
            )
        return ""

    _try("Dallas permits: 'data center'", dallas)


def dockets() -> None:
    """Route 2: FERC and state utility commissions. No APIs; ASP.NET forms only."""
    print("\n2. FERC eLibrary and state PUC dockets")
    for label, url in [
        ("FERC eLibrary API", "https://elibrary.ferc.gov/eLibraryAPI/api/v1/documents/search"),
        ("Ohio PUCO DIS", "https://dis.puc.state.oh.us/DocumentSearch.aspx"),
        ("Virginia SCC docket search", "https://scc.virginia.gov/DocketSearch"),
        ("Texas PUC Interchange", "https://interchange.puc.texas.gov/search/filings/"),
    ]:

        def reach(u=url):
            request = urllib.request.Request(u, headers=UA)
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                kind = (response.headers.get("Content-Type") or "?").split(";")[0]
                return f"{kind} — machine-readable: {'yes' if 'json' in kind else 'NO'}"

        _try(label, reach)


def government_feeds() -> None:
    """Route 3: county and agency news feeds, scored by this project's own filter."""
    print("\n3. Government news feeds, filtered by tracker's own discovery rules")
    from tracker.ingest import discover

    _feeds, spec = discover.load_config()
    candidates = {
        "loudoun-county-va": "https://www.loudoun.gov/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml",
        "abilene-tx": "https://www.abilenetx.gov/RSSFeed.aspx?ModID=76&CID=All-0",
        "new-albany-oh": "https://www.newalbanyohio.org/feed/",
        "maricopa-county-az": "https://www.maricopa.gov/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml",
        "energy-gov": "https://www.energy.gov/rss/articles.xml",
        "eia-today-in-energy": "https://www.eia.gov/rss/todayinenergy.xml",
    }
    total_kept = total_seen = 0
    for name, url in candidates.items():
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                xml = response.read().decode("utf-8", "replace")
            entries = discover.parse_feed(
                xml, discover.FeedSpec(name=name, url=url, source_type="government_doc")
            )
        except Exception as exc:
            print(f"  err  {name:34} {type(exc).__name__}")
            continue
        kept = [c for c in entries if spec.matches(f"{c.title} {getattr(c, 'content', '')}")[0]]
        total_kept += len(kept)
        total_seen += len(entries)
        print(f"  ok   {name:34} {len(kept):>3} kept of {len(entries):>3}")
    print(f"       total: {total_kept} candidate(s) from {total_seen} entries")


def legistar() -> None:
    """Route 4: planning-commission agendas, where rezonings actually appear."""
    print("\n4. Legistar agenda API")
    for client in ("loudoun", "pwcva", "abilene", "phoenix", "columbus", "sanantonio"):

        def bodies(c=client):
            _get(f"https://webapi.legistar.com/v1/{c}/bodies?$top=1")
            return "responds"

        _try(client, bodies)


def main() -> int:
    print(__doc__.splitlines()[0])
    print(f"Aimed at the markets holding the capacity: {MARKETS}")
    socrata()
    dockets()
    government_feeds()
    legistar()
    print(
        "\nThe finding this reproduces: every uniform, machine-readable route either\n"
        "does not cover the jurisdictions that matter, or returns the wrong kind of\n"
        "document. See docs/government-sources.md for what to do instead."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
