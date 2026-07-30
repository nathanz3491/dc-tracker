"""Confidence scoring rules, each tested in isolation.

No database: `SourceView` is a plain value object precisely so these rules can
be pinned down without one.
"""

from __future__ import annotations

import pytest

from tracker.confidence import (
    MIN_FIELDS_FOR_HIGH_CONFIDENCE,
    SOURCE_WEIGHTS,
    SourceView,
    cited_fields,
    compute,
    find_agreements,
    find_conflicts,
    independent_domains,
    needs_review,
    registrable_domain,
)
from tracker.vocab import SOURCE_TYPES

#: A field list long enough to clear the coverage penalty, so tests that are
#: about source weighting are not silently capped by it.
RICH = "name,company,city,state,mw_planned,phase,investment_usd"


def src(source_type: str, url: str, *, fields: str = RICH, **claims) -> SourceView:
    return SourceView(source_type=source_type, url=url, fields=fields, claims=claims)


# --- Domain reduction -------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.datacenterdynamics.com/en/news/x/", "datacenterdynamics.com"),
        ("https://datacenterdynamics.com/a", "datacenterdynamics.com"),
        ("http://news.microsoft.com/source/features/x/", "microsoft.com"),
        ("https://ir.example.co.uk/a/b", "example.co.uk"),
        ("https://www.bbc.co.uk/news", "bbc.co.uk"),
        ("https://apps.dnr.wi.gov/permit", "wi.gov"),
        ("https://example.com", "example.com"),
        ("https://example.com:8443/x", "example.com"),
        ("", ""),
    ],
)
def test_registrable_domain(url, expected):
    assert registrable_domain(url) == expected


def test_same_outlet_many_articles_is_one_source():
    """Aggregators recycle each other; counting rows would inflate confidence."""
    sources = [
        src("trade_press", "https://www.datacenterdynamics.com/a"),
        src("trade_press", "https://datacenterdynamics.com/b"),
        src("trade_press", "https://www.datacenterdynamics.com/c"),
    ]
    assert independent_domains(sources) == {"datacenterdynamics.com"}
    assert compute(sources).value == 2, "three articles from one outlet must not beat two outlets"


# --- Floor and ceiling ------------------------------------------------------


def test_a_placeholder_url_is_not_a_citation():
    """Observed live: a real project reached confidence 3 because a placeholder
    seed row supplied the "strongest source". A URL that does not exist cannot
    earn trust, however authoritative the source_type claims to be."""
    fake = src("company_filing", "https://news.microsoft.com/PLACEHOLDER-replace-me/")
    score = compute([fake])
    assert score.value == 0
    assert any("placeholder" in r for r in score.reasons)


def test_a_placeholder_does_not_inflate_a_real_citation():
    real = src("general_media", "https://racinecountyeye.com/a/")
    fake = src("company_filing", "https://news.microsoft.com/PLACEHOLDER-replace-me/")
    alone = compute([real])
    with_fake = compute([real, fake])
    assert with_fake.value == alone.value, "the placeholder must add nothing"
    assert "1 independent" not in " ".join(with_fake.reasons)


def test_no_sources_is_zero():
    assert compute([]).value == 0


@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_any_citation_floors_at_one(source_type):
    """PRD definition of done: never 0 when we hold a credible URL."""
    score = compute([src(source_type, "https://example.com/a", fields="name")])
    assert score.value >= 1


def test_score_never_exceeds_three():
    sources = [
        src("company_filing", "https://news.microsoft.com/a", mw_planned=900),
        src("government_doc", "https://dnr.wi.gov/b", mw_planned=900),
        src("trade_press", "https://www.datacenterfrontier.com/c", mw_planned=900),
    ]
    assert compute(sources).value == 3


# --- Source-type weighting --------------------------------------------------


def test_a_single_source_never_reaches_three():
    """3 means corroborated or human-checked. One press release is neither."""
    for source_type in ("company_filing", "government_doc", "trade_press", "manual"):
        score = compute([src(source_type, "https://news.microsoft.com/a")])
        assert score.value <= 2, f"{source_type} alone must not reach 3"


def test_single_authoritative_source_lands_at_two():
    score = compute([src("company_filing", "https://news.microsoft.com/a")])
    assert score.value == 2
    assert any("single source" in r for r in score.reasons)


def test_official_single_source_beats_media_single_source():
    official = compute([src("company_filing", "https://news.microsoft.com/a")])
    media = compute([src("general_media", "https://www.example-news.com/a")])
    assert official.value > media.value


def test_iso_queue_alone_caps_at_one():
    """The public ISO queues are generator queues with no data-center column, so
    a match there is a keyword guess, not an authoritative fact."""
    assert SOURCE_WEIGHTS["iso_queue"] == 1
    score = compute([src("iso_queue", "https://www.pjm.com/planning/queues.aspx#AG1-234")])
    assert score.value == 1
    assert needs_review(score.value) is True


def test_manual_seed_lands_at_two_not_three():
    """PRD open question Q1: our own curation is a claim, not authority."""
    score = compute([src("manual", "https://news.microsoft.com/a")])
    assert score.value == 2


# --- Independence and agreement --------------------------------------------


def test_two_independent_domains_raise_the_score():
    one = compute([src("trade_press", "https://www.datacenterdynamics.com/a")])
    two = compute(
        [
            src("trade_press", "https://www.datacenterdynamics.com/a"),
            src("trade_press", "https://www.datacenterfrontier.com/b"),
        ]
    )
    assert two.value > one.value


def test_agreement_requires_independent_domains():
    """Two articles from one outlet asserting the same number is not corroboration."""
    same_outlet = [
        src("trade_press", "https://www.dcd.com/a", mw_planned=900),
        src("trade_press", "https://www.dcd.com/b", mw_planned=900),
    ]
    assert find_agreements(same_outlet) == set()

    two_outlets = [
        src("trade_press", "https://www.dcd.com/a", mw_planned=900),
        src("trade_press", "https://www.dcf.com/b", mw_planned=900),
    ]
    assert "mw_planned" in find_agreements(two_outlets)


def test_near_identical_numbers_agree_rather_than_conflict():
    """ "900 MW" and "1,000 MW" are one story told twice, within tolerance."""
    sources = [
        src("trade_press", "https://www.dcd.com/a", mw_planned=900),
        src("trade_press", "https://www.dcf.com/b", mw_planned=1000),
    ]
    assert find_conflicts(sources) == {}
    assert "mw_planned" in find_agreements(sources)


# --- Conflicts --------------------------------------------------------------


def test_material_numeric_disagreement_is_a_conflict():
    sources = [
        src("iso_queue", "https://www.pjm.com/q#1", mw_planned=300),
        src("trade_press", "https://www.dcd.com/a", mw_planned=450),
    ]
    conflicts = find_conflicts(sources)
    assert "mw_planned" in conflicts
    assert sorted(conflicts["mw_planned"]) == [300, 450]


def test_conflict_penalizes_the_score():
    agreeing = [
        src("trade_press", "https://www.dcd.com/a", mw_planned=900),
        src("trade_press", "https://www.dcf.com/b", mw_planned=900),
    ]
    conflicting = [
        src("trade_press", "https://www.dcd.com/a", mw_planned=300),
        src("trade_press", "https://www.dcf.com/b", mw_planned=900),
    ]
    assert compute(conflicting).value < compute(agreeing).value
    assert any("conflict" in r for r in compute(conflicting).reasons)


def test_string_fields_conflict_case_insensitively():
    same = [
        src("trade_press", "https://www.dcd.com/a", phase="construction"),
        src("trade_press", "https://www.dcf.com/b", phase="Construction"),
    ]
    assert find_conflicts(same) == {}

    different = [
        src("trade_press", "https://www.dcd.com/a", phase="construction"),
        src("trade_press", "https://www.dcf.com/b", phase="cancelled"),
    ]
    assert "phase" in find_conflicts(different)


def test_a_single_claim_cannot_conflict_with_itself():
    assert find_conflicts([src("trade_press", "https://a.com/x", mw_planned=900)]) == {}


# --- Coverage penalty -------------------------------------------------------


def test_sparse_project_is_capped_regardless_of_source_quality():
    """An official source that says almost nothing is still almost nothing."""
    score = compute([src("company_filing", "https://news.microsoft.com/a", fields="name,company")])
    assert score.value == 1
    assert any("tracked fields cited" in r for r in score.reasons)


def test_coverage_uses_populated_count_when_given():
    sources = [src("company_filing", "https://news.microsoft.com/a", fields="name")]
    assert compute(sources, populated_tracked_fields=MIN_FIELDS_FOR_HIGH_CONFIDENCE).value >= 2


def test_cited_fields_unions_across_sources():
    sources = [
        src("iso_queue", "https://www.pjm.com/q#1", fields="state,phase"),
        src("trade_press", "https://www.dcd.com/a", fields="mw_planned, phase"),
    ]
    assert cited_fields(sources) == {"state", "phase", "mw_planned"}


def test_uncited_fields_do_not_earn_confidence():
    """Default coverage counts CITED fields, not whatever happens to be non-null."""
    sources = [src("company_filing", "https://news.microsoft.com/a", fields=None)]
    assert compute(sources).value == 1


# --- Operator verification --------------------------------------------------


def test_operator_verification_reaches_three_from_a_weak_source():
    """A human checking the row is the only path to 3 from one weak citation."""
    weak = [src("general_media", "https://www.example-news.com/a", fields="name")]
    assert compute(weak).value == 1
    assert compute(weak, operator_verified=True).value == 3


# --- Review threshold -------------------------------------------------------


@pytest.mark.parametrize(("score", "expected"), [(0, True), (1, True), (2, False), (3, False)])
def test_needs_review_threshold(score, expected):
    """PRD open question Q3: < 2 needs review, >= 2 is auto-approved."""
    assert needs_review(score) is expected


# --- Claims parsing ---------------------------------------------------------


class _Row:
    def __init__(self, source_type, url, fields=None, claims=None, id=1):
        self.source_type, self.url, self.fields, self.claims, self.id = (
            source_type,
            url,
            fields,
            claims,
            id,
        )


def test_source_view_parses_claims_json():
    view = SourceView.from_row(
        _Row("trade_press", "https://a.com/x", "mw_planned", '{"mw_planned": 900}')
    )
    assert view.claims == {"mw_planned": 900}


def test_source_view_survives_malformed_claims_json(caplog):
    """A bad claims blob must not crash scoring for the whole project."""
    view = SourceView.from_row(_Row("trade_press", "https://a.com/x", None, "{not json"))
    assert view.claims == {}
    assert "unparseable claims" in caplog.text


def test_reasons_are_populated_for_explainability():
    """`tracker review` shows the operator WHY a row scored as it did."""
    score = compute([src("company_filing", "https://news.microsoft.com/a")])
    assert score.reasons
    assert any("company_filing" in r for r in score.reasons)
