"""Pages that cannot be news of a US campus, told by their address and headline.

The nightly crawl reads 600 queued pages and on 2026-10-19 only 169 of 594 produced a
campus (28%); 278 found none and 117 failed to fetch. Some of what it read could
never have produced one, and the address or headline already said so: a PDF, which
the reader takes no text from; Google's own campus pages again in twelve
translations; a stock-quote page; a ZIP-code page of a directory; a job posting; a
football recap from a campus town; a builder's post about Frankfurt. This module
names those kinds, so they are set aside before they are read
(`discover.set_aside_not_news`) and, from the news watch, never queued at all
(`watch._screened`).

**A rule here has to be safe on the whole queue, not right on average.** Each one
was measured against everything the crawl has ever read before it was written, and
the ones that would have cost a campus were narrowed or left out:

* *Listing pages are read.* `/tag/`, `/category/` and `?page=N` pages produced a
  citation in 30-40% of reads (rebusinessonline's data-center category, mlq.ai's
  permit listings), and a directory's county, state or market page about as often
  as the night's average, so only the one kind that never produced anything goes:
  morethanjustparks's ZIP-code pages, 0 in 16.
* *An old builder post is set aside only when it names no place and no campus
  event.* As first framed — "before 2024, unless it names a campus held" — the rule
  would have set aside Meta's "Henrico County, we are online!" (2020), "Hello again,
  Los Lunas" (2021) and Sabey's Umatilla announcement (2023), each of which created
  the row the database now holds for that campus: old builder posts produced a
  citation in 52 of 158 reads (33%), better than the night's average.
* *"Jobs", "closes" and "police" are not off-topic words.* Of 102 news-watch
  headlines in the queue on 2026-10-19 carrying one of a dozen such words, 77 were
  data-center news ("adding 700 jobs", "closes $3 billion financing", Bossier
  Parish's police jury approving a campus), and 10 of the other 25 were job
  postings, which the address rules catch. Off-topic local news is judged only by
  the news watch, which has the snippet, and only for a result that names no
  data-center term (`off_topic`).

Every verdict is a short reason that says what the page is, stored on the row with
:data:`SET_ASIDE` in front, so a reader of `tracker queue` sees why, and a rule that
is later dropped or narrowed gives its pages back (`discover.set_aside_not_news`
re-asks every page it set aside on each run).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

#: The `error` a queued page carries once it is set aside here; the reason follows
#: it. A constant, not prose: `discover.set_aside_not_news` finds the pages it set
#: aside by it, to put back any that no rule names any more.
SET_ASIDE: Final = "set aside before reading: "


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().split("@")[-1].split(":")[0].removeprefix("www.")


def _on(host: str, domain: str) -> bool:
    """Whether `host` is `domain` or a subdomain of it, on label boundaries."""
    return host == domain or host.endswith("." + domain)


# --- Files, not pages -----------------------------------------------------------------

#: Image, video, audio and archive files. The plain client hands their bytes to the
#: crawl as text; nothing in them is a sentence.
_MEDIA: Final = frozenset(
    {
        *("jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "tif", "tiff", "ico", "heic"),
        *("avif", "mp4", "m4v", "mov", "avi", "wmv", "webm", "mkv", "mpg", "mpeg", "mp3"),
        *("m4a", "wav", "aac", "ogg", "flac", "zip", "rar", "7z", "gz", "tgz", "dmg", "exe"),
    }
)

#: Documents in a binary format. **A PDF is set aside although many are filings
#: worth reading** — a county's findings report, a fiscal-impact study, comments on
#: a campus — because nothing in this pipeline takes text from one: the plain client
#: passes the file's bytes to the model as if they were prose (107,617 characters of
#: compressed streams for one 2025 Nevada tax-abatement PDF, all of it "English" to
#: the language check). Of 384 PDFs read since August, none produced a citation and
#: 352 were paid model calls on that garbage. When something does read them, delete
#: "pdf" here and the next run puts every one set aside back in the queue.
_DOCUMENTS: Final = frozenset({"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt"})


def _extension(path: str) -> str:
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[-1].lower() if "." in last else ""


def file_kind(url: str) -> str | None:
    """Why the address is a file rather than a page, or None."""
    parts = urlsplit(url)
    ext = _extension(parts.path)
    if ext == "pdf":
        return "a PDF, which the reader takes no text from"
    if ext in _DOCUMENTS:
        return f"a .{ext} document, which the reader takes no text from"
    if ext in _MEDIA:
        return f"a .{ext} file, not an article"
    if "attachment_id=" in parts.query or "/attachment/" in parts.path.lower():
        return "a WordPress attachment page"
    return None


#: Google's data-center site serves every page again under `/intl/<language>_ALL/`,
#: in twelve translations: 114 of its 166 pages in the queue on 2026-10-19 were these
#: copies, each also queued in English. The crawl refuses a page not in English, so
#: each would have cost a fetch for nothing.
_TRANSLATION: Final = re.compile(r"^/intl/([a-z]{2})(?:[-_][a-z0-9]{2,3})?(?:_all)?/", re.I)


def translation(url: str) -> str | None:
    match = _TRANSLATION.match(urlsplit(url).path)
    if match and match.group(1).lower() != "en":
        return f"a translated copy of a page ({match.group(0).rstrip('/')})"
    return None


# --- Sites and sections that are not news ---------------------------------------------

#: Hosts whose pages are never campus news, with what each is. Measured on every
#: read before 2026-10-19 (citations / reads):
#:
#: * Stock chatter and quotes: seekingalpha 0/21 (paywall), simplywall.st 1/11,
#:   stocktwits 1/9, fool.com 0/4, zacks 0/2, advfn 0/2, gurufocus 0/2, marketbeat
#:   0/2 ("instant alerts", refused as thin), tickeron 0/1 and ad-hoc-news.de's
#:   price ticks. Left open, because they read and cite: stocktitan (29/71, it
#:   reprints wire releases and filings), investing.com's news (9/17),
#:   marketscreener (4/4), benzinga (1/1); and tipranks, read once (a 403) while three
#:   of its queued headlines are campus news found nowhere else in the queue
#:   ("STACK Infrastructure Plans 51MW High-Density Expansion in New Albany").
#: * Directories that answer every request with HTTP 429: datacenters.com 0/192 and
#:   aterio.io 0/14. nasdaq.com's press-release mirrors failed 7 of 7 (403).
#: * Job boards, sports results, restaurant reviews: nothing there is a campus. Not
#:   builtin.com and its city sites, which publish articles beside their postings;
#:   a posting there has a `/job/` in its address.
_NOT_NEWS_HOSTS: Final[dict[str, str]] = {
    **dict.fromkeys(
        (
            "seekingalpha.com",
            "simplywall.st",
            "stocktwits.com",
            "fool.com",
            "zacks.com",
            "advfn.com",
            "gurufocus.com",
            "marketbeat.com",
            "tickeron.com",
            "ad-hoc-news.de",
            "insidermonkey.com",
            "stockanalysis.com",
            "finviz.com",
        ),
        "stock chatter and quotes",
    ),
    **dict.fromkeys(
        ("datacenters.com", "aterio.io", "nasdaq.com"),
        "a site that has refused every read (HTTP 429 or 403)",
    ),
    **dict.fromkeys(
        (
            "simplyhired.com",
            "jobleads.com",
            "ziprecruiter.com",
            "monster.com",
            "careerbuilder.com",
            "career.com",
            "interviewpal.com",
            "callings.ai",
            "jobright.ai",
            "earnbetter.com",
            "trabajo.org",
            "lensa.com",
            "talent.com",
            "jooble.org",
            "myworkdayjobs.com",
            "icims.com",
            "smartrecruiters.com",
        ),
        "a job board",
    ),
    **dict.fromkeys(
        ("maxpreps.com", "tripadvisor.com", "yelp.com", "eater.com", "opentable.com"),
        "sports, dining or reviews",
    ),
}

#: A job posting on a company's own site: `jobs.apple.com`, `careers.equinix.com`,
#: `ats.rippling.com`, `ironmountain.jobs`, or an address with a `/job/` segment
#: (tesla.com/careers/search/job/...). Not `/jobs/` on its own: The Dispatch files
#: campus news under /story/marketplace/jobs/ and the Dallas Morning News under
#: /business/jobs/.
_JOB_HOST: Final = re.compile(r"^(?:jobs|careers|ats|apply|boards|job-boards)\.|\.jobs$")
_JOB_PATH: Final = re.compile(r"(?:^|/)job(?:/|$)", re.I)

#: Stock-quote pages, by site: a ticker and at most one tab after it. Not the pages
#: beneath a quote that are articles: finance.yahoo.com's earnings-call transcripts
#: (/quote/CIFR/earnings/CIFR-Q3-2025-earnings_call-....html) produced 3 citations in
#: 7 reads, and marketscreener files news under /quote/stock/<name>/news/<story>.
_QUOTE_PAGES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (host, re.compile(pattern, re.I))
    for host, pattern in (
        ("finance.yahoo.com", r"^/quote/[^/]+(?:/[a-z-]+)?/?$"),
        ("investing.com", r"^/equities/[^/]+/?$"),
        ("marketscreener.com", r"^/quote/stock/[^/]+(?:/[a-z-]+)?/?$"),
        ("marketwatch.com", r"^/investing/stock/[^/]+(?:/[a-z-]+)?/?$"),
        ("cnbc.com", r"^/quotes/[^/]+/?$"),
        ("google.com", r"^/finance/quote/[^/]+/?$"),
        ("bloomberg.com", r"^/quote/[^/]+/?$"),
        ("reuters.com", r"^/markets/companies/[^/]+(?:/[a-z-]+)?/?$"),
        ("morningstar.com", r"^/stocks/[^/]+/[^/]+/[a-z-]+/?$"),
        ("wsj.com", r"^/market-data/quotes/[^/]+(?:/[a-z-]+)?/?$"),
        ("stocktitan.net", r"^/(?:quote|chart)/[^/]+/?$"),
    )
)

#: Directory listings that have never produced a campus: morethanjustparks's
#: ZIP-code pages ("54 Data Centers Near New Albany, OH (43054)"), nothing in 16
#: reads, each a radius around campus pages the directory also publishes (98
#: citations in 122 reads). **Other directory listings are read**, because they do
#: produce campuses: dcmap.us's county, state and market pages 6 citations in 19
#: reads and morethanjustparks's state pages 8 in 21, about the night's average,
#: and on 2026-10-19 nine of them created or filled a row ("Pecos County, TX Data
#: Centers: 8 Sites, 8.3 GW" is the only source one Pecos campus has, and one of two
#: for two more).
_LISTINGS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (host, re.compile(pattern, re.I))
    for host, pattern in (("morethanjustparks.com", r"^/data-center-tracker/near/"),)
)

#: A car dealer's inventory: "New 2026 Subaru ASCENT", queued for Ascent Corp.'s
#: Plano campus because the company's name is a trim level.
_DEALER: Final = re.compile(r"/inventory/(?:new|used|certified|pre-owned)-", re.I)


def site_kind(url: str) -> str | None:
    """Why the site or section is not news, or None."""
    host = _host(url)
    path = urlsplit(url).path
    for domain, what in _NOT_NEWS_HOSTS.items():
        if _on(host, domain):
            return f"{domain}: {what}"
    if _JOB_HOST.search(host) or _JOB_PATH.search(path):
        return "a job posting"
    for domain, pattern in _QUOTE_PAGES:
        if _on(host, domain) and pattern.match(path):
            return "a stock-quote page"
    for domain, pattern in _LISTINGS:
        if _on(host, domain) and pattern.match(path):
            return f"a directory listing on {domain}, not an article"
    if _DEALER.search(path):
        return "a car dealer's inventory"
    return None


def not_news(url: str) -> str | None:
    """Why the page at this address cannot be news of a US campus, or None when it
    might be: a file, a translated copy, or a site or section that is not news."""
    return file_kind(url) or translation(url) or site_kind(url)


#: Local news about something else whatever town it names: a game, an obituary, a
#: killing, a restaurant, a car lot. The news watch found these because a campus's
#: town or company was in the result ("Football Recap: New Albany Extends Home
#: Winning Streak" for the New Albany Co.'s corridor, "Beantown Taqueria Closing MIT
#: Storefront" for MIT's Cambridge lab, "Elon Musk Threatens to Open a Tesla Diner"
#: for Tesla's Palo Alto Dojo). Only the watch asks it, where the snippet is in hand,
#: and only of a result kept for naming a campus and carrying no data-center term:
#: in the queue, where only the headline is, "Worker killed in Ashburn trench
#: collapse" was queued because its snippet said "data center", and is news of a site.
_OFF_TOPIC: Final = re.compile(
    r"\b(?:football|basketball|baseball|softball|volleyball|soccer|wrestling)\s+(?:recap|scores?)\b"
    r"|\bbox score\b|\bhomecoming\b|\bobituar(?:y|ies)\b|\bfuneral\b"
    r"|\b(?:murder(?:ed)?|homicide|stabbing|manslaughter)\b"
    r"|\b(?:restaurant|taqueria|diner|eatery|bakery|dealership|test drive)\b",
    re.I,
)


def off_topic(text: str) -> str | None:
    """Why a headline and snippet are local news about something else, or None."""
    match = _OFF_TOPIC.search(text)
    return f"local news about something else ({match.group(0).lower()!r})" if match else None


# --- A builder's own archive: abroad, and old ---------------------------------------
#
# Builders' newsrooms are read whole (`discover.watch_newsrooms`), so they bring
# everything a builder ever posted: CyrusOne's Frankfurt and Madrid, Aligned's ODATA
# in Brazil and Mexico, Cologix's Canada, Google's Hamina and Inzai. A campus abroad
# cannot become a row (a row needs a US state; the crawl drops it as "needs company,
# state and a locality", which is what Meta's 2019 Odense and Singapore posts did on
# 2026-10-19). Set aside only when the address and headline name a foreign place and
# no US one, and no campus the database holds.

#: Countries and regions, their adjectives, and foreign cities that no US town of
#: data-center note shares. Left out on purpose: London, Dublin, Paris, Berlin, Rome,
#: Vienna, Athens, Toledo, Cambridge, Manchester, Birmingham, Melbourne and Vancouver
#: are all US towns too, as are Lebanon, Jordan, Peru, Cuba, Holland and Turkey.
#: "Mexico" and "England" count only when not "New Mexico" and "New England"
#: (`_NEW_PREFIXED`), and "Georgia" is always the state.
_FOREIGN: Final = frozenset(
    {
        # countries
        *("argentina", "australia", "austria", "bahrain", "belgium", "brazil", "bulgaria"),
        *("canada", "chile", "china", "colombia", "croatia", "czech republic", "czechia"),
        *("denmark", "danmark", "egypt", "england", "estonia", "finland", "france"),
        *("germany", "deutschland", "greece", "hong kong", "hungary", "iceland", "india"),
        *("indonesia", "ireland", "israel", "italy", "japan", "kenya", "korea", "latvia"),
        *("lithuania", "luxembourg", "malaysia", "mexico", "morocco", "netherlands"),
        *("new zealand", "nigeria", "norway", "oman", "pakistan", "philippines", "poland"),
        *("portugal", "qatar", "romania", "russia", "saudi arabia", "scotland", "serbia"),
        *("singapore", "slovakia", "slovenia", "south africa", "spain", "sweden"),
        *("switzerland", "taiwan", "thailand", "uae", "united arab emirates"),
        *("united kingdom", "uk", "uruguay", "vietnam"),
        # regions
        *("europe", "emea", "apac", "asia", "asia pacific", "latin america", "latam"),
        *("nordics", "middle east", "africa"),
        # adjectives
        *("australian", "austrian", "belgian", "brazilian", "british", "canadian", "chilean"),
        *("chinese", "colombian", "danish", "dutch", "european", "finnish", "french"),
        *("german", "indian", "indonesian", "irish", "italian", "japanese", "korean"),
        *("malaysian", "mexican", "nordic", "norwegian", "portuguese", "saudi", "scottish"),
        *("singaporean", "spanish", "swedish", "swiss", "taiwanese", "thai", "vietnamese"),
        # cities, and the towns of builders' campuses abroad
        *("amsterdam", "frankfurt", "madrid", "barcelona", "milano", "munich", "copenhagen"),
        *("stockholm", "oslo", "helsinki", "zurich", "geneva", "warsaw", "prague"),
        *("brussels", "lisbon", "marseille", "toronto", "montreal", "calgary", "ottawa"),
        *("quebec", "edmonton", "winnipeg", "tokyo", "osaka", "inzai", "seoul", "taipei"),
        *("shanghai", "beijing", "shenzhen", "mumbai", "chennai", "hyderabad", "bangalore"),
        *("bengaluru", "jakarta", "johor", "kuala lumpur", "bangkok", "manila", "sydney"),
        *("auckland", "sao paulo", "rio de janeiro", "santiago", "bogota", "buenos aires"),
        *("queretaro", "monterrey", "guadalajara", "dubai", "abu dhabi", "riyadh", "doha"),
        *("tel aviv", "johannesburg", "cape town", "lagos", "nairobi", "istanbul", "monaco"),
        *("slough", "hanau", "eemshaven", "middenmeer", "winschoten", "hamina", "muhos"),
        *("vaala", "horndal", "fredericia", "odense", "esbjerg", "aabenraa", "ghislain"),
        *("quilicura", "cerrillos", "canelones", "waltham cross", "lulea", "bahia"),
    }
)

#: A US state and the foreign place inside its name: "New Mexico", "New England".
_NEW_PREFIXED: Final = re.compile(r" new (?:mexico|england) ")

#: Words that place a page in the US without naming a state: "U.S.", "America", and
#: a state's people. "Fermi enlists Korean firms for Texan reactors" is about
#: Amarillo.
_US_WORDS: Final = frozenset(
    {
        *("us", "u s", "usa", "united states", "america", "american", "americas"),
        *("nationwide", "texan", "texans", "virginian", "virginians", "ohioan", "ohioans"),
        *("californian", "californians", "floridian", "arizonan", "oklahoman", "coloradan"),
        *("nevadan", "utahn", "idahoan", "hoosier", "hoosiers", "iowan", "kansan"),
        *("kentuckian", "louisianan", "louisianian", "marylander", "michigander"),
        *("minnesotan", "mississippian", "missourian", "nebraskan", "new yorker"),
        *("carolinian", "dakotan", "oregonian", "pennsylvanian", "tennessean"),
        *("washingtonian", "wisconsinite", "wyomingite", "alabamian", "alaskan"),
        *("arkansan", "georgian", "illinoisan", "hawaiian", "montanan", "new englander"),
    }
)

#: Two-letter state codes that are not also English words or common abbreviations,
#: for slugs like "plano-tx" and "ashburn-va". Not "in", "or", "me", "oh", "ok",
#: "hi", "de", "la", "ma", "co", "id", "al", "pa", "ne", "mo", "ms", "md", and not
#: "dc", which in a builder's slug is a data center ("fra7-dc").
_STATE_CODES: Final = frozenset(
    {
        *("ak", "az", "ar", "ca", "ct", "fl", "ga", "ia", "il", "ks", "ky", "mi", "mn"),
        *("mt", "nc", "nd", "nh", "nj", "nm", "nv", "ny", "ri", "sc", "sd", "tn", "tx"),
        *("ut", "va", "vt", "wa", "wi", "wv", "wy"),
    }
)

#: What a row's city holds when the article named none.
_NOT_A_PLACE: Final = frozenset({"various", "multiple", "tbd", "unknown", "n a", "na"})


@dataclass(frozen=True)
class UsPlaces:
    """Every name that places a page in the US: states, and the towns and counties of
    the campuses held. Built once a run (`us_places`)."""

    names: frozenset[str]

    def named_in(self, haystack: str) -> bool:
        """Whether a normalized haystack (`discover.normalize_haystack`) names one."""
        words = haystack.split()
        if any(word in _STATE_CODES for word in words):
            return True
        return _any_phrase(self.names | _US_WORDS, haystack)


def _any_phrase(phrases: Iterable[str], haystack: str) -> bool:
    return any(f" {phrase} " in haystack for phrase in phrases)


def us_places(localities: Iterable[str | None]) -> UsPlaces:
    """The US place names: every state, and `localities` (the towns and counties of
    the campuses held), each normalized the way a haystack is."""
    from tracker.ingest.discover import normalize_haystack
    from tracker.normalize import STATE_CODES, state_name

    names = {normalize_haystack(state_name(code) or "").strip() for code in STATE_CODES}
    for raw in localities:
        name = normalize_haystack(raw or "").strip()
        if not name or len(name) < 4 or name in _NOT_A_PLACE:
            continue
        names.add(name)
        bare = re.sub(r" (county|parish|borough|township)$", "", name)
        if len(bare) >= 4:
            names.add(bare)
    return UsPlaces(frozenset(n for n in names if n))


def abroad(haystack: str, places: UsPlaces) -> str | None:
    """The foreign place a haystack names, when it names one and no US place."""
    if places.named_in(haystack):
        return None
    text = _NEW_PREFIXED.sub(" ", haystack)
    for place in sorted(_FOREIGN):
        if f" {place} " in text:
            return place
    return None


#: The year before which a builder's post is old. Its own date when the archive gave
#: one; else the latest year its address names, but only up to 2022, a year's margin:
#: "since 2022" in a 2025 Cologix slug, "2023 ESG report" published in 2024.
OLD_BEFORE: Final = dt.datetime(2024, 1, 1)
_SLUG_YEAR: Final = re.compile(r"(?<!\d)(19[89]\d|20[0-2]\d)(?!\d)")

#: Words of a campus's own story, for an old post that names no place: "H5 CLE
#: opening day 2019" is the Cleveland campus held as #222, under its airport code.
#: Such a post is read. The keyword filter is no help here: its signal tier passes
#: every builder post ("celebrates", "selected", "capacity").
_CAMPUS_EVENT: Final = re.compile(
    r" (?:open\w*|groundbreak\w*|break\w* ground|online|energi[sz]\w*|expan\w*|acqui\w*"
    r"|complet\w*|launch\w*|hello|campus\w*|mw|megawatt\w*|gw|gigawatt\w*|build\w*"
    r"|construct\w*|milestone\w*|phase|announc\w*|site|sites|facilit\w*|location\w*) "
)


def post_year(url: str, published_at: dt.datetime | None) -> int | None:
    """The year a builder's post is from, as far as its row and address say."""
    from tracker.ingest.discover import _month_from_path
    from tracker.ingest.fetch import date_from_url

    if published_at is not None:
        return published_at.year
    dated = date_from_url(url) or _month_from_path(url)
    if dated is not None:
        return dated.year
    years = [int(y) for y in _SLUG_YEAR.findall(urlsplit(url).path)]
    return max(years) if years and max(years) <= OLD_BEFORE.year - 2 else None


def builder_aside(
    url: str,
    title: str | None,
    published_at: dt.datetime | None,
    *,
    places: UsPlaces,
    held: Callable[[str, str | None], bool],
) -> str | None:
    """Why a builder's archive post is set aside, or None.

    `held(url, title)` says whether the post names a campus the database holds
    (`discover.matches_known_project`); such a post is always read. Otherwise a post
    naming a foreign place and no US one is abroad, and a post from before
    :data:`OLD_BEFORE` that names no place and no campus event is old news of
    nothing: an award, an Earth Day post, a market essay. An old post naming a US
    town is read: that is how the campuses a builder announced before the database
    existed come in.
    """
    from tracker.ingest.discover import normalize_haystack

    if held(url, title):
        return None
    haystack = normalize_haystack(f"{title or ''} {urlsplit(url).path}")
    where = abroad(haystack, places)
    if where:
        return f"about {where.title()}, outside the US"
    year = post_year(url, published_at)
    if (
        year is not None
        and year < OLD_BEFORE.year
        and not places.named_in(haystack)
        and not _CAMPUS_EVENT.search(haystack)
    ):
        return f"a builder's post from {year} that names no campus, no place and no campus event"
    return None


#: (how a reason begins or ends, the kind it is counted under) for a run's one-line
#: summary, first match wins.
_KINDS: Final[tuple[tuple[str, str], ...]] = (
    ("a PDF", "PDFs"),
    ("a translated copy", "translated copies"),
    ("outside the US", "posts about places abroad"),
    ("a builder's post from", "old builder posts about nothing"),
    ("stock chatter", "stock-chatter pages"),
    ("a stock-quote page", "stock-quote pages"),
    ("refused every read", "pages on sites that refuse every read"),
    ("job", "job postings"),
    ("a directory listing", "ZIP-code listings"),
    ("document", "office documents"),
    ("file, not an article", "media files"),
    ("attachment page", "attachment pages"),
    ("sports, dining", "sports and dining pages"),
    ("car dealer", "dealer listings"),
)


def kind(reason: str) -> str:
    """The kind a reason is counted under in a run's summary ("PDFs")."""
    for marker, label in _KINDS:
        if marker in reason:
            return label
    return reason


def summary(reasons: Iterable[str]) -> str:
    """ "114 translated copies, 59 PDFs, ..." for a run's report, largest first."""
    from collections import Counter

    counts = Counter(kind(reason) for reason in reasons)
    return ", ".join(f"{n} {label}" for label, n in counts.most_common())


__all__ = [
    "OLD_BEFORE",
    "SET_ASIDE",
    "UsPlaces",
    "abroad",
    "builder_aside",
    "file_kind",
    "kind",
    "not_news",
    "off_topic",
    "post_year",
    "site_kind",
    "summary",
    "translation",
    "us_places",
]
