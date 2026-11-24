"""Quoted money that is not spending on the site, and never the campus's investment.

On 2026-10-21 Riot Corsicana's "extrapolated total capital expenditure ... potentially
ranging from $8.3 billion" replaced a quoted $333M, and IREN Childress took "IREN's $9.7
billion AI cloud agreement with Microsoft" as its investment. On 2026-11-11 the stored
figures also held land and building prices, financing rounds and other people's
estimates. Each is kept as evidence with its reason, and left out of the merge.
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
            "In Q1 2027, we project it to host an estimated 546k H100-equivalents of AI compute, "
            "supported by 214 MW of IT power at a capital cost of $8.1 B.",
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
        # Somebody else's estimate, or a figure the sentence only assumes.
        (
            "Epoch AI estimates Microsoft Fairwater Atlanta, with 636 MW of IT power, at a "
            "capital cost of $24.1 billion.",
            24.1e9,
            "speculative",
        ),
        (
            "A commissioned EY model uses $69.2 billion in phase-one development spending.",
            69.2e9,
            "speculative",
        ),
        (
            "Industrial Info estimates that these campuses will require more than $20 billion.",
            20e9,
            "speculative",
        ),
        (
            "Local media has reported that total investment in the Buckeye data center sites "
            "could reach $20 billion.",
            20e9,
            "speculative",
        ),
        (
            "Assuming an $11B investment, annual new county revenues could exceed $25M.",
            11e9,
            "speculative",
        ),
        (
            "real estate people traded a rumor about a $350 million waterfront investment",
            3.5e8,
            "speculative",
        ),
        # What somebody paid.
        (
            "Microsoft recently acquired a 1,385-acre megasite for $26.8 million.",
            26.8e6,
            "purchase_price",
        ),
        (
            "In November 2025, Amazon paid $700 million for roughly 189 acres.",
            7e8,
            "purchase_price",
        ),
        (
            "the proposed $22 million acquisition consists of $500,000 in cash",
            22e6,
            "purchase_price",
        ),
        # A loan or a financing round.
        (
            "DataBank has secured $2.0B in construction financing to develop three data centers",
            2e9,
            "financing",
        ),
        (
            "it has secured over $300 million to be used for the development of its site",
            3e8,
            "financing",
        ),
        # Money around the site.
        (
            "the county approved $120 million in tax incentives for the project",
            1.2e8,
            "not_spending",
        ),
        ("an economic impact of $3.4 billion over ten years", 3.4e9, "not_spending"),
        # Building costs, however they are phrased: each was refused by an early draft.
        (
            "Building the center would cost $4.8 billion, of which $3.2 billion is equipment",
            4.8e9,
            None,
        ),
        ("site construction would cost $286 million", 2.86e8, None),
        ("Under $1M spent so far, of about $6.7B if built as planned", 6.7e9, None),
        ("secured a permit for a $3 million data center upgrade", 3e6, None),
        (
            "announced the second phase of a $15 billion joint venture to fund the campus",
            15e9,
            None,
        ),
        (
            "approve the sale of 500 acres to Huntley LLC for a $10 billion data center project",
            1e10,
            None,
        ),
        ("the county approved $5 billion in industrial revenue bonds for the campus", 5e9, None),
        ("The company estimated the project's cost at $1.2 billion in its filing", 1.2e9, None),
        # The purchase is another figure than the one claimed.
        (
            "acquired the land for $40 million and plans to invest $2 billion in the campus",
            2e9,
            None,
        ),
        # Every tense and the noun of a purchase, however the price follows it. On
        # 2026-11-24 eleven campuses' stored investment was one of these.
        (
            "Cologix has agreed to acquire vXchnge's CA-01 data center campus in Santa Clara, "
            "California for $58.1m, which it will rename as SV1.",
            58.1e6,
            "purchase_price",
        ),
        (
            "Digital Realty is buying a 40-acre suburban Chicago office campus for $81.5 million",
            81.5e6,
            "purchase_price",
        ),
        (
            "WhiteFiber has completed the acquisition of two industrial properties in Yadkin "
            "County, North Carolina, for $60 million in cash.",
            60e6,
            "purchase_price",
        ),
        (
            "MARA Holdings, Inc. today announced that it has entered into a definitive agreement "
            "to acquire Long Ridge Energy & Power LLC from FTAI Infrastructure Inc. (NASDAQ: FIP) "
            "for a total transaction value of approximately $1.5 billion.",
            1.5e9,
            "purchase_price",
        ),
        (
            "MARA has agreed to buy Long Ridge Energy & Power in a deal valued at about $1.5 "
            "billion.",
            1.5e9,
            "purchase_price",
        ),
        (
            'MARA will acquire Long Ridge Energy for "approximately $1.5 billion," including debt.',
            1.5e9,
            "purchase_price",
        ),
        (
            "Digital Realty Trust is set to pay $90 million for the 108-acre Hawthorne Race Course",
            9e7,
            "purchase_price",
        ),
        ("The unknown buyer that offered $90M for the 108-acre race course", 9e7, "purchase_price"),
        (
            "this $670M transaction added four data centers to DataBank's footprint",
            6.7e8,
            "purchase_price",
        ),
        ("The $90 million sale closed on 2 September 2026.", 9e7, "purchase_price"),
        (
            "The Brookfield subsidiary plunked down $97 million in cash for two data centers",
            97e6,
            "purchase_price",
        ),
        (
            "CoreSite acquired the buildings from private equity and investment firm GI Partners "
            "for $100 million",
            1e8,
            "purchase_price",
        ),
        # A figure written in words is money too.
        (
            "RagingWire acquired the building from SunPower in September 2019 for 63.5 million "
            "dollars.",
            63.5e6,
            "purchase_price",
        ),
        ("Analysts estimate the campus will cost USD 20bn.", 20e9, "speculative"),
        # Money still to be raised is borrowed.
        (
            "TeraWulf plans to raise $3.5 billion led by Morgan Stanley, including leveraged "
            "loans and high-yield bonds, to expand its campus",
            3.5e9,
            "financing",
        ),
        # Building between the purchase and the price makes it the project's cost.
        ("The company plans to acquire land and build a data center for $500 million.", 5e8, None),
        (
            "It will purchase the site and begin construction of the facility for $2 billion",
            2e9,
            None,
        ),
        # Money for the site, written in words.
        ("Google will invest 17 billion dollars in Laramie County.", 17e9, None),
        # An offer of something else than a price.
        (
            "the county offered $120 million in tax incentives for the project",
            1.2e8,
            "not_spending",
        ),
    ],
)
def test_money_that_is_not_spending_is_named(quote, value, expected):
    assert money_kind(quote, value) == expected


def test_a_refused_figure_is_never_the_campus_investment(session):
    """A land price used to stand as the 待确认 fallback when nothing else stated a figure."""
    from tracker.models import Project, Source
    from tracker.upsert import recompute_from_sources

    project = Project(
        name="Person County",
        company="Microsoft",
        city="Roxboro",
        state="NC",
        dedup_key="microsoft|city:roxboro|NC",
        phase="announced",
        confidence=1,
        investment_usd=26_800_000,
    )
    session.add(project)
    session.flush()
    session.add(
        Source(
            project_id=project.id,
            url="https://e.test/land",
            source_type="trade_press",
            claims=json.dumps({"investment_usd": 26_800_000}),
            quotes=json.dumps(
                {"investment_usd": "Microsoft acquired a 1,385-acre megasite for $26.8 million."}
            ),
            unconfirmed_fields="investment_usd",
            unconfirmed_reasons=json.dumps({"investment_usd": "purchase_price"}),
        )
    )
    session.flush()
    session.refresh(project)
    recompute_from_sources(session, project)
    assert project.investment_usd is None, "a price paid is not the campus's investment"

    session.add(
        Source(
            project_id=project.id,
            url="https://e.test/plan",
            source_type="trade_press",
            claims=json.dumps({"investment_usd": 1_000_000_000}),
            quotes=json.dumps({"investment_usd": "Microsoft plans to invest $1 billion there."}),
            fields="investment_usd",
        )
    )
    session.flush()
    session.refresh(project)
    recompute_from_sources(session, project)
    assert project.investment_usd == 1_000_000_000


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


def test_a_ruled_out_figure_is_not_confirmation(session):
    """Riot: two superseded $333M/$640M claims still listed in `fields` kept $8.3B in."""
    from tracker import capex
    from tracker.models import Project, Source

    project = Project(
        name="Corsicana",
        company="Riot",
        city="Corsicana",
        state="TX",
        dedup_key="riot|city:corsicana|TX",
        phase="construction",
        confidence=1,
        investment_usd=8_300_000_000,
    )
    session.add(project)
    session.flush()
    for url, reason in (
        ("https://e.test/old", "superseded"),
        ("https://e.test/new", "speculative"),
    ):
        session.add(
            Source(
                project_id=project.id,
                url=url,
                source_type="trade_press",
                claims=json.dumps({"investment_usd": 1}),
                fields="investment_usd" if reason == "superseded" else None,
                unconfirmed_fields="investment_usd",
                unconfirmed_reasons=json.dumps({"investment_usd": reason}),
            )
        )
    session.flush()
    assert project.id in capex.unconfirmed_investment_ids(session)
