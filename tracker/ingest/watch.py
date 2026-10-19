"""The news watch: a dated news search for every campus, on a schedule set by its stage.

The research that fills a row (`tracker enrich`) asks what a campus is *missing*. Once
its fields are filled it never asks again, so a campus that goes live, breaks ground,
doubles or is cancelled produces no work: Meta's "Kuna, we are online!" (2026-09-03)
reached the database on 10-17, by accident, because that night's research happened
to pick Kuna for an unrelated empty field. This module asks the other question —
**what has been said about this campus since we last looked?** — about every campus,
on a schedule:

* **A campus's first look goes back a year** (`baseline_at` is NULL). Two searches:
  the campus in the news, and the campus with the words for the stage it is waiting
  to reach ("online", "energized", "delayed" for one under construction). This is
  the catch-up — the first nights after the watch is switched on look at every row
  in the database, largest first, a few hundred a night (`--baseline`), or all at
  once (`tracker watch --all`).
* **Every later look covers only what is new since the last one**, at a cadence set
  by the stage (:data:`CADENCE`) — daily for campuses still being decided or built,
  less often for operating ones, and daily for every campus of a company a reader
  watches. A missed night is late news, not lost news: the next look's window
  reaches back to the last one.

A result is kept when its headline and snippet pass the discovery filter in
`seed/feeds.toml`, or name this campus, and its date falls in the window. Kept results
are queued (`ingest_url.feed` = `watch:#<id>`) for the nightly crawl, which reads them
ahead of the backlog and decides, with its identity check, which campus each is about.
Nothing here calls a model; a search costs about ¥0.007 of prepaid credit and is
counted against the night's search cap (`TRACKER_SEARCH_CAP`).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import CompanyWatch, NewsWatch, Project, Watch, utcnow

log = logging.getLogger(__name__)

#: How long after its last look a campus is looked at again, by stage.
CADENCE: Final[dict[str, dt.timedelta]] = {
    "announced": dt.timedelta(days=1),
    "permitting": dt.timedelta(days=1),
    "construction": dt.timedelta(days=1),
    "operational": dt.timedelta(days=3),
    "paused": dt.timedelta(days=7),
    "cancelled": dt.timedelta(days=14),
}

#: For a stage not in :data:`CADENCE`.
DEFAULT_CADENCE: Final = dt.timedelta(days=3)

#: A watched company's campuses are looked at no less often than this.
WATCHED_CADENCE: Final = dt.timedelta(days=1)

#: How far back a campus's first look reaches.
BASELINE_DAYS: Final = 365

#: First looks per run unless told otherwise: about 600 searches, a little over half
#: the night's default search cap, so the catch-up over ~620 campuses takes two
#: nights and leaves room for every campus already due.
BASELINE_PER_RUN: Final = 300

#: What a campus at each stage is waiting to hear, for the first look's second
#: search. Google reads OR between quoted words.
NEXT_WORDS: Final[dict[str, tuple[str, ...]]] = {
    "announced": (
        "approved",
        "rezoning",
        "permit",
        "groundbreaking",
        "construction",
        "cancelled",
        "withdrawn",
    ),
    "permitting": (
        "approved",
        "denied",
        "permit",
        "groundbreaking",
        "construction",
        "lawsuit",
        "moratorium",
    ),
    "construction": (
        "online",
        "operational",
        "energized",
        "opens",
        "completed",
        "delayed",
        "expansion",
    ),
    "operational": ("expansion", "expanding", "new building", "phase", "acquired", "sold"),
    "paused": ("resumes", "restart", "cancelled", "sold", "revived"),
    "cancelled": ("revived", "resumes", "new developer", "sold"),
}

#: A company with at least this many campuses here is searched as a company too: its
#: next campus is likely somewhere the database holds nothing, which no campus search
#: can find. A company a reader watches is searched whatever it holds.
COMPANY_MIN_CAMPUSES: Final = 3

#: How often a company is searched as a company, and a watched one.
COMPANY_CADENCE: Final = dt.timedelta(days=7)
WATCHED_COMPANY_CADENCE: Final = dt.timedelta(days=1)

#: How far back a company's first search reaches. A month, not a year: the campus
#: watch's first look already read a year of news about every campus held, and what
#: this is for is the next one.
COMPANY_FIRST_DAYS: Final = 30

#: Results whose date is this much older than the window's start are still taken:
#: Google dates a story by its last update as often as by its first appearance.
SLACK: Final = dt.timedelta(days=2)

#: A look counts as due this much before its cadence is up. The watch runs once a
#: night, so any value under a day changes nothing from one night to the next; what
#: it decides is a look taken off the nightly rhythm. Each run stamps every look
#: with its own start, and the loop starts the watch after discovery, whose length
#: varies: with an exact 24 hours, a night that began a few seconds earlier than the
#: last skipped every "daily" campus. And at 6 hours, the first look at all 634
#: campuses, run by hand at 00:11 on 2026-10-19, made every daily campus due at
#: 18:11 -- seven minutes after that night's watch started -- and so a whole night
#: later. At 8, a look taken in the small hours is due that evening (16 hours on),
#: and a second run later the same day does not repeat it.
EARLY: Final = dt.timedelta(hours=8)

#: Words that describe a campus without naming it. A name made only of these is not
#: searched for: "Technology" or "Joint Venture" in the OR group would turn the query
#: into everything the company does.
_VAGUE: Final = frozenset(
    {
        "technology",
        "tech",
        "digital",
        "hyperscale",
        "complex",
        "complexes",
        "development",
        "research",
        "enterprise",
        "market",
        "clean",
        "industrial",
        "joint",
        "venture",
        "ai",
        "factory",
        "compute",
        "colocation",
        "buildout",
        "park",
        "business",
        "innovation",
        "energy",
        "power",
        "site",
        "sites",
        "portfolio",
        "expansion",
        "phase",
        "new",
        "hub",
        "region",
        "cloud",
    }
)

#: A city that is not a place: what a row holds when the article named none.
_NOT_A_PLACE: Final = frozenset({"various", "multiple", "tbd", "unknown", "n/a", "na"})

#: Corporate suffixes left out of the quoted company in a query, so "Meta Platforms,
#: Inc." searches as "Meta Platforms" and matches the way the press writes it.
#: A trailing "Data Centers" on a company name ("QTS Data Centers"): the press
#: writes "QTS", and an exact-phrase search for the longer form misses it.
_DC_SUFFIX = re.compile(r"\s+(data\s*cent(er|re)s?|datacent(er|re)s?)$", re.I)
_ADMIN = re.compile(r"\b(count(?:y|ies)|parish(?:es)?|boroughs?)\b", re.I)
_CODENAME = re.compile(r"\bproject\s+[a-z0-9][\w-]*", re.I)
_COARSE = re.compile(r"\b(week|month|year)s?\s+ago\b", re.I)
_WORDS = re.compile(r"[^A-Za-z0-9\u00c0-\u024f]+")

_SUFFIX = re.compile(
    r"[,\s]+(inc|incorporated|l\.?l\.?c|corp|corporation|co|company|ltd|limited|l\.?l\.?p|l\.?p|plc)\.?$",
    re.I,
)

#: Where a stored company joins two names: "Amazon (AWS)", "TA Realty / EdgeConneX",
#: "Yondr Group and Cerberus", "CoreSite - American Tower".
_JOINS = re.compile(r"\s*[()/&]\s*|\s+-\s+|\s+and\s+", re.I)


@dataclass(frozen=True)
class Query:
    """One search the watch will send."""

    project_id: int
    text: str
    #: "news" for Google News, "web" for the web index (the stage-word search).
    endpoint: str
    #: Serper's `tbs` window: "qdr:y" for a first look; for a follow-up, the dates
    #: from the last look to today, newest first ("cdr:1,cd_min:...,sbd:1").
    window: str
    #: The earliest date a result may carry and still be news to this look.
    since: dt.datetime
    baseline: bool


@dataclass
class CampusLook:
    """What one campus's look found."""

    project_id: int
    label: str
    baseline: bool
    queries: int = 0
    results: int = 0
    kept: int = 0
    queued: int = 0


@dataclass
class WatchReport:
    looks: list[CampusLook] = field(default_factory=list)
    queries: int = 0
    failures: int = 0
    #: Why the run ended before its plan did: the search cap, no key, an outage.
    stopped: str | None = None
    due_followups: int = 0
    due_baselines: int = 0

    @property
    def queued(self) -> int:
        return sum(look.queued for look in self.looks)

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("campuses due again", self.due_followups),
            ("campuses never looked at", self.due_baselines),
            ("campuses looked at", len(self.looks)),
            ("of them first looks", sum(1 for look in self.looks if look.baseline)),
            ("searches", self.queries),
            ("results", sum(look.results for look in self.looks)),
            ("kept by the filter", sum(look.kept for look in self.looks)),
            ("queued", self.queued),
            ("searches failed", self.failures),
        ]


# --- What to search for ---------------------------------------------------------


def _clean(name: str) -> str:
    """A company name as the press writes it: no corporate suffix, no "Data Centers"."""
    base = _SUFFIX.sub("", name.strip()).strip(" ,")
    short = _DC_SUFFIX.sub("", base).strip()
    # "The Data Centers LLC" must not become "The"; "QTS Data Centers" is "QTS".
    return short if short and short.lower() not in {"the", "a", "an"} else base


def _company(project: Project) -> str:
    """The company as the press writes it."""
    return _clean(project.company or "")


def _company_term(project: Project) -> str | None:
    """`"Company"`, or an OR of the names the press uses for it.

    A stored company can hold two names ("Amazon (AWS)", "TA Realty / EdgeConneX",
    "Yondr Group and Cerberus"): each is searched, and so is the whole when it is a
    phrase a headline would print. A company filed under an old name gets its current
    one too: four of Meta's rows are "Facebook".
    """
    from tracker.dedup import company_key

    raw = (project.company or "").strip()
    if not raw:
        return None
    names: list[str] = []
    if not re.search(r"[()/]", raw):
        names.append(_clean(raw))
    names += [_clean(part) for part in _JOINS.split(raw) if part and part.strip(" ,.")]
    key = company_key(raw)
    if key and not any(key in n.lower() or n.lower() in key for n in names):
        names.append(key.title())
    unique = list(dict.fromkeys(n for n in names if n and len(n) > 1))
    if not unique:
        return None
    if len(unique) == 1:
        return f'"{unique[0]}"'
    return "(" + " OR ".join(f'"{n}"' for n in unique) + ")"


def _places(project: Project) -> list[str]:
    """The town, and the county when it is not the town's own name.

    "Loudoun and Prince William counties" is two counties; a city of "Various" is
    none.
    """
    out: list[str] = []
    city = (project.city or "").strip()
    if city.lower() in _NOT_A_PLACE:
        city = ""
    county = (project.county or "").strip()
    if city:
        out.append(city)
    if county and (not city or city.lower() not in county.lower()):
        suffix = "Parish" if project.state == "LA" else "County"
        parts = re.split(r"\s+and\s+|\s*,\s*", _ADMIN.sub("", county).strip())
        for part in parts:
            part = part.strip()
            if part:
                out.append(f"{part} {suffix}" if not _ADMIN.search(part) else part)
    return out


def _names(project: Project) -> list[str]:
    """The campus's own name, when it is distinctive: "Fairwater", "Digital Gateway",
    "Project Jupiter".

    A codename ("Project X") is taken whole. Otherwise the name's words that are not
    the company's, not a place already searched, and not words every data-center
    headline carries (the discovery queue's `_GENERIC_NAME_TOKENS`) are kept, and
    neighbours stay together as one phrase: "Digital Gateway", not "Digital" OR
    "Gateway". A phrase made only of descriptive words (:data:`_VAGUE`), a state's
    name, or the company's own name under another spelling is dropped: each would
    widen the query to everything the company does.
    """
    from tracker.dedup import company_key
    from tracker.ingest.discover import _GENERIC_NAME_TOKENS, _company_words
    from tracker.normalize import STATE_CODES

    name = (project.name or "").strip()
    codename = _CODENAME.search(name)
    if codename:
        return [codename.group(0)]
    company = _company_words(project.company)
    own = company_key(project.company)
    placed = set(" ".join(_places(project)).lower().split())
    generic = _GENERIC_NAME_TOKENS | {"data", "center", "centre", "the", "and", "of"}
    states = {s.lower() for s in STATE_CODES if len(s) > 2} | {
        "carolina",
        "dakota",
        "northern virginia",
        "virginia",
        "new york metro",
    }
    phrases: list[list[str]] = [[]]
    for word in _WORDS.split(name):
        low = word.lower()
        if not word or low in generic or low in company or low in placed:
            if phrases[-1]:
                phrases.append([])
            continue
        phrases[-1].append(word)
    kept = []
    for words in phrases:
        phrase = " ".join(words)
        low = phrase.lower()
        if (
            len(phrase) <= 4
            or set(low.split()) <= _VAGUE
            or low in states
            or (own and company_key(phrase) == own)
        ):
            continue
        kept.append(phrase)
    return kept[:2]


def anchor(project: Project) -> str | None:
    """The query's subject: `"Company" ("Town" OR "County" OR "Name")`.

    None when there is nothing to anchor on — no place and no distinctive name — since
    a search for the company alone is a search for everything it does.
    """
    where = [f'"{w}"' for w in [*_places(project), *_names(project)] if w]
    if not where:
        return None
    place = where[0] if len(where) == 1 else f"({' OR '.join(where)})"
    company = _company_term(project)
    return f"{company} {place}" if company else place


def window_for(since: dt.datetime, now: dt.datetime) -> str:
    """The dates from the day before `since` to today, newest first.

    A follow-up gets one page of ten results. Google's fixed windows ("past week")
    rank that page by relevance, so for a busy place the week's older stories could
    fill it and push out the one published since the last look; a range sorted by
    date (`sbd:1`, checked against Serper on 2026-10-18) cannot. More than a month
    back, the past year.
    """
    if now - since > dt.timedelta(days=31):
        return "qdr:y"
    start = since - dt.timedelta(days=1)
    return f"cdr:1,cd_min:{start:%m/%d/%Y},cd_max:{now:%m/%d/%Y},sbd:1"


def plan_queries(project: Project, look: NewsWatch | None, *, now: dt.datetime) -> list[Query]:
    """The searches one campus's look sends: two on a first look, one after."""
    subject = anchor(project)
    if subject is None:
        return []
    baseline = look is None or look.baseline_at is None
    if baseline:
        since = now - dt.timedelta(days=BASELINE_DAYS)
        words = NEXT_WORDS.get(project.phase or "", NEXT_WORDS["announced"])
        status = " OR ".join(f'"{w}"' for w in words)
        return [
            Query(project.id, f"{subject} data center", "news", "qdr:y", since, True),
            Query(project.id, f"{subject} data center ({status})", "web", "qdr:y", since, True),
        ]
    since = look.checked_at
    return [
        Query(project.id, f"{subject} data center", "news", window_for(since, now), since, False)
    ]


# --- Who is due -------------------------------------------------------------------


def watched_keys(session: Session) -> tuple[set[str], set[int]]:
    """(company keys readers watch, project ids readers watch one by one)."""
    keys = set(session.scalars(select(Watch.company_key).distinct()))
    ids = {pid for pid in session.scalars(select(Watch.project_id).distinct()) if pid}
    return keys, ids


def cadence(project: Project, *, watched: bool) -> dt.timedelta:
    every = CADENCE.get(project.phase or "", DEFAULT_CADENCE)
    return min(every, WATCHED_CADENCE) if watched else every


def due(
    session: Session,
    *,
    now: dt.datetime,
    project_ids: list[int] | None = None,
) -> tuple[list[tuple[Project, NewsWatch]], list[tuple[Project, NewsWatch | None]]]:
    """(campuses due a follow-up, most overdue first; campuses never looked at,
    largest first)."""
    from tracker.dedup import company_key

    keys, ids = watched_keys(session)
    stmt = select(Project, NewsWatch).outerjoin(NewsWatch, NewsWatch.project_id == Project.id)
    if project_ids:
        stmt = stmt.where(Project.id.in_(project_ids))
    followups: list[tuple[float, Project, NewsWatch]] = []
    fresh: list[tuple[Project, NewsWatch | None]] = []
    for project, look in session.execute(stmt).all():
        if look is None or look.baseline_at is None:
            fresh.append((project, look))
            continue
        watched = project.id in ids or company_key(project.company) in keys
        every = cadence(project, watched=watched)
        gap = now - look.checked_at
        overdue = gap / every
        if gap >= every - EARLY or project_ids:
            followups.append((overdue, project, look))
    followups.sort(key=lambda item: (-item[0], item[1].id))
    fresh.sort(key=lambda item: (-(item[0].mw_planned or 0), item[0].id))
    return [(p, w) for _, p, w in followups], fresh


# --- Judging results ----------------------------------------------------------------


def _naive_utc(when: dt.datetime | None) -> dt.datetime | None:
    if when is None:
        return None
    if when.tzinfo is not None:
        when = when.astimezone(dt.UTC).replace(tzinfo=None)
    return when


def _screened(hits: list[Any], query: Query, *, now: dt.datetime):
    """(result, its headline and snippet, the date to store) for each result worth
    judging: a page on a usable host, in English, dated inside the window.

    **Not a page no campus story can be in** (`screen.not_news`): a PDF, a stock-quote
    or stock-chatter page, a job posting, a directory's ZIP-code page, a site that has
    refused every read. The watch had queued 125 such pages by 2026-10-19 -- 45
    stock-chatter pages, 34 PDFs, 16 ZIP-code pages, 15 on sites that refuse every
    read -- and of the 42 tried, one produced a citation. The queue sets the waiting
    ones aside (`discover.set_aside_not_news`); these are never queued at all."""
    from tracker.ingest import screen
    from tracker.ingest.discover import _news_date
    from tracker.ingest.search import is_useful_host
    from tracker.normalize import looks_english

    for hit in hits:
        if not hit.url or not is_useful_host(hit.url) or screen.not_news(hit.url):
            continue
        text = f"{hit.title} {hit.snippet}"
        if not looks_english(text):
            continue
        raw = getattr(hit, "date", "") or ""
        when = _naive_utc(_news_date(raw, now))
        if when is not None and when < query.since - SLACK:
            continue
        if when is not None and when > now + dt.timedelta(days=1):
            when = None  # a date in the future is a misread, not news from tomorrow
        # "3 weeks ago" is good enough to judge the window and not to store: the
        # model resolves "next month" against the stored date. The page's own date
        # fills it when the article is read.
        yield hit, text, (None if _COARSE.search(raw) else when)


def judge(
    hits: list[Any],
    query: Query,
    project: Project,
    *,
    spec: Any,
    identity: Any,
    implied: dict[str, str],
    now: dt.datetime,
) -> list[Any]:
    """The results worth reading, as discovery candidates labelled `watch:#<id>`.

    A result is kept when it passes the filter, or names this campus. **A result kept
    only for naming the campus is dropped when it is local news about something else**
    (`screen.off_topic`) and carries no data-center term: a campus's town and company
    are in a football recap, a taqueria's closing and a Subaru's trim level too, and
    the first look queued all three."""
    from tracker.ingest import screen
    from tracker.ingest.discover import (
        Candidate,
        first_term,
        matches_known_project,
        normalize_haystack,
    )

    kept: list[Candidate] = []
    for hit, text, stored in _screened(hits, query, now=now):
        haystack = f"{text} {urlsplit(hit.url).path}"
        keep = spec.matches(haystack)[0]
        if not keep and not (
            screen.off_topic(haystack)
            and first_term(spec.topic, normalize_haystack(haystack)) is None
        ):
            keep = (
                matches_known_project(hit.url, text, [identity], implied_companies=implied)
                == project.id
            )
        if keep:
            kept.append(
                Candidate(
                    url=hit.url,
                    title=hit.title or hit.url,
                    feed=f"watch:#{project.id}",
                    published_at=stored,
                    source_type="general_media",
                )
            )
    return kept


# --- Companies -------------------------------------------------------------------------


@dataclass
class CompanyLook:
    """What one company's search found."""

    company_key: str
    label: str
    baseline: bool
    queries: int = 0
    results: int = 0
    kept: int = 0
    queued: int = 0


@dataclass
class CompanyReport:
    looks: list[CompanyLook] = field(default_factory=list)
    due: int = 0
    queries: int = 0
    failures: int = 0
    stopped: str | None = None

    @property
    def queued(self) -> int:
        return sum(look.queued for look in self.looks)

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("companies due", self.due),
            ("companies looked at", len(self.looks)),
            ("of them first looks", sum(1 for look in self.looks if look.baseline)),
            ("searches", self.queries),
            ("results", sum(look.results for look in self.looks)),
            ("kept by the filter", sum(look.kept for look in self.looks)),
            ("queued", self.queued),
            ("searches failed", self.failures),
        ]


def companies_due(
    session: Session, *, now: dt.datetime
) -> list[tuple[str, str, CompanyWatch | None]]:
    """[(company key, the name to search, its last look)] for every company due a
    search: watched ones first, then by how many campuses each holds.

    The name is the spelling most of its rows carry ("Meta", not "Facebook"), or the
    reader's own entry for a watched company that holds none.
    """
    from collections import Counter, defaultdict

    from tracker.dedup import company_key

    counts: Counter[str] = Counter()
    spellings: dict[str, Counter[str]] = defaultdict(Counter)
    for company in session.scalars(select(Project.company)):
        key = company_key(company)
        if key:
            counts[key] += 1
            spellings[key][(company or "").strip()] += 1
    watched = {
        key: entry for key, entry in session.execute(select(Watch.company_key, Watch.entry)) if key
    }
    looks = {look.company_key: look for look in session.scalars(select(CompanyWatch))}
    keys = {key for key, n in counts.items() if n >= COMPANY_MIN_CAMPUSES} | set(watched)
    out: list[tuple[int, int, str, str, CompanyWatch | None]] = []
    for key in keys:
        look = looks.get(key)
        every = WATCHED_COMPANY_CADENCE if key in watched else COMPANY_CADENCE
        if (
            look is not None
            and look.baseline_at is not None
            and now - look.checked_at < every - EARLY
        ):
            continue
        label = spellings[key].most_common(1)[0][0] if spellings.get(key) else watched[key]
        out.append((0 if key in watched else 1, -counts.get(key, 0), key, label, look))
    out.sort()
    return [(key, label, look) for _, _, key, label, look in out]


def company_query(label: str, look: CompanyWatch | None, *, now: dt.datetime) -> Query | None:
    """`"Company" "data center"`, since the last look; a month back on the first."""
    term = _company_term(Project(company=label))
    if term is None:
        return None
    first = look is None or look.baseline_at is None
    if first:
        since = now - dt.timedelta(days=COMPANY_FIRST_DAYS)
        window = "qdr:m"
    else:
        since = look.checked_at
        window = window_for(since, now)
    return Query(0, f'{term} "data center"', "news", window, since, first)


def judge_company(
    hits: list[Any], query: Query, key: str, *, spec: Any, now: dt.datetime
) -> list[Any]:
    """The results worth reading, labelled `watch:company:<key>`. Only the filter
    decides: no campus of the company's is named, which is the point."""
    from tracker.ingest.discover import Candidate

    kept = []
    for hit, text, stored in _screened(hits, query, now=now):
        if spec.matches(f"{text} {urlsplit(hit.url).path}")[0]:
            kept.append(
                Candidate(
                    url=hit.url,
                    title=hit.title or hit.url,
                    feed=f"watch:company:{key}",
                    published_at=stored,
                    source_type="general_media",
                )
            )
    return kept


def run_companies(
    session: Session,
    *,
    provider: Any = None,
    max_queries: int | None = None,
    now: dt.datetime | None = None,
    run_id: str | None = None,
    feeds_path: Any = None,
) -> CompanyReport:
    """Search every company that is due, as a company. Commits after each."""
    from tracker.ingest.discover import DiscoverReport, load_config, queue_candidates
    from tracker.ingest.search import QuotaExhausted, SearchError, SerperProvider

    now = now or utcnow()
    report = CompanyReport()
    run_id = run_id or now.strftime("watch-companies-%Y%m%dT%H%M%S")
    plan = companies_due(session, now=now)
    report.due = len(plan)
    if not plan:
        return report
    if provider is None:
        try:
            provider = SerperProvider()
        except SearchError as exc:
            report.stopped = f"no news search: {exc}"
            return report
    _, spec = load_config(feeds_path)
    queue_report = DiscoverReport()
    failures_running = 0
    for key, label, look in plan:
        query = company_query(label, look, now=now)
        if query is None:
            continue
        if max_queries is not None and report.queries + 1 > max_queries:
            report.stopped = f"the run's search allowance is spent ({report.queries} sent)"
            break
        result = CompanyLook(key, label, baseline=query.baseline)
        try:
            hits = provider.news(query.text, window=query.window)
        except QuotaExhausted as exc:
            report.stopped = str(exc)
            break
        except SearchError as exc:
            report.failures += 1
            failures_running += 1
            log.warning("company watch search failed for %s: %s", key, exc)
            if failures_running >= 5:
                report.stopped = f"five searches failed in a row; the last: {exc}"
                break
            continue
        failures_running = 0
        report.queries += 1
        result.queries, result.results = 1, len(hits)
        candidates = judge_company(hits, query, key, spec=spec, now=now)
        result.kept = len(candidates)
        result.queued = len(
            queue_candidates(session, candidates, run_id=run_id, report=queue_report)
        )
        if look is None:
            look = CompanyWatch(company_key=key, checked_at=now, queries=0, results=0, queued=0)
            session.add(look)
        if query.baseline:
            look.baseline_at = now
        look.checked_at = now
        look.queries = (look.queries or 0) + 1
        look.results = (look.results or 0) + result.results
        look.queued = (look.queued or 0) + result.queued
        if result.queued:
            look.last_queued_at = now
        session.commit()
        report.looks.append(result)
    return report


# --- The run ------------------------------------------------------------------------


def run(
    session: Session,
    *,
    provider: Any = None,
    followups: int | None = None,
    baselines: int | None = BASELINE_PER_RUN,
    max_queries: int | None = None,
    project_ids: list[int] | None = None,
    now: dt.datetime | None = None,
    run_id: str | None = None,
    feeds_path: Any = None,
) -> WatchReport:
    """Look at every campus that is due, follow-ups first, then first looks.

    `followups` and `baselines` cap how many campuses of each kind (None: all of
    them); `max_queries` caps the searches. Commits after every campus, so a run cut
    short keeps what it did and the next run carries on where it stopped.
    """
    from tracker.ingest.discover import (
        DiscoverReport,
        load_config,
        newsroom_companies,
        project_identities,
        queue_candidates,
    )
    from tracker.ingest.search import QuotaExhausted, SearchError, SerperProvider

    now = now or utcnow()
    report = WatchReport()
    run_id = run_id or now.strftime("watch-%Y%m%dT%H%M%S")
    later, fresh = due(session, now=now, project_ids=project_ids)
    report.due_followups, report.due_baselines = len(later), len(fresh)
    plan = later[:followups] if followups is not None else later
    plan += fresh[:baselines] if baselines is not None else fresh
    if not plan:
        return report

    if provider is None:
        try:
            provider = SerperProvider()
        except SearchError as exc:
            report.stopped = f"no news search: {exc}"
            return report

    _, spec = load_config(feeds_path)
    implied = newsroom_companies(feeds_path)
    identities = {i.project_id: i for i in project_identities(session)}
    queue_report = DiscoverReport()
    failures_running = 0

    for project, look in plan:
        queries = plan_queries(project, look, now=now)
        if max_queries is not None and report.queries + len(queries) > max_queries:
            report.stopped = f"the run's search allowance is spent ({report.queries} sent)"
            break
        result = CampusLook(
            project.id,
            f"{project.company or '?'} — {project.name}",
            baseline=bool(queries and queries[0].baseline),
        )
        candidates = []
        stop: str | None = None
        for query in queries:
            try:
                if query.endpoint == "news":
                    hits = provider.news(query.text, window=query.window)
                else:
                    hits = provider.recent(query.text, window=query.window)
            except QuotaExhausted as exc:
                stop = str(exc)
                break
            except SearchError as exc:
                report.failures += 1
                failures_running += 1
                log.warning("watch search failed for #%s: %s", project.id, exc)
                if failures_running >= 5:
                    stop = f"five searches failed in a row; the last: {exc}"
                    break
                continue
            failures_running = 0
            report.queries += 1
            result.queries += 1
            result.results += len(hits)
            identity = identities.get(project.id)
            if identity is None:
                continue
            candidates.extend(
                judge(
                    hits,
                    query,
                    project,
                    spec=spec,
                    identity=identity,
                    implied=implied,
                    now=now,
                )
            )
        if stop and not result.queries:
            report.stopped = stop
            break
        result.kept = len(candidates)
        queued = queue_candidates(session, candidates, run_id=run_id, report=queue_report)
        result.queued = len(queued)
        if look is None:
            look = NewsWatch(project_id=project.id, checked_at=now, queries=0, results=0, queued=0)
            session.add(look)
        # The clock moves only when every search of this look went through: the next
        # look reaches back to here, and a failed one must not close its window.
        complete = result.queries == len(queries)
        if result.baseline and complete:
            look.baseline_at = now
        if complete:
            look.checked_at = now
        look.queries = (look.queries or 0) + result.queries
        look.results = (look.results or 0) + result.results
        look.queued = (look.queued or 0) + result.queued
        if result.queued:
            look.last_queued_at = now
        session.commit()
        report.looks.append(result)
        if stop:
            report.stopped = stop
            break

    return report


__all__ = [
    "BASELINE_DAYS",
    "BASELINE_PER_RUN",
    "CADENCE",
    "COMPANY_CADENCE",
    "COMPANY_FIRST_DAYS",
    "COMPANY_MIN_CAMPUSES",
    "NEXT_WORDS",
    "WATCHED_COMPANY_CADENCE",
    "CampusLook",
    "CompanyLook",
    "CompanyReport",
    "Query",
    "WatchReport",
    "anchor",
    "cadence",
    "companies_due",
    "company_query",
    "due",
    "judge",
    "judge_company",
    "plan_queries",
    "run",
    "run_companies",
    "window_for",
]
