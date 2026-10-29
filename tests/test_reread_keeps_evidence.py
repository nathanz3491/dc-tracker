"""A re-read may add evidence, never quietly take it back (`upsert.omitted_quietly`).

Traced over 10-18 to 10-28: 81 stored values vanished because a second reading of the
same article did not repeat a field, 42 of them with a sentence on file stating the
value outright. These pin the rule that replaced that: an omitted quoted claim is kept
while the article still contains its sentence, and withdrawn when it does not.
"""

from __future__ import annotations

import datetime as dt
import json

from tracker.ingest.crawl import still_states
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.models import Project, Source
from tracker.upsert import SOURCE_NOTE_PREFIX, upsert_record

URL = "https://example.com/qts-richmond"
T0 = dt.datetime(2026, 10, 1, 12, 0, 0)
T1 = dt.datetime(2026, 10, 20, 12, 0, 0)

MW_SENTENCE = "That facility offers 110MW of capacity across three buildings."
DATE_SENTENCE = "The campus is scheduled to open in March 2027."
ARTICLE = f"QTS Richmond expands. {MW_SENTENCE} {DATE_SENTENCE} More text follows."


def reading(*, at: dt.datetime = T0, **fields) -> IngestRecord:
    """One reading of the article: `fields` maps a claim to (value, sentence)."""
    claims = {"name": "Richmond", "company": "QTS", "city": "Richmond", "state": "VA"}
    quotes = {}
    for name, (value, sentence) in fields.items():
        claims[name] = value
        quotes[name] = sentence
    return IngestRecord(
        project={"name": "Richmond", "company": "QTS", "city": "Richmond", "state": "VA"},
        sources=[
            SourceRecord(
                url=URL,
                source_type="trade_press",
                fetched_at=at,
                excerpt=MW_SENTENCE,
                claims=claims,
                quotes=quotes,
            )
        ],
    )


def first_read(session) -> Project:
    result = upsert_record(
        session,
        reading(mw_built=(110, MW_SENTENCE), expected_online=("2027-03-01", DATE_SENTENCE)),
    )
    session.flush()
    project = session.get(Project, result.project_id)
    assert project.mw_built == 110
    return project


def citation(session, project: Project) -> Source:
    (row,) = [s for s in project.sources if s.url == URL]
    return row


def test_a_re_read_that_omits_a_quoted_claim_keeps_it_while_the_article_says_it(session):
    project = first_read(session)

    upsert_record(
        session,
        reading(at=T1, expected_online=("2027-03-01", DATE_SENTENCE)),
        quote_holds=still_states(ARTICLE),
    )
    session.flush()

    assert project.mw_built == 110
    row = citation(session, project)
    assert json.loads(row.claims)["mw_built"] == 110
    assert json.loads(row.quotes)["mw_built"] == MW_SENTENCE
    assert "mw_built" in row.fields.split(",")
    kept = [line for line in project.notes.splitlines() if "kept mw_built = 110" in line]
    assert len(kept) == 1 and kept[0].startswith(SOURCE_NOTE_PREFIX)


def test_an_article_edited_to_drop_the_sentence_still_withdraws_the_claim(session):
    project = first_read(session)
    edited = f"QTS Richmond expands. {DATE_SENTENCE}"

    upsert_record(
        session,
        reading(at=T1, expected_online=("2027-03-01", DATE_SENTENCE)),
        quote_holds=still_states(edited),
    )
    session.flush()

    assert project.mw_built is None
    assert "mw_built" not in json.loads(citation(session, project).claims)


def test_without_the_article_a_re_read_replaces_the_citation_as_before(session):
    """Every caller but the crawl has no article to check against."""
    project = first_read(session)

    upsert_record(session, reading(at=T1, expected_online=("2027-03-01", DATE_SENTENCE)))
    session.flush()

    assert project.mw_built is None


def test_a_field_the_new_reading_claims_is_the_new_reading_s_answer(session):
    """Carrying the old value past a new one would undo every gate improvement."""
    project = first_read(session)
    newer = "The facility now offers 140MW of capacity."

    upsert_record(
        session,
        reading(at=T1, mw_built=(140, newer)),
        quote_holds=still_states(f"{ARTICLE} {newer}"),
    )
    session.flush()

    assert project.mw_built == 140
    row = citation(session, project)
    assert json.loads(row.quotes)["mw_built"] == newer
    # The date it omitted is kept, by the same rule.
    assert project.expected_online == dt.date(2027, 3, 1)


def test_a_ruled_out_claim_comes_back_still_ruled_out(session):
    """Restoring evidence must never restore a figure somebody rejected."""
    project = first_read(session)
    row = citation(session, project)
    row.unconfirmed_reasons = json.dumps({"mw_built": "superseded"})
    row.unconfirmed_fields = "mw_built"
    session.flush()

    upsert_record(
        session,
        reading(at=T1, expected_online=("2027-03-01", DATE_SENTENCE)),
        quote_holds=still_states(ARTICLE),
    )
    session.flush()

    row = citation(session, project)
    assert json.loads(row.claims)["mw_built"] == 110
    assert json.loads(row.unconfirmed_reasons)["mw_built"] == "superseded"
    assert project.mw_built is None


def test_keeping_is_stable_across_repeated_re_reads(session):
    """The idempotence the write path promises: the same reading twice writes the
    same citation and does not move the row."""
    project = first_read(session)
    again = reading(at=T1, expected_online=("2027-03-01", DATE_SENTENCE))
    holds = still_states(ARTICLE)

    upsert_record(session, again, quote_holds=holds)
    session.flush()
    row = citation(session, project)
    before = (row.claims, row.quotes, row.fields, row.unconfirmed_fields, project.notes)
    result = upsert_record(session, again, quote_holds=holds)
    session.flush()

    assert result.action == "unchanged"
    row = citation(session, project)
    assert (row.claims, row.quotes, row.fields, row.unconfirmed_fields, project.notes) == before


def test_still_states_accepts_the_page_re_rendered_and_refuses_a_missing_sentence():
    holds = still_states(
        "Intro.\n\nThat  facility offers 110MW of capacity\nacross three buildings."
    )
    assert holds(MW_SENTENCE)
    assert not holds("The campus will draw 900MW from the grid.")
    assert not holds("   ")
