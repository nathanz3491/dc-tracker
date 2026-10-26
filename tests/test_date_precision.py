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
        # Nor the quarter or half it names. 1 January used to read as "a quarter"
        # here — the first, ending three months before the one the sentence gave.
        ("2027-01-01", "start operations in the second quarter of 2027.", None),
        ("2027-04-01", "operations begin in the third quarter of 2027", None),
        ("2026-01-01", "placed into service during the second half of 2026", None),
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


def test_a_quarter_named_twice_records_no_precision_rather_than_the_first_quarter():
    """The year twice, so `period_named_in_quote` cannot move the value: what is left
    is 1 January, and reading that as a quarter would call the campus late on 1 April
    — before the earliest quarter the sentence gives is over. It reads as the year."""
    from tracker import delays

    quote = (
        "The first building opens in the second quarter of 2027 "
        "and the second in the fourth quarter of 2027."
    )
    assert precision_in_quote("2027-01-01", quote) is None
    assert delays.period_end(dt.date(2027, 1, 1), None) == dt.date(2027, 12, 31)


# --- a period the model wrote as 1 January (the 2026-10-21 and 10-26 loops) ----------


@pytest.mark.parametrize(
    ("value", "quote", "expected"),
    [
        # Aligned SLC02: stored as 2021-01-01, year-precise.
        ("2021-01-01", "with completion projected in December of 2021.", ("2021-12-01", "month")),
        ("2027-01-01", "expected online in June 2027", ("2027-06-01", "month")),
        ("2027-01-01", "online by Sept. 2027", ("2027-09-01", "month")),
        # DataBank ATL3, DC Blox Conyers, NTT Gainesville: each stored as 1 January.
        (
            "2027-01-01",
            "to start operations in the second quarter of 2027.",
            ("2027-04-01", "quarter"),
        ),
        ("2025-01-01", "with expected delivery in 4Q 2025.", ("2025-10-01", "quarter")),
        (
            "2024-01-01",
            "the first scheduled to come online in the second quarter of 2024.",
            ("2024-04-01", "quarter"),
        ),
        ("2027-01-01", "in Q3 2027", ("2027-07-01", "quarter")),
        ("2027-01-01", "in Q2 of 2027", ("2027-04-01", "quarter")),
        ("2027-01-01", "in the 2nd quarter 2027", ("2027-04-01", "quarter")),
        ("2026-01-01", "by the end of the third quarter, 2026", ("2026-07-01", "quarter")),
        ("2026-01-01", "Ready H2 2026", ("2026-07-01", "half")),
        ("2027-01-01", "Ready 2H 2027, 18 MW initial phase", ("2027-07-01", "half")),
        ("2026-01-01", "in the second half of 2026", ("2026-07-01", "half")),
        # The period starts on 1 January: the day stands, the precision narrows.
        ("2027-01-01", "in January 2027", ("2027-01-01", "month")),
        ("2026-01-01", "operational by first quarter 2026.", ("2026-01-01", "quarter")),
        ("2028-01-01", "targeting completion in the first half of 2028.", ("2028-01-01", "half")),
        # No period before the year: nothing to correct.
        ("2027-01-01", "online in 2027", None),
        # The quarter is another year's: Google Cedar Rapids starts building in Q2 2024.
        ("2026-01-01", "set to start in Q2 2024 for a 2026 go-live date.", None),
        # A day before the year.
        ("2027-01-01", "on June 3, 2027", None),
        ("2027-01-01", "announced on 3 June 2027, in the second quarter", None),
        # The year twice: which mention the value came from is not knowable.
        ("2027-01-01", "phase one in March 2027 and phase two later in 2027", None),
        ("2027-01-01", "phase one in the second quarter of 2027 and phase two later in 2027", None),
        # Not 1 January: the value already says something narrower.
        ("2027-06-01", "in June 2027", None),
        ("2027-04-01", "in the second quarter of 2027", None),
        # Seasons and hedges name no calendar period, so they stay the year.
        ("2023-01-01", "it aims to complete it by fall of 2023", None),
        ("2027-01-01", "in spring 2027", None),
        ("2026-01-01", "expected to be complete by early 2026", None),
        ("2025-01-01", "online by mid-2025", None),
        ("2027-01-01", "expected in late 2027", None),
        ("2021-01-01", "finished by the end of 2021", None),
    ],
)
def test_a_named_period_corrects_a_january_the_model_wrote(value, quote, expected):
    """The value moves to the period's first day, and the precision the sentence is
    then read at is the period's own: the two readings cannot disagree."""
    from tracker.normalize import period_named_in_quote

    named = period_named_in_quote(value, quote)
    if expected is None:
        assert named is None
        return
    assert (named.value.isoformat(), named.precision) == expected
    assert precision_in_quote(named.value, quote) == named.precision


def test_month_of_year_is_a_month_not_a_year():
    assert precision_in_quote("2021-12-01", "completion projected in December of 2021") == "month"


@pytest.mark.parametrize("written", ["2027-04-01", "2027-01-01", "2027", "Q2 2027"])
def test_extraction_keeps_the_quarter_the_sentence_names(written):
    """However the model wrote it, the record holds the quarter. Written as the right
    day, the gate kept the bare year — it reads "Q2 2027", not "second quarter of
    2027" — and the claim was stored as 1 January, year-precise."""
    from tracker.ingest import crawl
    from tracker.ingest.fetch import FetchResult
    from tracker.llm import LLMReply
    from tracker.prompts import load_prompt

    sentence = "The company expects the facility to start operations in the second quarter of 2027."
    article = f"DataBank is expanding its ATL3 data center in Atlanta, Georgia.\n{sentence}\n"
    payload = {
        "projects": [
            {
                "name": "ATL3",
                "company": "DataBank",
                "city": "Atlanta",
                "state": "GA",
                "expected_online": written,
                "evidence": [{"field": "expected_online", "quote": sentence}],
            }
        ]
    }
    records = crawl.build_records(
        FetchResult(url="https://example.test/atl3", ok=True, markdown=article, status=200),
        payload,
        prompt=load_prompt("extract-v1"),
        reply=LLMReply("", "stop", 1, 1, "m"),
    )
    source = records[0].sources[0]
    assert source.claims["expected_online"] == dt.date(2027, 4, 1)
    assert source.claim_meta["expected_online"] == {"date_precision": "quarter"}


def test_the_backfill_corrects_a_stored_january_and_the_row(session):
    """The SLC02 claim: a `year` precision was recorded, so the old backfill kept it."""
    from tracker.backfill import derive_date_precision

    project = _stored_as_january(
        session, "with completion projected in December of 2021.", dt.date(2021, 1, 1)
    )

    report = derive_date_precision(session, apply=True)
    assert report.corrected == {"month": 1}
    assert project.expected_online == dt.date(2021, 12, 1)
    assert project.expected_online_precision == "month"
    assert derive_date_precision(session, apply=True).corrected == {}, "idempotent"


def _stored_as_january(session, quote: str, value: dt.date, *, phase: str = "operational"):
    """A campus whose one citation holds `value` at `year` precision beside `quote`."""
    from tracker.models import Project, Source

    project = Project(
        name="Campus",
        company="Someone",
        city="Atlanta",
        state="GA",
        dedup_key="k",
        phase=phase,
        confidence=1,
        expected_online=value,
        expected_online_precision="year",
    )
    session.add(project)
    session.flush()
    session.add(
        Source(
            project_id=project.id,
            url="https://example.test/campus",
            source_type="trade_press",
            fetched_at=dt.datetime(2026, 10, 21),
            claims=json.dumps({"expected_online": value.isoformat()}),
            quotes=json.dumps({"expected_online": quote}),
            claim_meta=json.dumps({"expected_online": {"date_precision": "year"}}),
            fields="expected_online",
        )
    )
    session.flush()
    session.refresh(project)
    return project


def test_the_backfill_moves_a_stored_quarter_and_the_row(session):
    """DataBank ATL3 (#63) on the 2026-10-26 copy: "the second quarter of 2027" held as
    2027-01-01, year-precise, so the row was due on 31 December rather than 30 June."""
    from tracker import delays
    from tracker.backfill import derive_date_precision

    project = _stored_as_january(
        session,
        "The company expects the facility to start operations in the second quarter of 2027.",
        dt.date(2027, 1, 1),
    )
    source = project.sources[0]

    preview = derive_date_precision(session, apply=False)
    assert preview.corrected == {"quarter": 1}
    assert json.loads(source.claims) == {"expected_online": "2027-01-01"}, (
        "a preview writes nothing"
    )

    report = derive_date_precision(session, apply=True)
    assert (report.corrected, report.projects_touched) == ({"quarter": 1}, 1)
    assert json.loads(source.claims) == {"expected_online": "2027-04-01"}
    assert json.loads(source.claim_meta) == {"expected_online": {"date_precision": "quarter"}}
    assert (project.expected_online, project.expected_online_precision) == (
        dt.date(2027, 4, 1),
        "quarter",
    )
    assert delays.period_end(project.expected_online, project.expected_online_precision) == (
        dt.date(2027, 6, 30)
    )
    again = derive_date_precision(session, apply=True)
    assert (again.corrected, again.changed, again.projects_touched) == ({}, 0, 0), "idempotent"


def test_the_backfill_narrows_a_first_quarter_without_moving_it(session):
    """Q1 starts on the day already stored; only the `year` recorded beside it was wrong."""
    from tracker.backfill import derive_date_precision

    project = _stored_as_january(
        session,
        "The project is expected to come online in the first quarter of 2027.",
        dt.date(2027, 1, 1),
    )

    assert derive_date_precision(session, apply=True).corrected == {"quarter": 1}
    assert json.loads(project.sources[0].claims) == {"expected_online": "2027-01-01"}
    assert (project.expected_online, project.expected_online_precision) == (
        dt.date(2027, 1, 1),
        "quarter",
    )


def test_a_quarter_stored_as_a_year_is_flagged_late_when_the_quarter_ends(session):
    """NTT Gainesville (#88): "the second quarter of 2024", read as the year, was not
    late until 2025. Corrected, it is late from 1 July 2024 — through the stored
    precision, which is what `delays` reads."""
    from tracker import delays
    from tracker.backfill import derive_date_precision

    project = _stored_as_january(
        session,
        "The first two-story building is to open in the second quarter of 2024.",
        dt.date(2024, 1, 1),
        phase="construction",
    )
    autumn = dt.date(2024, 9, 1)
    assert delays.suspected_delay(project, today=autumn, require_check=False) is None

    derive_date_precision(session, apply=True)
    late = delays.suspected_delay(project, today=autumn, require_check=False)
    assert (late.stated, late.due) == ("Q2 2024", dt.date(2024, 6, 30))
