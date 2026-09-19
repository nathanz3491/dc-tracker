"""Search-based discovery: find candidate articles by asking, not by waiting.

RSS discovery only sees what the feeds publish *now*, so a project announced two
years ago never appears. Search closes that gap.

**Two ways of deciding what to look for, and they reach different ground.**

*Place-anchored templates* are what `tracker sync` runs. A query names a **place**
and an **event** — "Loudoun County Virginia data center rezoning application" —
so it has to know neither the operator nor the campus. That is the point: a
county votes on a rezoning before anybody announces anything, and it publishes
the agenda either way, so this can surface a site nobody here has heard of. The
places come from the database (see `rank_places`) and the events from a fixed
table (`_PLACE_TEMPLATES`); no model is involved at any step.

*Model-proposed queries* (`generate_queries`, reached by `tracker search
--from-llm`) ask for project names instead. This is circular by construction —
to search for a project you must already name it, so the reachable set is
whatever is in the training data — but it is kept, because it is the only path
that can name an operator in a place we hold no rows for, and because running
both is what lets `tracker queue stats` say which is actually worth the quota
rather than leaving it to be asserted.

Where the two halves meet is the rule nothing may break:

* **A model's suggestion is search-query material and nothing else.** Not one of
  them is ever written to the database.
* **Search and extraction decide what is true.** A proposed project becomes a row
  only if a real search returns a real URL, the article fetches, and the evidence
  gate finds a verbatim quote for each value. If the model invented a project,
  the search finds nothing and the run moves on.

Discovery is allowed to be speculative precisely because storage is not.

Hits from either half go through the same two-tier keyword filter as feed
discovery and land in `ingest_url` as `discovered`, so `tracker sync` crawls them
with no special casing. What separates them afterwards is the label recorded on
each queued row — `search:<template>:<place>` against `search:<the query text>` —
which is what makes the comparison above measurable at all.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings
from tracker.ingest.discover import (
    Candidate,
    DiscoverReport,
    FilterSpec,
    load_config,
    normalize_haystack,
    queue_candidates,
)
from tracker.llm import Extractor, LLMError, parse_json_object
from tracker.models import utcnow
from tracker.normalize import looks_english

log = logging.getLogger(__name__)

SEARCH_KEY_HELP = """Web search is not configured. Pick one backend and add its key to .env.

  Serper  — easiest. Google's index, one variable, and the signup states
            "2,500 free queries, no credit card required".
            https://serper.dev
            TRACKER_SERPER_API_KEY=your-key

  Google  — the same index direct. 100 queries/day free, no card, but two
            setup steps instead of one.
            https://developers.google.com/custom-search/v1/introduction
            https://programmablesearchengine.google.com  (set it to the whole web)
            TRACKER_GOOGLE_API_KEY=your-key
            TRACKER_GOOGLE_CSE_ID=your-cx-id

  Brave   — an independent index, so it finds what Google does not. 2000
            queries/month free, but the signup asks for a card.
            https://api-dashboard.search.brave.com
            TRACKER_BRAVE_API_KEY=your-key

  Bocha   — registers from mainland China with no Cloudflare challenge, when the
            three above cannot be signed up for. Its index is Chinese-web-heavy
            and thin on US trade press, so expect far fewer citable hits.
            https://open.bochaai.com
            TRACKER_BOCHA_API_KEY=your-key

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

#: Domains that are never worth queueing: aggregators, directories and social
#: sites carry no first-hand project reporting.
#:
#: Matched on registrable-domain **boundaries** (`host == entry` or
#: `host.endswith("." + entry)`), never by substring. The original substring
#: test made `"x.com"` block every `equinix.com` URL — a top-five operator whose
#: newsroom already answers 403, so search was the one path to its coverage and
#: the filter silently closed it.
#:
#: `wikipedia.org` is deliberately NOT here. A campus's Wikipedia article is
#: routinely the top search hit, its prose is quotable (the evidence gate
#: applies unchanged), and its References section is mined for the primary
#: sources it cites — see `tracker.ingest.wiki`. What keeps that honest is
#: `confidence.TERTIARY_DOMAINS`: a wikipedia citation never counts as an
#: independent domain, so it can never corroborate the coverage it merely
#: summarizes.
#:
#: The second and third groups were added after measuring Bocha, whose index is
#: Chinese-web-heavy: a single query returned sohu, zhihu, toutiao, csdn,
#: researchgate and an ACM paper, and every one passed the original filter. Each
#: would have cost a fetch and an LLM call to discover it was a translated repost
#: or an unrelated academic paper — and any excerpt stored from one could not
#: satisfy the evidence gate, which requires a verbatim quote supporting an
#: English value.
_SKIP_DOMAINS = (
    # Social, directories, listings
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    # Instagram was missing, and search surfaces it: one live `enrich 10` fetched
    # four Instagram URLs, and the prose floor measured **0 characters of prose**
    # in each. A reel is categorically not an article — there is no sentence for
    # the evidence gate to quote — so the fetch is pure waste.
    "instagram.com",
    "threads.net",
    "tiktok.com",
    "youtube.com",
    "reddit.com",
    "pinterest.com",
    "glassdoor.com",
    "indeed.com",
    "zillow.com",
    "loopnet.com",
    "crunchbase.com",
    "datacentermap.com",
    "baxtel.com",
    # Chinese portals and UGC platforms. They repost and translate US coverage
    # rather than reporting it, so they are second-hand by construction.
    "sohu.com",
    "zhihu.com",
    "toutiao.com",
    "csdn.net",
    "juejin.cn",
    "jianshu.com",
    "cnblogs.com",
    "163.com",
    "qq.com",
    "sina.com.cn",
    "baidu.com",
    "eastmoney.com",
    "xueqiu.com",
    "weibo.com",
    "bilibili.com",
    "douban.com",
    "ce.cn",
    "china.com.cn",
    "chinaaet.com",
    "ofweek.com",
    # Document dumps and academic indexes: never project news, and often large
    # PDFs that waste the fetch budget.
    "book118.com",
    "docin.com",
    "doc88.com",
    "researchgate.net",
    "dl.acm.org",
    "ieee.org",
    "arxiv.org",
    "semanticscholar.org",
    "sciencedirect.com",
    "zaixian-fanyi.com",
)

#: host+path prefixes, for sites where only one section is junk. Bloomberg's
#: /profile pages are company stubs; its articles were never blocked.
_SKIP_PATH_PREFIXES = ("bloomberg.com/profile",)


class SearchError(RuntimeError):
    """Search is unconfigured or the provider refused the request."""


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    snippet: str = ""
    query: str = ""


@dataclass
class LabelStat:
    """One label's own funnel — the blind spot `tracker queue stats` cannot see.

    The funnel is derived from `ingest_url`, so a template that ran ten times and
    had every hit discarded by the keyword filter leaves **no row at all** and is
    indistinguishable there from a template that never ran. That is not
    hypothetical: `abatement` behaved exactly like this before "abatement" was
    added to `risk_signal`, and the report built to catch such a template could
    not have caught it.

    So this is counted in memory during the run and printed, and it is the only
    place `filtered` is attributable to the query that caused it.
    """

    label: str
    queries_run: int = 0
    hits: int = 0
    filtered: int = 0
    queued: int = 0


@dataclass
class SearchReport:
    queries_run: int = 0
    hits: int = 0
    filtered: int = 0
    already_known: int = 0
    queued: int = 0
    #: References mined from Wikipedia articles among the hits — leads a search
    #: snippet alone could never surface, e.g. the operator's IR press release.
    wiki_mined: int = 0
    quota_exhausted: bool = False
    errors: list[tuple[str, str]] = field(default_factory=list)
    #: Per-label detail, present only when `run` was given a `labels` map.
    by_label: dict[str, LabelStat] = field(default_factory=dict)

    def label(self, name: str) -> LabelStat:
        return self.by_label.setdefault(name, LabelStat(label=name))

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("queries run", self.queries_run),
            ("search hits", self.hits),
            ("filtered out", self.filtered),
            ("wikipedia refs mined", self.wiki_mined),
            ("already known", self.already_known),
            ("queued", self.queued),
        ]


class SearchProvider(Protocol):
    #: The backend's own name, as it appears in a report. Which engine answered
    #: is load-bearing information, not trivia: this project spent weeks with
    #: `enrich` searching Bocha's Chinese-web index for US trade press, which no
    #: output ever named, so the harvest looked broken rather than misconfigured.
    NAME: str

    def search(self, query: str, *, limit: int) -> list[SearchHit]: ...


def provider_name(provider: object) -> str:
    """The backend's name, for a report. Falls back to the class name."""
    return getattr(provider, "NAME", None) or type(provider).__name__


# --- Google Programmable Search --------------------------------------------


class GoogleCSEProvider:
    """The official Custom Search JSON API.

    Chosen over scraping result pages: scraping breaks Google's terms, is blocked
    in practice, and would contradict this project's decision not to defeat other
    sites' access controls either.
    """

    ENDPOINT = "https://www.googleapis.com/customsearch/v1"
    NAME = "google"

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
    NAME = "brave"

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
    NAME = "serper"

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


class BochaProvider:
    """Bocha (博查) web search.

    Registers from mainland China without a Cloudflare challenge, which is why it
    is here: Serper's signup, Brave's and Google's are all awkward or blocked from
    that network, and a backend you cannot sign up for is worth nothing.

    **Its index is Chinese-web-heavy, and that is a real limitation for this
    tool.** Measured against the live API: a query for a tracked project returned
    sohu, zhihu, xueqiu and 163; `site:datacenterfrontier.com` returned only that
    site's *homepage*; and querying the exact headline of an article already in the
    database returned no trade-press URL at all. The engine works — it simply does
    not index US data center trade press at article depth, and no query tuning
    fixes an index gap.

    So it is best treated as a way to learn that a project *exists*, not as a way
    to obtain the citation. `_SKIP_DOMAINS` carries the Chinese portals it favours,
    so its reposts are dropped before they cost a fetch and an LLM call.
    """

    ENDPOINT = "https://api.bochaai.com/v1/web-search"
    NAME = "bocha"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.has_bocha_key():
            raise SearchError(SEARCH_KEY_HELP)

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        try:
            response = httpx.post(
                self.ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.settings.bocha_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "count": min(limit, 50),
                    "freshness": "noLimit",
                    # Ask for the page summary too: it costs nothing extra and gives
                    # the keyword filter more than a one-line snippet to judge.
                    "summary": True,
                },
                timeout=httpx.Timeout(60.0, connect=15.0),
            )
        except httpx.RequestError as exc:
            raise SearchError(f"search request failed: {exc}") from exc

        if response.status_code == 429:
            raise QuotaExhausted("Bocha rate limit or balance exhausted (HTTP 429).")
        if response.status_code in (401, 403):
            raise SearchError(
                f"Bocha refused the request (HTTP {response.status_code}). Usually "
                f"TRACKER_BOCHA_API_KEY is wrong or out of credit."
                f"\n\nResponse: {response.text[:400]}"
            )
        if response.status_code >= 400:
            raise SearchError(f"search returned HTTP {response.status_code}: {response.text[:400]}")

        payload = response.json()
        # Bocha answers HTTP 200 with an error code in the body, so the status line
        # alone does not tell you the call succeeded.
        if isinstance(payload, dict) and payload.get("code") not in (200, None):
            raise SearchError(
                f"Bocha returned code {payload.get('code')}: "
                f"{payload.get('msg') or payload.get('message') or payload}"
            )

        data = (payload or {}).get("data") or {}
        pages = data.get("webPages") or {}
        results = pages.get("value") or [] if isinstance(pages, dict) else []
        return [
            SearchHit(
                url=item.get("url", ""),
                # Bocha calls the title "name".
                title=item.get("name", ""),
                snippet=item.get("snippet") or item.get("summary") or "",
                query=query,
            )
            for item in results
            if isinstance(item, dict) and item.get("url")
        ]


#: Every backend by name. Adding one is a single entry plus a class; nothing else
#: in the system knows which engine answered.
PROVIDERS: dict[str, type] = {
    "google": GoogleCSEProvider,
    "brave": BraveProvider,
    "serper": SerperProvider,
    "bocha": BochaProvider,
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


# --- Place-anchored discovery -----------------------------------------------
#
# The other half of this module, and the one `sync` runs by default.
#
# `generate_queries` above asks a model to NAME a project, which means the set it
# can reach is the set it already knows — the famous campuses, which are the ones
# already stored. Nothing there can find a site nobody wrote a training corpus
# about.
#
# These queries name a PLACE and an EVENT instead, so they need to know neither
# the operator nor the campus. A county votes on a rezoning before anybody
# announces anything, and it publishes the agenda either way.


#: What to look for, keyed by the name the funnel will group on.
#:
#: The query is ``f"{place.phrase} data center {phrase}"``, and "data center" is
#: not optional: `hits_to_candidates` never sets `topic_implied`, so a hit whose
#: title, snippet and URL path never say it is dropped as off-topic however good
#: the rest of the match is.
#:
#: **Every phrase must carry a term that is verbatim in `seed/feeds.toml`'s
#: `signal` or `risk_signal` tier.** The words here steer the search engine; they
#: do not decide what is kept — `FilterSpec.matches` does, and it needs a topic
#: term AND a signal-or-risk term. A phrase whose own vocabulary the filter
#: rejects returns hits that are all discarded before they cost a fetch, and
#: leaves nothing behind to say so. `test_every_template_phrase_survives_the_real_filter`
#: is what stops the next one being added by eye.
#:
#: Note the keys are short and the phrases are not: "interconnect" and
#: "groundbreak" are not filter terms, "interconnection" and "groundbreaking"
#: are.
_PLACE_TEMPLATES: dict[str, str] = {
    "rezoning": "rezoning application",
    "permit": "building permit filed",
    "interconnect": "interconnection queue",
    "substation": "substation transmission line",
    "groundbreak": "groundbreaking ceremony",
    "investment": "billion investment announced",
    "moratorium": "moratorium vote",
    "opposition": "residents oppose",
    "water": "water use aquifer",
    "abatement": "tax abatement approved",
}

#: What a state calls its counties. Everywhere else says "County".
_COUNTY_WORD: dict[str, str] = {"LA": "Parish", "AK": "Borough"}


def _county_phrase(display: str, state: str) -> str:
    """ "Loudoun" -> "Loudoun County", but "Richland Parish" left alone.

    The word matters to a search engine and not at all to the database, which is
    why it is added here rather than stored. "Loudoun Virginia" is ambiguous
    enough to return the town, the school district and the newspaper; "Loudoun
    County Virginia" is the phrase reporting actually uses. The value we hold may
    already carry the word, since it is whatever a source wrote.
    """
    from tracker.dedup import _COUNTY_SUFFIXES

    lowered = display.lower()
    if any(lowered.endswith(suffix) for suffix in _COUNTY_SUFFIXES):
        return display
    return f"{display} {_COUNTY_WORD.get(state, 'County')}"


def templates() -> frozenset[str]:
    """The template names, for anything that has to recognise a stored label.

    `funnel.feed_group` needs this to tell `search:rezoning:loudoun-va` from a
    hand-typed query that merely happens to contain a colon. Exposed as a
    function rather than the dict so a caller cannot edit the registry by
    accident.
    """
    return frozenset(_PLACE_TEMPLATES)


#: Territories. Excluded from the absent-state tier because a query for
#: "American Samoa data center rezoning" is quota spent on a certainty.
_TERRITORIES: frozenset[str] = frozenset({"GU", "AS", "MP", "VI", "PR"})

#: How many never-seen states one run may anchor on. Hard-capped so a tier that
#: has never paid for itself cannot crowd out the two that have.
STATE_ONLY_SLOTS = 2

#: Places below this many projects are not a cluster, they are a single row.
CLUSTER_MIN = 2


@dataclass(frozen=True)
class Place:
    """Somewhere to point a query, and why it ranked."""

    #: Goes into the stored label, so it must never contain a colon —
    #: `funnel.feed_group` splits on them. `dedup._slug` strips every character
    #: that is not a letter or a digit, which is what guarantees it.
    slug: str
    #: What the query actually says: "Richland Parish Louisiana".
    phrase: str
    state: str
    #: ``county``, ``city`` or ``state``.
    kind: str
    #: Projects already held here. The rank reason, carried so a report can say
    #: why this place was chosen rather than leaving it to be inferred.
    projects: int


def rank_places(
    session: Session,
    *,
    limit: int = 40,
    state_only: int = STATE_ONLY_SLOTS,
    spec: FilterSpec | None = None,
) -> tuple[list[Place], list[tuple[str, str]]]:
    """Where to look, best first, and the places we are unable to look at.

    Derived from the database rather than from a list somebody maintains — the
    argument `probe.py` makes for feeds applies here unchanged: the answer is
    already in the rows. Three tiers, and they buy different things.

    1. **Clusters.** Data centers cluster hard, so a county already holding four
       campuses is the likeliest place for a fifth. Cheapest and highest-yield.
    2. **Thin states.** Present, below-median project count, above-median
       capacity: we found the big one and missed its neighbours.
    3. **Absent states.** Not in the database at all. Capped at `state_only`,
       because this tier is a standing experiment rather than a bet.

    Tier 1 spends the budget where we are *least* blind, which is worth saying
    out loud: it is self-reinforcing, and tiers 2 and 3 exist precisely to stop
    it being the only thing that ever runs. Which tier actually pays is a
    question `tracker queue stats` will answer once the labels have a history.

    The second return value is places that **cannot be searched at all**, with
    the reason. Reported rather than skipped, because a place we are structurally
    unable to look at is a finding — see the note on `exclude` below.
    """
    from sqlalchemy import func, select

    from tracker.dedup import locality
    from tracker.models import Project
    from tracker.normalize import STATE_CODES, state_name

    if spec is None:
        spec = load_config()[1]

    rows = list(session.scalars(select(Project)))

    # Grouped through `dedup.locality`, NOT `GROUP BY county, state`.
    #
    # `ck_project_locality` guarantees one of city/county is set but not which,
    # so a raw county grouping drops every city-only row into one NULL bucket
    # that then outranks every real county. `locality` also already resolves the
    # ISO-import case where "Racine County" was written into the `city` column,
    # which `county_key(p.county)` alone would file as a city.
    clusters: dict[tuple[str, str, str], dict[str, Any]] = {}
    for project in rows:
        loc = locality(project.city, project.county)
        if not loc.key or not project.state:
            continue
        bucket = clusters.setdefault(
            (loc.kind, loc.key, project.state),
            {"count": 0, "display": loc.display},
        )
        bucket["count"] += 1

    present = {p.state for p in rows if p.state}
    refused: list[tuple[str, str]] = []
    out: list[Place] = []

    def offer(place: Place) -> None:
        """Keep a place unless the discovery filter could never accept it.

        `exclude` is a plain substring test over the whole haystack, so two real
        county names are already unsearchable for ANY query: "summit" (added for
        conference write-ups) kills Summit County in CO, OH and UT, and "stock"
        (added for finance coverage) kills Stockton, Woodstock and Comstock.
        Left in the plan, such a place spends its slot every run and returns
        nothing, with no queued row to explain the silence.

        Checked against every template rather than one, so a term tripped by a
        particular pairing is caught too. Ten string tests per place.
        """
        for phrase in _PLACE_TEMPLATES.values():
            probe = normalize_haystack(f"{place.phrase} data center {phrase}")
            hit = next((t for t in spec.exclude if t in probe), None)
            if hit:
                refused.append((place.phrase, f"the discovery filter excludes {hit!r}"))
                return
        out.append(place)

    # Tier 1 — clusters.
    for (kind, key, state), bucket in sorted(
        clusters.items(), key=lambda kv: (-kv[1]["count"], kv[0][1])
    ):
        if bucket["count"] < CLUSTER_MIN:
            continue
        name = state_name(state) or state
        where = bucket["display"]
        offer(
            Place(
                slug=f"{key}-{state}".lower().replace(" ", "-"),
                phrase=f"{_county_phrase(where, state) if kind == 'county' else where} {name}",
                state=state,
                kind=kind,
                projects=bucket["count"],
            )
        )

    # Tier 2 — thin states: present, but light on rows for the capacity held.
    by_state = session.execute(
        select(
            Project.state,
            func.count(Project.id),
            func.coalesce(func.sum(Project.mw_planned), 0.0),
        ).group_by(Project.state)
    ).all()
    if len(by_state) >= 2:
        counts = sorted(c for _, c, _ in by_state)
        mws = sorted(float(m or 0.0) for _, _, m in by_state)
        mid_count = counts[len(counts) // 2]
        mid_mw = mws[len(mws) // 2]
        thin = [
            (state, count)
            for state, count, mw in by_state
            if count <= mid_count and float(mw or 0.0) >= mid_mw
        ]
        for state, count in sorted(thin, key=lambda sc: (sc[1], sc[0])):
            name = state_name(state)
            if not name:
                continue
            offer(Place(slug=state.lower(), phrase=name, state=state, kind="state", projects=count))

    # Tier 3 — states with nothing at all.
    #
    # Only meaningful once something is present: on an empty database every state
    # is "absent", which is not a finding about anywhere, and anchoring on the
    # first two alphabetically would spend the first run on Alabama and Alaska.
    #
    # Ordered by how much has already been tried there rather than by name, for
    # the reason `plan_queries` gives: a fixed order means the same two states
    # every run, for ever, since a state that yields nothing leaves no row to
    # demote it. This walks the alphabet instead of standing at the front of it.
    if present:
        tried = _label_counts(session)
        by_state: dict[str, int] = {}
        for label, n in tried.items():
            parts = label.split(":", 2)
            if len(parts) >= 3:
                by_state[parts[2]] = by_state.get(parts[2], 0) + n
        absent = sorted(
            STATE_CODES - present - _TERRITORIES,
            key=lambda s: (by_state.get(s.lower(), 0), s),
        )
        for state in absent[:state_only]:
            name = state_name(state)
            if not name:
                continue
            offer(Place(slug=state.lower(), phrase=name, state=state, kind="state", projects=0))

    # A state can reach tier 2 and tier 3 only by being both present and absent,
    # which cannot happen — but a slug collision would silently merge two
    # templates' histories, so it is cheaper to assert it than to trust it.
    seen: set[str] = set()
    unique = [p for p in out if not (p.slug in seen or seen.add(p.slug))]
    return unique[:limit], refused


@dataclass(frozen=True)
class PlannedQuery:
    """One query, and the label its results will be filed under."""

    text: str
    template: str
    place: Place

    @property
    def label(self) -> str:
        # The place is truncated, never the whole label: cutting the label as one
        # string could drop the template segment, and two long places would then
        # merge into a single bucket that reads as one template's history.
        return f"search:{self.template}:{self.place.slug[:100]}"


def plan_queries(
    session: Session,
    *,
    count: int,
    templates: dict[str, str] | None = None,
    limit_places: int = 40,
) -> tuple[list[PlannedQuery], list[tuple[str, str]]]:
    """The next `count` template-and-place pairs worth running.

    **Ordered by what has never produced anything**, then diagonally across the
    cross product, then by rank. Two keys, and both earn their place.

    The first is the whole design: a plan that simply took the first N pairs would
    run the same N queries every night, everything would be `already_known` by the
    second run, and this would have rebuilt the problem it exists to fix in a new
    costume. It also fixes a quota failure — `run` stops at the first
    `QuotaExhausted`, so under a fixed order the tail of the plan is never reached,
    not once; here a pair that did not execute left no rows and heads the next
    plan.

    The second stops a run spending its whole budget on one county. Ranked order
    alone gives ten queries about Loudoun and nothing about anywhere else, which
    is the opposite of what a discovery run is for; walking the diagonal spreads
    each run over both axes while still starting from the best place and the first
    template.

    **What this cannot see is a pair that ran and queued nothing** — no row is
    written, so it is indistinguishable from one that never ran, and it will come
    round again. That is deliberate rather than tolerated: unlike a feed, which is
    a fixed publisher, a place with no data-center news this quarter may have some
    next quarter, and re-asking is how that is noticed. The cost is bounded — one
    query per full cycle of the cross product — and a template that is barren
    *everywhere* shows up in the per-label counts `run` returns, which is the one
    place that distinction exists.
    """
    templates = templates or _PLACE_TEMPLATES
    places, refused = rank_places(session, limit=limit_places)
    if not places:
        return [], refused

    tried = _label_counts(session)
    order = {name: i for i, name in enumerate(templates)}
    rank = {place.slug: i for i, place in enumerate(places)}

    plan = [
        PlannedQuery(text=f"{place.phrase} data center {phrase}", template=name, place=place)
        for place in places
        for name, phrase in templates.items()
    ]
    plan.sort(
        key=lambda q: (
            tried.get(q.label, 0),
            rank[q.place.slug] + order[q.template],  # the diagonal
            rank[q.place.slug],
            order[q.template],
        )
    )

    # One text can only be planned once: `run`'s labels map is keyed on the query
    # string, so a duplicate would hand the same hits to two labels.
    seen: set[str] = set()
    unique = [q for q in plan if not (q.text in seen or seen.add(q.text))]
    return unique[:count], refused


def _label_counts(session: Session) -> dict[str, int]:
    """How many queued URLs each search label has ever produced."""
    from sqlalchemy import func, select

    from tracker.models import IngestUrl

    rows = session.execute(
        select(IngestUrl.feed, func.count())
        .where(IngestUrl.feed.like("search:%"))
        .group_by(IngestUrl.feed)
    ).all()
    return {str(feed): int(count) for feed, count in rows if feed}


# --- Filtering and queueing -------------------------------------------------


def is_useful_host(url: str) -> bool:
    """Reject aggregators and social sites, which carry no first-hand reporting.

    Domains are matched on label boundaries, not as substrings: ``x.com`` blocks
    ``x.com`` and ``mobile.x.com``, and must not block ``equinix.com`` — which
    the substring version this replaces silently did.
    """
    parts = urlsplit(url)
    host = parts.netloc.lower().split("@")[-1].split(":")[0].removeprefix("www.")
    if any(host == d or host.endswith("." + d) for d in _SKIP_DOMAINS):
        return False
    hostpath = host + parts.path.lower()
    return not any(hostpath.startswith(p) for p in _SKIP_PATH_PREFIXES)


def hits_to_candidates(
    hits: list[SearchHit],
    spec: FilterSpec,
    *,
    report: SearchReport,
    labels: dict[str, str] | None = None,
) -> list[Candidate]:
    """Apply the same two-tier filter feed discovery uses.

    `topic_implied` is never set here: a search result could be from anywhere, so
    an article has to prove for itself that it is about a data center. The snippet
    participates in matching, which a feed entry does not have.

    `labels` maps a query string to the label its results should be filed under,
    so a planned query records **which template and place found this**, not the
    sentence that was typed. A query with no entry keeps the verbatim form, which
    is what a hand-typed `tracker search "…"` still gets.

    **Labelled here rather than by rewriting the candidates afterwards**, which is
    how `prospect` does it and is wrong for this caller. `prospect` calls this
    function once per operator, so it can relabel the whole batch; `run` calls it
    once over every hit, and the `seen` set below is what stops a URL two queries
    both found being queued twice. Splitting the call per query to relabel would
    push that de-duplication into `queue_candidates`, where the second sighting
    increments `already_known` — inflating the exact counter this change exists to
    bring down. So: one map, one pass, and the first query in plan order owns the
    URL.
    """
    labels = labels or {}
    kept: list[Candidate] = []
    seen: set[str] = set()

    def note(hit: SearchHit, field_name: str) -> None:
        """Count one outcome against the label that paid for it, if there is one."""
        label = labels.get(hit.query)
        if label:
            stat = report.label(label)
            setattr(stat, field_name, getattr(stat, field_name) + 1)

    for hit in hits:
        report.hits += 1
        note(hit, "hits")
        if not hit.url or hit.url in seen:
            continue
        seen.add(hit.url)
        if not is_useful_host(hit.url):
            report.filtered += 1
            note(hit, "filtered")
            continue
        if not looks_english(f"{hit.title} {hit.snippet}"):
            # A translated repost: it cannot satisfy the evidence gate for any
            # numeric field, so fetching it would buy nothing.
            log.debug("skip %s (not English-language)", hit.url)
            report.filtered += 1
            note(hit, "filtered")
            continue
        haystack = f"{hit.title} {hit.snippet} {urlsplit(hit.url).path}"
        keep, reason = spec.matches(haystack)
        if not keep:
            log.debug("skip %s (%s)", hit.url, reason)
            report.filtered += 1
            note(hit, "filtered")
            continue
        kept.append(
            Candidate(
                url=hit.url,
                title=hit.title or hit.url,
                # Recorded in `ingest_url.feed` so a queued row shows where it came
                # from: the template and place for a planned query, the query text
                # itself for one typed by hand.
                feed=labels.get(hit.query) or f"search:{hit.query}"[:120],
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
    mine_wikipedia: bool = True,
    labels: dict[str, str] | None = None,
) -> tuple[SearchReport, list[Candidate]]:
    """Run each query, filter the hits, and queue what survives.

    `labels` maps query text to the label its results are filed under; see
    `hits_to_candidates`. Absent, every candidate records the query verbatim,
    exactly as before.
    """
    from tracker.ingest import wiki

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
        if labels and query in labels:
            report.label(labels[query]).queries_run += 1
        log.info("%r -> %d hit(s)", query, len(hits))
        all_hits.extend(hits)

    candidates = hits_to_candidates(all_hits, spec, report=report, labels=labels)

    # A Wikipedia hit is worth more than its own page: its References section
    # names the primary sources. Mined from the raw hits rather than the kept
    # candidates, because the wiki page itself can fail the keyword filter (an
    # opaque snippet) while its references are still exactly what we want.
    if mine_wikipedia:
        wiki_urls = [h.url for h in all_hits if wiki.is_wikipedia(h.url)]
        if wiki_urls:
            already = {c.url for c in candidates}
            mined = [
                c for c in wiki.mine(wiki_urls, spec, settings=settings) if c.url not in already
            ]
            report.wiki_mined = len(mined)
            candidates.extend(mined)

    # Reuses the feed-discovery queueing wholesale, including its rule that a URL
    # already in ingest_url is left completely alone.
    shim = DiscoverReport()
    queued = queue_candidates(session, candidates, run_id=run_id, report=shim)
    report.already_known = shim.already_known
    report.queued = shim.queued
    for candidate in queued:
        if candidate.feed in report.by_label:
            report.by_label[candidate.feed].queued += 1

    if dry_run:
        session.rollback()
        return report, candidates
    session.commit()
    return report, queued


__all__ = [
    "BING_RETIRED_HELP",
    "CLUSTER_MIN",
    "LLM_BATCH",
    "PROVIDERS",
    "QUERY_PROMPT_SYSTEM",
    "SEARCH_KEY_HELP",
    "STATE_ONLY_SLOTS",
    "BochaProvider",
    "BraveProvider",
    "GoogleCSEProvider",
    "LabelStat",
    "Place",
    "PlannedQuery",
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
    "plan_queries",
    "provider_name",
    "rank_places",
    "run",
    "templates",
]
