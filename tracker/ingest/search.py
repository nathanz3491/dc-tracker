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

SEARCH_KEY_HELP = """Web search is not configured. Pick one backend and add its key to .env.

  Brave   — independent index, one key, no cloud account. 2000 queries/month free.
            https://api-dashboard.search.brave.com
            TRACKER_BRAVE_API_KEY=your-key

  Google  — Custom Search JSON API. Two values, 100 queries/day free.
            https://developers.google.com/custom-search/v1/introduction
            https://programmablesearchengine.google.com  (set it to the whole web)
            TRACKER_GOOGLE_API_KEY=your-key
            TRACKER_GOOGLE_CSE_ID=your-cx-id

  Serper  — Google results over a simpler API. 2500 free credits.
            https://serper.dev
            TRACKER_SERPER_API_KEY=your-key

Whichever you add is picked up automatically. To pin one explicitly:

  TRACKER_SEARCH_PROVIDER=brave

Without any key you can still generate queries and run them yourself:
  tracker search --from-llm 20 --print-only
"""

#: Why "bing" is not one of the options. Raised by name so an operator who asks
#: for it gets the reason rather than "unknown provider".
BING_RETIRED_HELP = """There is no Bing backend, because the Bing Search API no longer exists.

Microsoft retired the standalone Bing Search APIs on 2025-08-11 — their own
documentation now carries `is_retired: true` — so no new subscription key can be
created for them.

The successor, Grounding with Bing Search in Azure AI Foundry, is licensed for
grounding a model's reply, not for building a stored database of facts and
citations. That is precisely what this tool does, so it is the wrong instrument
here regardless of the plumbing.

Brave is the closest drop-in: an independent index (not a Google or Bing
reseller), a free tier, one header, no cloud account.

  https://api-dashboard.search.brave.com
  TRACKER_BRAVE_API_KEY=your-key
  TRACKER_SEARCH_PROVIDER=brave
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
        # `has_search_keys` now means "some backend is configured", so it would let
        # this class start on a Brave-only setup and fail at request time.
        if not self.settings.has_google_keys():
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


class BraveProvider:
    """Brave Search API.

    The recommended alternative now that Bing's API is retired: an independent
    index rather than a Google or Bing reseller, so it genuinely widens coverage
    instead of re-asking the same engine.

    Two quirks worth knowing. The free tier is rate limited to roughly one query a
    second and answers HTTP 429 the instant you exceed it, which is a *pacing*
    problem rather than an exhausted quota — so it is retried after a pause before
    being treated as fatal. And `count` is capped at 20.
    """

    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    #: Seconds to wait out the free tier's per-second limit before giving up on a
    #: query. Brave does not always send Retry-After, so this is a fixed pause.
    RATE_LIMIT_PAUSE_S = 1.5

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.has_brave_key():
            raise SearchError(SEARCH_KEY_HELP)

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        import time

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.settings.brave_api_key.get_secret_value(),
        }
        params = {
            "q": query,
            "count": min(limit, 20),
            # US English: the tracker is US-only, and this cuts the non-US noise
            # that otherwise costs an LLM call to discover and discard.
            "country": "us",
            "search_lang": "en",
            "result_filter": "web",
        }

        for attempt in (1, 2):
            try:
                response = httpx.get(
                    self.ENDPOINT,
                    params=params,
                    headers=headers,
                    timeout=httpx.Timeout(30.0, connect=10.0),
                )
            except httpx.RequestError as exc:
                raise SearchError(f"search request failed: {exc}") from exc

            if response.status_code == 429 and attempt == 1:
                # Almost always the one-query-per-second free tier rather than the
                # monthly allowance, so pace and retry once before calling it spent.
                time.sleep(self.RATE_LIMIT_PAUSE_S)
                continue
            break

        if response.status_code == 429:
            raise QuotaExhausted(
                "Brave rate limit still hit after pausing (HTTP 429). The free tier "
                "allows about one query per second and 2000 per month."
            )
        if response.status_code in (401, 403):
            raise SearchError(
                "Brave refused the request (HTTP "
                f"{response.status_code}). Usually TRACKER_BRAVE_API_KEY is wrong or "
                f"the subscription is inactive.\n\nResponse: {response.text[:400]}"
            )
        if response.status_code >= 400:
            raise SearchError(f"search returned HTTP {response.status_code}: {response.text[:400]}")

        payload = response.json()
        results = (payload.get("web") or {}).get("results") or []
        return [
            SearchHit(
                url=item.get("url", ""),
                title=item.get("title", ""),
                # Brave calls the snippet "description".
                snippet=item.get("description", ""),
                query=query,
            )
            for item in results
            if item.get("url")
        ]


class SerperProvider:
    """Serper — Google's results over a simpler API and a larger free allowance.

    Not an independent index: it returns Google results, so it widens the *quota*
    rather than the coverage. Useful when the Google CSE daily cap is the binding
    constraint, not when Google itself is missing the articles.
    """

    ENDPOINT = "https://google.serper.dev/search"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.has_serper_key():
            raise SearchError(SEARCH_KEY_HELP)

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        try:
            response = httpx.post(
                self.ENDPOINT,
                headers={
                    "X-API-KEY": self.settings.serper_api_key.get_secret_value(),
                    "Content-Type": "application/json",
                },
                json={"q": query, "num": min(limit, 10), "gl": "us", "hl": "en"},
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        except httpx.RequestError as exc:
            raise SearchError(f"search request failed: {exc}") from exc

        if response.status_code == 429:
            raise QuotaExhausted("Serper credits are exhausted (HTTP 429).")
        if response.status_code in (401, 403):
            raise SearchError(
                f"Serper refused the request (HTTP {response.status_code}). Usually "
                f"TRACKER_SERPER_API_KEY is wrong.\n\nResponse: {response.text[:400]}"
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
            for item in payload.get("organic") or []
            if item.get("link")
        ]


#: Every backend by name. Adding one is a single entry plus a class; nothing else
#: in the system knows which engine answered.
PROVIDERS: dict[str, type] = {
    "google": GoogleCSEProvider,
    "brave": BraveProvider,
    "serper": SerperProvider,
}


def build_provider(settings: Settings | None = None, name: str | None = None):
    """The configured search backend, or a SearchError explaining what is missing."""
    settings = settings or get_settings()
    chosen = (name or settings.resolve_search_provider() or "").strip().lower()

    if not chosen:
        raise SearchError(SEARCH_KEY_HELP)
    if chosen == "bing":
        raise SearchError(BING_RETIRED_HELP)
    if chosen not in PROVIDERS:
        known = ", ".join(sorted(PROVIDERS))
        raise SearchError(f"unknown search provider {chosen!r}. Available: {known}")
    return PROVIDERS[chosen](settings)


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
    "BING_RETIRED_HELP",
    "LLM_BATCH",
    "PROVIDERS",
    "QUERY_PROMPT_SYSTEM",
    "SEARCH_KEY_HELP",
    "BraveProvider",
    "GoogleCSEProvider",
    "QuotaExhausted",
    "SearchError",
    "SearchHit",
    "SearchProvider",
    "SearchReport",
    "SerperProvider",
    "build_provider",
    "generate_queries",
    "hits_to_candidates",
    "is_useful_host",
    "known_projects",
    "run",
]
