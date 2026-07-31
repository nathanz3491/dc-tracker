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
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings
from tracker.ingest.fetch import Fetcher, FetchResult, cache_path, fetch_all
from tracker.ingest.records import (
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
from tracker.prompts import Prompt, load_prompt
from tracker.upsert import upsert_record
from tracker.vocab import DEFAULT_RISK_SEVERITY, EVENT_TYPES, TRACKED_FIELDS, severity_rank

log = logging.getLogger(__name__)

#: Hard ceiling on projects taken from one article. An article listing twenty
#: sites in passing is a roundup, not twenty citable projects.
MAX_PROJECTS_PER_ARTICLE = 5

#: Hard ceiling on obstacles taken from one article for one project. There are ten
#: categories and a single article realistically reports two or three; a list longer
#: than this means the model is enumerating speculation rather than reporting.
MAX_RISKS_PER_PROJECT = 6

#: Marker inserted where the middle of an over-long article was dropped.
TRUNCATION_MARKER = "\n\n[... middle of article omitted for length ...]\n\n"

#: Domain patterns to source_type. Ordered: first match wins.
_SOURCE_TYPE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|\.)sec\.gov$"), "company_filing"),
    (re.compile(r"^(news|about|blog|ir|investor|newsroom|press)\."), "company_filing"),
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
    if operator_hosts and host.removeprefix("www.") in operator_hosts:
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


#: Article wording that evidences each phase.
#:
#: `phase` is the one tracked field that is a *judgement* rather than a value
#: copied out of the text: an article says "broke ground", never
#: `phase: construction`. Asking for a quote containing the literal word
#: discarded 60 of 90 correct classifications, and because `phase` is NOT NULL
#: every one of them silently became the `announced` default — so the stored
#: phase distribution was an artefact of the gate, not of the projects.
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
#: `blocker` used to be here and no longer is, because it is no longer a value the
#: model returns: obstacles come back in `risks[]` and the column is derived from
#: the stored rows. `_risks` applies a strictly stronger form of this same
#: carve-out — the quote must be real *and* must contain wording for the category
#: it is filed under, so an unrelated real sentence under a plausible label is not
#: enough. Trusting the label alone is the weakest link here, and the risk path is
#: where that was worth removing.
_SUMMARY_FIELDS = frozenset({"phase", "notes"})


def _stated_in(field: str, value: Any, quote: str) -> bool:
    """Does `quote` actually assert `value` for `field`?

    Comparison is on *normalized* values, not on strings, so "200 megawatt"
    evidences ``mw_planned=200.0`` and "1.2GW" evidences ``1200.0``. That is the
    whole point: the model's own words for a number never match our storage form.
    """
    if field == "phase":
        low = quote.lower()
        return any(token in low for token in _PHASE_EVIDENCE.get(str(value), ()))

    if field in {"mw_planned", "mw_built"}:
        return _matches_quantity(value, quote, _MW_EXPR, norm_mw_detail, field)
    if field == "investment_usd":
        return _matches_quantity(value, quote, _MONEY_EXPR, norm_money_detail, field)
    if field in {"first_announced", "expected_online"}:
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


def evidence_gate(
    values: dict[str, Any], evidence: list[dict[str, Any]], article_text: str
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Keep only values the article is verified to actually state.

    Returns ``(kept, quotes_by_field, dropped_field_names)``.

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
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        name = entry.get("field")
        quote = entry.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            continue
        if _normalize_for_match(quote) not in haystack:
            log.warning("evidence quote for %r is not in the article; ignoring", name)
            continue
        verified.append(quote.strip())
        if isinstance(name, str):
            quotes.setdefault(name, quote.strip())

    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for name, value in values.items():
        if value is None:
            continue
        # `country` is structural, not a claim: it is how we know the project is
        # in scope at all, and every source here is US news.
        if name == "country":
            kept[name] = value
            continue
        # A paraphrase cannot be matched against its own source text, so for those
        # fields the model's label over a verified quote is what we have.
        #
        # The language check closes the one hole that carve-out opens. Every other
        # field is protected from a foreign-language source for free, because
        # "230兆瓦" matches no MW pattern and no English phase keyword — but a
        # summary field skips value matching entirely, so a Chinese sentence could
        # evidence `phase=construction`. Measured against a real translated repost:
        # it did, while every quantity on the same article was correctly dropped.
        if name in _SUMMARY_FIELDS and name in quotes and looks_english(quotes[name]):
            kept[name] = value
            continue
        support = next((q for q in verified if _stated_in(name, value, q)), None)
        if support is not None:
            kept[name] = value
            quotes.setdefault(name, support)
        else:
            dropped.append(name)
    return kept, quotes, dropped


def _coerce(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
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

    return values, notes


def _events(raw: dict[str, Any], url: str) -> list[EventRecord]:
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
        events.append(EventRecord(when, event_type, description, url))
    return events


def _risk_quote_supports(category: str, quote: str) -> bool:
    """Does this quote contain wording for the category it is filed under?"""
    normalized = _normalize_for_match(quote)
    return any(token in normalized for token in _RISK_EVIDENCE.get(category, ()))


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
    """
    haystack = _normalize_for_match(article_text)
    kept: list[RiskRecord] = []
    notes: list[str] = []
    seen: set[tuple[str, dt.date | None]] = set()
    dropped_unsupported: list[str] = []

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

        quote = entry.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            dropped_unsupported.append(category)
            continue
        if _normalize_for_match(quote) not in haystack:
            log.warning("risk quote for %s is not in the article; dropping the risk", category)
            dropped_unsupported.append(category)
            continue
        if not _risk_quote_supports(category, quote):
            log.warning("risk quote for %s does not state that category; dropping", category)
            dropped_unsupported.append(category)
            continue

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
                quote=norm_excerpt(quote),
                first_seen=first_seen_value,
                delay_days=delay_days,
                source_url=url,
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

    if dropped_unsupported:
        notes.append(
            "dropped unsupported risk(s) for "
            + ", ".join(sorted(set(dropped_unsupported)))
            + " (no verbatim quote from the article states them)"
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
        values, coercion_notes = _coerce(raw)
        evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
        kept, quotes, dropped = evidence_gate(values, evidence, result.markdown)

        # Identity: without a company and a locality there is nothing to dedup on,
        # so the "project" is a passing mention rather than a record.
        company = kept.get("company") or values.get("company")
        state = kept.get("state") or values.get("state")
        city = kept.get("city") or values.get("city")
        county = kept.get("county") or values.get("county")
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

        # Report only what was genuinely discarded. `dropped` is the gate's raw
        # verdict, but identity fields (name, company, city, county, state) are
        # then restored from the ungated values because a project row cannot exist
        # without them and the article self-evidently concerns it — and `notes` is
        # a summary, never a citable claim, so it is recorded separately below.
        # Listing either as "dropped" told the operator something untrue, which is
        # corrosive in the one place they look to judge data quality.
        actually_dropped = sorted(f for f in dropped if f not in claims and f != "notes")
        notes = list(coercion_notes)
        if actually_dropped:
            notes.append(
                "dropped unsupported value(s) for "
                + ", ".join(actually_dropped)
                + " (no verbatim quote from the article states them)"
            )
        if values.get("notes"):
            notes.append(f"extracted summary: {values['notes']}")

        risks, risk_notes = _risks(raw, result.markdown, result.url)
        notes.extend(risk_notes)

        # A source that reported an obstacle supports `blocker`, even though the
        # column is derived rather than claimed. Recording it keeps the "every
        # non-null tracked field appears in some source's `fields`" invariant true
        # without the merge path ever reading the value: `upsert` skips `blocker` in
        # the recompute loop and derives it from the `risk` rows instead.
        if risks:
            claims["blocker"] = max(risks, key=lambda r: severity_rank(r.severity)).summary

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
                    extractor=f"crawl:{prompt.stamp}:{reply.model}:{result.via}",
                )
            ],
            events=_events(raw, result.url),
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
    )

    body = truncate(result.markdown, settings.max_input_chars)
    user = prompt.render_user(
        url=result.url,
        published_date=published_date,
        markdown=body,
        max_projects=MAX_PROJECTS_PER_ARTICLE,
    )

    last_error: str | None = None
    for attempt in range(1, max(1, settings.llm_max_attempts) + 1):
        message = user
        if attempt > 1:
            message = (
                user + "\n\nYour previous reply was not a single valid JSON object. "
                "Return ONLY the JSON object, with no prose and no code fences."
            )
        try:
            reply = extractor.complete(system=prompt.system, user=message)
        except LLMError as exc:
            outcome.status = "llm_error"
            outcome.error = str(exc)
            return outcome

        outcome.prompt_tokens += reply.prompt_tokens or 0
        outcome.completion_tokens += reply.completion_tokens or 0

        if reply.finish_reason == "length":
            last_error = "reply truncated at the token limit"
            log.warning("%s: %s (attempt %d)", result.url, last_error, attempt)
            continue

        try:
            payload = parse_json_object(reply.text)
        except LLMJsonError as exc:
            last_error = str(exc)
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
    session.flush()


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
    run_id: str | None = None,
) -> IngestReport:
    """Fetch, extract and upsert a list of article URLs.

    `extractor` is injectable and is resolved *before* any fetch, so a missing API
    key fails immediately rather than after paying for forty page loads — and so
    tests can supply a fake without needing a key at all.
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
    fetched = (
        asyncio.run(fetch_all(to_fetch, fetcher=fetcher, escalate=escalate, settings=settings))
        if to_fetch
        else []
    )
    if cache_dir:
        _write_cache(fetched, cache_dir)

    for result in [*cached, *fetched]:
        if not result.ok:
            report.fetch_error += 1
            outcome = ExtractionOutcome(
                url=result.url,
                status="fetch_error",
                error=result.error,
                http_status=result.status,
                via=result.via,
                attempts=result.attempts,
            )
            log.warning("fetch failed: %s (%s)", result.url, result.error)
            record_url(session, run_id, outcome)
            _checkpoint(session, dry_run)
            continue

        outcome = extract_one(result, prompt=prompt, extractor=extractor, settings=settings)
        if outcome.status == "parse_error":
            report.parse_error += 1
            log.warning("could not parse a reply for %s: %s", result.url, outcome.error)
        elif outcome.status == "llm_error":
            report.parse_error += 1
            log.error("LLM error for %s: %s", result.url, outcome.error)

        for record in outcome.records:
            upsert = upsert_record(session, record)
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
