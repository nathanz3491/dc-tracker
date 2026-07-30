"""Search-based discovery: find candidate articles by asking, not by waiting.

RSS discovery only sees what the feeds publish *now*, so a project announced two
years ago never appears. Search closes that gap — you can go looking for
"Microsoft data center Mount Pleasant megawatts" and get the article that names
the number.

Two halves, and the division between them is the important part:

* **MiniMax proposes what to look for.** Asked for candidate US data center
  projects, it returns names and locations from its training data. Those are
  *guesses*, and this module treats them as nothing more than search-query
  material. **Not one of them is ever written to the database.**
* **Search and extraction decide what is true.** A proposed project only becomes
  a row if a real search returns a real URL, the article fetches, and the evidence
  gate finds a verbatim quote for each value. If the model invented a project, the
  search finds nothing and nothing happens.

That asymmetry is what makes it safe to let a language model brainstorm here while
refusing to let it assert anything. Discovery is allowed to be speculative
precisely because storage is not.

Hits go through the same two-tier keyword filter as feed discovery and land in
`ingest_url` as `discovered`, so `tracker sync` crawls them with no special
casing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings
from tracker.ingest.discover import (
    Candidate,
    DiscoverReport,
    FilterSpec,
    load_config,
    queue_candidates,
)
from tracker.llm import Extractor, LLMError, parse_json_object
from tracker.models import utcnow

log = logging.getLogger(__name__)

SEARCH_KEY_HELP = """Google search is not configured.

Two values are needed, both free:

  1. An API key for the Custom Search JSON API
     https://developers.google.com/custom-search/v1/introduction
  2. A Programmable Search Engine id ("cx"), set to search the entire web
     https://programmablesearchengine.google.com

Add both to .env:

  TRACKER_GOOGLE_API_KEY=your-key
  TRACKER_GOOGLE_CSE_ID=your-cx-id

The free tier is 100 queries/day, about 1000 candidate URLs.

Without them you can still generate queries and run them yourself:
  tracker search --from-llm 20 --print-only
"""

#: How many project ideas to ask the model for at once. Larger batches drift into
#: repetition and invented names, which cost a search quota each.
LLM_BATCH = 25

#: Domains that are never worth queueing: aggregators, directories and
#: encyclopaedias carry no first-hand project reporting.
_SKIP_HOSTS = (
    "wikipedia.org",
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "reddit.com",
    "pinterest.com",
    "glassdoor.com",
    "indeed.com",
    "zillow.com",
    "loopnet.com",
    "crunchbase.com",
    "bloomberg.com/profile",
    "datacentermap.com",
    "baxtel.com",
)


class SearchError(RuntimeError):
    """Search is unconfigured or the provider refused the request."""


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    snippet: str = ""
    query: str = ""


@dataclass
class SearchReport:
    queries_run: int = 0
    hits: int = 0
    filtered: int = 0
    already_known: int = 0
    queued: int = 0
    quota_exhausted: bool = False
    errors: list[tuple[str, str]] = field(default_factory=list)

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("queries run", self.queries_run),
            ("search hits", self.hits),
            ("filtered out", self.filtered),
            ("already known", self.already_known),
            ("queued", self.queued),
        ]


class SearchProvider(Protocol):
    def search(self, query: str, *, limit: int) -> list[SearchHit]: ...


# --- Google Programmable Search --------------------------------------------


class GoogleCSEProvider:
    """The official Custom Search JSON API.

    Chosen over scraping result pages: scraping breaks Google's terms, is blocked
    in practice, and would contradict this project's decision not to defeat other
    sites' access controls either.
    """

    ENDPOINT = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.has_search_keys():
            raise SearchError(SEARCH_KEY_HELP)

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        params = {
            "key": self.settings.google_api_key.get_secret_value(),
            "cx": self.settings.google_cse_id,
            "q": query,
            "num": min(limit, 10),
            # US English results: the tracker is US-only, and this cuts the
            # non-US noise that otherwise costs an LLM call to discover.
            "gl": "us",
            "lr": "lang_en",
        }
        try:
            response = httpx.get(
                self.ENDPOINT, params=params, timeout=httpx.Timeout(30.0, connect=10.0)
            )
        except httpx.RequestError as exc:
            raise SearchError(f"search request failed: {exc}") from exc

        if response.status_code == 429:
            raise QuotaExhausted(
                "Google search quota exhausted (HTTP 429). The free tier allows 100 "
                "queries/day; it resets at midnight Pacific."
            )
        if response.status_code == 403:
            raise SearchError(
                "Google refused the request (HTTP 403). Usually the daily quota is "
                "spent, the Custom Search JSON API is not enabled for this key, or "
                f"the cx id is wrong.\n\nResponse: {response.text[:400]}"
            )
        if response.status_code >= 400:
            raise SearchError(f"search returned HTTP {response.status_code}: {response.text[:400]}")

        payload = response.json()
        return [
            SearchHit(
                url=item.get("link", ""),
                title=item.get("title", ""),
                snippet=item.get("snippet", ""),
                query=query,
            )
            for item in payload.get("items") or []
            if item.get("link")
        ]


class QuotaExhausted(SearchError):
    """The daily search allowance is spent. Not a failure worth retrying today."""


# --- Query generation -------------------------------------------------------

QUERY_PROMPT_SYSTEM = """You propose search queries for finding news about US data
center construction projects. You output ONLY a JSON object, no prose.

These are search LEADS, not facts. Nothing you output is stored. Every project is
verified against a fetched article before it is recorded, so a wrong guess costs
one search and is discarded. Breadth is therefore more useful than caution: prefer
naming many plausible projects over a few certain ones.

Rules:
1. US projects only.
2. Spread across operators AND states. Do not return ten Microsoft sites.
3. Include hyperscalers (Microsoft, Meta, Google, Amazon, Oracle), AI labs and
   their partners (OpenAI, xAI, Anthropic, Crusoe, CoreWeave), colocation
   operators (Equinix, Digital Realty, QTS, Vantage, Aligned, STACK, CyrusOne,
   Switch, Novva, EdgeConneX, DataBank, Prime), and the newer power-led entrants
   (TeraWulf, Applied Digital, Cipher Mining, Galaxy, Crusoe, Terawulf).
4. A good query names the operator, the place, and a fact-bearing word. Examples:
     "Meta Richland Parish Louisiana data center megawatts investment"
     "Vantage Data Centers Phoenix campus megawatts announced"
     "Oracle Abilene Texas data center construction gigawatt"
5. Return exactly the number requested."""

QUERY_PROMPT_USER = """Return $count search queries as JSON:

{"queries": ["<query>", ...]}

Each query must name a distinct US data center project or campus. Avoid any
project in this list, which is already tracked:
$known

Return the JSON object now."""


def generate_queries(
    extractor: Extractor,
    *,
    count: int = LLM_BATCH,
    known: list[str] | None = None,
) -> list[str]:
    """Ask the model for search queries. Returns query strings only.

    Nothing here is stored. If the model invents a project, the search returns
    nothing and the run simply moves on — which is why it is safe to let it
    speculate at this step.
    """
    import string

    known_text = "\n".join(f"- {k}" for k in (known or [])[:60]) or "- (nothing yet)"
    user = string.Template(QUERY_PROMPT_USER).safe_substitute(count=count, known=known_text)
    try:
        reply = extractor.complete(system=QUERY_PROMPT_SYSTEM, user=user, max_tokens=4096)
    except LLMError as exc:
        raise SearchError(f"could not generate queries: {exc}") from exc

    try:
        payload = parse_json_object(reply.text)
    except ValueError as exc:
        raise SearchError(f"model did not return a query list: {exc}") from exc

    raw = payload.get("queries") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise SearchError(f"expected a 'queries' list, got: {json.dumps(payload)[:200]}")

    queries: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in queries:
            queries.append(text)
    log.info("model proposed %d search quer(ies)", len(queries))
    return queries


def known_projects(session: Session) -> list[str]:
    """ "Company — Name (ST)" for every tracked project, to steer queries elsewhere."""
    from sqlalchemy import select

    from tracker.models import Project

    return [
        f"{p.company} — {p.name} ({p.state})"
        for p in session.scalars(select(Project).order_by(Project.company, Project.name))
    ]


# --- Filtering and queueing -------------------------------------------------


def is_useful_host(url: str) -> bool:
    """Reject aggregators and social sites, which carry no first-hand reporting."""
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    full = host + urlsplit(url).path.lower()
    return not any(skip in full for skip in _SKIP_HOSTS)


def hits_to_candidates(
    hits: list[SearchHit], spec: FilterSpec, *, report: SearchReport
) -> list[Candidate]:
    """Apply the same two-tier filter feed discovery uses.

    `topic_implied` is never set here: a search result could be from anywhere, so
    an article has to prove for itself that it is about a data center. The snippet
    participates in matching, which a feed entry does not have.
    """
    kept: list[Candidate] = []
    seen: set[str] = set()
    for hit in hits:
        report.hits += 1
        if not hit.url or hit.url in seen:
            continue
        seen.add(hit.url)
        if not is_useful_host(hit.url):
            report.filtered += 1
            continue
        haystack = f"{hit.title} {hit.snippet} {urlsplit(hit.url).path}"
        keep, reason = spec.matches(haystack)
        if not keep:
            log.debug("skip %s (%s)", hit.url, reason)
            report.filtered += 1
            continue
        kept.append(
            Candidate(
                url=hit.url,
                title=hit.title or hit.url,
                # Recorded in `ingest_url.feed` so a queued row shows where it came
                # from, and which query found it.
                feed=f"search:{hit.query}"[:120],
                published_at=None,
                source_type="general_media",
            )
        )
    return kept


def run(
    session: Session,
    queries: list[str],
    *,
    provider: SearchProvider,
    settings: Settings | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
) -> tuple[SearchReport, list[Candidate]]:
    """Run each query, filter the hits, and queue what survives."""
    settings = settings or get_settings()
    _, spec = load_config()
    report = SearchReport()
    run_id = run_id or utcnow().strftime("search-%Y%m%dT%H%M%S")

    all_hits: list[SearchHit] = []
    for query in queries[: settings.search_max_queries]:
        try:
            hits = provider.search(query, limit=settings.search_results_per_query)
        except QuotaExhausted as exc:
            report.quota_exhausted = True
            report.errors.append((query, str(exc)))
            log.warning("%s", exc)
            break  # every further query would fail the same way
        except SearchError as exc:
            report.errors.append((query, str(exc)))
            log.warning("query %r failed: %s", query, exc)
            continue
        report.queries_run += 1
        log.info("%r -> %d hit(s)", query, len(hits))
        all_hits.extend(hits)

    candidates = hits_to_candidates(all_hits, spec, report=report)

    # Reuses the feed-discovery queueing wholesale, including its rule that a URL
    # already in ingest_url is left completely alone.
    shim = DiscoverReport()
    queued = queue_candidates(session, candidates, run_id=run_id, report=shim)
    report.already_known = shim.already_known
    report.queued = shim.queued

    if dry_run:
        session.rollback()
        return report, candidates
    session.commit()
    return report, queued


__all__ = [
    "LLM_BATCH",
    "QUERY_PROMPT_SYSTEM",
    "SEARCH_KEY_HELP",
    "GoogleCSEProvider",
    "QuotaExhausted",
    "SearchError",
    "SearchHit",
    "SearchProvider",
    "SearchReport",
    "generate_queries",
    "hits_to_candidates",
    "is_useful_host",
    "known_projects",
    "run",
]
