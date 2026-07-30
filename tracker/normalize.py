"""Field-by-field validation and coercion.

One function per field. Every function here is side-effect-free and takes a
single raw value, which is what makes this the cheapest module in the project to
test exhaustively and the right place to concentrate the PRD's highest-severity
risk: *"LLM extracts fields in wrong types"*.

Two-tier failure policy:

* A **blank or sentinel** value (``""``, ``"N/A"``, ``"TBD"``, ``"-"``,
  ``"undisclosed"``, …) returns ``None``. This is a correct answer, not a
  failure — a null we can cite is worth more than a guess we can't.
* A **non-blank value we cannot parse** raises :class:`NormalizationError`.
  The caller decides severity: required field → reject the row and log; optional
  field → wrap with :func:`soft` to log and fall back to ``None``.

Where coercion loses information (a MW *range* collapsed to its lower bound, a
``"Q3 2025"`` collapsed to a specific day) the ``*_detail`` variants return a
human-readable note alongside the value so the ingest path can record what it
did in ``project.notes`` rather than silently discarding the nuance.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import unicodedata
from collections.abc import Callable
from typing import Any, NamedTuple, TypeVar

from tracker.vocab import DEFAULT_PHASE, EVENT_TYPES, PHASES, SOURCE_TYPES

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Hard cap on stored quotes. Unbounded scraped text is both a copyright and a
#: database-size problem; 500 characters comfortably holds the "1-3 sentence
#: quote" the PRD asks for.
EXCERPT_MAX = 500

#: Values that mean "the source did not say", not "the source said this".
_NULL_TOKENS = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "?",
        "n/a",
        "n.a.",
        "na",
        "none",
        "null",
        "nil",
        "nan",
        "tbd",
        "tba",
        "unknown",
        "undisclosed",
        "not disclosed",
        "not available",
        "not stated",
        "not specified",
        "unspecified",
        "pending",
        # A seed-file field the operator has not filled in yet. It means "no
        # value", so it normalizes to None like any other sentinel. The
        # *refusal* to ingest a file still containing them lives in
        # ingest/manual.py, where it can be overridden for a smoke test.
        "placeholder",
    }
)


class NormalizationError(ValueError):
    """A non-blank value could not be coerced to the field's type."""

    def __init__(self, field: str, value: Any, reason: str) -> None:
        super().__init__(f"{field}: cannot parse {value!r} ({reason})")
        self.field = field
        self.value = value
        self.reason = reason


def soft(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T | None:
    """Run a normalizer, downgrading a parse failure to a warning and ``None``.

    Use for optional fields. Required fields should let the error propagate so
    the ingest path can reject the row.
    """
    try:
        return fn(*args, **kwargs)
    except NormalizationError as exc:
        log.warning("dropping unparseable value: %s", exc)
        return None


# --- Primitives -------------------------------------------------------------


def is_blank(raw: Any) -> bool:
    """True for None, empty/whitespace strings, and sentinel non-values."""
    if raw is None:
        return True
    if isinstance(raw, str):
        return _clean(raw).lower() in _NULL_TOKENS
    return False


def _clean(raw: str) -> str:
    """Normalize unicode, collapse whitespace, strip.

    NFKC matters more than it looks: scraped text carries non-breaking spaces,
    full-width digits and typographic dashes, all of which break naive parsing.
    """
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"\s+", " ", text).strip()


def norm_text(raw: Any, *, field: str = "text", max_len: int | None = None) -> str | None:
    """Cleaned free text, or None if blank."""
    if is_blank(raw):
        return None
    text = _clean(str(raw))
    if max_len is not None and len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text or None


def norm_excerpt(raw: Any) -> str | None:
    """A source quote, hard-capped at :data:`EXCERPT_MAX` characters."""
    return norm_text(raw, field="excerpt", max_len=EXCERPT_MAX)


# --- State and country ------------------------------------------------------

# fmt: off
_STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    # Federal district and territories. Included because they are 2-letter and
    # would otherwise pass the length check while meaning nothing.
    "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC",
    "puerto rico": "PR", "guam": "GU", "american samoa": "AS",
    "northern mariana islands": "MP", "u.s. virgin islands": "VI",
    "us virgin islands": "VI", "virgin islands": "VI",
}
# fmt: on

STATE_CODES: frozenset[str] = frozenset(_STATE_NAMES.values())

_COUNTRY_NAMES = {
    "us": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states": "US",
    "united states of america": "US",
    "america": "US",
    "ca": "CA",
    "canada": "CA",
    "mx": "MX",
    "mexico": "MX",
}


def norm_state(raw: Any, *, field: str = "state") -> str | None:
    """Any US state spelling to its 2-letter USPS code."""
    if is_blank(raw):
        return None
    text = _clean(str(raw))
    upper = text.upper()
    if upper in STATE_CODES:
        return upper
    key = text.lower().rstrip(".").replace("  ", " ")
    if key in _STATE_NAMES:
        return _STATE_NAMES[key]
    raise NormalizationError(field, raw, "not a US state name or code")


def norm_country(raw: Any, *, field: str = "country") -> str | None:
    if is_blank(raw):
        return None
    text = _clean(str(raw))
    if len(text) == 2 and text.isalpha():
        return text.upper()
    key = text.lower()
    if key in _COUNTRY_NAMES:
        return _COUNTRY_NAMES[key]
    raise NormalizationError(field, raw, "not a recognized country name or ISO code")


# --- Numbers ----------------------------------------------------------------


class ParsedNumber(NamedTuple):
    """A coerced number plus a note when coercion discarded information."""

    value: float | None
    note: str | None = None


# "~" binds directly to its number ("~500 MW") while word forms need a space
# ("about 500 MW"), so the two cases cannot share one `\s+`.
_APPROX = re.compile(
    r"^\s*(?:~\s*|(?:about|approx(?:imately)?|around|nearly|up\s+to|over|"
    r"more\s+than|at\s+least|almost)\s+)",
    re.I,
)
_RANGE = re.compile(
    r"^(?P<lo>[\d,.]+)\s*(?:-|to|through|thru|–)\s*(?P<hi>[\d,.]+)\s*(?P<unit>[a-z]*)\s*$",
    re.I,
)
_POWER_UNITS = {
    "mw": 1.0,
    "megawatt": 1.0,
    "megawatts": 1.0,
    "mws": 1.0,
    "gw": 1000.0,
    "gigawatt": 1000.0,
    "gigawatts": 1000.0,
    "gws": 1000.0,
    "kw": 0.001,
    "kilowatt": 0.001,
    "kilowatts": 0.001,
}


def _to_float(token: str, field: str, raw: Any) -> float:
    try:
        return float(token.replace(",", "").replace("_", ""))
    except ValueError as exc:
        raise NormalizationError(field, raw, "not numeric") from exc


def norm_mw_detail(raw: Any, *, field: str = "mw_planned") -> ParsedNumber:
    """Power capacity to megawatts.

    ``"1,000 MW"`` → 1000.0 · ``"1.5 GW"`` → 1500.0 · ``"800MW"`` → 800.0.
    A range takes the **lower bound** and reports the range in the note, so we
    never overstate capacity.
    """
    if is_blank(raw):
        return ParsedNumber(None)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if value < 0:
            raise NormalizationError(field, raw, "negative capacity")
        return ParsedNumber(value)

    text = _clean(str(raw))
    note: str | None = None
    stripped = _APPROX.sub("", text)
    if stripped != text:
        note = f"{field} stated approximately as {text!r}"
        text = stripped

    match = _RANGE.match(text)
    if match:
        lo = _to_float(match.group("lo"), field, raw)
        hi = _to_float(match.group("hi"), field, raw)
        unit = (match.group("unit") or "mw").lower()
        factor = _POWER_UNITS.get(unit)
        if factor is None:
            raise NormalizationError(field, raw, f"unknown power unit {unit!r}")
        return ParsedNumber(
            lo * factor,
            f"{field} given as range {lo:g}-{hi:g} {unit.upper()}; stored lower bound",
        )

    m = re.match(r"^(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]*)\.?$", text)
    if not m:
        raise NormalizationError(field, raw, "no number found")
    unit = (m.group("unit") or "mw").lower()
    factor = _POWER_UNITS.get(unit)
    if factor is None:
        raise NormalizationError(field, raw, f"unknown power unit {unit!r}")
    value = _to_float(m.group("num"), field, raw) * factor
    if value < 0:
        raise NormalizationError(field, raw, "negative capacity")
    return ParsedNumber(value, note)


def norm_mw(raw: Any, *, field: str = "mw_planned") -> float | None:
    return norm_mw_detail(raw, field=field).value


_MONEY_SCALES = {
    "": 1,
    "k": 1_000,
    "thousand": 1_000,
    "m": 1_000_000,
    "mm": 1_000_000,
    "mn": 1_000_000,
    "million": 1_000_000,
    "b": 1_000_000_000,
    "bn": 1_000_000_000,
    "billion": 1_000_000_000,
    "t": 1_000_000_000_000,
    "tn": 1_000_000_000_000,
    "trillion": 1_000_000_000_000,
}
#: Leading currency marker: "US$1B", "USD 3,300,000,000", "$500 million".
#: Stripped as a prefix rather than matched anywhere, because "US$1" has no
#: separator between the marker and the digits.
_CURRENCY_PREFIX = re.compile(r"^\s*(?:us\s*\$|usd|us\$|\$)\s*", re.I)
_CURRENCY_WORD = re.compile(r"\b(?:usd|dollars?)\b", re.I)


def norm_money_detail(raw: Any, *, field: str = "investment_usd") -> ParsedNumber:
    """Money to whole US dollars.

    ``"$3.3 billion"`` → 3_300_000_000 · ``"3.3B"`` → 3_300_000_000 ·
    ``"USD 3,300,000,000"`` → 3_300_000_000. A range takes the lower bound.
    """
    if is_blank(raw):
        return ParsedNumber(None)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if raw < 0:
            raise NormalizationError(field, raw, "negative investment")
        return ParsedNumber(float(raw))

    text = _clean(str(raw))
    note: str | None = None
    stripped = _APPROX.sub("", text)
    if stripped != text:
        note = f"{field} stated approximately as {text!r}"
        text = stripped

    text = _CURRENCY_PREFIX.sub("", text)
    # Any remaining "$" is an interior marker, as in "$500 million to $1 billion".
    text = _CURRENCY_WORD.sub(" ", text.replace("$", " "))
    text = _clean(text)

    range_match = re.match(
        r"^(?P<lo>[\d,.]+)\s*(?P<lounit>[a-z]*)\s*(?:-|to|–)\s*(?P<hi>[\d,.]+)\s*(?P<hiunit>[a-z]*)$",
        text,
        re.I,
    )
    if range_match:
        hi_unit = (range_match.group("hiunit") or "").lower()
        lo_unit = (range_match.group("lounit") or hi_unit).lower()
        if lo_unit not in _MONEY_SCALES or hi_unit not in _MONEY_SCALES:
            raise NormalizationError(field, raw, "unknown money scale in range")
        lo = _to_float(range_match.group("lo"), field, raw) * _MONEY_SCALES[lo_unit]
        hi = _to_float(range_match.group("hi"), field, raw) * _MONEY_SCALES[hi_unit]
        return ParsedNumber(
            float(int(lo)),
            f"{field} given as range ${lo:,.0f}-${hi:,.0f}; stored lower bound",
        )

    m = re.match(r"^(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]*)\.?$", text)
    if not m:
        raise NormalizationError(field, raw, "no monetary amount found")
    unit = (m.group("unit") or "").lower()
    if unit not in _MONEY_SCALES:
        raise NormalizationError(field, raw, f"unknown money scale {unit!r}")
    value = _to_float(m.group("num"), field, raw) * _MONEY_SCALES[unit]
    if value < 0:
        raise NormalizationError(field, raw, "negative investment")
    return ParsedNumber(float(int(value)), note)


def norm_money(raw: Any, *, field: str = "investment_usd") -> int | None:
    parsed = norm_money_detail(raw, field=field)
    return None if parsed.value is None else int(parsed.value)


def norm_coord(raw: Any, *, field: str, lo: float, hi: float) -> float | None:
    if is_blank(raw):
        return None
    try:
        value = float(_clean(str(raw)))
    except ValueError as exc:
        raise NormalizationError(field, raw, "not numeric") from exc
    if not lo <= value <= hi:
        raise NormalizationError(field, raw, f"outside [{lo}, {hi}]")
    return value


def norm_lat(raw: Any) -> float | None:
    return norm_coord(raw, field="lat", lo=-90.0, hi=90.0)


def norm_lon(raw: Any) -> float | None:
    return norm_coord(raw, field="lon", lo=-180.0, hi=180.0)


# --- Dates ------------------------------------------------------------------


class ParsedDate(NamedTuple):
    """A date plus how precise the source actually was.

    Precision is recorded because ``"Q3 2025"`` and ``"2025-07-01"`` are stored
    identically but mean very different things, and an operator reviewing a row
    needs to know which one they are looking at.
    """

    value: dt.date | None
    precision: str | None = None  # "day" | "month" | "quarter" | "half" | "year"
    note: str | None = None


_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_QUARTER_MONTH = {1: 1, 2: 4, 3: 7, 4: 10}

#: Hedged-but-anchored temporal language: a qualifier plus an explicit year.
#: "late 2027" is genuinely informative — resolving it to a *quarter* keeps the
#: information at the precision the source actually offered, which is better than
#: discarding it. The precision is reported as "quarter" and a note records the
#: coarsening, so nothing downstream mistakes it for a firm date.
#:
#: Announcements hedge this way constantly, and treating every hedge as NULL was
#: throwing away most of `expected_online`.
_QUALIFIER_QUARTER: dict[str, int] = {
    "early": 1,
    "beginning of": 1,
    "start of": 1,
    "spring": 2,
    "mid": 3,
    "middle of": 3,
    "summer": 3,
    "late": 4,
    "end of": 4,
    "fall": 4,
    "autumn": 4,
    "winter": 4,
}

_QUALIFIED_YEAR = re.compile(
    r"^(?P<qual>early|beginning of|start of|spring|mid|middle of|summer|"
    r"late|end of|fall|autumn|winter)"
    r"[\s\-–—of]*"
    r"(?P<year>(?:19|20)\d{2})\.?$",
    re.I,
)

#: A hedge with a year but no sub-year signal: "by 2028", "around 2027". Resolved
#: to year precision, exactly like a bare "2028" already is — no more precision is
#: implied than the source gave.
#:
#: Deliberately excludes "before" and "after": those state a *direction* relative
#: to the year, so storing the year itself would point the wrong way.
_HEDGED_YEAR = re.compile(
    r"^(?:by|around|circa|approx(?:imately)?|roughly|sometime in|in or around)"
    r"[\s\-–—]*(?P<year>(?:19|20)\d{2})\.?$",
    re.I,
)

#: Temporal language with no anchor at all. These stay NULL: there is no year to
#: attach a quarter to, so any date would be invented outright.
_UNANCHORED = re.compile(
    r"^(?:by|before|after|around|circa|next|this|last|soon|tbd|imminent|"
    r"shortly|eventually|future|later|early|mid|late|end of|beginning of|"
    r"spring|summer|fall|autumn|winter|h1|h2)\b",
    re.I,
)


def norm_date_detail(raw: Any, *, field: str = "date") -> ParsedDate:
    """Parse a date, reporting the precision the source actually offered.

    ISO ``2025-07-01`` → day · ``"March 2025"`` → month (day 1) ·
    ``"Q3 2025"`` → quarter (2025-07-01) · ``"2025"`` → year (2025-01-01).

    A hedge with a year attached is resolved to the quarter it implies:
    ``"late 2027"`` → 2027-10-01 at quarter precision, with a note. A hedge with
    no year (``"next spring"``, ``"soon"``) stays ``None``, because there is
    nothing to anchor it to and any date would be invented outright.
    """
    if is_blank(raw):
        return ParsedDate(None)
    if isinstance(raw, dt.datetime):
        return ParsedDate(raw.date(), "day")
    if isinstance(raw, dt.date):
        return ParsedDate(raw, "day")

    text = _clean(str(raw))

    qualified = _QUALIFIED_YEAR.match(text)
    if qualified:
        quarter = _QUALIFIER_QUARTER[qualified.group("qual").lower()]
        year = int(qualified.group("year"))
        return ParsedDate(
            _safe_date(year, _QUARTER_MONTH[quarter], 1, field, raw),
            "quarter",
            f"{field} stated as {text!r}; read as Q{quarter} {year} and stored as the "
            "first day of that quarter, so treat it as approximate",
        )

    hedged = _HEDGED_YEAR.match(text)
    if hedged:
        year = int(hedged.group("year"))
        return ParsedDate(
            _safe_date(year, 1, 1, field, raw),
            "year",
            f"{field} stated as {text!r}; stored as January 1 {year} at year precision, "
            "so treat it as approximate",
        )

    # ISO 8601, with or without a time component.
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$", text)
    if iso:
        return ParsedDate(_safe_date(int(iso[1]), int(iso[2]), int(iso[3]), field, raw), "day")

    # US slashed formats: 3/1/2025, 03/01/25.
    us = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$", text)
    if us:
        month, day, year = int(us[1]), int(us[2]), int(us[3])
        if year < 100:
            year += 2000 if year < 70 else 1900
        return ParsedDate(_safe_date(year, month, day, field, raw), "day")

    # 2025-07 or 2025/07
    ym = re.match(r"^(\d{4})[-/](\d{1,2})$", text)
    if ym:
        return ParsedDate(
            _safe_date(int(ym[1]), int(ym[2]), 1, field, raw),
            "month",
            f"{field} given to month precision ({text!r}); stored as day 1",
        )

    # Quarter: Q3 2025, 2025 Q3, 3Q25
    q = re.match(r"^(?:q(?P<q1>[1-4])\s*(?:of\s*)?(?P<y1>\d{4}))$", text, re.I) or re.match(
        r"^(?:(?P<y1>\d{4})\s*q(?P<q1>[1-4]))$", text, re.I
    )
    if q:
        quarter, year = int(q.group("q1")), int(q.group("y1"))
        return ParsedDate(
            _safe_date(year, _QUARTER_MONTH[quarter], 1, field, raw),
            "quarter",
            f"{field} given as {text!r}; stored as first day of that quarter",
        )

    # Halves: "H1 2026", "first half of 2026". A coarser bucket than a
    # quarter -- H1 spans Jan-Jun -- so it keeps its own precision rather than
    # being flattened into Q1.
    h = re.match(
        r"^(?:h(?P<h>[12])|(?P<word>first|second)\s+half\s+of)\s*"
        r"(?P<y>(?:19|20)\d{2})\.?$",
        text,
        re.I,
    )
    if h:
        year = int(h.group("y"))
        first_half = h.group("h") == "1" or (h.group("word") or "").lower() == "first"
        month = 1 if first_half else 7
        return ParsedDate(
            _safe_date(year, month, 1, field, raw),
            "half",
            f"{field} given as {text!r}; stored as first day of that half",
        )

    # Month name forms: "March 2025", "Mar 2025", "March 5, 2025", "5 March 2025"
    mn = re.match(r"^(?P<mon>[a-z]+)\.?\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})$", text, re.I)
    if mn and mn.group("mon").lower() in _MONTHS:
        return ParsedDate(
            _safe_date(int(mn["year"]), _MONTHS[mn["mon"].lower()], int(mn["day"]), field, raw),
            "day",
        )
    dm = re.match(r"^(?P<day>\d{1,2})\s+(?P<mon>[a-z]+)\.?\s+(?P<year>\d{4})$", text, re.I)
    if dm and dm.group("mon").lower() in _MONTHS:
        return ParsedDate(
            _safe_date(int(dm["year"]), _MONTHS[dm["mon"].lower()], int(dm["day"]), field, raw),
            "day",
        )
    my = re.match(r"^(?P<mon>[a-z]+)\.?\s+(?P<year>\d{4})$", text, re.I)
    if my and my.group("mon").lower() in _MONTHS:
        return ParsedDate(
            _safe_date(int(my["year"]), _MONTHS[my["mon"].lower()], 1, field, raw),
            "month",
            f"{field} given to month precision ({text!r}); stored as day 1",
        )

    # Bare year.
    if re.fullmatch(r"(19|20)\d{2}", text):
        return ParsedDate(
            _safe_date(int(text), 1, 1, field, raw),
            "year",
            f"{field} given only as year {text}; stored as January 1",
        )

    # Checked last, as a fallback rather than an early gate: "H1 2026" starts with
    # a qualifier the unanchored pattern also matches, so gating on it up front
    # would swallow perfectly parseable dates before the format branches ran.
    if _UNANCHORED.match(text):
        return ParsedDate(
            None, None, f"{field} stated as {text!r} with no year to anchor it; left unset"
        )

    raise NormalizationError(field, raw, "unrecognized date format")


def _safe_date(year: int, month: int, day: int, field: str, raw: Any) -> dt.date:
    try:
        return dt.date(year, month, day)
    except ValueError as exc:
        raise NormalizationError(field, raw, str(exc)) from exc


def norm_date(raw: Any, *, field: str = "date") -> dt.date | None:
    return norm_date_detail(raw, field=field).value


# --- Closed vocabularies ----------------------------------------------------

# fmt: off
_PHASE_SYNONYMS: dict[str, str] = {
    "announced": "announced", "announce": "announced", "proposed": "announced",
    "proposal": "announced", "planned": "announced", "plan": "announced",
    "planning": "announced", "pre-construction": "announced",
    "preconstruction": "announced", "early stage": "announced",
    "permitting": "permitting", "permit": "permitting", "permits": "permitting",
    "permit filed": "permitting", "permit review": "permitting",
    "under review": "permitting", "in review": "permitting",
    "zoning": "permitting", "rezoning": "permitting", "approval": "permitting",
    "approved": "permitting", "application": "permitting",
    "under study": "permitting", "active": "permitting",
    "construction": "construction", "under construction": "construction",
    "constructing": "construction", "building": "construction",
    "built": "construction", "groundbreaking": "construction",
    "broke ground": "construction", "in progress": "construction",
    "engineering and procurement": "construction",
    "partially in service - under construction": "construction",
    "operational": "operational", "operating": "operational",
    "operation": "operational", "in operation": "operational",
    "online": "operational", "in service": "operational",
    "in-service": "operational", "live": "operational",
    "energized": "operational", "complete": "operational",
    "completed": "operational", "commissioned": "operational",
    "done": "operational",
    "paused": "paused", "pause": "paused", "on hold": "paused",
    "hold": "paused", "suspended": "paused", "halted": "paused",
    "stalled": "paused", "shelved": "paused",
    "cancelled": "cancelled", "canceled": "cancelled", "cancel": "cancelled",
    "withdrawn": "cancelled", "retracted": "cancelled",
    "abandoned": "cancelled", "scrapped": "cancelled",
    "terminated": "cancelled", "deactivated": "cancelled", "dead": "cancelled",
}
# fmt: on


def norm_phase(raw: Any, *, field: str = "phase", default: str | None = None) -> str | None:
    """Any phase wording to one of :data:`tracker.vocab.PHASES`.

    Returns ``default`` (usually None) when the source states no phase. Ingest
    paths that then fall back to :data:`DEFAULT_PHASE` must omit ``phase`` from
    ``source.fields``, so confidence scoring treats it as uncited.
    """
    if is_blank(raw):
        return default
    text = _clean(str(raw)).lower().rstrip(".")
    if text in PHASES:
        return text
    if text in _PHASE_SYNONYMS:
        return _PHASE_SYNONYMS[text]
    # Substring fallback, longest synonym first so "under construction" is not
    # matched by the shorter "construction" entry with a different meaning.
    for key in sorted(_PHASE_SYNONYMS, key=len, reverse=True):
        if key in text:
            return _PHASE_SYNONYMS[key]
    raise NormalizationError(field, raw, "unrecognized project phase")


def norm_choice(raw: Any, allowed: tuple[str, ...], *, field: str) -> str | None:
    if is_blank(raw):
        return None
    text = _clean(str(raw)).lower().replace(" ", "_").replace("-", "_")
    if text in allowed:
        return text
    raise NormalizationError(field, raw, f"not one of {', '.join(allowed)}")


def norm_source_type(raw: Any, *, field: str = "source_type") -> str | None:
    return norm_choice(raw, SOURCE_TYPES, field=field)


def norm_event_type(raw: Any, *, field: str = "event_type") -> str | None:
    return norm_choice(raw, EVENT_TYPES, field=field)


def norm_url(raw: Any, *, field: str = "url") -> str | None:
    """A citation URL. Must be http(s) — a citation you cannot open is not one."""
    if is_blank(raw):
        return None
    text = _clean(str(raw))
    if not re.match(r"^https?://[^\s/$.?#].[^\s]*$", text, re.I):
        raise NormalizationError(field, raw, "not an absolute http(s) URL")
    return text


__all__ = [
    "DEFAULT_PHASE",
    "EXCERPT_MAX",
    "STATE_CODES",
    "NormalizationError",
    "ParsedDate",
    "ParsedNumber",
    "is_blank",
    "norm_coord",
    "norm_country",
    "norm_date",
    "norm_date_detail",
    "norm_event_type",
    "norm_excerpt",
    "norm_lat",
    "norm_lon",
    "norm_money",
    "norm_money_detail",
    "norm_mw",
    "norm_mw_detail",
    "norm_phase",
    "norm_source_type",
    "norm_state",
    "norm_text",
    "norm_url",
    "soft",
]
