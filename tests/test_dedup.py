"""Project identity: company normalization and the city/county granularity rule."""

from __future__ import annotations

import pytest

from tracker import dedup
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


# --- One campus, several parties ---------------------------------------------


def test_a_consortium_string_yields_every_party():
    assert dedup.company_parts("OpenAI/Oracle") == {"openai", "oracle"}
    assert dedup.company_parts("OpenAI, Oracle & SoftBank") == {"openai", "oracle", "softbank"}
    assert dedup.company_parts("Amazon Web Services, Inc.") == {"amazon"}


def test_the_abilene_case_is_recognised():
    """One campus stored four times, because four companies are attached to it.

    Crusoe builds it, Oracle leases it, OpenAI occupies it. Every dedup key was
    correct and the building was one, so grouping by end customer counted 1.2 GW
    four times.
    """
    rows = [
        ("Stargate Abilene", "Crusoe"),
        ("Stargate", "OpenAI/Oracle"),
        ("Stargate", "OpenAI"),
        ("Stargate - Abilene", "Oracle"),
    ]
    for i, (a_name, a_co) in enumerate(rows):
        for b_name, b_co in rows[i + 1 :]:
            assert dedup.looks_like_the_same_site(a_name, a_co, b_name, b_co, locality="Abilene"), (
                f"{a_co} vs {b_co}"
            )


def test_one_busy_locality_is_not_one_project():
    """Ashburn holds fourteen campuses from fourteen real operators.

    Locality alone must never imply a duplicate, or the densest markets in the
    country collapse into a single row.
    """
    rows = [
        ("Ashburn Campus", "Digital Realty"),
        ("North Ashburn Campus", "Equinix"),
        ("VA2 Data Center", "RagingWire Data Centers"),
        ("Shellhorn DC-1", "QTS Data Centers"),
        ("Dulles Berry", "Vizsla Ventures"),
    ]
    for i, (a_name, a_co) in enumerate(rows):
        for b_name, b_co in rows[i + 1 :]:
            assert not dedup.looks_like_the_same_site(
                a_name, a_co, b_name, b_co, locality="Ashburn"
            ), f"{a_co} vs {b_co}"


def test_the_locality_is_never_a_distinctive_token():
    """Otherwise every project in a town matches every other one."""
    assert dedup.distinctive_name_tokens("Ashburn Campus", locality="Ashburn") == frozenset()


def test_generic_industry_words_are_not_distinctive():
    assert dedup.distinctive_name_tokens("Data Center Campus Phase II") == frozenset()
