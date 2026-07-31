"""Per-field provenance: which tier a value rests on, and the words behind it.

The distinction these tests protect is the one the whole tier system exists for.
A value shown with a sentence beneath it makes a claim about *that* sentence
evidencing *that* value. Three ways it could lie, one test each:

* the quote is really the source's whole excerpt and mentions something else;
* the quote comes from a source whose value lost the merge;
* the value is 待确认 and a quote makes it look confirmed.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker.gaps import DEFAULTED, DERIVED, REPORTED, UNCONFIRMED, basis, provenance
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.models import Project
from tracker.upsert import upsert_record

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)

QUOTE_MW = "the campus is designed for 900 megawatts of critical load"
QUOTE_CITY = "Microsoft's Fairwater datacenter in Mount Pleasant, Wisconsin"


def build(session, sources, **project_overrides) -> Project:
    """Upsert one project from the given SourceRecords and return the row."""
    rec = IngestRecord(
        project={
            "company": "Microsoft",
            "name": "Fairwater",
            "city": "Mount Pleasant",
            "state": "WI",
            **project_overrides,
        },
        sources=sources,
    )
    result = upsert_record(session, rec)
    session.flush()
    return session.get(Project, result.project_id)


def source(url, *, claims, quotes=None, unconfirmed=(), **kwargs) -> SourceRecord:
    return SourceRecord(
        url=url,
        source_type=kwargs.pop("source_type", "trade_press"),
        fetched_at=kwargs.pop("fetched_at", T0),
        excerpt=kwargs.pop("excerpt", "An excerpt covering everything at once."),
        claims=claims,
        quotes=quotes or {},
        unconfirmed=frozenset(unconfirmed),
        **kwargs,
    )


# --- the quote itself -------------------------------------------------------


def test_exact_quote_is_returned_and_flagged_exact(session):
    project = build(
        session,
        [
            source(
                "https://example.com/a",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_planned": 900.0},
                quotes={"mw_planned": QUOTE_MW, "city": QUOTE_CITY},
            )
        ],
    )
    result = provenance(project, "mw_planned")
    assert result is not None
    assert result.tier == REPORTED
    assert result.quote == QUOTE_MW
    assert result.quote_is_exact is True


def test_excerpt_fallback_is_flagged_not_exact(session):
    """The 264 citations predating migration 0007 have no per-field quote.

    They must still show something — an operator reviewing a row wants the
    citation's words — but the page has to be able to say it is the excerpt.
    """
    project = build(
        session,
        [
            source(
                "https://example.com/a",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_planned": 900.0},
                excerpt="A paragraph about several different things.",
                quotes=None,
            )
        ],
    )
    result = provenance(project, "mw_planned")
    assert result.quote == "A paragraph about several different things."
    assert result.quote_is_exact is False


def test_a_field_with_no_recorded_quote_falls_back_even_when_others_have_one(session):
    """Per-field, not per-source: one quoted field must not vouch for its neighbours."""
    project = build(
        session,
        [
            source(
                "https://example.com/a",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_planned": 900.0},
                excerpt="Excerpt text.",
                quotes={"mw_planned": QUOTE_MW},
            )
        ],
    )
    assert provenance(project, "mw_planned").quote_is_exact is True
    assert provenance(project, "city").quote_is_exact is False
    assert provenance(project, "city").quote == "Excerpt text."


# --- whose quote ------------------------------------------------------------


def test_quote_comes_from_the_source_whose_value_won(session):
    """Two sources, two capacities, one row. The quote must match the row.

    `mw_planned` is PREFER_WEIGHT, so the company filing's 1200 wins over the
    trade-press 900. Quoting the loser would print a sentence stating a number the
    project does not have — the exact failure this function exists to prevent, and
    the reason it asks the write path which claim won rather than picking the
    strongest tier.
    """
    project = build(
        session,
        [
            source(
                "https://trade.example.com/a",
                source_type="trade_press",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_planned": 900.0},
                quotes={"mw_planned": "designed for 900 megawatts"},
            ),
            source(
                "https://news.microsoft.com/b",
                source_type="company_filing",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_planned": 1200.0},
                quotes={"mw_planned": "1,200 megawatts at full build"},
            ),
        ],
    )
    assert project.mw_planned == 1200.0
    result = provenance(project, "mw_planned")
    assert result.quote == "1,200 megawatts at full build"
    assert result.source_url == "https://news.microsoft.com/b"


def test_source_index_matches_the_export_ordering(session):
    """`source_index` has to index into the array the export actually emits."""
    from tracker.export import to_json_object

    project = build(
        session,
        [
            source(
                "https://zeta.example.com/a",
                source_type="company_filing",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_planned": 1200.0},
                quotes={"mw_planned": "1,200 megawatts"},
            ),
            source(
                "https://alpha.example.com/b",
                claims={"company": "Microsoft", "city": "Mount Pleasant"},
            ),
        ],
    )
    result = provenance(project, "mw_planned")
    emitted = to_json_object(project)
    assert emitted["sources"][result.source_index]["url"] == result.source_url


# --- tiers ------------------------------------------------------------------


def test_unconfirmed_value_carries_no_exact_quote(session):
    """待确认 means nothing quotable backs it. It must not acquire one."""
    project = build(
        session,
        [
            source(
                "https://example.com/a",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_built": 315.0},
                unconfirmed=["mw_built"],
                quotes=None,
            )
        ],
    )
    result = provenance(project, "mw_built")
    assert result.tier == UNCONFIRMED
    assert result.quote_is_exact is False


def test_a_quoted_source_beats_an_unconfirmed_one_for_the_same_field(session):
    project = build(
        session,
        [
            source(
                "https://guess.example.com/a",
                source_type="company_filing",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_built": 400.0},
                unconfirmed=["mw_built"],
            ),
            source(
                "https://quoted.example.com/b",
                source_type="trade_press",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_built": 315.0},
                quotes={"mw_built": "315 megawatts are live today"},
            ),
        ],
    )
    result = provenance(project, "mw_built")
    assert result.tier == REPORTED
    assert result.source_url == "https://quoted.example.com/b"


def test_derived_tier_survives(session):
    """A Census lookup is a real citation but it is not testimony."""
    project = build(
        session,
        [
            source(
                "https://www2.census.gov/geo/docs/place_by_county2020.txt",
                source_type="government_doc",
                extractor="derived:geo-census-v1",
                claims={
                    "company": "Microsoft",
                    "city": "Mount Pleasant",
                    "county": "Racine County",
                },
                quotes={"county": "Mount Pleasant village, WI -> Racine County"},
            )
        ],
    )
    assert provenance(project, "county").tier == DERIVED


def test_null_field_has_no_provenance(session):
    project = build(
        session,
        [
            source(
                "https://example.com/a", claims={"company": "Microsoft", "city": "Mount Pleasant"}
            )
        ],
    )
    assert project.investment_usd is None
    assert provenance(project, "investment_usd") is None
    assert basis(project, "investment_usd") is None


def test_basis_is_the_tier_half_of_provenance(session):
    """One definition of the tier ladder, not two that can drift."""
    project = build(
        session,
        [
            source(
                "https://example.com/a",
                claims={
                    "company": "Microsoft",
                    "city": "Mount Pleasant",
                    "mw_planned": 900.0,
                    "mw_built": 315.0,
                },
                unconfirmed=["mw_built"],
            )
        ],
    )
    for field in ("company", "city", "mw_planned", "mw_built", "investment_usd"):
        result = provenance(project, field)
        assert basis(project, field) == (result.tier if result else None)


def test_a_defaulted_phase_is_not_reported_as_unconfirmed(session):
    """Nobody said "announced". The NOT NULL column did.

    `phase` is NOT NULL with a server default, and ingest paths deliberately omit
    it from `source.fields` when no source states one — see `vocab.DEFAULT_PHASE`.
    Calling that 待确认 asserted a source had claimed it and failed to prove it,
    which was untrue of every project whose phase no article mentions.
    """
    project = build(
        session,
        [
            source(
                "https://example.com/a", claims={"company": "Microsoft", "city": "Mount Pleasant"}
            )
        ],
    )
    assert project.phase == "announced"
    result = provenance(project, "phase")
    assert result.tier == DEFAULTED
    assert result.source_url is None


def test_a_stated_phase_is_reported_not_defaulted(session):
    """Even when the stated value happens to equal the default."""
    project = build(
        session,
        [
            source(
                "https://example.com/a",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "phase": "announced"},
                quotes={"phase": "the project was announced this week"},
            )
        ],
    )
    result = provenance(project, "phase")
    assert result.tier == REPORTED
    assert result.quote == "the project was announced this week"


# --- persistence ------------------------------------------------------------


def test_quotes_round_trip_through_the_source_row(session):
    project = build(
        session,
        [
            source(
                "https://example.com/a",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_planned": 900.0},
                quotes={"mw_planned": QUOTE_MW},
            )
        ],
    )
    stored = project.sources[0]
    assert json.loads(stored.quotes) == {"mw_planned": QUOTE_MW}


def test_a_source_with_no_quotes_stores_null_not_an_empty_object(session):
    project = build(
        session,
        [
            source(
                "https://example.com/a", claims={"company": "Microsoft", "city": "Mount Pleasant"}
            )
        ],
    )
    assert project.sources[0].quotes is None


def test_re_ingesting_the_same_quotes_does_not_touch_the_row(session):
    """Byte-identical JSON on a re-run, like `claims`, or idempotence breaks."""
    sources = [
        source(
            "https://example.com/a",
            claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_planned": 900.0},
            quotes={"mw_planned": QUOTE_MW, "city": QUOTE_CITY},
        )
    ]
    project = build(session, sources)
    first = project.sources[0].quotes
    updated_at = project.updated_at

    build(session, sources)
    session.refresh(project)
    assert project.sources[0].quotes == first
    assert project.updated_at == updated_at


@pytest.mark.parametrize("bad", ["not json at all", "[1, 2, 3]"])
def test_unparseable_quotes_json_degrades_to_the_excerpt(session, bad):
    """A corrupt column must not take the page down with it."""
    project = build(
        session,
        [
            source(
                "https://example.com/a",
                claims={"company": "Microsoft", "city": "Mount Pleasant", "mw_planned": 900.0},
                excerpt="Excerpt text.",
            )
        ],
    )
    project.sources[0].quotes = bad
    session.flush()
    result = provenance(project, "mw_planned")
    assert result.quote == "Excerpt text."
    assert result.quote_is_exact is False
