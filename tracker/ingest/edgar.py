"""SEC filings as a source: the one publisher that cannot lock us out.

Every other source in this project is somebody's website, and the good ones
increasingly answer 403 to anything that is not a browser. Filings are different
in kind: publication is a legal obligation, the format is fixed, and they are
served from a government host with a documented rate limit instead of a bot
filter. They are also where the three fields we cover worst actually live —
`investment_usd` in the cash-flow statement, `expected_online` in MD&A, and the
end customer in the ASC 842 lease footnotes.

**This module produces article text; it does not extract facts.** The section it
selects is written into the same article cache `discover.cache_feed_text` uses,
and `crawl.run` then reads it through the ordinary path: same prompt, same
evidence gate, same normalizer, same upsert. Nothing here re-implements any of
that, so a filing is held to exactly the standard a news article is.

**A filing's date comes from SEC, never from its page.** A filing is an inline-XBRL
document with no `datePublished`, no `<meta>` and no `<time>`, so the fetcher finds no
date in it and never could: on 2026-10-26, 172 of the 173 citations of sec.gov had none.
SEC's own record of when each filing was filed is dated and authoritative, and
:class:`FilingDates` reads it — see the section of that name below.

Two things make search work, and both were measured rather than assumed.

**Scope by CIK, not by phrase.** Unfiltered, `"data center campus"` returns 1,066
hits dominated by shell companies; scoped to one CIK it returns only that
company's filings. Precision is therefore a property of the query. The `sics`
parameter is accepted and silently ignored, so industry filtering is not
available. See `seed/edgar-companies.toml`.

**Select a section, do not truncate.** A Meta 10-Q is 2.1 MB of HTML — 369,000
characters of text against a 24,000-character model budget, 15x over. `truncate`
keeps the head and tail, which is right for a news article and wrong here: a
filing's data center facts sit in MD&A and the footnotes, which are neither.
:func:`extract_section` scores paragraphs on the density of things the evidence
gate can actually verify and keeps the best with their neighbours. Measured over
39 filings it compresses to ~6% of the document; where the budget is not the
binding constraint it retains 99% of the fact-bearing paragraphs.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

import httpx

from tracker.config import Settings, get_settings, seed_path, without_links
from tracker.ingest.crawl import _MONEY_EXPR, _MW_EXPR, _RECENT_DATE_EXPR
from tracker.ingest.fetch import cache_path, html_to_text

log = logging.getLogger(__name__)

FULL_TEXT_SEARCH = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SUBMISSIONS = "https://data.sec.gov/submissions"

#: SEC asks for no more than 10 requests/second across their hosts, and enforces
#: it. One tenth of a second between requests keeps a single-threaded run under
#: the limit with room to spare; this is not a throughput-critical path.
REQUEST_INTERVAL_S = 0.15

#: SEC rejects a request whose User-Agent does not identify a real caller. The
#: shipped default is a placeholder, and sending it gets the whole run blocked —
#: better to say so up front than to collect twenty identical 403s.
_PLACEHOLDER_UA = "set-me@example.com"


class EdgarError(RuntimeError):
    """The run cannot proceed. Message is operator-facing."""


@dataclass(frozen=True)
class Company:
    name: str
    cik: str
    kind: str = "unknown"
    #: Phrases to search this company's filings for, resolved at load time from
    #: `[search.by_kind]`. Empty means "use the shared list".
    #:
    #: Per kind rather than per company, because the thing that varies is what
    #: the class of filer writes: a utility discloses "large load" and an
    #: interconnection agreement, an E&C contractor discloses backlog, and
    #: neither writes "anchor tenant". Asking every filer the hyperscaler
    #: question is how a new source class looks like it added nothing.
    phrases: tuple[str, ...] = ()
    #: Forms to search for THIS filer, overriding the shared `[search] forms`.
    #: Empty means "use the shared list".
    #:
    #: Per company rather than per kind, because what varies is where the filer is
    #: incorporated, not what it does. A foreign private issuer files 20-F and 6-K
    #: and never a 10-K, so the shared list — 10-K, 10-Q, 8-K, which was 97% of
    #: hits in testing — returns literally nothing for it. Measured: Nebius, a
    #: rostered neocloud with a Kansas City campus, was on this list from the start
    #: and contributed zero filings, because every query asked for forms it does
    #: not file. Widening the shared list instead would double the cost of every
    #: domestic company to fix one foreign one.
    forms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Filing:
    """One filing document, identified well enough to fetch and to cite."""

    company: str
    cik: str
    #: Accession number, dashed form, e.g. "0001326801-23-000067".
    adsh: str
    filename: str
    form: str
    file_date: str

    @property
    def url(self) -> str:
        """The canonical public URL, which is also what gets cited.

        Both halves come out of the search hit's `_id`, which is
        ``"<accession>:<filename>"``. The CIK is unpadded here and the accession
        has its dashes stripped — neither is optional, and getting either wrong
        returns a 404 rather than an error that says what is wrong.
        """
        return f"{ARCHIVES}/{int(self.cik)}/{self.adsh.replace('-', '')}/{self.filename}"


# --- Configuration -----------------------------------------------------------


def default_companies_path() -> Path:
    return seed_path("edgar-companies.toml")


def load_companies(path: Path | None = None) -> tuple[list[Company], list[str], list[str]]:
    """Read the company list. Returns ``(companies, phrases, forms)``."""
    path = path or default_companies_path()
    if not path.is_file():
        raise EdgarError(
            f"no company list at {path}.\n"
            "Expected a TOML file with [[company]] entries; see "
            "seed/edgar-companies.toml in the repository for the format."
        )
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise EdgarError(f"{path.name} is not valid TOML: {exc}") from exc

    search = data.get("search") or {}
    phrases = [str(p) for p in (search.get("phrases") or ['"data center"'])]
    forms = [str(f) for f in (search.get("forms") or ["10-K", "10-Q"])]
    by_kind = {
        str(kind): tuple(str(p) for p in (words or ()))
        for kind, words in (search.get("by_kind") or {}).items()
    }

    companies: list[Company] = []
    for entry in data.get("company") or []:
        cik = str(entry.get("cik") or "").strip()
        if not cik.isdigit() or len(cik) != 10:
            raise EdgarError(
                f"{path.name}: cik {cik!r} for {entry.get('name')!r} must be 10 digits, "
                "zero-padded. An unpadded CIK silently returns no hits."
            )
        kind = str(entry.get("kind") or "")
        own_forms = entry.get("forms") or []
        if not isinstance(own_forms, list):
            raise EdgarError(f"{path.name}: forms for {entry.get('name')!r} must be a list")
        companies.append(
            Company(
                name=str(entry.get("name") or cik),
                cik=cik,
                kind=kind,
                phrases=by_kind.get(kind, ()),
                forms=tuple(str(f).strip() for f in own_forms if str(f).strip()),
            )
        )
    if not companies:
        raise EdgarError(f"{path.name} defines no [[company]] entries")

    return companies, phrases, forms


# --- HTTP --------------------------------------------------------------------


def _headers(settings: Settings) -> dict[str, str]:
    ua = settings.user_agent
    if _PLACEHOLDER_UA in ua:
        raise EdgarError(
            "SEC requires a User-Agent naming a real contact, and TRACKER_USER_AGENT is "
            "still the shipped placeholder.\n\n"
            "Set it in .env:\n"
            "  TRACKER_USER_AGENT=dc-tracker/0.1 (+contact: you@example.com)\n\n"
            "Sending the placeholder gets the whole run blocked rather than one request."
        )
    # SEC refuses a User-Agent that carries a link; see `Settings.user_agent_for`.
    return {"User-Agent": without_links(ua), "Accept-Encoding": "gzip, deflate"}


class _Client:
    """A rate-limited httpx client. SEC enforces its published limit."""

    def __init__(self, settings: Settings) -> None:
        self._headers = _headers(settings)
        self._client = httpx.Client(
            timeout=httpx.Timeout(settings.fetch_timeout_s, connect=10.0),
            follow_redirects=True,
            headers=self._headers,
        )
        self._last = 0.0

    def get(self, url: str, **params: Any) -> httpx.Response:
        wait = REQUEST_INTERVAL_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            response = self._client.get(url, params=params or None)
        finally:
            self._last = time.monotonic()
        return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def search(
    client: _Client,
    company: Company,
    phrase: str,
    forms: list[str],
    *,
    limit: int = 10,
    since: dt.date | None = None,
) -> list[Filing]:
    """Full-text search scoped to one company, newest first.

    `sort` is not a parameter the API honours, and its default ordering is by
    relevance — which on a company-scoped query surfaced a 2014 Digital Realty
    8-K and a 2015 Equinix one ahead of anything current. For tracking projects
    under construction that is the wrong end of the archive, so the window is
    applied server-side and the results are re-sorted here by filing date.
    """
    # The company's own list wins where it has one: see `Company.forms`. Passed in
    # rather than read from the config here so the caller stays the only thing that
    # reads the file.
    wanted = list(company.forms) or forms
    params: dict[str, Any] = {"q": phrase, "ciks": company.cik, "forms": ",".join(wanted)}
    if since is not None:
        params["dateRange"] = "custom"
        params["startdt"] = since.isoformat()
        params["enddt"] = dt.date.today().isoformat()
    response = client.get(FULL_TEXT_SEARCH, **params)
    if response.status_code != 200:
        log.warning("search for %s returned HTTP %s", company.name, response.status_code)
        return []
    try:
        hits = response.json().get("hits", {}).get("hits", [])
    except ValueError:
        log.warning("search for %s returned unparseable JSON", company.name)
        return []

    filings: list[Filing] = []
    hits.sort(key=lambda h: str((h.get("_source") or {}).get("file_date") or ""), reverse=True)
    for hit in hits[:limit]:
        source = hit.get("_source") or {}
        adsh, _, filename = str(hit.get("_id") or "").partition(":")
        if not adsh or not filename:
            continue
        # Only the primary document is worth reading. Exhibits are mostly
        # signature pages, certifications and XBRL, and each one costs a fetch.
        if not filename.lower().endswith((".htm", ".html")):
            continue
        forms_field = source.get("root_forms") or source.get("file_type") or []
        form = forms_field[0] if isinstance(forms_field, list) and forms_field else str(forms_field)
        filings.append(
            Filing(
                company=company.name,
                cik=company.cik,
                adsh=adsh,
                filename=filename,
                form=str(form or "?"),
                file_date=str(source.get("file_date") or "?"),
            )
        )
    return filings


# --- Filing dates ------------------------------------------------------------
#
# SEC says when each filing was filed in one place: the submissions API, one JSON
# document per company that lists the accession number and filing date of every
# filing it is a party to (`filings.recent`, the newest thousand or so) and names
# further documents for the rest (`filings.files`, each giving the span of dates it
# covers). One request therefore dates every filing a company has made, which is why
# a run asks once per company and remembers the answer rather than asking once per
# page: the 172 undated citations measured on 2026-10-26 were pages of 90 filings
# by 24 companies.


@dataclass(frozen=True)
class FilingRef:
    """Which filing a URL is a page of, in the terms SEC's records use."""

    cik: int
    #: Accession number in its dashed form, as `Filing.adsh` holds it.
    adsh: str


#: Every page of a filing sits under `/Archives/edgar/data/<cik>/`: the primary
#: document, an exhibit, an XBRL `R8.htm` page, the filing's own index. `<cik>` is the
#: filer's or a co-registrant's, unpadded.
_ARCHIVE_PATH: Final = re.compile(r"^/Archives/edgar/data/(\d{1,10})/(.+)$", re.IGNORECASE)

#: The folder a filing's documents sit in: its accession number without the dashes.
_FILING_FOLDER: Final = re.compile(r"^(\d{10})(\d{2})(\d{6})(?:/|$)")

#: Two pages are named for the accession itself and sit beside the folder rather than
#: in it: the older spelling of a filing's index, `0001326801-23-000067-index.htm`, and
#: the whole submission as one text file, `0001326801-23-000067.txt`.
_NAMED_PAGE: Final = re.compile(r"^(\d{10}-\d{2}-\d{6})(?:-index\.html?|\.txt)$", re.IGNORECASE)

#: SEC's inline-XBRL viewer wraps an archive address in a query,
#: `/ix?doc=/Archives/edgar/data/...`, which is the address a link from a news article
#: or a search result often carries.
_VIEWER_PATHS: Final = frozenset({"/ix", "/ixviewer/ix.html"})

#: What a `filings.files` entry may be called. It goes into a request URL, so it is
#: checked rather than trusted.
_OLDER_PAGE: Final = re.compile(r"^CIK\d{10}-submissions-\d+\.json$")

#: EDGAR's first electronic filings are from 1993; a date before it is not SEC's.
_EDGAR_BEGINS: Final = dt.date(1993, 1, 1)


def parse_filing_url(url: str) -> FilingRef | None:
    """The filing a URL is a page of, or None when it is not an EDGAR archive address.

    Reads the CIK and the accession number out of the path, whichever page of the
    filing it is: the primary document, an exhibit, an XBRL `R` page, the filing's
    folder or its `-index.htm`, the whole-submission `.txt`, or the inline viewer's
    `?doc=` wrapper of any of those. A query string such as a tracking `?ref=` is
    ignored. SEC's own pages that are not filings — its news, its rules, a company's
    directory listing — come back None, so a caller can ask of every URL it holds.
    """
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host != "sec.gov" and not host.endswith(".sec.gov"):
        return None
    path = parts.path
    if path.lower() in _VIEWER_PATHS:
        wrapped = parse_qs(parts.query).get("doc")
        path = urlsplit(wrapped[0]).path if wrapped else ""
    match = _ARCHIVE_PATH.match(path)
    if match is None:
        return None
    cik, rest = int(match.group(1)), match.group(2)
    if cik <= 0:
        return None
    folder = _FILING_FOLDER.match(rest)
    if folder is not None:
        return FilingRef(cik, "-".join(folder.groups()))
    named = _NAMED_PAGE.match(rest)
    return FilingRef(cik, named.group(1)) if named is not None else None


def _accession_year(adsh: str) -> int:
    """The year an accession number was assigned, from its two-digit middle part."""
    two = int(adsh[11:13])
    return 1900 + two if two >= 93 else 2000 + two


def _may_hold(page: Mapping[str, Any], year: int) -> bool:
    """Whether an older-filings page covers filings from `year`, give or take one.

    The span a page states is of filing dates, and an accession number carries the year
    it was assigned, which can differ from the filing's by a day at a year's end — so a
    year either side is allowed. A page that states no span might hold anything.
    """
    first, last = str(page.get("filingFrom") or "")[:4], str(page.get("filingTo") or "")[:4]
    if not (first.isdigit() and last.isdigit()):
        return True
    return int(first) - 1 <= year <= int(last) + 1


def _as_datetime(day: str | None) -> dt.datetime | None:
    """SEC's `YYYY-MM-DD` as the naive midnight the schema stores publication dates in."""
    if not day:
        return None
    try:
        when = dt.date.fromisoformat(day[:10])
    except ValueError:
        return None
    if not _EDGAR_BEGINS <= when <= dt.date.today() + dt.timedelta(days=2):
        return None
    return dt.datetime.combine(when, dt.time())


@dataclass
class _Company:
    """What SEC's submissions record has said so far about one company."""

    #: Dashed accession number -> the filing date as SEC states it, "2026-07-30".
    dates: dict[str, str] = field(default_factory=dict)
    #: The older-filings pages (`filings.files`) not yet read, newest first.
    older: list[dict[str, Any]] = field(default_factory=list)

    def take(self, block: Mapping[str, Any]) -> None:
        """Remember every filing a block of the record lists. A malformed one adds nothing."""
        accessions, filed = block.get("accessionNumber"), block.get("filingDate")
        if not isinstance(accessions, list) or not isinstance(filed, list):
            return
        for adsh, day in zip(accessions, filed, strict=False):
            if isinstance(adsh, str) and isinstance(day, str) and adsh and day:
                self.dates[adsh] = day


class FilingDates:
    """SEC's own record of when each filing was filed, asked once per company.

    :meth:`filed` answers for one filing and :meth:`of` for one URL. The company's whole
    record is read on the first question about it and remembered for the life of this
    object, so a run's cost is its number of companies, not its number of pages, and
    SEC's limit of ten requests a second is kept by the same client the search uses.

    **It never raises.** A date is metadata about a page that was read, so failing to
    find one must not fail the read: every refusal, timeout, malformed answer or
    unknown filing is logged and answers None, and a caller leaves the date empty.
    A filing SEC has no record of is a None as well, never a guess.

    **It stops asking when SEC stops answering.** Three failures in a row — a block, a
    rate limit, a night when the host is down — end the questions for this object, with
    one line saying so, rather than sending a request per page into a refusal. A 404
    is not one of them: it is SEC saying it knows no such company, which is an answer.

    `client` stands in for the HTTP client, for tests that serve the record without a
    network. Anything with a `get(url)` returning a status code and `json()` will do.
    Without one the first question builds the search's own rate-limited client, which
    refuses a placeholder User-Agent — SEC blocks the whole run for sending it — and
    that refusal is one more failure to log, not an exception.
    """

    #: Failures in a row after which SEC is taken to be refusing and nothing more is asked.
    GIVE_UP_AFTER: Final = 3

    def __init__(self, settings: Settings | None = None, *, client: Any = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        #: CIK -> its record, or None for one SEC does not know or did not answer for:
        #: asked once either way, because asking again within a run changes nothing.
        self._companies: dict[int, _Company | None] = {}
        self._failures = 0
        self._gave_up = False

    def __enter__(self) -> FilingDates:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def of(self, url: str) -> dt.datetime | None:
        """The date SEC filed the filing this URL is a page of, or None."""
        ref = parse_filing_url(url)
        return self.filed(ref) if ref is not None else None

    def filed(self, ref: FilingRef) -> dt.datetime | None:
        """The date SEC filed this filing, as the day at midnight, or None."""
        try:
            return self._filed(ref)
        except Exception:
            log.warning("could not date SEC filing %s of CIK %d", ref.adsh, ref.cik, exc_info=True)
            return None

    def _filed(self, ref: FilingRef) -> dt.datetime | None:
        company = self._company(ref.cik)
        if company is None:
            return None
        day = company.dates.get(ref.adsh)
        year = _accession_year(ref.adsh)
        # The newest thousand filings come with the company; an older one is in a page
        # of its own, and the span each page states says whether it is worth a request.
        while day is None and not self._gave_up:
            page = next((p for p in company.older if _may_hold(p, year)), None)
            if page is None:
                break
            company.older.remove(page)
            block = self._get(f"{SUBMISSIONS}/{page['name']}")
            if block is not None:
                company.take(block)
                day = company.dates.get(ref.adsh)
        if day is None:
            log.info("SEC's record of CIK %d lists no filing %s", ref.cik, ref.adsh)
        return _as_datetime(day)

    def _company(self, cik: int) -> _Company | None:
        if self._gave_up:
            return None
        if cik not in self._companies:
            data = self._get(f"{SUBMISSIONS}/CIK{cik:010d}.json")
            self._companies[cik] = _read_company(data) if data is not None else None
        return self._companies[cik]

    def _get(self, url: str) -> dict[str, Any] | None:
        """One document from SEC as parsed JSON, or None after logging why not."""
        if self._gave_up:
            return None
        try:
            if self._client is None:
                self._client = _Client(self._settings or get_settings())
        except EdgarError as exc:
            # The placeholder User-Agent: every request would be refused, so no more
            # are tried. The message is multi-line operator advice; the first line says it.
            log.warning("SEC filing dates not asked for: %s", str(exc).splitlines()[0])
            self._gave_up = True
            return None
        try:
            response = self._client.get(url)
        except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
            return self._refused(url, f"{type(exc).__name__}: {exc}")
        if response.status_code == 404:
            log.info("SEC has no record at %s", url)
            self._failures = 0
            return None
        if response.status_code != 200:
            return self._refused(url, f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            return self._refused(url, "an answer that is not JSON")
        if not isinstance(data, dict):
            return self._refused(url, "an answer that is not a JSON object")
        self._failures = 0
        return data

    def _refused(self, url: str, why: str) -> None:
        self._failures += 1
        log.warning("SEC filing dates: %s gave %s", url, why)
        if self._failures >= self.GIVE_UP_AFTER:
            self._gave_up = True
            log.warning(
                "SEC filing dates: %d requests in a row failed; no more are asked this run",
                self._failures,
            )
        return None


def _read_company(data: Mapping[str, Any]) -> _Company:
    """A company's record from the submissions document SEC serves for it."""
    filings = data.get("filings")
    filings = filings if isinstance(filings, dict) else {}
    company = _Company()
    recent = filings.get("recent")
    if isinstance(recent, dict):
        company.take(recent)
    pages = filings.get("files")
    company.older = [
        page
        for page in (pages if isinstance(pages, list) else [])
        if isinstance(page, dict) and _OLDER_PAGE.match(str(page.get("name") or ""))
    ]
    return company


def dates_of(
    urls: Iterable[str], *, resolver: FilingDates | None = None, settings: Settings | None = None
) -> dict[str, dt.datetime]:
    """url -> the date SEC filed it, for each of `urls` that is a page of a filing SEC dates.

    The one question both callers ask — `crawl.run` of the pages it has just read and
    `dates.run` of the citations still undated — so it lives beside the thing it asks.
    A URL that is not an EDGAR page, or that SEC does not date, is simply absent. With
    no `resolver` one is made, and closed, for this call; a caller asking more than
    once shares one so that a company is read once between them.
    """
    wanted = {url: ref for url in urls if (ref := parse_filing_url(url)) is not None}
    if not wanted:
        return {}
    found: dict[str, dt.datetime] = {}
    own = resolver if resolver is not None else FilingDates(settings)
    try:
        for url, ref in wanted.items():
            when = own.filed(ref)
            if when is not None:
                found[url] = when
    finally:
        if resolver is None:
            own.close()
    return found


# --- Section selection -------------------------------------------------------

#: A paragraph must mention the subject to be worth scoring at all. Without this
#: anchor the scorer happily returns the executive-compensation table, which is
#: dense with dollar amounts and dates and says nothing about a data center.
_ANCHOR = re.compile(r"data\s?cent|hyperscale|colocation|megawatt|campus", re.I)

_TENANT = re.compile(
    r"\b(tenant|lessee|customer|counterparty|anchor|build-to-suit|take-or-pay|"
    r"lease agreement|master lease)\b",
    re.I,
)
_PHASE = re.compile(
    r"\b(under construction|commenced construction|broke ground|placed in service|"
    r"substantially complete|commissioned|energiz|operational|delivered|"
    r"expected to be completed|cancell?ed|paused|suspended)\b",
    re.I,
)

#: Boilerplate that scores well and is worthless. Risk factors are hypothetical
#: by definition and forward-looking-statement blocks are lawyers hedging, so a
#: value quoted from either is not a fact about a project.
_NOISE = re.compile(
    r"forward-looking statements|risk factors|we may be unable|no assurance|"
    r"could adversely affect|undue reliance",
    re.I,
)

#: Vocabulary that appears throughout a contract and essentially never in
#: disclosure prose. Used at document level, not paragraph level — see
#: :func:`looks_like_contract`.
_LEGALESE = re.compile(
    r"\bhereunder\b|\bhereto\b|\bherein\b|\bthereof\b|\bthereto\b|"
    r"collateral agent|credit part(y|ies)|\blien\b|indemnif|"
    r"shall have the meaning|as defined in|pursuant to section|"
    r"\bcovenant\b|governing law|in accordance with section",
    re.I,
)

#: Legal terms per 10,000 characters above which a document is a contract.
#:
#: Measured, and the separation is not marginal:
#:
#:     8-K press release exhibit    0.5
#:     Meta 10-Q                    0.3
#:     8-K credit agreement        20.1
#:
#: 5.0 sits in a forty-fold gap, so this is a threshold on a bimodal distribution
#: rather than a tuned constant.
CONTRACT_LEGALESE_PER_10K = 5.0


def looks_like_contract(text: str) -> bool:
    """True for a credit agreement, lease or indenture rather than disclosure.

    An 8-K exhibit is as often a contract as a press release, and a contract is
    *dense* with exactly what :func:`score_paragraph` rewards — "Data Center
    Site", "power supply has commenced", "delivered". Observed on the first live
    run against real filings: a 545,000-character CoreWeave financing exhibit
    produced a top-scoring 18,000-character section of lien and collateral
    language that stated nothing about any site, and cost an LLM call to discover.

    Rejecting at document level rather than penalising paragraphs, because a
    contract is not uniformly legalistic — the operative clauses that score well
    are often the ones with the least boilerplate in them, so a per-paragraph
    filter lets precisely the wrong paragraphs through.
    """
    if not text:
        return False
    density = len(_LEGALESE.findall(text)) / max(1, len(text)) * 10_000
    return density >= CONTRACT_LEGALESE_PER_10K


#: MW and money lead because they are the scarcest facts we hold.
_WEIGHTS: tuple[tuple[re.Pattern[str], float], ...] = (
    (_MW_EXPR, 5.0),
    (_MONEY_EXPR, 4.0),
    (_TENANT, 3.0),
    (_PHASE, 2.5),
    (_RECENT_DATE_EXPR, 1.5),
)

#: Below this, a "paragraph" is a table cell or a page number.
_MIN_PARAGRAPH = 40


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) >= _MIN_PARAGRAPH]


def score_paragraph(paragraph: str) -> float:
    """How much verifiable data center fact this paragraph carries."""
    if not _ANCHOR.search(paragraph):
        return 0.0
    score = 0.0
    for pattern, weight in _WEIGHTS:
        hits = len(pattern.findall(paragraph))
        if hits:
            # Diminishing returns: a table of forty dates is not forty facts.
            score += weight * (1 + 0.3 * (hits - 1))
    if _NOISE.search(paragraph):
        score *= 0.15
    # Normalised by length, so a 6,000-character table cannot win on volume.
    return score / max(1.0, len(paragraph) / 800.0)


def extract_section(text: str, *, budget: int, context: int = 1) -> str:
    """The highest-scoring paragraphs, with neighbours, within `budget` chars.

    `context` pulls in the paragraph either side of a hit, because a figure
    routinely sits in the sentence after the one naming the project. Paragraphs
    are re-joined in document order, not score order, so the model reads prose
    rather than a ranked list.
    """
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return ""
    ranked = sorted(
        ((score_paragraph(p), i) for i, p in enumerate(paragraphs) if score_paragraph(p) > 0),
        reverse=True,
    )
    chosen: set[int] = set()
    used = 0
    for _score, index in ranked:
        block = [j for j in range(index - context, index + context + 1) if 0 <= j < len(paragraphs)]
        cost = sum(len(paragraphs[j]) + 2 for j in block if j not in chosen)
        if used + cost > budget:
            continue
        chosen.update(block)
        used += cost
    return "\n\n".join(paragraphs[j] for j in sorted(chosen))


# --- Run ---------------------------------------------------------------------


@dataclass
class EdgarReport:
    companies: int = 0
    filings_found: int = 0
    already_cached: int = 0
    fetched: int = 0
    fetch_errors: int = 0
    contracts_skipped: int = 0
    no_section: int = 0
    prepared: int = 0

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("companies searched", self.companies),
            ("filings found", self.filings_found),
            ("already cached", self.already_cached),
            ("documents fetched", self.fetched),
            ("fetch errors", self.fetch_errors),
            ("contracts skipped", self.contracts_skipped),
            ("no relevant section", self.no_section),
            ("prepared for extraction", self.prepared),
        ]


def prepare(
    *,
    companies_path: Path | None = None,
    cache_dir: Path,
    settings: Settings | None = None,
    per_company: int = 2,
    only: str | None = None,
    kind: str | None = None,
    since_days: int | None = 730,
) -> tuple[EdgarReport, list[str]]:
    """Find filings, cache the relevant section of each, return their URLs.

    The URLs are handed to `crawl.run` with the same `cache_dir`, which serves
    every one of them from disk. No part of the extraction, gating or upsert
    logic is duplicated here.

    Args:
        per_company: filings per company per phrase. Each becomes one LLM call
            downstream, so this is the cost dial.
        only: restrict to one company by name, case-insensitive.
        kind: restrict to one class — hyperscaler, neocloud, landlord, utility,
            contractor. This is the cost dial that matters once the list covers
            more than one kind of filer: reading the utilities is a different
            question from reading the hyperscalers, and worth being able to ask
            on its own.
        since_days: ignore filings older than this. Defaults to two years, because
            a decade-old 8-K describes a campus that is long since built.
    """
    settings = settings or get_settings()
    companies, phrases, forms = load_companies(companies_path)
    if only:
        wanted = only.strip().lower()
        companies = [c for c in companies if c.name.lower() == wanted]
        if not companies:
            raise EdgarError(f"no company named {only!r} in the company list")
    if kind:
        wanted_kind = kind.strip().lower()
        companies = [c for c in companies if c.kind.lower() == wanted_kind]
        if not companies:
            known = sorted({c.kind for c in load_companies(companies_path)[0] if c.kind})
            raise EdgarError(f"no companies of kind {kind!r}. Known kinds: {', '.join(known)}")

    since = dt.date.today() - dt.timedelta(days=since_days) if since_days else None
    report = EdgarReport()
    urls: list[str] = []
    cache_dir.mkdir(parents=True, exist_ok=True)

    with _Client(settings) as client:
        for company in companies:
            report.companies += 1
            seen: set[str] = set()
            for phrase in company.phrases or phrases:
                for filing in search(
                    client, company, phrase, forms, limit=per_company, since=since
                ):
                    if filing.url in seen:
                        continue
                    seen.add(filing.url)
                    report.filings_found += 1

                    path = cache_path(filing.url, cache_dir)
                    if path.is_file():
                        report.already_cached += 1
                        urls.append(filing.url)
                        continue

                    response = client.get(filing.url)
                    if response.status_code != 200:
                        log.warning(
                            "%s %s: HTTP %s", company.name, filing.form, response.status_code
                        )
                        report.fetch_errors += 1
                        continue
                    report.fetched += 1

                    body = html_to_text(response.text)
                    if looks_like_contract(body):
                        log.info(
                            "%s %s %s: exhibit is a contract, not disclosure — skipping",
                            company.name,
                            filing.form,
                            filing.file_date,
                        )
                        report.contracts_skipped += 1
                        continue

                    section = extract_section(body, budget=settings.max_input_chars)
                    if not section:
                        # The filing matched on a passing mention with no figure
                        # attached. Spending an LLM call on it would buy nothing.
                        log.info(
                            "%s %s %s: no scoring paragraphs, skipping",
                            company.name,
                            filing.form,
                            filing.file_date,
                        )
                        report.no_section += 1
                        continue

                    # The heading gives the extractor the filer and the form,
                    # which the section body does not repeat and which it needs
                    # to attribute the facts to a company.
                    header = f"{company.name} — SEC {filing.form} filed {filing.file_date}"
                    path.write_text(f"{header}\n\n{section}", encoding="utf-8")
                    report.prepared += 1
                    urls.append(filing.url)
                    log.info(
                        "%s %s %s: %d chars prepared",
                        company.name,
                        filing.form,
                        filing.file_date,
                        len(section),
                    )
    return report, urls


__all__ = [
    "Company",
    "EdgarError",
    "EdgarReport",
    "Filing",
    "FilingDates",
    "FilingRef",
    "dates_of",
    "extract_section",
    "load_companies",
    "parse_filing_url",
    "prepare",
    "score_paragraph",
    "search",
]
