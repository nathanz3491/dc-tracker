"""News-article ingest: fetch, extract with an LLM, gate on evidence, upsert.

The prompt asks the model for a verbatim quote behind every non-null value.
:func:`evidence_gate` then **discards any value whose quote is missing or is not
actually present in the fetched text**. That distinction is the whole design:
a prompt instruction is a request, and models under-comply with requests; the
gate is a mechanism, and the model cannot win by guessing because guesses are
thrown away regardless of what it claims.

Structure is two phases per run:

1. all fetching, concurrently, in one `asyncio.run`;
2. extraction and upsert, serially and synchronously.

Fetching is what benefits from concurrency. LLM calls are the *cost* bottleneck,
so serializing them keeps spend accounting, rate-limit handling and progress
reporting trivial, and keeps the SQLAlchemy session single-threaded.
"""

from __future__ import annotations

import datetime as dt
import difflib
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings
from tracker.dedup import looks_like_county
from tracker.ingest.fetch import Fetcher, FetchResult, cache_path, fetch_all
from tracker.ingest.records import (
    BlockRecord,
    EventRecord,
    IngestRecord,
    IngestReport,
    RiskRecord,
    SourceRecord,
)
from tracker.llm import Extractor, LLMError, LLMJsonError, LLMReply, parse_json_object
from tracker.models import IngestUrl, utcnow
from tracker.normalize import (
    NormalizationError,
    is_blank,
    looks_english,
    norm_country,
    norm_date_detail,
    norm_excerpt,
    norm_money_detail,
    norm_mw_detail,
    norm_phase,
    norm_risk_category,
    norm_risk_severity,
    norm_state,
    norm_text,
    soft,
)
from tracker.parallel import map_ordered
from tracker.prompts import Prompt, load_prompt
from tracker.upsert import upsert_record
from tracker.vocab import (
    BOUND_MARKERS,
    CLAIM_AXIS_DEFAULTS,
    CLAIM_BOUNDS,
    CLAIM_MODALITIES,
    CLAIM_SCOPES,
    DEFAULT_RISK_SEVERITY,
    EVENT_TYPES,
    TRACKED_FIELDS,
    risk_precedence,
)

log = logging.getLogger(__name__)

#: Hard ceiling on projects taken from one article. An article listing twenty
#: sites in passing is a roundup, not twenty citable projects.
MAX_PROJECTS_PER_ARTICLE = 5

#: Hard ceiling on obstacles taken from one article for one project. There are ten
#: categories and a single article realistically reports two or three; a list longer
#: than this means the model is enumerating speculation rather than reporting.
MAX_RISKS_PER_PROJECT = 6

#: Dollars per megawatt above which an investment figure is not about this site.
#:
#: The failure this catches is specific and common: an article about one campus
#: quotes the *programme* it belongs to. "OpenAI's $500 billion Stargate" appears
#: in a piece about a single 1,167 MW location, the gate verifies the number
#: really is in the text — it is — and the site is recorded at $165 billion.
#: Nothing about the evidence is wrong. The number is simply not this project's.
#:
#: The threshold is read off the live distribution rather than assumed, and it
#: lands in a genuine gap rather than cutting through a cluster. Across 41
#: projects citing both figures: median $6.2M/MW, upper quartile $11.1M/MW, the
#: bulk of the range topping out at $23.3M/MW — then nothing until $83.3M, $141.4M
#: and $190.9M, all three of which are programme totals or scale errors.
#:
#: Deliberately generous. A campus that buys its own accelerators legitimately
#: costs far more per megawatt than one leasing shell space, and this must not
#: fire on those.
MAX_USD_PER_MW = 50_000_000

#: Field the ratio check demotes, and a stable substring identifying its
#: disclosure in a project's notes.
#:
#: The note is the only durable record of *why* a value is 待确认. Both causes
#: land at the same tier — nothing quotable backs it — but they call for opposite
#: work: an unquoted figure needs a second source, whereas a programme total
#: needs correcting, because the sentence behind it is real and simply about
#: something larger. The console reads this marker to tell them apart, so the
#: wording around it may change and these words may not.
SCALE_NOTE_FIELD = "investment_usd"
SCALE_NOTE_MARKER = "plausibility ceiling"

#: Marker inserted where the middle of an over-long article was dropped.
TRUNCATION_MARKER = "\n\n[... middle of article omitted for length ...]\n\n"

#: Subdomains a company publishes its own announcements under. Only consulted
#: when the *parent* domain is already known to belong to an operator — see
#: `classify_source_type`.
_NEWSROOM_SUBDOMAINS: Final[frozenset[str]] = frozenset(
    {"news", "about", "blog", "ir", "investor", "newsroom", "press"}
)

#: Domain patterns to source_type. Ordered: first match wins.
_SOURCE_TYPE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|\.)sec\.gov$"), "company_filing"),
    (re.compile(r"(^|\.)(gov|mil)$"), "government_doc"),
    (re.compile(r"\.state\.[a-z]{2}\.us$"), "government_doc"),
    (
        re.compile(
            r"(^|\.)(datacenterdynamics|datacenterfrontier|datacenterknowledge|utilitydive"
            r"|rtoinsider|latitudemedia|heatmap|semianalysis|theregister)\.com$"
        ),
        "trade_press",
    ),
)


class CrawlError(RuntimeError):
    """The run cannot proceed."""


@dataclass
class ExtractionOutcome:
    """What one URL produced, for both the report and the ingest_url row."""

    url: str
    status: str
    records: list[IngestRecord] = field(default_factory=list)
    error: str | None = None
    http_status: int | None = None
    via: str = "httpx"
    attempts: int = 1
    content_sha1: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Carried through from the fetch so `record_url` can persist it. None for a
    #: cache hit, which serves text with the metadata already stripped out.
    published_at: dt.datetime | None = None


def classify_source_type(url: str, *, operator_hosts: frozenset[str] | None = None) -> str:
    """Guess how authoritative a URL is.

    Never returns `company_filing`/`government_doc` on a guess about a general
    domain, because those weights are what let a project reach confidence 2.

    `operator_hosts` carries the data center operators' own domains, taken from the
    newsroom entries in `seed/feeds.toml`. Without it the subdomain rules below
    recognise `news.microsoft.com` and `about.fb.com` but not
    `www.stackinfra.com/news/…`, so a first-party press release — the single most
    authoritative source there is for capacity, investment and timeline — was
    scored `general_media`, weight 1. That is the opposite of what it deserves.
    """
    host = url.split("//", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    bare = host.removeprefix("www.")
    if operator_hosts and bare in operator_hosts:
        return "company_filing"
    # A company's own newsroom lives on a subdomain of its own site, so the
    # subdomain is only evidence of authorship together with a domain we already
    # know belongs to an operator. On its own it is not evidence of anything:
    # `^(news|about|...)\.` used to match here unconditionally, which typed
    # `news.17173.com` — a Chinese gaming portal — and `news.futunn.com` — a
    # brokerage — as `company_filing`, weight 3, the heaviest in the system. On
    # Fairwater (#1) the gaming site was the *only* company_filing on the row: it
    # decided the stored $3.3B investment and supplied the "strongest source"
    # line in the confidence rationale.
    if operator_hosts and "." in bare:
        parent = bare.split(".", 1)[1]
        if parent in operator_hosts and bare.split(".", 1)[0] in _NEWSROOM_SUBDOMAINS:
            return "company_filing"
    for pattern, source_type in _SOURCE_TYPE_RULES:
        if pattern.search(host):
            return source_type
    return "general_media"


@lru_cache(maxsize=1)
def operator_hosts() -> frozenset[str]:
    """Domains belonging to data center operators, from the newsroom sitemaps.

    Cached: it reads and parses `seed/feeds.toml`, and it is consulted once per
    extracted article. An empty set is returned if the config cannot be read —
    misclassifying a source is far better than failing an ingest run over it.
    """
    try:
        from tracker.ingest.discover import newsroom_companies

        return frozenset(newsroom_companies())
    except Exception as exc:
        log.warning("could not read operator newsrooms: %s", exc)
        return frozenset()


def truncate(text: str, limit: int) -> str:
    """Trim to a character budget, keeping the head and the tail.

    Head-biased with a middle drop: a news lead carries the who/where/how-much,
    the close often carries timelines and objections, and the middle is where
    boilerplate and related-links live.
    """
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head - len(TRUNCATION_MARKER)
    if tail <= 0:
        return text[:limit]
    return text[:head] + TRUNCATION_MARKER + text[-tail:]


#: A line has to be at least this long to be counted as prose rather than as a
#: navigation label. `INSIGHTS`, `< Back`, `Terms & Conditions` and
#: `© 2026 — Copyright` are what a teaser card is made of; a sentence somebody
#: wrote is longer. Measured in *characters* rather than words on purpose — a
#: word count scores every Chinese-language article at zero, and refusing a real
#: 4,392-character article as `thin_content` would record a false reason.
_PROSE_LINE_CHARS: Final = 60

#: Below this much prose, a page is navigation furniture and is refused before it
#: costs an LLM call.
#:
#: **Why a floor is needed.** A fetch that returns 200 and 600 characters of
#: chrome is not an article, but nothing checked, so the model was handed a
#: teaser card, invented a plausible project from the title, and then *every*
#: quote failed — `company / city / county / state / phase` refused together.
#: `build_records` then restored the identity fields from the ungated values and
#: wrote a row anyway, with zero confirmed fields.
#:
#: **Why 200, and why on prose rather than on raw length.** Measured over the 544
#: cached articles that could be matched to a URL:
#:
#: * Raw length cannot draw the line. A real Meta 8-K excerpt is 590 characters
#:   and an Applied Digital teaser card is 598 — 8 apart, with the *shorter* one
#:   genuine.
#: * Prose separates them cleanly. The same 8-K scores 553; all 15 cached Applied
#:   Digital campus-update cards score 74, being one site-wide banner line
#:   ("Applied Digital has signed a 210 MW lease at Delta Forge 2… Read More >>")
#:   — which is exactly the sentence the model was inventing projects out of.
#: * It cannot fire on the main corpus: 246 trade-press articles, the thinnest
#:   3,025, which is 15x this floor.
#: * It cannot fire on real filings either, which was the stated risk: of 115
#:   cached SEC filings only one falls below — a bare quarterly revenue table
#:   with no sentence in it. The shortest genuine one clears the floor 2.8x over.
#:
#: 20 of the 544 are refused, and every one was read: 17 nav-only operator pages,
#: an 8-character stub, that revenue table, and one Chinese wire newsflash. The
#: margin above is thinner than the margin below — the next genuine page up
#: scores 269 — so raising this without repeating the measurement is not safe.
#:
#: **The one known asymmetry.** A character floor is stricter for Chinese, where
#: the same meaning fits in roughly a third of the characters, and the three
#: Chinese-language reposts in the corpus score 180, 269 and 282 against an
#: English median in the thousands. Accepted rather than corrected: the measure
#: was chosen over a word count precisely so those pages score honestly instead
#: of zero, and a Chinese repost is a source this pipeline can barely use anyway
#: — `looks_english` refuses its summary fields and its phase wording, and no
#: quantity pattern matches its numerals. A per-script floor would be the fix if
#: that ever stops being true.
#: A refusal is recorded as `thin_content`, which is visible in
#: `tracker queue --failed` and retried by `--retry-failed`, so a page a site
#: later serves in full is recoverable rather than lost.
MIN_PROSE_CHARS: Final = 200


def prose_length(text: str) -> int:
    """Characters of `text` that sit in lines long enough to be sentences."""
    return sum(
        len(stripped)
        for line in text.splitlines()
        if len(stripped := line.strip()) >= _PROSE_LINE_CHARS
    )


def _normalize_for_match(text: str) -> str:
    """Fold whitespace, unicode and quote style, for substring comparison."""
    folded = unicodedata.normalize("NFKC", text)
    folded = (
        folded.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
        .replace("–", "-")
        .replace("—", "-")
    )
    return re.sub(r"\s+", " ", folded).strip().lower()


def _normalize_with_offsets(text: str) -> tuple[str, list[int]]:
    """Normalize, and keep an index back to the original for every character.

    Needed because a recovered quote must be shown in the article's own words —
    capitalisation and all — and the match is found in the normalized form. NFKC
    can change a character's width, so this folds one character at a time and
    keeps only the first source index for each result character; that is enough
    to bound a span, which is all the caller needs.
    """
    out: list[str] = []
    index: list[int] = []
    pending_space = False
    for position, char in enumerate(text):
        folded = unicodedata.normalize("NFKC", char)
        folded = (
            folded.replace("“", '"')
            .replace("”", '"')
            .replace("‘", "'")
            .replace("’", "'")
            .replace("–", "-")
            .replace("—", "-")
        ).lower()
        if folded.isspace() or not folded:
            pending_space = bool(out)
            continue
        if pending_space:
            out.append(" ")
            index.append(position)
            pending_space = False
        for character in folded:
            out.append(character)
            index.append(position)
    return "".join(out), index


#: A recovered run must be at least this many characters, and at least this much
#: of the quote the model offered.
#:
#: **Why recovery exists at all.** Measured over 131 real evidence quotes from 8
#: cached articles, 33 failed exact containment — and the dominant reason was not
#: fabrication. The model resolves references while quoting: the article says
#: "The campus is a single building comprising two data halls that serve as a
#: 16.5 MW data center" and the model writes "The *Austin* campus is a single
#: building…", substituting the name for the pronoun. Helpful for a reader,
#: fatal for a substring test, and it was costing real capacity figures.
#:
#: **Why it is still safe.** The run itself must be verbatim, the *article's* text
#: is what gets stored rather than the model's edit, and `_stated_in` then runs
#: against that stored text — so a value still has to be evidenced by words
#: somebody actually published. Tuned against a negative control: every quote was
#: also tested against an unrelated article, and at these thresholds not one
#: crossed. 27 of the 33 came back; acceptance went from 75% to 95%.
MIN_RUN_CHARS: Final = 40
MIN_RUN_FRACTION: Final = 0.5


#: How much of a rejected quote to put in the log. Long enough to recognise the
#: sentence and judge the refusal, short enough that a wall of them stays readable.
_LOGGED_QUOTE_CHARS: Final = 160


def _for_log(quote: str) -> str:
    """A quote flattened to one readable line for a warning."""
    flat = re.sub(r"\s+", " ", quote).strip()
    if len(flat) <= _LOGGED_QUOTE_CHARS:
        return flat
    return flat[: _LOGGED_QUOTE_CHARS - 1] + "…"


class _Run(NamedTuple):
    """What the article really contains of an offered quote.

    `text` is the recovery — the article's own words — and is None when the
    longest run cleared neither floor, which is what a fabricated quote looks
    like. `chars` and `of` survive that refusal deliberately: the rejection log
    has to say *how close* the quote came, and recomputing the match to find out
    would pay for the expensive part twice on the path that is already failing.
    """

    text: str | None
    chars: int
    of: int

    def shortfall(self) -> str:
        """The refusal in the form the log wants: `best run 31 of 88 chars, 35%`."""
        share = self.chars / self.of if self.of else 0.0
        return f"best run {self.chars} of {self.of} chars, {share:.0%}"


def _verbatim_run(quote: str, article: str) -> _Run:
    """The article's own words for the longest stretch of `quote` it really contains."""
    normalized_quote = _normalize_for_match(quote)
    if not normalized_quote:
        return _Run(None, 0, 0)
    haystack, offsets = _normalize_with_offsets(article)
    match = difflib.SequenceMatcher(
        None, normalized_quote, haystack, autojunk=False
    ).find_longest_match(0, len(normalized_quote), 0, len(haystack))
    run = _Run(None, match.size, len(normalized_quote))
    if match.size < MIN_RUN_CHARS:
        return run
    if match.size / len(normalized_quote) < MIN_RUN_FRACTION:
        return run
    start = offsets[match.b]
    end = offsets[match.b + match.size - 1] + 1
    start, end = _widen_to_sentence(article, start, end)
    # Collapse the wrapping. Filings and PDF-derived text carry hard newlines
    # mid-sentence, and a quote rendered with them looks broken in the drawer.
    return run._replace(text=re.sub(r"\s+", " ", article[start:end]).strip())


#: How far either side of a matched run to look for a sentence boundary. Enough
#: to pick up a clause the model dropped, short enough that a quote stays a quote.
_WIDEN_BY: Final = 160

#: A full stop that really ends a sentence, tolerating any wrapping after it.
_SENTENCE_END: Final = re.compile(r"[.!?](?=\s)")


def _widen_to_sentence(article: str, start: int, end: int) -> tuple[int, int]:
    """Grow a span outward to sentence edges, staying inside the article.

    Without this a run can stop mid-number. Observed: "…the offering was $" —
    the model wrote "$1,300.0 million", the article breaks the line inside the
    figure, and the longest common run ends at the dollar sign. The quote is then
    real but no longer evidences the value it was cited for, so `_stated_in`
    drops it anyway and the recovery bought nothing.

    Safe because everything added is the article's own text; widening cannot
    introduce a word nobody published.
    """
    # `[.!?]` followed by *any* whitespace, not ". " — fetched articles are hard
    # wrapped, so most sentences end at ".\n" and a literal ". " misses them.
    before = article[max(0, start - _WIDEN_BY) : start]
    ends = list(_SENTENCE_END.finditer(before))
    if ends:
        start -= len(before) - ends[-1].end()
    else:
        paragraph = before.rfind("\n\n")
        start -= len(before) - (paragraph + 2) if paragraph != -1 else 0

    after = article[end : end + _WIDEN_BY]
    found = _SENTENCE_END.search(after)
    if found:
        end += found.start() + 1  # keep the full stop, drop the whitespace
    else:
        paragraph = after.find("\n\n")
        end += paragraph if paragraph != -1 else 0
    return start, end


#: Article wording that evidences each phase.
#:
#: `phase` is the one tracked field that is a *judgement* rather than a value
#: copied out of the text: an article says "broke ground", never
#: `phase: construction`. Asking for a quote containing the literal word
#: discarded 60 of 90 correct classifications, and because `phase` is NOT NULL
#: every one of them silently became the `announced` default — so the stored
#: phase distribution was an artefact of the gate, not of the projects.
#:
#: Consulted only from `_stated_in`, and therefore only reachable because `phase` is
#: *not* in `_SUMMARY_FIELDS`. While it was, any quote the model labelled `phase`
#: skipped this table entirely — see that set's docstring.
_PHASE_EVIDENCE: dict[str, tuple[str, ...]] = {
    "announced": ("announce", "plans to", "proposed", "propose", "unveil", "will build"),
    "permitting": ("permit", "zoning", "rezon", "entitlement", "application", "approval"),
    "construction": (
        "under construction",
        "construction",
        "broke ground",
        "break ground",
        "breaking ground",
        "groundbreaking",
        "being built",
        "underway",
    ),
    "operational": (
        "operational",
        "came online",
        "comes online",
        "went live",
        "is live",
        "energiz",
        "in service",
        "opened",
        "now serving",
    ),
    "paused": ("paused", "on hold", "halted", "suspend", "shelved"),
    "cancelled": ("cancel", "scrapped", "abandon", "withdrew", "withdrawn", "terminated"),
}

#: Article wording that evidences each risk category.
#:
#: Same mechanism and same reason as `_PHASE_EVIDENCE` above. A risk category is a
#: *classification*, not a value copied out of the text: an article says "the county
#: board rejected the rezoning", never `category: community_opposition`. Requiring
#: the category name to appear verbatim would discard every correct classification,
#: which is exactly what happened to the old free-text `blocker` field — a
#: paraphrase can never be a verbatim substring, so the stricter gate was taking its
#: coverage to zero.
#:
#: What is gated is the pairing: the quote must be real (verified against the
#: fetched article) AND must contain wording for the category claimed. A model that
#: labels an unrelated sentence `water` gets nothing through.
_RISK_EVIDENCE: dict[str, tuple[str, ...]] = {
    "grid_capacity": (
        "grid capacity",
        "not enough power",
        "insufficient power",
        "capacity constraint",
        "power constraint",
        "curtail",
        "load growth",
        "cannot supply",
        "energy shortfall",
        "queue position",
        "interconnection",
    ),
    "transmission": (
        "transmission",
        "substation",
        "transmission line",
        "power line",
        "kilovolt",
        "kv line",
        "upgrade the grid",
        "grid upgrade",
        "interconnection",
        "energiz",
    ),
    "permitting": (
        "permit",
        "zoning",
        "rezon",
        "entitlement",
        "approval",
        "variance",
        "moratorium",
        "special use",
        "planning commission",
        "board of supervisors",
        "council",
    ),
    "environmental": (
        "environmental",
        "air quality",
        "air permit",
        "emissions",
        "wetland",
        "endangered",
        "impact statement",
        "epa",
        "pollution",
    ),
    "equipment_supply": (
        "transformer",
        "switchgear",
        "chiller",
        "cooling equipment",
        "turbine",
        "generator",
        "lead time",
        "supply chain",
        "shortage",
        "backlog",
        "delivery",
    ),
    "chip_supply": (
        "chip",
        "gpu",
        "accelerator",
        "semiconductor",
        "nvidia",
        "allocation",
        "silicon",
    ),
    "financing": (
        "financ",
        "funding",
        "capital",
        "investor",
        "debt",
        "loan",
        "raise",
        "cost overrun",
        "budget",
    ),
    "offtake": (
        "tenant",
        "customer",
        "lease",
        "leasing",
        "offtake",
        "pre-leas",
        "preleas",
        "commitment",
        "speculative",
        "unleased",
    ),
    "community_opposition": (
        "opposition",
        "oppose",
        "resident",
        "neighbor",
        "neighbour",
        "lawsuit",
        "sued",
        "sue",
        "litigation",
        "referendum",
        "petition",
        "protest",
        "noise",
        "backlash",
        "objection",
    ),
    "water": (
        "water",
        "aquifer",
        "groundwater",
        "gallons",
        "cooling water",
        "drought",
        "wastewater",
        "discharge",
    ),
    # `unclassified` is deliberately absent: it exists for a human assertion via
    # `ingest manual` and for the 0004 backfill. The extractor must classify, or the
    # row cannot be aggregated and the table has no purpose.
}

#: Quantity expressions to hunt for inside a verified quote, per field.
_MW_EXPR = re.compile(r"[\d][\d.,]*\s*(?:mw|gw|megawatt|gigawatt)s?\b", re.I)
_MONEY_EXPR = re.compile(
    r"(?:us)?\$\s?[\d][\d.,]*\s*(?:billion|million|bn|b|m)?\b|"
    r"[\d][\d.,]*\s*(?:billion|million)\s*dollars?\b",
    re.I,
)
_DATE_EXPR = re.compile(
    r"\b(?:q[1-4]\s*(?:of\s*)?20[2-4]\d|"
    r"(?:early|mid|late|end of|beginning of|start of|first half of|second half of|"
    r"spring|summer|fall|autumn|winter)\s+20[2-4]\d|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+20[2-4]\d|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+20[2-4]\d|"
    r"20[2-4]\d-\d{2}-\d{2}|"
    r"20[2-4]\d)\b",
    re.I,
)


#: Fields whose value is a *paraphrase* of the article rather than a copy of it.
#:
#: A summary is written as "grid interconnection delays" where the article says
#: "the project awaits two 345-kilovolt upgrades" — correct, and sharing no
#: substring with its own evidence. For these, the model's label plus a quote
#: verified to be real is the strongest check available; demanding the value
#: appear verbatim would discard every honest summary.
#:
#: Two fields have left this set, both for the same reason: a wording table is a
#: strictly stronger form of the same carve-out, because it asks the quote to be real
#: *and* to contain wording for the label it is filed under, so an unrelated real
#: sentence under a plausible label is no longer enough. Trusting the label alone is
#: the weakest link here.
#:
#: * `blocker`, because it is no longer a value the model returns: obstacles come
#:   back in `risks[]`, are checked against `_RISK_EVIDENCE`, and the column is
#:   derived from the stored rows.
#: * `phase`, which had `_PHASE_EVIDENCE` all along and never needed the carve-out.
#:   While the carve-out sat in front of it the table never ran on any quote the
#:   model happened to label `phase`, so `phase="operational"` beside a genuine
#:   "announced plans to build" sentence was stored as a confirmed fact. `phase` is
#:   also where that is least recoverable: `upsert._resolve_ladder` merges by taking
#:   the furthest-along value, so one overclaim outranks every later correction.
#:
#: `notes` is what genuinely needs it, and it is the whole set: a note falls through
#: to the verbatim-substring branch of `_stated_in`, which no paraphrase can pass.
_SUMMARY_FIELDS = frozenset({"notes"})


def _stated_in(field: str, value: Any, quote: str) -> bool:
    """Does `quote` actually assert `value` for `field`?

    Comparison is on *normalized* values, not on strings, so "200 megawatt"
    evidences ``mw_planned=200.0`` and "1.2GW" evidences ``1200.0``. That is the
    whole point: the model's own words for a number never match our storage form.
    """
    if field == "phase":
        # The one field whose evidence is *wording* rather than a value, and so the
        # one field a translated repost can still reach. Every `_PHASE_EVIDENCE`
        # token is ASCII, which refuses a wholly Chinese sentence for free — but one
        # English lifecycle word left standing inside a Chinese paragraph would be
        # enough, and value matching is what protects every other field ("230兆瓦"
        # matches no MW pattern). The check used to guard only the quote the model
        # *labelled*, in `evidence_gate`; here it guards whichever quote is tested.
        if not looks_english(quote):
            return False
        low = quote.lower()
        return any(token in low for token in _PHASE_EVIDENCE.get(str(value), ()))

    # `mw` and `energized_on` are the capacity-block field names. Listed here
    # rather than translated at the call site because dispatch is on the name and a
    # block's `mw` really is a capacity quantity — sending it through the string
    # branch silently refuses every well-quoted figure a filing states.
    if field in {"mw_planned", "mw_built", "mw"}:
        return _matches_quantity(value, quote, _MW_EXPR, norm_mw_detail, field)
    if field == "investment_usd":
        return _matches_quantity(value, quote, _MONEY_EXPR, norm_money_detail, field)
    if field in {"first_announced", "expected_online", "energized_on"}:
        return _matches_quantity(value, quote, _DATE_EXPR, norm_date_detail, field)

    # Everything else is a string we copied out of the article, so it has to be
    # in the quote verbatim.
    if isinstance(value, str) and value.strip():
        return _normalize_for_match(value) in _normalize_for_match(quote)
    return False


def _matches_quantity(value: Any, quote: str, expr: re.Pattern[str], parser, field: str) -> bool:
    """True when any quantity in `quote` normalizes to `value`."""
    for match in expr.finditer(quote):
        try:
            parsed = parser(match.group(0), field=field)
        except NormalizationError:
            continue
        if parsed.value is None:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if abs(float(parsed.value) - float(value)) < 0.01:
                return True
        elif parsed.value == value:
            return True
    return False


#: Wording that licenses each non-default `bound` — `vocab.BOUND_MARKERS`, not a
#: copy of it. It moved there when the console and the CLI needed to *display* a
#: bound derived from a stored quote: two readers of one rule, and this codebase's
#: recurring defect is the same rule written down twice.
#:
#: **Still not positional here**, which is a known limitation recorded in
#: HANDOFF.md: this asks only whether a hedge appears somewhere in the sentence, so
#: Hyperion's "more than $50 billion ... up from the roughly $27 billion plan" can
#: license `approximate` off the other number's hedge. Making it positional needs
#: the figure, and `axis_gate` is deliberately never given one — see its docstring.
#: `vocab.bound_from_quote` is positional and is what the display path uses.
_BOUND_MARKERS: Final[dict[str, tuple[str, ...]]] = BOUND_MARKERS

#: Wording that licenses each non-default `modality`, same rule.
#:
#: `planned` is the default and needs no marker, because it is what an
#: unqualified statement about a future data center means. The two that matter
#: are the ends: `achieved` must not be assertable without evidence of having
#: happened, and `speculated` is what keeps "some reports have surfaced that an
#: interim milestone of 1.5 GW is being targeted" from standing beside an SEC
#: filing at equal weight.
_MODALITY_MARKERS: Final[dict[str, tuple[str, ...]]] = {
    "achieved": (
        "came online",
        "went live",
        "has begun",
        "began ",
        "broke ground",
        "completed",
        "opened",
        "is now",
        "has been",
        "energized",
        "energised",
        "delivered",
        "started",
    ),
    "contracted": (
        "signed",
        "has agreed",
        "agreement",
        "filed an application",
        "contract",
        "committed",
        "approved",
        "secured",
        "entered into",
    ),
    "targeted": ("targeted", "target", "aims to", "hopes to", "goal of", "by the end of"),
    "speculated": (
        "reports have",
        "reportedly",
        "could ",
        "may ",
        "might ",
        "potential",
        "expected to grow",
        "possible",
        "rumou",
        "unconfirmed",
    ),
}

#: Wording that licenses a scope other than `this_site` or a resolvable block.
_SCOPE_MARKERS: Final[dict[str, tuple[str, ...]]] = {
    "programme": ("programme", "program", "across all", "in total", "nationwide", "overall"),
    "region": ("to the region", "regional", "local economy", "statewide", "in the state", "county"),
    "portfolio": ("portfolio", "its data centers", "all of its", "company-wide", "fleet"),
}


def axis_gate(
    entry: dict[str, Any], quote: str, *, block_labels: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Keep the claim-envelope axes the quote actually licenses.

    The counterpart of `evidence_gate`, and the same argument: a prompt
    instruction is a request, a gate is a mechanism. `evidence_gate` asks whether
    the article states the *value*; this asks whether it states the *qualifier*.

    Every axis degrades to its neutral value rather than being dropped, and the
    underlying figure is never touched — this pass cannot reduce coverage. A model
    that labels everything `at_least` to sound careful therefore gains nothing,
    which is the property that stops the axis drifting into decoration.

    `as_of` is checked for parseability and for the one contradiction a date can
    have with a modality: something `achieved` cannot be dated in the future. It
    is deliberately not checked against the article's own publication date, which
    would refuse every correct backward reference.
    """
    low = _normalize_for_match(quote or "")
    out: dict[str, Any] = {}

    scope = str(entry.get("scope") or "").strip()
    if scope.startswith("block:"):
        label = scope[len("block:") :].strip().lower()
        # Referential integrity, not judgement: we cannot tell whether the model
        # picked the *right* tranche, but we can refuse one that does not exist.
        scope = scope if label and label in block_labels else CLAIM_AXIS_DEFAULTS["scope"]
    elif scope in _SCOPE_MARKERS:
        if not any(marker in low for marker in _SCOPE_MARKERS[scope]):
            scope = CLAIM_AXIS_DEFAULTS["scope"]
    elif scope not in CLAIM_SCOPES:
        scope = CLAIM_AXIS_DEFAULTS["scope"]
    out["scope"] = scope

    bound = str(entry.get("bound") or "").strip()
    if bound not in CLAIM_BOUNDS or (
        bound in _BOUND_MARKERS and not any(m in low for m in _BOUND_MARKERS[bound])
    ):
        bound = CLAIM_AXIS_DEFAULTS["bound"]
    out["bound"] = bound

    as_of = str(entry.get("as_of") or "").strip() or None
    if as_of:
        try:
            as_of = dt.date.fromisoformat(as_of).isoformat()
        except ValueError:
            as_of = None

    modality = str(entry.get("modality") or "").strip()

    # The hard invariant runs *before* the marker check, and the order matters.
    # A thing that has happened cannot be dated later than today, and the date is
    # itself the evidence for that — no wording in the sentence is needed to know
    # it. Demoting to `targeted` here rather than letting the marker check drop it
    # to the generic default keeps the more informative answer: this is exactly
    # Hyperion's live defect, where "an interim milestone of 1.5 GW is being
    # targeted by the end of 2027" was stored as `announced`, dated 2027-12-31,
    # and counted as *reached* on the track strip.
    if modality == "achieved" and as_of and dt.date.fromisoformat(as_of) > dt.date.today():
        modality = "targeted"
    elif modality not in CLAIM_MODALITIES or (
        modality in _MODALITY_MARKERS and not any(m in low for m in _MODALITY_MARKERS[modality])
    ):
        modality = CLAIM_AXIS_DEFAULTS["modality"]

    out["modality"] = modality
    if as_of:
        out["as_of"] = as_of
    return out


def evidence_gate(
    values: dict[str, Any], evidence: list[dict[str, Any]], article_text: str
) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Keep only values the article is verified to actually state.

    Returns ``(kept, quotes_by_field, dropped)``, where `dropped` maps each
    refused field to *why*, from `vocab.UNCONFIRMED_REASONS`.

    The reason is not decoration. "Nobody quoted this" and "the quote is real and
    does not say that" land in the same 待确认 bucket and ask for opposite work —
    the first wants another source, the second wants a correction — and anything
    reading the flag back cannot tell them apart without it. `capex` is the case
    that matters: it excludes unconfirmed investment figures from the sums, and
    the figure it means to exclude is the programme-wide total the `$/MW` ceiling
    demoted, not the campus figure nobody happened to quote.

    Every quote is first checked to be a real substring of the fetched text. That
    is the anti-fabrication guarantee: a model that *paraphrases* the article into
    a quote which sounds right but was never written gets nothing through.

    A value then survives if any verified quote asserts it — **whichever field the
    model filed that quote under**. The label is the model's bookkeeping, and
    models are unreliable bookkeepers: T5@Augusta supplied "…a 140-acre, 200
    megawatt campus in Georgia", tagged it for another field, and lost a correct
    `mw_planned=200`. Across the first 90 projects that bookkeeping requirement
    discarded 89 correctly-evidenced values.

    Matching the *value* rather than trusting the label is also a stronger check
    than the one it replaces: a labelled quote never had to contain the number it
    was cited for, so an unrelated real sentence used to be enough.
    """
    haystack = _normalize_for_match(article_text)
    quotes: dict[str, str] = {}
    verified: list[str] = []
    #: Fields the model offered a quote for that turned out not to be in the
    #: article. Kept so a refusal can say "the quote was fabricated" rather than
    #: the much weaker "nothing quoted this".
    unverified: set[str] = set()
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        name = entry.get("field")
        quote = entry.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            continue
        if _normalize_for_match(quote) in haystack:
            settled = quote.strip()
        else:
            # Not verbatim — but the usual reason is the model resolving a
            # reference at the edge of a sentence it otherwise copied exactly.
            # Take the part it really did copy, in the article's own words.
            recovered = _verbatim_run(quote, article_text)
            if recovered.text is None:
                # Name what was refused, not just which field lost. Without the
                # quote and the shortfall this warning cannot be acted on — and
                # answering "are we losing data?" needed an instrumented replay
                # that this line makes unnecessary next time.
                log.warning(
                    "evidence quote for %r is not in the article (%s); offered: %r",
                    name,
                    recovered.shortfall(),
                    _for_log(quote),
                )
                if isinstance(name, str):
                    unverified.add(name)
                continue
            log.debug(
                "evidence quote for %r was edited by the model; keeping the %d "
                "characters it actually copied",
                name,
                len(recovered.text),
            )
            settled = recovered.text
        verified.append(settled)
        if isinstance(name, str):
            quotes.setdefault(name, settled)

    kept: dict[str, Any] = {}
    dropped: dict[str, str] = {}
    for name, value in values.items():
        if value is None:
            continue
        # `country` is structural, not a claim: it is how we know the project is
        # in scope at all, and every source here is US news.
        if name == "country":
            kept[name] = value
            continue
        # A paraphrase cannot be matched against its own source text, so for `notes`
        # the model's label over a verified quote is what we have.
        #
        # The language check closes the one hole that carve-out opens: every other
        # field is protected from a foreign-language source by value matching —
        # "230兆瓦" matches no MW pattern — and a summary field skips value matching
        # entirely. `phase` used to be here and is now checked against
        # `_PHASE_EVIDENCE` in `_stated_in`, which carries the same check.
        if name in _SUMMARY_FIELDS and name in quotes and looks_english(quotes[name]):
            kept[name] = value
            continue
        support = next((q for q in verified if _stated_in(name, value, q)), None)
        if support is not None:
            kept[name] = value
            # Prefer the quote that actually states the value over the one the
            # model *labelled* for this field above. Same reasoning as the gate
            # itself: models are unreliable bookkeepers, and a labelled quote was
            # never required to contain the number it was filed under. That was
            # tolerable while these quotes only fed `_excerpt`, which blends three
            # of them for display. It is not tolerable now that `source.quotes`
            # persists the pairing and the UI shows one sentence beneath one
            # value — a mislabelled quote would state something the row does not.
            if name not in quotes or not _stated_in(name, value, quotes[name]):
                quotes[name] = support
        else:
            # Which of the three refusals this was, in the order that makes the
            # strongest true statement. A verified quote filed under this name
            # means the article does have a sentence about it and the sentence
            # does not state this value; a failed quote means the model wrote one
            # nobody published; neither means simply that nothing backed it.
            if name in quotes:
                dropped[name] = "quote_off_target"
            elif name in unverified:
                dropped[name] = "quote_unverified"
            else:
                dropped[name] = "no_quote"
    return kept, quotes, dropped


def _coerce(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """Type-coerce one extracted project. Returns (values, disclosure notes).

    Every field goes through `normalize`, which is the direct mitigation for the
    PRD's top risk: the LLM returning "1000 MW" as a string or a date as prose.
    """
    notes: list[str] = []

    def numeric(key: str, parser) -> Any:
        value = raw.get(key)
        if is_blank(value):
            return None
        try:
            parsed = parser(value, field=key)
        except NormalizationError as exc:
            log.warning("dropping %s: %s", key, exc)
            return None
        if parsed.note:
            notes.append(parsed.note)
        return parsed.value

    values: dict[str, Any] = {
        "name": norm_text(raw.get("name")),
        "company": norm_text(raw.get("company")),
        "customer": norm_text(raw.get("customer")),
        "city": norm_text(raw.get("city")),
        "county": norm_text(raw.get("county")),
        "state": soft(norm_state, raw.get("state")),
        "country": soft(norm_country, raw.get("country")) or "US",
        "mw_planned": numeric("mw_planned", norm_mw_detail),
        "mw_built": numeric("mw_built", norm_mw_detail),
        "phase": soft(norm_phase, raw.get("phase")),
        # `blocker` is deliberately absent: it is no longer a claim an article makes
        # but a value derived from the `risk` rows. See `_risks` below and
        # `upsert._derive_blocker`.
        "notes": norm_text(raw.get("notes")),
    }

    money = numeric("investment_usd", norm_money_detail)
    values["investment_usd"] = None if money is None else int(money)

    # `parse_date` has always reported how precisely the source stated a date,
    # and its own docstring says why it matters: "Q3 2025" and "2025-07-01" are
    # stored identically and mean very different things. Nothing outside
    # `normalize` had ever read it -- the precision went into a prose note and the
    # row rendered a day-precision date the article never gave.
    precisions: dict[str, str] = {}
    for key in ("first_announced", "expected_online"):
        value = raw.get(key)
        if is_blank(value):
            values[key] = None
            continue
        try:
            parsed = norm_date_detail(value, field=key)
        except NormalizationError as exc:
            log.warning("dropping %s: %s", key, exc)
            values[key] = None
            continue
        if parsed.note:
            notes.append(parsed.note)
        values[key] = parsed.value
        if parsed.precision:
            precisions[key] = parsed.precision

    return values, notes, precisions


def _implausible_investment(claims: dict[str, Any]) -> str | None:
    """A disclosure note when the money and the megawatts cannot both be this site.

    Returns the note, or None when the pair is plausible or one of them is absent.

    This is the one check here that the evidence gate cannot make. The gate asks
    "did the article say this number", and for a programme total quoted in a piece
    about one campus the answer is yes. Only the *ratio* to the capacity on the
    same row reveals that the number belongs to something larger.
    """
    money = claims.get("investment_usd")
    mw = claims.get("mw_planned")
    if not isinstance(money, (int, float)) or not isinstance(mw, (int, float)):
        return None
    if isinstance(money, bool) or isinstance(mw, bool) or mw <= 0 or money <= 0:
        return None
    per_mw = money / mw
    if per_mw <= MAX_USD_PER_MW:
        return None
    return (
        f"{SCALE_NOTE_FIELD} of ${int(money):,} against {mw:g} MW is ${per_mw / 1e6:,.0f}M per MW, "
        f"above the ${MAX_USD_PER_MW / 1e6:,.0f}M {SCALE_NOTE_MARKER} — usually a programme-wide "
        "total quoted in an article about one site. Kept as 待确认 rather than stored as fact."
    )


def _events(raw: dict[str, Any], article_text: str, url: str) -> list[EventRecord]:
    """Milestones from one extracted project, each verified against the article.

    The last extracted structure to get a gate. The prompt has said "Only
    milestones whose date you can quote" since v1 — a request with no mechanism,
    which is exactly the distinction this module's docstring draws — and the
    events fed the track strip on the model's say-so. Observed live on Fairwater
    (#1): a `groundbreaking` dated 2026-06-23 whose own description reads "Open
    house event held to announce opening", counted as breaking ground two years
    after the site actually did.

    Same discipline as `_risks`, and the same non-requirement: the *description*
    stays the model's own words (demanding it be verbatim is what took the old
    `blocker` to zero coverage). What must be real is the quote beside it — a
    failed or missing one demotes the event to 待确认 rather than deleting it,
    because a milestone an article stated vaguely is still worth showing, just
    never worth showing as verified.
    """
    events: list[EventRecord] = []
    for entry in raw.get("events") or []:
        if not isinstance(entry, dict):
            continue
        event_type = str(entry.get("event_type") or "").strip().lower().replace(" ", "_")
        if event_type not in EVENT_TYPES:
            continue
        try:
            when = norm_date_detail(entry.get("event_date"), field="event_date").value
        except NormalizationError:
            continue
        description = norm_text(entry.get("description"))
        if when is None or not description:
            continue

        offered = norm_text(entry.get("quote"))
        unconfirmed: str | None = None
        quote: str | None = None
        if not offered:
            unconfirmed = "no_quote"
        else:
            recovered = _verbatim_run(offered, article_text)
            if recovered.text is None:
                # A sentence nobody published cannot vouch for a date. The event
                # survives; the fabrication does not.
                unconfirmed = "quote_unverified"
            elif not _event_quote_supports(event_type, recovered.text):
                # Real sentence, wrong tense. "Peak construction workforce expected
                # to reach 5,000" is a forecast, and filed as `equipment_install`
                # with a date that has since passed it made a campus with nothing
                # installed report its construction track complete.
                log.warning(
                    "event quote for %s describes a plan rather than an event; keeping it as 待确认",
                    event_type,
                )
                unconfirmed = "quote_off_target"
            else:
                quote = recovered.text[:500]

        events.append(
            EventRecord(when, event_type, description, url, quote=quote, unconfirmed=unconfirmed)
        )
    return events


def _risk_quote_supports(category: str, quote: str) -> bool:
    """Does this quote contain wording for the category it is filed under?"""
    normalized = _normalize_for_match(quote)
    return any(token in normalized for token in _RISK_EVIDENCE.get(category, ()))


#: Wording that makes a sentence a PLAN rather than a record of something done.
#:
#: A milestone is something that happened. `tracks.standing` now refuses to count
#: an event dated in the future, which catches the honest case — but not a
#: forward-looking sentence given a date already past. Hyperion (#10) recorded
#: "Peak construction workforce expected to reach 5,000" as `equipment_install`
#: on 2026-06-01, and because that date had come, the construction track read
#: `equipment_install, complete` on a campus with nothing installed.
#:
#: The list is the one `prompts/_industry.txt` §6 enumerates, so the prompt that
#: refuses to emit these and the gate that refuses to trust them cite one source.
_PLANNED_WORDING: Final[tuple[str, ...]] = (
    "expected to",
    "is expected",
    "are expected",
    "expects to",
    "scheduled to",
    "scheduled for",
    "is scheduled",
    "targeted for",
    "targeting",
    "will begin",
    "will start",
    "will be",
    "plans to",
    "planned for",
    "aims to",
    "set to",
    "due to begin",
    "once operational",
    "when complete",
    "upon completion",
    "would be",
    "could be",
)


def _event_quote_supports(event_type: str, quote: str) -> bool:
    """Does this quote record something that HAPPENED, not something planned?

    Deliberately narrower than `_risk_quote_supports`: that one asks whether the
    sentence is about the right *category*, this asks whether it is about the past
    at all. Checking for milestone-specific vocabulary as well was tempting and
    wrong — articles describe a groundbreaking in a hundred ways, and a keyword
    list would reject good citations to catch a failure mode that is really about
    tense.

    A failing quote demotes the event to 待确认 rather than deleting it, the same
    as an unverifiable one: an article that mentions a milestone in a forward-looking
    sentence may still be reporting a real milestone somewhere else, and deletion
    is the one repair this codebase has learned is not durable.
    """
    normalized = _normalize_for_match(quote)
    return not any(token in normalized for token in _PLANNED_WORDING)


#: A block may name at most this many tranches. Beyond it the model is dividing a
#: campus rather than reporting what an article distinguished. Same log-and-truncate
#: as `MAX_PROJECTS_PER_ARTICLE`.
MAX_BLOCKS_PER_PROJECT: Final = 8


def _sentence_around(quote: str, article: str) -> str:
    """The article's own sentence containing `quote`, or the quote if not found.

    A model quotes a fragment — "up to 48 megawatts of IT capacity" — from a
    sentence that named the block earlier: "Iron Mountain is building AZP-2 in
    Phoenix, a three-story facility spanning 530,000 square feet and up to 48
    megawatts of IT capacity." Testing the fragment for the block's name would
    reject a perfectly good citation, so the check is applied to the sentence the
    article actually wrote. Reuses `_widen_to_sentence`, and everything it adds is
    the article's own text.
    """
    haystack, offsets = _normalize_with_offsets(article)
    needle = _normalize_for_match(quote)
    if not needle:
        return quote
    at = haystack.find(needle)
    if at == -1:
        return quote
    start = offsets[at]
    end = offsets[min(at + len(needle), len(offsets)) - 1] + 1

    # Full sentence bounds, not `_widen_to_sentence`. That helper deliberately
    # leaves a span alone when it finds no boundary, which is right when it is
    # recovering an edited quote and wrong here: the commonest case is a quote from
    # the *first* sentence of an article, where there is no preceding boundary to
    # find and the block's name sits at the very start.
    boundaries = [m.end() for m in _SENTENCE_END.finditer(article, 0, start)]
    if boundaries:
        start = boundaries[-1]
    else:
        paragraph = article.rfind(chr(10) * 2, 0, start)
        start = paragraph + 2 if paragraph != -1 else 0

    closing = _SENTENCE_END.search(article, end)
    end = closing.start() + 1 if closing else len(article)
    return article[start:end].strip()


def _quote_names_the_block(label: str, parent: str | None, quote: str) -> bool:
    """Does this verified quote actually mention the block it is cited for?

    **The most important check in the block path**, and it exists because
    `evidence_gate` deliberately does the opposite. That function ignores the
    model's field labels and lets any verified quote support any value — earned
    behaviour that recovered 89 correctly-evidenced values across 64 projects.

    At block granularity the same tolerance *is* the project-39 bug: it would let
    an SEC filing's sentence about "AZP-3 Phase 3" evidence AZP-2's capacity. Block
    megawatts get summed, so a quote about the wrong tranche does not merely
    mislabel a value — it changes a total.

    A quote passes when it satisfies **any one segment** of the label or its parent:
    the segment's head must appear, and if that segment carries an ordinal then some
    form of the ordinal must appear too. Requiring the ordinal is what separates
    AZP-2 from AZP-3, which share a stem; allowing any *form* of it is what admits
    "The first phase 8 megawatts of customer capacity" for a block labelled
    "Phase 1", the commonest phrasing in the corpus.
    """
    from tracker import blocks as blocks_mod

    words = set(re.findall(r"[a-z0-9]+", _normalize_for_match(quote)))
    requirements = blocks_mod.segment_requirements(label, parent)
    if not requirements:
        return False
    return any(
        head in words and (not ordinals or bool(ordinals & words))
        for head, ordinals in requirements
    )


def _blocks(
    raw: dict[str, Any], article_text: str, url: str
) -> tuple[list[BlockRecord], list[str]]:
    """Capacity blocks from one extracted project. Returns ``(kept, disclosures)``.

    Structured like `_risks`, and gated twice for the same reason.

    **Each block's evidence pool is sealed.** `evidence_gate` is called once per
    block over that block's own evidence list only, so inside a block a quote may
    support any of its fields — keeping the recovery behaviour that matters — while
    across blocks nothing crosses. That containment is the whole point: project 39
    is one row holding AZP-2's capacity beside AZP-3's money and date.
    """
    from tracker.vocab import BLOCK_STATUSES, DEFAULT_BLOCK_STATUS

    entries = raw.get("blocks")
    if not isinstance(entries, list) or not entries:
        return [], []

    kept: list[BlockRecord] = []
    notes: list[str] = []
    seen: set[str] = set()

    for entry in entries[:MAX_BLOCKS_PER_PROJECT]:
        if not isinstance(entry, dict):
            continue
        label = norm_text(entry.get("label"))
        if not label:
            continue

        parent = norm_text(entry.get("parent"))
        # Dedup in Python, not on the UNIQUE: two same-key blocks in one payload
        # would abort the whole article, which `_upsert_events` records as a real
        # failure mode.
        from tracker import blocks as blocks_mod

        key = blocks_mod.block_key(label, parent).value
        if key in seen:
            log.debug("%s: two blocks resolve to %r; keeping the first", url, key)
            continue
        seen.add(key)

        status = norm_text(entry.get("status")) or DEFAULT_BLOCK_STATUS
        if status not in BLOCK_STATUSES:
            log.warning("%s: block %r has unusable status %r; defaulting", url, label, status)
            status = DEFAULT_BLOCK_STATUS

        values: dict[str, Any] = {}
        for name, parse in (
            ("mw", lambda v: soft(norm_mw_detail, v, field="mw_planned")),
            ("investment_usd", lambda v: soft(norm_money_detail, v, field="investment_usd")),
            ("expected_online", lambda v: soft(norm_date_detail, v, field="expected_online")),
            ("energized_on", lambda v: soft(norm_date_detail, v, field="expected_online")),
        ):
            detail = parse(entry.get(name)) if entry.get(name) is not None else None
            if detail is not None and detail.value is not None:
                values[name] = detail.value
        customer = norm_text(entry.get("customer"))
        if customer:
            values["customer"] = customer

        # Sealed: this block's evidence and nothing else.
        pool = entry.get("evidence")
        confirmed, quotes, dropped = evidence_gate(
            values, pool if isinstance(pool, list) else [], article_text
        )

        # Second gate: a verified quote must name this block. A real sentence about
        # a different tranche is exactly what must not get through.
        for name in list(confirmed):
            quote = quotes.get(name)
            in_context = _sentence_around(quote, article_text) if quote else ""
            if quote and not _quote_names_the_block(label, parent, in_context):
                log.debug(
                    "%s: quote for block %r field %r does not name the block; "
                    "keeping the value as 待确认",
                    url,
                    label,
                    name,
                )
                confirmed.pop(name, None)
                quotes.pop(name, None)
                # The quote is the article's own; it just belongs to a different
                # tranche. Same reason a mislabelled field quote gets.
                dropped[name] = "quote_off_target"

        unconfirmed = {name for name in values if name not in confirmed}
        kept.append(
            BlockRecord(
                label=label,
                parent=parent,
                mw=values.get("mw"),
                status=status,
                customer=values.get("customer"),
                expected_online=values.get("expected_online"),
                energized_on=values.get("energized_on"),
                investment_usd=values.get("investment_usd"),
                # Only for fields the gate actually confirmed. `quotes` also holds
                # the model's own labels for values that were dropped — pairing one
                # with a 待确认 value would dress it as a quoted fact, which is the
                # single thing the tier exists to prevent. Same filter the source
                # path applies to `SourceRecord.quotes`.
                quotes={k: q for k, q in quotes.items() if k in confirmed},
                unconfirmed=frozenset(unconfirmed),
            )
        )

    if len(entries) > MAX_BLOCKS_PER_PROJECT:
        notes.append(
            f"{len(entries) - MAX_BLOCKS_PER_PROJECT} further block(s) named by this "
            "article were not recorded"
        )
    notes.extend(vague_block_note(kept))
    return kept, notes


def vague_block_note(kept: list[BlockRecord]) -> list[str]:
    """The 待确认 disclosure for a set of blocks. Empty when they are all cited.

    Split out because the backfill routes a portfolio article's blocks across
    several project rows *after* `_blocks` has run, and the note has to describe
    the blocks that row actually kept. It did not: the first tranche told STACK's
    Chicago row that "Portland Expansion" was unconfirmed, about a block that had
    already been sent to the Portland row.
    """
    vague = sorted(
        {b.label for b in kept for name in b.unconfirmed if name in {"mw", "investment_usd"}}
    )
    if not vague:
        return []
    return [
        "block figures kept as 待确认 because no quote in the article names that "
        f"block: {', '.join(vague)}"
    ]


def _risks(raw: dict[str, Any], article_text: str, url: str) -> tuple[list[RiskRecord], list[str]]:
    """Obstacles from one extracted project. Returns ``(kept, disclosure notes)``.

    Two checks, and both are needed:

    * The quote must really appear in the fetched article. Same anti-fabrication
      guarantee as `evidence_gate` — a paraphrase dressed as a quote gets nothing
      through.
    * The quote must contain wording for the *claimed category*. Without this the
      model could attach any real sentence to any category, and the aggregation this
      table exists for would be built on labels nobody checked.

    What is deliberately NOT required is that `summary` be quotable. It is one
    sentence of the model's own words, and demanding it be a verbatim substring is
    precisely what took the old `blocker` field's coverage to zero.

    **Failing either check no longer deletes the obstacle.** It is kept with
    `unconfirmed` set to why, and without the quote that failed — the same answer
    migration 0006 gave for a field value the gate could not verify. Deleting it
    was the last place in the ingest path that destroyed extracted information,
    and it fell hardest on the field the database is worst at: no press release
    names its own blocker, so an adversarial second source is the only thing that
    ever records one, and refusing it outright loses the obstacle rather than
    merely its citation.

    Two things still drop, because there is no 待确认 version of them: a category
    outside the vocabulary (an unaggregatable risk is a hole in the one thing this
    table is for) and a missing summary (nothing left to show).
    """
    haystack = _normalize_for_match(article_text)
    kept: list[RiskRecord] = []
    notes: list[str] = []
    seen: set[tuple[str, dt.date | None]] = set()
    unconfirmed_kept: list[str] = []

    for entry in raw.get("risks") or []:
        if not isinstance(entry, dict):
            continue

        category = soft(norm_risk_category, entry.get("category"))
        if category is None or category not in _RISK_EVIDENCE:
            # Includes `unclassified`, which is not the extractor's to assert: an
            # unclassified risk cannot be aggregated, so it would silently be a hole
            # in the one thing this table is for.
            log.warning("dropping a risk from %s: unusable category %r", url, entry.get("category"))
            continue

        summary = norm_text(entry.get("summary"))
        if not summary:
            log.warning("dropping a %s risk from %s: no summary", category, url)
            continue

        # Why the gate could not confirm this obstacle, or None when it could.
        # Set rather than `continue`d: the risk is kept either way, and the quote
        # is dropped whenever this is set — a sentence that failed its check is
        # never stored beside the thing it failed to support.
        unconfirmed: str | None = None

        quote = entry.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            unconfirmed, quote = "no_quote", None
        elif _normalize_for_match(quote) in haystack:
            quote = quote.strip()
        else:
            # Same reference-resolution problem as `evidence_gate`, same remedy:
            # fall back to the article's own words for the stretch the model really
            # copied. The category check below then runs against *that*, which is
            # strictly harder to satisfy than checking the model's edit.
            recovered = _verbatim_run(quote, article_text)
            if recovered.text is None:
                log.warning(
                    "risk quote for %s is not in the article (%s); keeping the risk "
                    "as 待确认; offered: %r",
                    category,
                    recovered.shortfall(),
                    _for_log(quote),
                )
                unconfirmed, quote = "quote_unverified", None
            else:
                quote = recovered.text
        if quote is not None and not _risk_quote_supports(category, quote):
            log.warning(
                "risk quote for %s does not state that category; keeping the risk as 待确认",
                category,
            )
            unconfirmed, quote = "quote_off_target", None
        if unconfirmed:
            unconfirmed_kept.append(category)

        first_seen = soft(norm_date_detail, entry.get("first_seen"), field="first_seen")
        first_seen_value = first_seen.value if first_seen is not None else None

        key = (category, first_seen_value)
        if key in seen:
            # The stored UNIQUE is (project, category, first_seen), so two entries
            # agreeing on both would collide on insert. Keeping the first is the same
            # accepted cost `event` already documents.
            continue
        seen.add(key)

        delay = entry.get("delay_days")
        delay_days = int(delay) if isinstance(delay, int) and not isinstance(delay, bool) else None
        if delay_days is not None and delay_days < 0:
            delay_days = None

        kept.append(
            RiskRecord(
                category=category,
                # Unrecognized severity defaults to `watch` rather than dropping the
                # risk: the obstacle is real and evidenced, only its stated effect is
                # unclear, and understating that is the safe direction.
                severity=soft(norm_risk_severity, entry.get("severity")) or DEFAULT_RISK_SEVERITY,
                summary=summary,
                quote=norm_excerpt(quote) if quote else None,
                first_seen=first_seen_value,
                delay_days=delay_days,
                source_url=url,
                unconfirmed=unconfirmed,
            )
        )
        if len(kept) >= MAX_RISKS_PER_PROJECT:
            log.warning(
                "%s reported more than %d risks; kept the first %d",
                url,
                MAX_RISKS_PER_PROJECT,
                MAX_RISKS_PER_PROJECT,
            )
            break

    if unconfirmed_kept:
        notes.append(
            "risk(s) kept as 待确认 for "
            + ", ".join(sorted(set(unconfirmed_kept)))
            + " — reported by the source, but no verbatim quote from the article "
            "states them"
        )
    return kept, notes


def build_records(
    result: FetchResult,
    payload: dict[str, Any],
    *,
    prompt: Prompt,
    reply: LLMReply,
    max_projects: int = MAX_PROJECTS_PER_ARTICLE,
) -> list[IngestRecord]:
    """Turn a validated LLM payload into IngestRecords. Pure, no I/O."""
    projects = payload.get("projects")
    if not isinstance(projects, list):
        raise LLMJsonError(f"payload has no `projects` list: {payload!r}")

    source_type = classify_source_type(result.url, operator_hosts=operator_hosts())
    records: list[IngestRecord] = []

    for raw in projects[:max_projects]:
        if not isinstance(raw, dict):
            continue
        values, coercion_notes, precisions = _coerce(raw)
        evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
        kept, quotes, dropped = evidence_gate(values, evidence, result.markdown)

        # Identity: without a company and a locality there is nothing to dedup on,
        # so the "project" is a passing mention rather than a record.
        company = kept.get("company") or values.get("company")
        state = kept.get("state") or values.get("state")
        city = kept.get("city") or values.get("city")
        county = kept.get("county") or values.get("county")

        # A county or parish name in the `city` column is not a city.
        #
        # Identity fields skip the evidence gate on purpose (see below) — a row
        # cannot exist without them — and `city` is FILL_ONLY, so once a wrong one
        # is written no recompute, merge or re-extraction ever corrects it.
        # Hyperion (#10) has carried city="Richland Parish" since the day it was
        # created, from a source that has since been re-extracted away.
        #
        # Routing it is provably key-neutral: `dedup.locality` already reads a
        # county-suffixed `city` as county granularity, so `dedup_key` does not
        # move and no row forks. `ck_project_locality` holds because the value
        # lands in `county`.
        if city and looks_like_county(city):
            county, city = county or city, None

        if not company or not state or not (city or county):
            log.warning(
                "dropping a project from %s: needs company, state and a locality "
                "(got company=%r state=%r city=%r county=%r)",
                result.url,
                company,
                state,
                city,
                county,
            )
            continue

        claims = dict(kept)
        claims.update({"company": company, "state": state})
        if city:
            claims["city"] = city
        if county:
            claims["county"] = county
        claims.setdefault("name", values.get("name") or f"{company} {city or county}")
        claims.pop("notes", None)

        # The PRD's 待确认 tier: a value the model extracted but no quote supports is
        # kept and flagged, not destroyed. Measured before this existed, the gate
        # was deleting 194 such values across 92 of 124 projects — nearly half of
        # everything found for `expected_online`.
        #
        # They enter `claims` like any other value so they can be resolved and
        # displayed, and `SourceRecord.unconfirmed` keeps them out of
        # `source.fields`, which is what confidence and the 9-of-12 count read.
        #
        # Each carries the gate's reason, so a reader of the flag can tell "nobody
        # quoted this" from "the quote says something else" — see `evidence_gate`.
        reasons = {
            f: why
            for f, why in dropped.items()
            if f not in claims and f != "notes" and values.get(f) is not None
        }
        for name in reasons:
            claims[name] = values[name]

        # A quoted figure can be real and still not be about this project. Demoted
        # to 待确认 rather than deleted, which is the same answer migration 0006
        # gave for values the gate could not verify: keep it, refuse to call it a
        # fact, and let a human see what the source actually said.
        #
        # `out_of_scale` overwrites any earlier reason on purpose: this figure was
        # quoted and verified and is still not credible, which is a strictly
        # stronger statement than any way of failing to be quoted.
        scale_note = _implausible_investment(claims)
        if scale_note:
            reasons["investment_usd"] = "out_of_scale"
        unconfirmed = set(reasons)

        risks, risk_notes = _risks(raw, result.markdown, result.url)

        # Blocks, each gated against its own sealed evidence pool. This is where a
        # campus stops being one `phase` and starts being the several states it
        # actually is.
        blocks, block_notes = _blocks(raw, result.markdown, result.url)

        # A source that reported an obstacle supports `blocker`, even though the
        # column is derived rather than claimed. Recording it keeps the "every
        # non-null tracked field appears in some source's `fields`" invariant true
        # without the merge path ever reading the value: `upsert` skips `blocker` in
        # the recompute loop and derives it from the `risk` rows instead.
        #
        # Confirmed risks outrank unconfirmed ones (`risk_precedence`), and if the
        # winner is still unconfirmed the derived `blocker` inherits that: an
        # obstacle the gate refused must not arrive in `source.fields` reading as
        # cited, which is exactly what the 待确认 tier exists to prevent.
        if risks:
            worst = max(risks, key=risk_precedence)
            claims["blocker"] = worst.summary
            if worst.unconfirmed:
                reasons["blocker"] = worst.unconfirmed
                unconfirmed.add("blocker")

        # Report only what was genuinely discarded. `dropped` is the gate's raw
        # verdict, but identity fields (name, company, city, county, state) are
        # then restored from the ungated values because a project row cannot exist
        # without them and the article self-evidently concerns it — and `notes` is
        # a summary, never a citable claim, so it is recorded separately below.
        # Listing either as "dropped" told the operator something untrue, which is
        # corrosive in the one place they look to judge data quality.
        #
        # Written after the risks, not before: `blocker` only joins this set once
        # `_risks` has run, and a disclosure that omits it is the same untruth.
        notes = list(coercion_notes)
        if unconfirmed:
            notes.append(
                "unconfirmed (待确认): "
                + ", ".join(sorted(unconfirmed))
                + " — extracted but not established as this project's own figure"
            )
        if scale_note:
            notes.append(scale_note)
        if values.get("notes"):
            notes.append(f"extracted summary: {values['notes']}")
        notes.extend(risk_notes)
        notes.extend(block_notes)

        record = IngestRecord(
            project={
                "name": claims["name"],
                "company": company,
                "state": state,
                "city": city,
                "county": county,
                "country": claims.get("country", "US"),
            },
            sources=[
                SourceRecord(
                    url=result.url,
                    source_type=source_type,
                    fetched_at=result.fetched_at or utcnow(),
                    excerpt=_excerpt(quotes),
                    claims=claims,
                    unconfirmed=frozenset(unconfirmed),
                    unconfirmed_reasons=tuple(sorted(reasons.items())),
                    # Only what the gate confirmed. `quotes` can also hold the
                    # model's labels for values that were dropped, and `claims`
                    # additionally carries identity fields restored from the
                    # ungated values and the 待确认 set — none of those have a
                    # verified sentence behind them, and pairing one with a quote
                    # would dress an unconfirmed value as a quoted fact.
                    quotes={k: q for k, q in quotes.items() if k in kept},
                    # Same restriction as `quotes`, and for the same reason: an
                    # axis describes a value the article was verified to state, so
                    # attaching one to a 待确认 figure would qualify a claim
                    # nothing supports.
                    claim_meta=_claim_axes(evidence, quotes, kept, blocks, precisions),
                    blocks=blocks,
                    extractor=f"crawl:{prompt.stamp}:{reply.model}:{result.via}",
                )
            ],
            events=_events(raw, result.markdown, result.url),
            risks=risks,
            notes=notes,
        )
        records.append(record)

    if len(projects) > max_projects:
        log.warning(
            "%s described %d projects; kept the first %d (--max-projects)",
            result.url,
            len(projects),
            max_projects,
        )
    return records


def _claim_axes(
    evidence: list[dict[str, Any]],
    quotes: dict[str, str],
    kept: dict[str, Any],
    blocks: list[Any],
    precisions: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run every field's envelope through `axis_gate`, keyed by field.

    Each axis is verified against the quote that was *stored* for the field, not
    against the model's own offered text. That matters because `_verbatim_run`
    may have repaired the quote to the article's own words — checking the model's
    version instead would let it license a hedge by writing one into a sentence
    nobody published, which is exactly the fabrication route `evidence_gate`
    closed for values.
    """
    labels = frozenset(b.label.strip().lower() for b in blocks if getattr(b, "label", None))
    out: dict[str, dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        if field not in kept or field not in quotes:
            continue
        axes = axis_gate(item, quotes[field], block_labels=labels)
        # An entry that is neutral on every axis says nothing, and storing it
        # would inflate coverage with rows carrying no information — the exact
        # measurement `axis_census` exists to catch, so it must not be gamed here.
        if any(value != CLAIM_AXIS_DEFAULTS.get(axis) for axis, value in axes.items()):
            out[field] = axes

    # Date precision does not go through `axis_gate`, and should not: it is not a
    # label the model asserted about the article, it is what our own parser
    # observed while reading the date. There is nothing to verify against a quote
    # -- the evidence is the parse itself.
    for field, precision in (precisions or {}).items():
        if field in kept and precision and precision != "day":
            out.setdefault(field, {})["date_precision"] = precision
    return out


def _excerpt(quotes: dict[str, str]) -> str | None:
    """Up to three quotes, preferring the contested quantitative fields.

    `source.excerpt` is capped at 500 characters, so this picks the quotes an
    operator most needs to see when reviewing the row.
    """
    if not quotes:
        return None
    priority = ("mw_planned", "investment_usd", "phase", "expected_online", "mw_built", "customer")
    ordered = [quotes[f] for f in priority if f in quotes]
    ordered += [q for f, q in sorted(quotes.items()) if f not in priority]
    # dict.fromkeys dedupes while preserving order: one sentence often supports
    # several fields, and repeating it wastes the 500-character budget.
    return norm_excerpt(" ... ".join(list(dict.fromkeys(ordered))[:3]))


# --- Extraction -------------------------------------------------------------


def extract_one(
    result: FetchResult,
    *,
    prompt: Prompt,
    extractor: Extractor,
    settings: Settings | None = None,
    published_date: str = "unknown",
) -> ExtractionOutcome:
    """Run one article through the LLM, with a single corrective retry."""
    settings = settings or get_settings()
    outcome = ExtractionOutcome(
        url=result.url,
        status="ok",
        via=result.via,
        attempts=result.attempts,
        http_status=result.status,
        content_sha1=result.sha1 if result.markdown else None,
        # Set before any early return, so a page refused as thin still records the
        # date it stated. The refusal is about prose, not about metadata.
        published_at=result.published_at,
    )

    # Before the call, not after: a page with nothing to quote cannot produce a
    # cited value, so paying to find that out is the one refusal that is strictly
    # cheaper than the alternative. It also prevents the phantom row, which is
    # the part that outlives the wasted call.
    prose = prose_length(result.markdown)
    if prose < MIN_PROSE_CHARS:
        outcome.status = "thin_content"
        outcome.error = (
            f"{prose} characters of prose in {len(result.markdown)} of page; "
            f"needs {MIN_PROSE_CHARS}"
        )
        log.warning(
            "not an article, refused before the LLM call: %s (%s)", result.url, outcome.error
        )
        return outcome

    body = truncate(result.markdown, settings.max_input_chars)
    user = prompt.render_user(
        url=result.url,
        published_date=published_date,
        markdown=body,
        max_projects=MAX_PROJECTS_PER_ARTICLE,
    )

    last_error: str | None = None
    #: Set when a reply ran out of budget inside its own reasoning, so the retry
    #: can be a *different* request. Retrying that verbatim reproduces the ramble
    #: — measured on one Chinese-language article: two identical attempts, two
    #: identical failures, two calls paid for and nothing extracted.
    starved = False
    for attempt in range(1, max(1, settings.llm_max_attempts) + 1):
        message = user
        budget: int | None = None
        if starved:
            message = (
                user + "\n\nYour previous reply spent its whole budget reasoning and never "
                "produced an answer. Do not deliberate. Emit the JSON object immediately, "
                "with no prose and no code fences."
            )
            budget = settings.max_completion_tokens * 2
        elif attempt > 1:
            message = (
                user + "\n\nYour previous reply was not a single valid JSON object. "
                "Return ONLY the JSON object, with no prose and no code fences."
            )
        try:
            reply = extractor.complete(system=prompt.system, user=message, max_tokens=budget)
        except LLMError as exc:
            outcome.status = "llm_error"
            outcome.error = str(exc)
            return outcome

        outcome.prompt_tokens += reply.prompt_tokens or 0
        outcome.completion_tokens += reply.completion_tokens or 0

        if reply.finish_reason == "length":
            last_error = "reply truncated at the token limit"
            starved = True
            log.warning("%s: %s (attempt %d)", result.url, last_error, attempt)
            continue

        try:
            payload = parse_json_object(reply.text)
        except LLMJsonError as exc:
            last_error = str(exc)
            # `finish_reason` is not reliable here: a provider has reported "stop" on
            # a reply that plainly stops mid-sentence inside `<think>`. An unclosed
            # block is the observable fact, so trust that over the label.
            starved = exc.ran_out_thinking
            log.warning("%s: %s (attempt %d)", result.url, last_error, attempt)
            continue

        try:
            outcome.records = build_records(result, payload, prompt=prompt, reply=reply)
        except LLMJsonError as exc:
            last_error = str(exc)
            continue

        outcome.status = "ok" if outcome.records else "no_project"
        return outcome

    outcome.status = "parse_error"
    outcome.error = last_error
    return outcome


# --- ingest_url bookkeeping -------------------------------------------------


def record_url(session: Session, run_id: str, outcome: ExtractionOutcome) -> None:
    """Upsert the per-URL outcome.

    This table is why re-running a URL list is cheap: URLs already `ok` are
    skipped, and `--retry-failed` can target just the ones that were not.
    """
    row = session.scalar(select(IngestUrl).where(IngestUrl.url == outcome.url))
    now = utcnow()
    if row is None:
        row = IngestUrl(url=outcome.url, run_id=run_id, first_seen_at=now, attempts=0)
        session.add(row)
    row.run_id = run_id
    row.status = outcome.status
    row.http_status = outcome.http_status
    row.via = outcome.via
    row.attempts = (row.attempts or 0) + outcome.attempts
    row.error = (outcome.error or None) and outcome.error[:1000]
    row.content_sha1 = outcome.content_sha1
    row.last_tried_at = now
    # Filled, never overwritten, and never cleared. `upsert` applies the same rule
    # one table over, for the same reason: the date a publisher put on an article
    # does not change, so a later reading of it has nothing to add. The guard on
    # None is what makes a cache hit harmless — cached bodies are text with the
    # metadata already stripped, so they always report None, and without this a
    # re-extraction run would erase every date the original fetch found.
    if row.published_at is None and outcome.published_at is not None:
        row.published_at = outcome.published_at
    session.flush()


def published_dates(session: Session, urls: list[str]) -> dict[str, str]:
    """url -> the date its publisher published it, as `YYYY-MM-DD`.

    Feeds the prompt's `ARTICLE_DATE`, which RULE 5 resolves relative timing
    against — "construction starts next year" is only a date if you know when
    "next" was written. With the date unknown the rule correctly forces every such
    phrase to null, so an absent `ARTICLE_DATE` does not fabricate anything; it
    quietly costs schedule fields instead.

    It had never been supplied. `extract_one`'s `published_date` parameter has
    defaulted to `"unknown"` since it was written and no caller ever passed it,
    while `discover` was recording the date on `ingest_url` the whole time — 78%
    populated when this was added.

    URLs with no recorded date are simply absent from the map, so the caller keeps
    the honest `"unknown"` rather than being handed a guess.
    """
    if not urls:
        return {}
    rows = session.execute(
        select(IngestUrl.url, IngestUrl.published_at).where(
            IngestUrl.url.in_(urls), IngestUrl.published_at.is_not(None)
        )
    )
    return {url: when.date().isoformat() for url, when in rows if when}


def already_done(session: Session, urls: list[str]) -> set[str]:
    """URLs a previous run already extracted successfully."""
    if not urls:
        return set()
    rows = session.scalars(
        select(IngestUrl).where(IngestUrl.url.in_(urls), IngestUrl.status == "ok")
    ).all()
    return {r.url for r in rows}


def stale_sources(session: Session, *, older_than_days: int, limit: int | None = None) -> list[str]:
    """Source URLs of existing projects that have not been re-read recently.

    This is how a project's data gets *updated* rather than merely added: articles
    are edited, phases advance, and a campus that was "announced" last quarter is
    under construction now. Re-running a known citation through the same extract
    path refreshes every field it supports.

    Placeholder URLs are excluded — they are not fetchable and never will be.
    Oldest first, so a capped run always makes progress on the most stale rows.
    """
    from tracker.confidence import PLACEHOLDER_MARKER
    from tracker.models import Source

    cutoff = utcnow() - dt.timedelta(days=older_than_days)
    stmt = (
        select(Source.url, func.min(Source.fetched_at).label("oldest"))
        .where(Source.fetched_at < cutoff)
        .where(Source.url.not_like(f"%{PLACEHOLDER_MARKER}%"))
        .group_by(Source.url)
        .order_by("oldest")
    )
    if limit:
        stmt = stmt.limit(limit)
    return [row[0] for row in session.execute(stmt)]


def stale_by_prompt(session: Session, *, stamp: str, limit: int | None = None) -> list[str]:
    """Source URLs that were extracted by some prompt other than the current one.

    `stale_sources` asks whether the *article* might have changed. This asks
    whether *we* have — and the answer has never been checked. The gate has been
    tightened repeatedly (placeholder demotion, the prose floor, per-field
    quotes, `unconfirmed_reasons`), each improvement applies only to rows written
    after it landed, and `source.extractor` has recorded which prompt produced
    every row since `0001` without anything ever comparing it to the current one.

    Measured when this was written: 348 of 368 extracted sources sat on a
    superseded prompt, and **all 89** values stored as established with no
    sentence behind them came from two vintages that predate migration `0007` —
    the migration that added the column a quote lives in. So those rows are not
    wrong because the model was worse; they are wrong because the gate that would
    have caught them did not exist yet, and nothing re-ran it.

    Distinct URLs, oldest first, so a `--limit`ed run works steadily through the
    oldest stratum instead of reshuffling between runs. Placeholders are excluded
    for the same reason `stale_sources` excludes them — they are not fetchable.
    So are `derived:` and `inferred:` rows, which no prompt produced: a Census
    lookup has no vintage to be stale against.

    Callers should pass `cache_dir`, unlike the refresh path which deliberately
    does not. Refreshing wants to know whether the article changed; this wants
    the *same* article read by a better prompt, and re-fetching would confound
    the two.
    """
    from tracker.confidence import PLACEHOLDER_MARKER
    from tracker.models import Source

    stmt = (
        select(Source.url, func.min(Source.fetched_at).label("oldest"))
        .where(Source.extractor.like("crawl:%"))
        .where(Source.extractor.not_like(f"crawl:{stamp}:%"))
        .where(Source.url.not_like(f"%{PLACEHOLDER_MARKER}%"))
        .group_by(Source.url)
        .order_by("oldest", Source.url)
    )
    if limit:
        stmt = stmt.limit(limit)
    return [row[0] for row in session.execute(stmt)]


def read_urls(path: Path) -> list[str]:
    """One URL per line; `#` comments and blanks ignored."""
    urls: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.lower().startswith(("http://", "https://")):
            log.warning("skipping %r: not an http(s) URL", line)
            continue
        urls.append(line)
    return list(dict.fromkeys(urls))


# --- Run --------------------------------------------------------------------


def run(
    session: Session,
    urls: list[str],
    *,
    prompt_name: str = "extract-v1",
    fetcher: Fetcher | None = None,
    escalate: Fetcher | None = None,
    extractor: Extractor | None = None,
    settings: Settings | None = None,
    dry_run: bool = False,
    force: bool = False,
    cache_dir: Path | None = None,
    cached_only: bool = False,
    existing_only: bool = False,
    policy: Any = None,
    run_id: str | None = None,
    route: Callable[[IngestRecord], int | None] | None = None,
) -> IngestReport:
    """Fetch, extract and upsert a list of article URLs.

    `extractor` is injectable and is resolved *before* any fetch, so a missing API
    key fails immediately rather than after paying for forty page loads — and so
    tests can supply a fake without needing a key at all.

    `cached_only` reads what is already on disk and skips the rest, counting them in
    `report.skipped_uncached`. Without it a re-extraction run quietly becomes a
    crawl: `--stale-prompt` picks URLs by prompt vintage, and three quarters of
    those have no cached text.

    `existing_only` refuses to create projects, counting them in
    `report.refused_new`. The guard a **re-read** needs: re-reading an article the
    database already cites also yields whatever else that article names, and a
    repair pass that quietly adds campuses is an ingest with no worklist and no
    review.

    `policy` is the source policy, loaded from `seed/sources.toml` when not given.
    **This is where the ignore list becomes a guarantee rather than a convention**:
    `sync`, `enrich`, `ingest crawl` and `backfill` all funnel through here, so
    filtering once covers callers nobody remembered to update, and covers queue
    rows that predate the policy. Pass an explicit `policy.EMPTY` to disable it.
    """
    import asyncio

    settings = settings or get_settings()
    prompt = load_prompt(prompt_name)
    if extractor is None:
        from tracker.llm import default_extractor

        extractor = default_extractor(settings)

    report = IngestReport()
    run_id = run_id or utcnow().strftime("%Y%m%dT%H%M%S")

    wanted = list(dict.fromkeys(urls))

    # Filtering, never re-ordering. Ordering here would be a second authority
    # competing with the queue's own, and `[*cached, *fetched]` below reshuffles
    # anyway — it would buy nothing but a disagreement.
    if policy is None:
        from tracker import policy as policy_mod

        policy = policy_mod.load()
    if getattr(policy, "entries", ()):
        keep, dropped = policy.partition(wanted)
        if dropped:
            report.skipped_ignored = len(dropped)
            log.info("skipping %d URL(s) on publishers seed/sources.toml ignores", len(dropped))
        wanted = keep

    if not force:
        done = already_done(session, wanted)
        if done:
            log.info("skipping %d URL(s) already extracted; --force to redo", len(done))
            report.filtered += len(done)
            wanted = [u for u in wanted if u not in done]
    report.read = len(wanted) + report.filtered
    if not wanted:
        return report

    cached, to_fetch = _split_cached(wanted, cache_dir)
    if cached_only and to_fetch:
        # The re-extraction case, and the one this flag exists for. `--stale-prompt`
        # wants the *same* article read by a better prompt, so a cache miss is a URL
        # to leave alone rather than a page to go and get: re-fetching confounds
        # "the prompt improved" with "the article changed", and doing it silently
        # turned a free re-read of 113 cached pages into 1,754 paid fetches inside
        # what the operator asked to be a cache-only run. Same discipline as
        # `backfill.run`'s `refetch=False`.
        log.info("cached-only: leaving %d URL(s) with no cached text", len(to_fetch))
        report.skipped_uncached += len(to_fetch)
        to_fetch = []
    fetched = (
        asyncio.run(fetch_all(to_fetch, fetcher=fetcher, escalate=escalate, settings=settings))
        if to_fetch
        else []
    )
    if cache_dir:
        _write_cache(fetched, cache_dir)

    # Looked up once for the whole batch rather than per URL: the loop below is
    # already paying for an LLM call each time round, and one query is easier to
    # reason about than N.
    published = published_dates(session, [r.url for r in [*cached, *fetched] if r.ok])

    def outcome_for(result: FetchResult) -> ExtractionOutcome:
        """One article's outcome, with no database in sight.

        Pulled out of the loop so it can run on a worker thread. Everything it
        touches is a plain value — a `FetchResult` in, an `ExtractionOutcome` out —
        which is the precondition `parallel.map_ordered` documents: a SQLAlchemy
        session is not thread-safe, and a lazy load from a worker is a race that
        surfaces as wrong data rather than as an error.

        A failed fetch is handled here rather than in the loop below so the loop
        keeps iterating `[*cached, *fetched]` in one pass and in its original
        order. It costs a worker slot for the microseconds it takes to build a
        dataclass and spends nothing.
        """
        if not result.ok:
            return ExtractionOutcome(
                url=result.url,
                status="fetch_error",
                error=result.error,
                http_status=result.status,
                via=result.via,
                attempts=result.attempts,
            )
        return extract_one(
            result,
            prompt=prompt,
            extractor=extractor,
            settings=settings,
            published_date=published.get(result.url, "unknown"),
        )

    # The model is most of this loop's elapsed time and one article's extraction
    # knows nothing about another's, so the calls overlap while the writes below
    # stay single-threaded and keep their commit-per-article checkpoint. Results
    # arrive in input order, so the run's log reads the same as it always did.
    # `llm_workers()` is 1 for a local model, which restores the serial path
    # exactly — see `Settings.ollama_concurrency`.
    for result, outcome in map_ordered(
        [*cached, *fetched],
        outcome_for,
        limit=settings.llm_workers(),
        label="extract",
    ):
        if not result.ok:
            report.fetch_error += 1
            log.warning("fetch failed: %s (%s)", result.url, result.error)
            record_url(session, run_id, outcome)
            _checkpoint(session, dry_run)
            continue

        if outcome.status == "parse_error":
            report.parse_error += 1
            log.warning("could not parse a reply for %s: %s", result.url, outcome.error)
        elif outcome.status == "llm_error":
            report.parse_error += 1
            log.error("LLM error for %s: %s", result.url, outcome.error)
        elif outcome.status == "thin_content":
            report.thin_content += 1

        # Counted for every outcome, including the failures: a reply that ran out
        # of budget mid-reasoning is the most expensive kind there is, and leaving
        # it out of the total would understate exactly the runs worth looking at.
        # `thin_content` is refused before the call and reports zero, which is what
        # makes the saving visible rather than merely claimed.
        if outcome.prompt_tokens or outcome.completion_tokens:
            report.llm_calls += 1
            report.prompt_tokens += outcome.prompt_tokens
            report.completion_tokens += outcome.completion_tokens

        for record in outcome.records:
            # Asked per record, not per run: one URL list can describe several
            # campuses, and the question "which project is this?" is only
            # answerable once the article has been read. A hook rather than a
            # project id for the same reason — the caller decides per article,
            # from what that article turned out to say.
            target = route(record) if route is not None else None
            upsert = upsert_record(session, record, existing_only=existing_only, route_to=target)
            if upsert.action == "refused":
                # Named, not merely counted: a refused campus is a candidate to
                # add deliberately later, and a run that reported only a number
                # would leave nothing to act on.
                report.refused_new += 1
                log.info(
                    "refused a new project from %s: %s", result.url, record.project.get("name")
                )
                continue
            report.bump(upsert.action)
            report.events += upsert.events_written
            report.risks += upsert.risks_written
            report.conflicts += len(upsert.conflicts)
            if upsert.duplicate_of is not None:
                report.duplicates_flagged += 1

        record_url(session, run_id, outcome)
        _checkpoint(session, dry_run)

    if dry_run:
        session.rollback()
    return report


def _checkpoint(session: Session, dry_run: bool) -> None:
    """Commit after each URL rather than once at the end of the run.

    Two reasons, both learned the hard way on a 150-article run:

    * **Lock contention.** One transaction spanning 150 articles holds SQLite's
      write lock for around 25 minutes, and anything else touching the database in
      that window fails with "database is locked" -- taking the whole run with it.
    * **Durability.** A failure on article 149 previously discarded the other 148.
      Ingestion is idempotent by design, so committing as it goes means a
      re-run resumes instead of starting over.

    A dry run holds everything so the outer rollback still discards it.
    """
    if not dry_run:
        session.commit()


def _split_cached(urls: list[str], cache_dir: Path | None) -> tuple[list[FetchResult], list[str]]:
    """Serve article text from disk when we have it.

    Iterating on the prompt is the common inner loop, and it should never re-fetch.
    """
    if not cache_dir:
        return [], urls
    cached: list[FetchResult] = []
    remaining: list[str] = []
    for url in urls:
        path = cache_path(url, cache_dir)
        if path.is_file():
            cached.append(
                FetchResult(
                    url,
                    True,
                    markdown=path.read_text(encoding="utf-8"),
                    fetched_at=dt.datetime.fromtimestamp(path.stat().st_mtime).replace(
                        microsecond=0
                    ),
                    via="cache",
                )
            )
        else:
            remaining.append(url)
    if cached:
        log.info("served %d article(s) from %s", len(cached), cache_dir)
    return cached, remaining


def _write_cache(results: list[FetchResult], cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    for result in results:
        if result.ok and result.markdown:
            cache_path(result.url, cache_dir).write_text(result.markdown, encoding="utf-8")


def count_populated(record: IngestRecord) -> int:
    """How many of the 12 tracked PRD fields this record actually carries.

    The definition of done asks for at least 9 of 12 from a known article, so it
    needs to be measurable.
    """
    claims = record.sources[0].claims if record.sources else {}
    return sum(1 for f in TRACKED_FIELDS if claims.get(f) is not None)


__all__ = [
    "MAX_PROJECTS_PER_ARTICLE",
    "MAX_USD_PER_MW",
    "SCALE_NOTE_FIELD",
    "SCALE_NOTE_MARKER",
    "TRUNCATION_MARKER",
    "CrawlError",
    "ExtractionOutcome",
    "build_records",
    "classify_source_type",
    "count_populated",
    "evidence_gate",
    "extract_one",
    "read_urls",
    "record_url",
    "run",
    "stale_sources",
    "truncate",
]
