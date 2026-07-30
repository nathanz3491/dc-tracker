"""Project identity: company normalization and the city/county granularity rule."""

from __future__ import annotations

import pytest

from tracker.dedup import (
    city_key,
    company_key,
    county_key,
    dedup_key,
    is_cross_granularity_match,
    locality,
    looks_like_county,
    same_company_and_state,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Microsoft", "microsoft"),
        ("Microsoft Corporation", "microsoft"),
        ("Microsoft Corp.", "microsoft"),
        ("MICROSOFT CORP", "microsoft"),
        ("Amazon Web Services", "amazon"),
        ("Amazon Web Services, Inc.", "amazon"),
        ("AWS", "amazon"),
        ("Amazon Data Services", "amazon"),
        ("Meta Platforms, Inc.", "meta"),
        ("Facebook", "meta"),
        ("Alphabet Inc", "google"),
        ("xAI", "xai"),
        ("X.AI", "xai"),
        ("Crusoe Energy Systems LLC", "crusoe"),
        ("Vantage Data Centers", "vantage"),
        ("Foo Holdings LLC", "foo"),
        ("Bar Technologies Group Inc", "bar"),
        ("", ""),
        (None, ""),
    ],
)
def test_company_key(raw, expected):
    assert company_key(raw) == expected


def test_company_key_strips_stacked_suffixes():
    """ "Foo Properties Holdings, LLC" is the same operator as "Foo"."""
    assert company_key("Foo Properties Holdings, LLC") == "foo"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Racine County", "racine"),
        ("Racine", "racine"),
        ("Richland Parish", "richland"),
        ("Matanuska-Susitna Borough", "matanuska susitna"),
        ("Loudoun County", "loudoun"),
    ],
)
def test_county_key_strips_the_county_word(raw, expected):
    assert county_key(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Racine County", True),
        ("Richland Parish", True),
        ("Mount Pleasant", False),
        ("Memphis", False),
        ("", False),
        (None, False),
    ],
)
def test_looks_like_county(raw, expected):
    assert looks_like_county(raw) is expected


def test_city_key_normalizes_punctuation_and_accents():
    assert city_key("St. Joseph") == "st joseph"
    assert city_key("Coeur d'Alene") == "coeur d alene"


# --- The granularity rule ---------------------------------------------------


def test_locality_prefers_city_over_county():
    loc = locality("Mount Pleasant", "Racine County")
    assert (loc.kind, loc.key) == ("city", "mount pleasant")
    assert loc.is_precise is True


def test_locality_falls_back_to_county():
    loc = locality(None, "Racine County")
    assert (loc.kind, loc.key) == ("county", "racine")
    assert loc.is_precise is False


def test_a_county_written_into_the_city_field_is_still_a_county():
    """An ISO row whose county landed in `city` must not pose as a municipality."""
    loc = locality("Racine County", None)
    assert (loc.kind, loc.key) == ("county", "racine")


def test_dedup_key_shape():
    assert dedup_key("Microsoft Corp", "Mount Pleasant", None, "wi") == (
        "microsoft|city:mount pleasant|WI"
    )
    assert dedup_key("Microsoft", None, "Racine County", "WI") == "microsoft|county:racine|WI"


def test_county_and_city_rows_get_different_keys():
    """The PRD's High risk case. Different keys means the UNIQUE index cannot
    merge them, so the ambiguity surfaces instead of corrupting data."""
    city = dedup_key("Microsoft", "Mount Pleasant", None, "WI")
    county = dedup_key("Microsoft", None, "Racine County", "WI")
    assert city != county


def test_same_project_spelled_differently_gets_one_key():
    a = dedup_key("Microsoft Corporation", "Mount Pleasant", None, "WI")
    b = dedup_key("MICROSOFT", "mount pleasant", None, "wi")
    assert a == b


def test_is_cross_granularity_match_identifies_the_review_case():
    city = dedup_key("Microsoft", "Racine", None, "WI")
    county = dedup_key("Microsoft", None, "Racine County", "WI")
    assert is_cross_granularity_match(city, county) is True
    assert is_cross_granularity_match(county, city) is True


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Same key is not a "match" to propose -- it is the same row.
        (
            dedup_key("Microsoft", "Racine", None, "WI"),
            dedup_key("Microsoft", "Racine", None, "WI"),
        ),
        # Different company.
        (
            dedup_key("Microsoft", None, "Racine County", "WI"),
            dedup_key("Google", "Racine", None, "WI"),
        ),
        # Different state.
        (
            dedup_key("Microsoft", None, "Racine County", "WI"),
            dedup_key("Microsoft", "Racine", None, "TX"),
        ),
        # Same granularity, different locality.
        (
            dedup_key("Microsoft", "Racine", None, "WI"),
            dedup_key("Microsoft", "Madison", None, "WI"),
        ),
        # Cross-granularity but different locality names.
        (
            dedup_key("Microsoft", None, "Dane County", "WI"),
            dedup_key("Microsoft", "Racine", None, "WI"),
        ),
    ],
)
def test_is_cross_granularity_match_rejects_non_candidates(a, b):
    assert is_cross_granularity_match(a, b) is False


def test_same_company_and_state_is_a_weaker_signal():
    a = dedup_key("Google", "Council Bluffs", None, "IA")
    b = dedup_key("Google", "Cedar Rapids", None, "IA")
    assert same_company_and_state(a, b) is True
    assert is_cross_granularity_match(a, b) is False, "different cities are not a granularity issue"
