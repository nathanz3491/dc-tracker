"""Quoted money that is not spending on the site: an extrapolation, a contract's value.

On 2026-10-21 Riot Corsicana's "extrapolated total capital expenditure ... potentially
ranging from $8.3 billion" replaced a quoted $333M, and IREN Childress took "IREN's $9.7
billion AI cloud agreement with Microsoft" as its investment. Both are kept, marked
待确认 with their reason, and left out of Capex.
"""

from __future__ import annotations

import json

import pytest

from tracker.ingest.crawl import money_kind


@pytest.mark.parametrize(
    ("quote", "value", "expected"),
    [
        (
            "an extrapolated total capital expenditure for the Corsicana AI lab potentially "
            "ranging from $8.3 billion to $9.1 billion",
            8.3e9,
            "speculative",
        ),
        (
            "In Q1 2027, we project it to host 546k H100s at a capital cost of $8.1 B.",
            8.1e9,
            "speculative",
        ),
        (
            "IREN's $9.7 billion AI cloud agreement with Microsoft faces delays",
            9.7e9,
            "contract_value",
        ),
        ("signed a cloud contract worth $14 billion with Meta", 14e9, "contract_value"),
        # The hedge is on another figure than the one claimed.
        ("$400M+ minimum committed, potentially closer to $1B", 4e8, None),
        # Ordinary planned budgets, however they are hedged.
        ("Total investment in the project could reach up to $900 million.", 9e8, None),
        ("At 11 gigawatts, Fermi estimates its costs could top $60 billion.", 6e10, None),
        ("The roughly 700-acre project, estimated at $14 billion, cleared council", 1.4e10, None),
        # Money for the site under an agreement is still money for the site.
        ("signed a $10 billion investment agreement with the state", 1e10, None),
        ("a $2B campus that could potentially bring 500 jobs", 2e9, None),
    ],
)
def test_money_that_is_not_spending_is_named(quote, value, expected):
    assert money_kind(quote, value) == expected


def test_the_nightly_regate_marks_a_stored_claim_and_takes_its_own_mark_back(session):
    from tracker import capex
    from tracker.backfill import regate_scope
    from tracker.models import Project, Source

    project = Project(
        name="Childress",
        company="IREN",
        city="Childress",
        state="TX",
        dedup_key="iren|city:childress|TX",
        phase="construction",
        confidence=1,
        investment_usd=9_700_000_000,
    )
    session.add(project)
    session.flush()
    quote = "IREN's $9.7 billion AI cloud agreement with Microsoft faces potential delivery delays"
    source = Source(
        project_id=project.id,
        url="https://example.test/iren",
        source_type="trade_press",
        claims=json.dumps({"investment_usd": 9_700_000_000}),
        quotes=json.dumps({"investment_usd": quote}),
        fields="investment_usd",
    )
    session.add(source)
    session.flush()
    session.refresh(project)

    report = regate_scope(session, apply=True)
    assert report.money_marked == {"contract_value": 1}
    assert source.unconfirmed_fields == "investment_usd" and not source.fields
    assert json.loads(source.unconfirmed_reasons) == {"investment_usd": "contract_value"}
    assert project.id in capex.unconfirmed_investment_ids(session), "left out of Capex"
    assert regate_scope(session, apply=True).money_marked == {}, "idempotent"

    # A rule change that no longer reads it so restores exactly what it marked.
    source.quotes = json.dumps({"investment_usd": "IREN will invest $9.7 billion in Childress"})
    session.flush()
    assert regate_scope(session, apply=True).money_restored == 1
    assert source.fields == "investment_usd" and not source.unconfirmed_fields
