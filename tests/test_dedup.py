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


def test_the_plural_of_a_generic_word_is_still_generic():
    """`Aligned Data Centers Phoenix` and `NTT Global Data Centers Americas Phoenix`
    were reported as one campus on the shared token "centers" — two unrelated
    operators, one city, and a word that appears in a third of the names in the
    industry. The list held the singular and not the plural."""
    assert not dedup.looks_like_the_same_site(
        "Aligned Data Centers Phoenix",
        "Aligned Data Centers",
        "NTT Global Data Centers Americas Phoenix",
        "NTT Global Data Centers Americas",
        locality="Phoenix",
    )


def test_a_renamed_operator_folds_to_one_key():
    """Iris Energy became IREN in 2024 and both names are still in circulation, so
    the Childress campus was stored twice."""
    assert dedup.company_key("Iris Energy") == dedup.company_key("IREN Limited") == "iren"
    assert dedup.shares_a_party("IREN Limited", "Iris Energy")


# --- the four judgements the duplicates report rests on ----------------------
#
# Every case below is a pair that was measured on the live database, not an
# invented one: these are the strings that produced a wrong answer.


def test_the_same_company_and_the_same_name_is_identity():
    """Six suspected pairs held both, and all six were reported as the weakest class.

    `distinctive_name_tokens` strips generic industry words and the locality, so
    "Stafford Technology Campus" in Stafford reduces to nothing and two identical
    names produced no name evidence at all.
    """
    assert dedup.exact_identity(
        "Stafford Technology Campus",
        "STACK Infrastructure",
        "Stafford Technology Campus",
        "STACK Infrastructure",
    )
    # The alias table is part of it: one row says the long name, the other the short.
    assert dedup.exact_identity(
        "Atlanta-Douglasville", "Flexential", "atlanta douglasville", "Flexential"
    )
    assert (
        dedup.distinctive_name_tokens("Stafford Technology Campus", locality="Stafford")
        == frozenset()
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Lithia Springs Campus", "Lithia Springs Campus II"),  # one has no ordinal
        ("VA2 Data Center", "VA-2 Data Center"),  # same ordinal
        ("Hyperion", "Hyperion Data Center"),
        (None, "Polaris Forge 1"),
    ],
)
def test_a_number_on_one_side_is_not_a_sibling(a, b):
    assert not dedup.sibling_ordinals(a, b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Polaris Forge 1", "Polaris Forge 2"),
        ("SLC02", "SLC-04"),
        ("ATL 3", "ATL 5"),
    ],
)
def test_neighbouring_phases_are_siblings_not_duplicates(a, b):
    """Applied Digital's Polaris Forge 1 (Ellendale) and 2 (Harwood) are two real
    campuses that share the tranche key `forge-2.polaris`, because one article listed
    both. Once a shared tranche can carry a merge across localities, one digit is all
    that stands between them and being folded together."""
    assert dedup.sibling_ordinals(a, b)


@pytest.mark.parametrize(
    "key",
    [
        "capacity-1",
        "existing",
        "a-1.building",
        "planned",
        "total.capacity",
    ],
)
def test_a_key_made_of_type_words_names_a_kind_of_tranche(key):
    assert dedup.is_vocabulary_block_key(key)


@pytest.mark.parametrize(
    "key", ["stingray", "horizon-1", "sweetwater-1", "county.shackelford", "dfw-3.expansion"]
)
def test_a_key_that_names_a_building_survives(key):
    assert not dedup.is_vocabulary_block_key(key)


def test_the_locality_is_never_a_tranche_either():
    """`austin` is a tranche label on Switch's Austin campus and on Sabey's in Round
    Rock — one metro, two buildings. The rule `distinctive_name_tokens` applies to
    names, applied one level down."""
    assert dedup.is_vocabulary_block_key("austin", localities={"Austin"})
    assert not dedup.is_vocabulary_block_key("austin", localities={"Round Rock"})


@pytest.mark.parametrize("key", ["iad-3", "IAD3", "va-2", "ord 1", "ph-1", "acc-9"])
def test_a_facility_number_is_recognised(key):
    """Whether it is identity depends on where the rows are, which is why it is not
    folded into `is_vocabulary_block_key`: inside one market the code is the market
    and the number is the building."""
    assert dedup.is_facility_number(key)


@pytest.mark.parametrize("key", ["stingray", "horizon-1", "douglasville-2", "forge-2.polaris"])
def test_a_named_building_is_not_a_facility_number(key):
    assert not dedup.is_facility_number(key)


def test_two_spellings_of_one_company_share_no_party():
    """The distinction that keeps `party` worth trusting for a merge.

    `capex`'s second pass buckets rows *by* company, so `shares_a_party` is true of
    every pair it produces and means nothing there. Recording it would have offered
    to fold NTT's Itasca campus into NTT's Chicago one, 31.7 km away.
    """
    assert dedup.shares_a_party(
        "NTT Global Data Centers Americas", "NTT Global Data Centers Americas"
    )
    assert not dedup.shared_parties_across_companies(
        "NTT Global Data Centers Americas", "NTT Global Data Centers Americas"
    )
    # What the signal was built for survives.
    assert dedup.shared_parties_across_companies("OpenAI/Oracle", "Oracle") == {"oracle"}


@pytest.mark.parametrize(
    ("key", "localities"),
    [
        ("iad-3", set()),  # the airport form needs no locality to be recognised
        ("hillsboro-1", {"Hillsboro"}),
        ("chicago-2", {"Chicago"}),
        ("sweetwater-1", {"Sweetwater"}),
        ("douglasville-2", {"Douglasville"}),
    ],
)
def test_a_market_and_a_number_is_not_a_building(key, localities):
    """Two spellings of one thing: the airport code and the town's own name.

    Kept as evidence and refused as merge authority, which is the only split that
    works — `sweetwater-1` is the whole of what connects IREN's Sweetwater campus to
    the copy stored under its old name, and `hillsboro-1` is held by Flexential's
    Hillsboro site and NTT's.
    """
    assert dedup.is_market_sequence(key, localities=localities)
    assert not dedup.is_vocabulary_block_key(key, localities=localities)


@pytest.mark.parametrize(
    ("key", "localities"),
    [
        ("expansion.houston", {"Houston"}),
        ("expansion.portland", {"Portland"}),
        ("expansion.hillsboro", {"Hillsboro"}),
    ],
)
def test_a_type_word_and_a_town_names_nothing(key, localities):
    """ "An expansion, in this town" is true of every expansion in that town.

    `expansion.houston` paired Element Critical's Houston One with Switch's Houston
    campus; `expansion.portland` paired STACK's Portland site with its Hillsboro one.
    """
    assert dedup.is_vocabulary_block_key(key, localities=localities)


def test_a_tranche_named_after_a_county_the_campus_reaches_into_survives():
    """The flagship case, and it nearly died to the word "County".

    `county.shackelford` ties Stargate's Abilene row to its Shackelford County one.
    Building place words straight from the county field made "county" a place word,
    which reduced the whole key to vocabulary.
    """
    assert not dedup.is_vocabulary_block_key(
        "county.shackelford", localities={"Shackelford County"}
    )


def test_the_operators_own_name_is_not_a_distinctive_site_token():
    """Every STACK project is called "STACK something", so the word says which
    company and not which building."""
    firms = "STACK Infrastructure"
    assert "stack" in dedup.distinctive_name_tokens("STACK Portland Expansion")
    assert "stack" not in dedup.distinctive_name_tokens("STACK Portland Expansion", company=firms)
    # And what actually names a site still survives.
    assert dedup.distinctive_name_tokens("STACK Kincora Campus", company=firms) == frozenset(
        {"kincora"}
    )
