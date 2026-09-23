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
    canonical_url,
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
    url_identity,
    url_variants,
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
    # Composite placeholder shapes: a token set cannot list "$TBD" or
    # "TBD (est.)", so a start-anchored pattern catches decoration around the
    # non-answer. Each of these was a gap found by feeding real-world spellings
    # to the old check.
    "$TBD",
    "TBD MW",
    "TBD (est.)",
    "to be determined",
    "To Be Announced",
    "not yet determined",
    "coming soon",
    "N.D.",
    "PLACEHOLDER - paste the 1-3 sentence verbatim quote that supports the values",
    "...",
    "??",
    "xx",
    "___",
]


@pytest.mark.parametrize("raw", NULLISH)
def test_sentinels_are_blank(raw):
    assert is_blank(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        "0",
        "Microsoft",
        "WI",
        0,
        0.0,
        # Contains a placeholder token without *being* one — the pattern is
        # start-anchored precisely so a sentence mentioning TBD survives.
        "the TBD facility name will be announced",
        "the placeholder text was replaced with a real quote",
        "ND",  # is_blank must not eat North Dakota before norm_state sees it
        "x",
        "Xcel Energy",
    ],
)
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


def test_every_one_decimal_amount_is_stored_exactly():
    """Scaled as a float and truncated, 4.1 billion was stored as $4,099,999,999 —
    32 of the 1,998 one-decimal amounts from 0.1 to 99.9 million and billion."""
    wrong = [
        f"{n / 10:.1f} {unit}"
        for unit, scale in (("million", 100_000), ("billion", 100_000_000))
        for n in range(1, 1000)
        if norm_money(f"{n / 10:.1f} {unit}") != n * scale
    ]
    assert wrong == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4.1 billion", 4_100_000_000),
        ("$4.1B", 4_100_000_000),
        ("$1.15 billion", 1_150_000_000),
        ("2.675 million", 2_675_000),
        (4.1e9, 4_100_000_000),
    ],
)
def test_money_is_exact_rather_than_truncated(raw, expected):
    assert norm_money(raw) == expected


@pytest.mark.parametrize("raw", [float("nan"), float("inf")])
def test_a_number_that_is_not_an_amount_is_refused(raw):
    """`json.loads` hands these over from a reply that says NaN or Infinity."""
    with pytest.raises(NormalizationError):
        norm_money(raw)


def test_a_money_range_keeps_an_exact_lower_bound():
    assert int(norm_money_detail("4.1-4.5 billion").value) == 4_100_000_000


def test_every_one_decimal_capacity_is_stored_exactly():
    """16.1 GW became 16,100.000000000002 MW by the same float scaling."""
    wrong = [f"{n / 10:.1f} GW" for n in range(1, 1000) if norm_mw(f"{n / 10:.1f} GW") != n * 100]
    assert wrong == []


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
    ("raw", "expected", "quarter"),
    [
        ("early 2028", dt.date(2028, 1, 1), 1),
        ("beginning of 2027", dt.date(2027, 1, 1), 1),
        ("spring 2027", dt.date(2027, 4, 1), 2),
        ("mid-2026", dt.date(2026, 7, 1), 3),
        ("mid 2026", dt.date(2026, 7, 1), 3),
        ("summer 2026", dt.date(2026, 7, 1), 3),
        ("late 2027", dt.date(2027, 10, 1), 4),
        ("end of 2026", dt.date(2026, 10, 1), 4),
        ("fall 2027", dt.date(2027, 10, 1), 4),
        ("autumn 2027", dt.date(2027, 10, 1), 4),
    ],
)
def test_a_hedge_with_a_year_resolves_to_that_quarter(raw, expected, quarter):
    """Announcements hedge constantly; discarding every hedge lost most dates.

    A qualifier plus a year is genuinely informative, so it is kept at quarter
    precision with a note recording the coarsening.
    """
    parsed = norm_date_detail(raw, field="expected_online")
    assert parsed.value == expected
    assert parsed.precision == "quarter"
    assert f"Q{quarter}" in parsed.note
    assert "approximate" in parsed.note


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("H1 2027", dt.date(2027, 1, 1)),
        ("H2 2027", dt.date(2027, 7, 1)),
        ("first half of 2027", dt.date(2027, 1, 1)),
        ("second half of 2026", dt.date(2026, 7, 1)),
    ],
)
def test_halves_keep_their_own_precision(raw, expected):
    """A half is a coarser bucket than a quarter -- H1 spans Jan-Jun, not Jan-Mar."""
    parsed = norm_date_detail(raw, field="expected_online")
    assert parsed.value == expected
    assert parsed.precision == "half"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("by 2028", dt.date(2028, 1, 1)),
        ("around 2027", dt.date(2027, 1, 1)),
        ("circa 2027", dt.date(2027, 1, 1)),
        ("approximately 2029", dt.date(2029, 1, 1)),
        ("sometime in 2028", dt.date(2028, 1, 1)),
    ],
)
def test_a_hedge_with_only_a_year_resolves_to_year_precision(raw, expected):
    """No more precision than a bare "2028" already gets."""
    parsed = norm_date_detail(raw, field="expected_online")
    assert parsed.value == expected
    assert parsed.precision == "year"
    assert "approximate" in parsed.note


@pytest.mark.parametrize(
    "raw",
    ["next year", "next spring", "soon", "late", "mid", "imminent", "eventually", "H2"],
)
def test_a_hedge_with_no_year_stays_none(raw):
    """With nothing to anchor to, any date would be invented outright."""
    parsed = norm_date_detail(raw)
    assert parsed.value is None
    assert parsed.note is not None and "no year to anchor it" in parsed.note


@pytest.mark.parametrize("raw", ["before 2028", "after 2027"])
def test_directional_hedges_stay_none(raw):
    """ "before 2028" points away from 2028, so storing 2028 would mislead."""
    assert norm_date_detail(raw).value is None


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
        # A terminal state wins over a progression phase in the same phrase. Scanning
        # the longest synonym first read every one of these as an advancing project,
        # and `capex` went on counting a dead one.
        ("construction paused", "paused"),
        ("construction halted", "paused"),
        ("construction on hold", "paused"),
        ("operational but suspended", "paused"),
        ("cancelled after construction began", "cancelled"),
        ("permitting withdrawn", "cancelled"),
        ("project cancellation", "cancelled"),
        # Longest-first, pinned where the two candidates *disagree*: nothing else in
        # this table does, so the invariant its old comment defended was untested.
        # "pre-construction" is `announced` and contains `construction`.
        ("pre-construction work begins in May", "announced"),
        ("currently under construction", "construction"),
        # Word boundaries: "deadline" is not `dead`.
        ("construction deadline", "construction"),
        ("plans submitted to the county", "announced"),
        ("commercial operations", "operational"),
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


@pytest.mark.parametrize(
    "raw",
    ["inactive", "installed capacity of 200 MW", "delivery expected in 2027", "decommissioned"],
)
def test_norm_phase_refuses_a_synonym_buried_in_an_unrelated_word(raw):
    """Four misreads the word-boundary match retires.

    Each of these resolved to a phase off a synonym inside an unrelated word:
    `active` in "inactive", `stalled` in "installed", `live` in "delivery",
    `commissioned` in "decommissioned" — and two landed on `operational`, the top of
    the ladder. Refusing is the right answer, and every caller handles it: `crawl`
    wraps this in `soft`, `pjm` notes that the status did not map and omits the field,
    `manual` rejects the record.
    """
    with pytest.raises(NormalizationError):
        norm_phase(raw)


def test_every_phase_synonym_still_matches_inside_a_sentence():
    r"""The boundary must not silently strand a key.

    `_PHASE_FALLBACK` wraps each synonym in `\b`, which is what stops `dead` matching
    "deadline" — and would quietly retire any key that only ever occurs as part of a
    longer word. Asserting the table is wholly reachable is what makes adding a
    synonym safe.
    """
    from tracker.normalize import _PHASE_SYNONYMS

    for key, phase in _PHASE_SYNONYMS.items():
        assert norm_phase(f"the project is {key} as of today") == phase, key


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


# --- One article, one spelling ----------------------------------------------------
#
# Measured on a copy of production: 19 projects cited the same article twice under
# two spellings of its URL. Nine differed only in Google's `srsltid` click-tracking
# parameter, four in a trailing slash, three in `www.`, one in the scheme — and two
# in an `?p=` that may genuinely select a different page, which is why a query
# string in general is left alone.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://www.researchandmarkets.com/reports/5527026/x?srsltid=AfmBOoo1s9YACrnh",
            "https://www.researchandmarkets.com/reports/5527026/x",
        ),
        (
            "https://datacentertracker.org/?utm_source=Poynter&utm_medium=email&mc_cid=f28&mc_eid=db3",
            "https://datacentertracker.org/",
        ),
        ("https://a.test/story?id=7&utm_campaign=x&fbclid=abc", "https://a.test/story?id=7"),
        ("HTTPS://News.Example.COM:443/Story", "https://news.example.com/Story"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://a.test/story?", "https://a.test/story"),
        # Left exactly as they are: a fragment keeps an ISO queue row unique, and a
        # real query parameter can select a different page.
        ("https://www.pjm.com/queues.aspx#AG1-234", "https://www.pjm.com/queues.aspx#AG1-234"),
        (
            "https://www.aboutamazon.com/news/x?p=amazons-virginia",
            "https://www.aboutamazon.com/news/x?p=amazons-virginia",
        ),
        ("https://example.com/Path/With/Case/", "https://example.com/Path/With/Case/"),
    ],
)
def test_canonical_url_drops_only_what_cannot_change_the_page(raw, expected):
    assert canonical_url(raw) == expected


def test_canonical_url_is_idempotent():
    for raw in (
        "https://www.x.test/a/?utm_source=y&b=1",
        "http://X.test:80",
        "https://x.test/#frag",
    ):
        once = canonical_url(raw)
        assert canonical_url(once) == once


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (
            "https://thetechcapital.com/legacy-investing-invesco-real-estate",
            "https://thetechcapital.com/legacy-investing-invesco-real-estate/",
        ),
        (
            "https://rcrwireless.com/20250514/ntt-data",
            "https://www.rcrwireless.com/20250514/ntt-data",
        ),
        (
            "http://fortisconstruction.com/news/metas-cheyenne-campus/",
            "https://fortisconstruction.com/news/metas-cheyenne-campus/",
        ),
        (
            "https://www.blackridgeresearch.com/blog/x?srsltid=AfmBOooGRk",
            "https://www.blackridgeresearch.com/blog/x?srsltid=AfmBOoqpjJ",
        ),
    ],
)
def test_spellings_of_one_article_share_an_identity(a, b):
    assert url_identity(a) == url_identity(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://www.pjm.com/queues.aspx#AG1-001", "https://www.pjm.com/queues.aspx#AG1-002"),
        ("https://x.test/news/x?p=1", "https://x.test/news/x?p=2"),
        ("https://x.test/a", "https://x.test/b"),
        ("https://x.test/", "https://y.test/"),
    ],
)
def test_different_pages_keep_different_identities(a, b):
    assert url_identity(a) != url_identity(b)


def test_url_variants_lists_the_spellings_a_stored_url_could_have():
    variants = url_variants("https://www.x.test/a/")
    assert "https://www.x.test/a/" in variants
    assert "http://x.test/a" in variants
    assert all(url_identity(v) == url_identity("https://x.test/a") for v in variants)


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
