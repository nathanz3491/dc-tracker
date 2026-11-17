"""A re-read replaces its citation: a field the new reading leaves out goes with it.

From 2026-10-30 to 11-16 a re-read kept any quoted claim it omitted while the article
still held the sentence. That was switched off on 11-17: a newer reading may leave a
field out because it no longer trusts the sentence, which nothing can tell from a
model answering differently twice, and the 32 values it kept never showed as changes,
so no review saw them. A dropped value now shows in `tracker changes` and in the
nightly `tracker losses` list, and a person puts it back with `losses --restore`.
"""

from __future__ import annotations

import datetime as dt
import json

from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.models import Project
from tracker.upsert import upsert_record

URL = "https://example.com/qts-richmond"
MW_SENTENCE = "That facility offers 110MW of capacity across three buildings."
DATE_SENTENCE = "The campus is scheduled to open in March 2027."


def reading(at: dt.datetime, **fields) -> IngestRecord:
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


def test_a_field_the_re_read_leaves_out_is_withdrawn_though_the_article_still_says_it(session):
    first = upsert_record(
        session,
        reading(
            dt.datetime(2026, 10, 1),
            mw_built=(110, MW_SENTENCE),
            expected_online=("2027-03-01", DATE_SENTENCE),
        ),
    )
    session.flush()
    project = session.get(Project, first.project_id)
    assert project.mw_built == 110

    upsert_record(
        session,
        reading(dt.datetime(2026, 10, 20), expected_online=("2027-03-01", DATE_SENTENCE)),
    )
    session.flush()

    assert project.mw_built is None
    (row,) = [s for s in project.sources if s.url == URL]
    assert "mw_built" not in json.loads(row.claims)
    assert "kept mw_built" not in (project.notes or "")
