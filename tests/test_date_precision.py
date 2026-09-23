"""How precisely a date was stated, read from the sentence that states it.

The extraction prompt asks for ISO dates and tells the model to write a bare year as
`YYYY-01-01`, so the parser saw a full day for "online in 2027" and recorded nothing.
4 of 1,455 date claims carried a precision; 110 of 203 stored online dates were
1 January. The quote beside the date still says what the article said.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker.normalize import precision_in_quote


@pytest.mark.parametrize(
    ("value", "quote", "expected"),
    [
        ("2026-01-01", "it's still expected to go online in 2026", "year"),
        ("2028-01-01", "Expected Completion\n2028", "year"),
        ("2027-01-01", "The data center is due live in early 2027", "year"),
        ("2025-10-01", "The Saline Township project was confirmed in October 2025, with", "month"),
        ("2024-01-01", "AWS announced two campuses in Madison County in January 2024", "month"),
        ("2020-04-01", "The first building is scheduled for delivery in Q2 2020.", "quarter"),
        ("2027-07-01", "operations begin in the third quarter of 2027", "quarter"),
        ("2027-07-01", "placed into service during the second half of 2027", "half"),
        # A day before the year is a day, whichever way round it is written.
        ("2025-03-01", "announced on March 1, 2025 that", None),
        ("2025-03-01", "announced on 1 March 2025", None),
        # The value does not start the period the sentence names: say nothing.
        ("2027-04-01", "online in 2027", None),
        ("2027-02-01", "completed by October 2027", None),
        # The sentence does not contain the year at all.
        ("2026-01-01", "construction continues", None),
        ("2026-01-15", "January 15, 2026", None),
    ],
)
def test_the_words_before_the_year_decide(value, quote, expected):
    assert precision_in_quote(value, quote) == expected


def test_a_date_object_is_read_like_its_iso_string():
    assert (
        precision_in_quote(dt.date(2029, 1, 1), "Phase 1 completion is targeted for 2029") == "year"
    )


def test_a_mention_of_the_day_anywhere_wins_over_a_bare_year():
    """Two readings of one year in one sentence: the day is the more precise one,
    and a precision coarser than the sentence gives would under-report it."""
    quote = "Opened on June 1, 2026 — the 2026 expansion follows."
    assert precision_in_quote("2026-06-01", quote) is None


def test_extraction_records_it_on_the_claim():
    """`_claim_axes` is where a new claim's envelope is built; the parse saw a day."""
    from tracker.ingest.crawl import _claim_axes

    axes = _claim_axes(
        evidence=[],
        quotes={"expected_online": "The campus is expected to be operational in 2027."},
        kept={"expected_online": dt.date(2027, 1, 1)},
        blocks=[],
        precisions={},
    )
    assert axes == {"expected_online": {"date_precision": "year"}}


def test_a_parsed_precision_is_never_overwritten():
    from tracker.ingest.crawl import _claim_axes

    axes = _claim_axes(
        evidence=[],
        quotes={"expected_online": "in the second half of 2027"},
        kept={"expected_online": dt.date(2027, 7, 1)},
        blocks=[],
        precisions={"expected_online": "quarter"},
    )
    assert axes["expected_online"]["date_precision"] == "quarter"


def test_the_backfill_fills_existing_claims_and_the_row_cache(session):
    from tracker.backfill import derive_date_precision
    from tracker.models import Project, Source

    project = Project(
        name="Campus",
        company="Someone",
        city="Abilene",
        state="TX",
        dedup_key="k",
        phase="construction",
        confidence=1,
        expected_online=dt.date(2027, 1, 1),
    )
    session.add(project)
    session.flush()
    session.add(
        Source(
            project_id=project.id,
            url="https://example.test/a",
            source_type="trade_press",
            claims=json.dumps({"expected_online": "2027-01-01"}),
            quotes=json.dumps({"expected_online": "Completion is scheduled for 2027."}),
            fields="expected_online",
        )
    )
    session.flush()
    session.refresh(project)

    preview = derive_date_precision(session, apply=False)
    assert (preview.claims, preview.changed) == (1, 1)
    assert project.expected_online_precision is None, "a preview writes nothing"

    applied = derive_date_precision(session, apply=True)
    assert applied.projects_touched == 1
    assert project.expected_online_precision == "year"
    assert derive_date_precision(session, apply=True).changed == 0, "idempotent"
