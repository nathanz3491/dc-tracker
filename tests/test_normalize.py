"""Exhaustive table-driven coverage of the field normalizers.

This is the cheapest high-value test module in the project: no DB, no network,
no LLM. It is also the direct mitigation for the PRD's highest-severity risk
("LLM extracts fields in wrong types"), so the cases below are drawn from the
shapes real ISO exports and real news prose actually produce.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from tracker.normalize import (
    EXCERPT_MAX,
    NormalizationError,
    is_blank,
    norm_country,
    norm_date,
    norm_date_detail,
    norm_event_type,
    norm_excerpt,
    norm_lat,
    norm_lon,
    norm_money,
    norm_money_detail,
    norm_mw,
    norm_mw_detail,
    norm_phase,
    norm_source_type,
    norm_state,
    norm_text,
    norm_url,
    soft,
)

# --- Blanks and sentinels ---------------------------------------------------

NULLISH = [
    "",
    "   ",
    "-",
    "--",
    "?",
    "N/A",
    "n/a",
    "na",
    "TBD",
    "tba",
    "None",
    "null",
    "unknown",
    "Undisclosed",
    "not disclosed",
    "not stated",
    None,
]


@pytest.mark.parametrize("raw", NULLISH)
def test_sentinels_are_blank(raw):
    assert is_blank(raw) is True


@pytest.mark.parametrize("raw", ["0", "Microsoft", "WI", 0, 0.0])
def test_real_values_are_not_blank(raw):
    assert is_blank(raw) is False


@pytest.mark.parametrize(
    "fn",
    [
        norm_state,
        norm_mw,
        norm_money,
        norm_date,
        norm_phase,
        norm_text,
        norm_url,
        norm_lat,
        norm_lon,
        norm_country,
        norm_source_type,
        norm_event_type,
    ],
)
@pytest.mark.parametrize("raw", ["", "N/A", "TBD", "-", None])
def test_every_normalizer_maps_sentinels_to_none(fn, raw):
    """A null is a correct answer. No normalizer may invent a value for one."""
    assert fn(raw) is None


# --- State ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("WI", "WI"),
        ("wi", "WI"),
        (" tx ", "TX"),
        ("Wisconsin", "WI"),
        ("wisconsin", "WI"),
        ("WISCONSIN", "WI"),
        ("Virginia", "VA"),
        ("West Virginia", "WV"),
        ("New Mexico", "NM"),
        ("North Dakota", "ND"),
        ("Texas", "TX"),
        ("District of Columbia", "DC"),
        ("Washington DC", "DC"),
        ("Puerto Rico", "PR"),
        ("Guam", "GU"),
        ("Virgin Islands", "VI"),
    ],
)
def test_norm_state(raw, expected):
    assert norm_state(raw) == expected


@pytest.mark.parametrize("raw", ["Xanadu", "ZZ", "Ontario", "United States", "W"])
def test_norm_state_rejects_non_states(raw):
    with pytest.raises(NormalizationError) as exc:
        norm_state(raw)
    assert exc.value.field == "state"


# --- Country ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("US", "US"),
        ("us", "US"),
        ("USA", "US"),
        ("United States", "US"),
        ("United States of America", "US"),
        ("Canada", "CA"),
        ("MX", "MX"),
    ],
)
def test_norm_country(raw, expected):
    assert norm_country(raw) == expected


def test_norm_country_rejects_garbage():
    with pytest.raises(NormalizationError):
        norm_country("Freedonia")


# --- Power ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1000", 1000.0),
        ("1000 MW", 1000.0),
        ("1,000 MW", 1000.0),
        ("1,000MW", 1000.0),
        ("800MW", 800.0),
        ("800 mw", 800.0),
        ("300 megawatts", 300.0),
        ("300 Megawatt", 300.0),
        ("1.5 GW", 1500.0),
        ("1.5GW", 1500.0),
        ("2 gigawatts", 2000.0),
        ("500000 kW", 500.0),
        ("0", 0.0),
        ("0 MW", 0.0),
        (1000, 1000.0),
        (1000.5, 1000.5),
        # Unicode grime from scraped pages: a non-breaking space before the unit
        # and full-width digits both have to survive NFKC normalization.
        ("1,000\u00a0MW", 1000.0),
        ("\uff11\uff10\uff10\uff10 MW", 1000.0),
    ],
)
def test_norm_mw(raw, expected):
    assert norm_mw(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "expected_lo"),
    [("500-700 MW", 500.0), ("500 to 700 MW", 500.0), ("500–700 MW", 500.0), ("1.5-2 GW", 1500.0)],
)
def test_norm_mw_range_takes_lower_bound_and_notes_it(raw, expected_lo):
    """Never overstate capacity: a range stores its floor and says so."""
    parsed = norm_mw_detail(raw)
    assert parsed.value == pytest.approx(expected_lo)
    assert parsed.note is not None and "range" in parsed.note


@pytest.mark.parametrize("raw", ["~500 MW", "about 500 MW", "approximately 500MW", "over 500 MW"])
def test_norm_mw_approximate_is_flagged(raw):
    parsed = norm_mw_detail(raw)
    assert parsed.value == pytest.approx(500.0)
    assert parsed.note is not None and "approximately" in parsed.note


@pytest.mark.parametrize("raw", ["lots", "a gigawatt", "500 bananas", "-5 MW", "MW"])
def test_norm_mw_rejects_unparseable(raw):
    with pytest.raises(NormalizationError):
        norm_mw(raw)


# --- Money ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$3.3 billion", 3_300_000_000),
        ("3.3 billion", 3_300_000_000),
        ("3.3B", 3_300_000_000),
        ("3.3bn", 3_300_000_000),
        ("$3.3B", 3_300_000_000),
        ("USD 3,300,000,000", 3_300_000_000),
        ("$3,300,000,000", 3_300_000_000),
        ("3300000000", 3_300_000_000),
        ("$500 million", 500_000_000),
        ("500M", 500_000_000),
        ("$700m", 700_000_000),
        ("$1.2 trillion", 1_200_000_000_000),
        ("$250,000", 250_000),
        ("US$1 billion", 1_000_000_000),
        ("$3.3 billion dollars", 3_300_000_000),
        (1_000_000, 1_000_000),
    ],
)
def test_norm_money(raw, expected):
    assert norm_money(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected_lo"),
    [
        ("$3-5 billion", 3_000_000_000),
        ("$500 million to $1 billion", 500_000_000),
        ("3.3-4.5B", 3_300_000_000),
    ],
)
def test_norm_money_range_takes_lower_bound(raw, expected_lo):
    parsed = norm_money_detail(raw)
    assert int(parsed.value) == expected_lo
    assert parsed.note is not None and "range" in parsed.note


@pytest.mark.parametrize("raw", ["a lot of money", "$$$", "several billion", "$3.3 zillion"])
def test_norm_money_rejects_unparseable(raw):
    with pytest.raises(NormalizationError):
        norm_money(raw)


# --- Dates ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected", "precision"),
    [
        ("2025-07-01", dt.date(2025, 7, 1), "day"),
        ("2025-07-01T12:30:00Z", dt.date(2025, 7, 1), "day"),
        ("2025-07-01 12:30:00", dt.date(2025, 7, 1), "day"),
        ("3/1/2025", dt.date(2025, 3, 1), "day"),
        ("03/01/25", dt.date(2025, 3, 1), "day"),
        ("March 5, 2025", dt.date(2025, 3, 5), "day"),
        ("Mar 5 2025", dt.date(2025, 3, 5), "day"),
        ("5 March 2025", dt.date(2025, 3, 5), "day"),
        ("March 2025", dt.date(2025, 3, 1), "month"),
        ("Sept 2025", dt.date(2025, 9, 1), "month"),
        ("2025-07", dt.date(2025, 7, 1), "month"),
        ("Q1 2025", dt.date(2025, 1, 1), "quarter"),
        ("Q3 2025", dt.date(2025, 7, 1), "quarter"),
        ("q4 2026", dt.date(2026, 10, 1), "quarter"),
        ("2025 Q3", dt.date(2025, 7, 1), "quarter"),
        ("H1 2026", dt.date(2026, 1, 1), "half"),
        ("H2 2026", dt.date(2026, 7, 1), "half"),
        ("2025", dt.date(2025, 1, 1), "year"),
        (dt.date(2025, 7, 1), dt.date(2025, 7, 1), "day"),
        (dt.datetime(2025, 7, 1, 8, 0), dt.date(2025, 7, 1), "day"),
    ],
)
def test_norm_date_value_and_precision(raw, expected, precision):
    parsed = norm_date_detail(raw)
    assert parsed.value == expected
    assert parsed.precision == precision


def test_coarse_dates_carry_a_note_about_the_collapse():
    """'Q3 2025' and '2025-07-01' are stored alike but mean different things."""
    assert "quarter" in norm_date_detail("Q3 2025").note
    assert "year" in norm_date_detail("2025").note
    assert norm_date_detail("2025-07-01").note is None


@pytest.mark.parametrize(
    "raw",
    [
        "late 2026",
        "early 2027",
        "mid-2026",
        "by 2028",
        "next year",
        "next spring",
        "end of 2026",
        "summer 2026",
        "around 2027",
        "soon",
    ],
)
def test_vague_dates_resolve_to_none_not_a_guess(raw):
    """Pinning vague language to a day would fabricate precision."""
    parsed = norm_date_detail(raw)
    assert parsed.value is None
    assert parsed.note is not None and "vaguely" in parsed.note


@pytest.mark.parametrize("raw", ["2025-13-01", "2025-02-30", "13/45/2025", "gibberish"])
def test_norm_date_rejects_invalid(raw):
    with pytest.raises(NormalizationError):
        norm_date(raw)


# --- Phase ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("announced", "announced"),
        ("Proposed", "announced"),
        ("planned", "announced"),
        ("permitting", "permitting"),
        ("Under Study", "permitting"),
        ("Active", "permitting"),
        ("zoning approval", "permitting"),
        ("construction", "construction"),
        ("Under Construction", "construction"),
        ("broke ground", "construction"),
        ("Engineering and Procurement", "construction"),
        ("operational", "operational"),
        ("In Service", "operational"),
        ("energized", "operational"),
        ("online", "operational"),
        ("paused", "paused"),
        ("On Hold", "paused"),
        ("suspended", "paused"),
        ("cancelled", "cancelled"),
        ("canceled", "cancelled"),
        ("Withdrawn", "cancelled"),
        ("Retracted", "cancelled"),
    ],
)
def test_norm_phase(raw, expected):
    assert norm_phase(raw) == expected


def test_norm_phase_default_is_returned_for_blank_not_invented():
    assert norm_phase("", default=None) is None
    assert norm_phase("N/A", default="announced") == "announced"


def test_norm_phase_rejects_unknown_wording():
    with pytest.raises(NormalizationError):
        norm_phase("Unknown Blah")


# --- Closed vocabularies ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("iso_queue", "iso_queue"),
        ("trade press", "trade_press"),
        ("trade-press", "trade_press"),
        ("Manual", "manual"),
    ],
)
def test_norm_source_type(raw, expected):
    assert norm_source_type(raw) == expected


def test_norm_source_type_rejects_fetch_error():
    """`fetch_error` is deliberately NOT a source_type -- it lives in ingest_url,
    because a failed fetch has no project to attach a source row to."""
    with pytest.raises(NormalizationError):
        norm_source_type("fetch_error")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("announced", "announced"),
        ("permit filed", "permit_filed"),
        ("Groundbreaking", "groundbreaking"),
        ("first-customer", "first_customer"),
    ],
)
def test_norm_event_type(raw, expected):
    assert norm_event_type(raw) == expected


# --- URLs -------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com",
        "http://example.com/a/b?c=d#e",
        "https://www.pjm.com/planning/queues.aspx#AG1-234",
    ],
)
def test_norm_url_accepts_absolute_http(raw):
    assert norm_url(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "example.com",
        "/relative/path",
        "ftp://example.com",
        "file:///C:/tmp/x.csv",
        "javascript:alert(1)",
    ],
)
def test_norm_url_rejects_non_http(raw):
    """A citation you cannot open in a browser is not a citation."""
    with pytest.raises(NormalizationError):
        norm_url(raw)


# --- Text and excerpts ------------------------------------------------------


def test_norm_text_collapses_whitespace_and_normalizes_unicode():
    assert norm_text("  Mount\u00a0 Pleasant\n\n") == "Mount Pleasant"


def test_norm_excerpt_is_capped():
    long = "word " * 300
    out = norm_excerpt(long)
    assert len(out) <= EXCERPT_MAX
    assert out.endswith("…")


def test_norm_excerpt_leaves_short_quotes_intact():
    quote = "Microsoft said the campus will draw 900 MW at full buildout."
    assert norm_excerpt(quote) == quote


# --- Failure policy ---------------------------------------------------------


def test_soft_downgrades_parse_failure_to_none_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert soft(norm_mw, "lots") is None
    assert "cannot parse" in caplog.text


def test_soft_passes_good_values_through():
    assert soft(norm_mw, "900 MW") == 900.0


def test_normalization_error_carries_field_and_value():
    """Ingest reject logs need the field and the offending value, not just a message."""
    with pytest.raises(NormalizationError) as exc:
        norm_mw("bananas", field="mw_built")
    assert exc.value.field == "mw_built"
    assert exc.value.value == "bananas"
    assert exc.value.reason
