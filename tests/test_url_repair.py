"""`backfill urls`: one article cited once per row, and read articles out of the retry pool.

Both are what ingest did before it compared URLs by identity. The ingest path now
prevents them; these tests are about the rows already stored, which prevention
does not reach.
"""

from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import select

from tracker.backfill import repair_urls
from tracker.models import Event, IngestUrl, Project, Source

ARTICLE = "https://example.test/news/campus"
T0 = dt.datetime(2026, 1, 1)


def _row(session, **fields) -> Project:
    project = Project(
        name="Campus",
        company="Acme",
        city="Racine",
        state="WI",
        dedup_key="acme|city:racine|WI",
        phase="announced",
        **fields,
    )
    session.add(project)
    session.flush()
    return project


def _cite(session, project, url, *, at, claims, quotes=None, extractor="crawl:v1") -> Source:
    source = Source(
        project_id=project.id,
        url=url,
        source_type="trade_press",
        fetched_at=at,
        claims=json.dumps(claims),
        quotes=json.dumps(quotes or {}),
        fields=",".join(sorted(k for k in claims if k in (quotes or {}))) or None,
        extractor=extractor,
    )
    session.add(source)
    session.flush()
    return source


def _queued(session, url, status) -> IngestUrl:
    row = IngestUrl(url=url, run_id="test", status=status)
    session.add(row)
    session.flush()
    return row


def test_a_second_spelling_of_one_article_is_folded_into_the_first(session):
    """The earliest copy is kept and absorbs what only the later copy said."""
    project = _row(session)
    first = _cite(
        session,
        project,
        ARTICLE,
        at=T0,
        claims={"mw_planned": 300.0},
        quotes={"mw_planned": "a 300 MW campus"},
    )
    again = _cite(
        session,
        project,
        f"{ARTICLE}/?srsltid=AfmBOoq",
        at=T0 + dt.timedelta(days=30),
        claims={"mw_planned": 300.0, "customer": "OpenAI"},
        quotes={"mw_planned": "a 300 MW campus", "customer": "leased to OpenAI"},
    )
    event = Event(
        project_id=project.id,
        event_type="announced",
        event_date=dt.date(2026, 1, 1),
        description="announced",
        source_id=again.id,
    )
    session.add(event)
    session.flush()

    report = repair_urls(session, apply=True)

    assert report.folded == [(project.id, ARTICLE, [f"{ARTICLE}/?srsltid=AfmBOoq"])]
    session.refresh(project)
    assert [s.id for s in project.sources] == [first.id]
    claims = json.loads(project.sources[0].claims)
    assert claims == {"mw_planned": 300.0, "customer": "OpenAI"}, "what only the copy said"
    assert project.customer == "OpenAI"
    assert session.get(Event, event.id).source_id == first.id, "the milestone keeps its citation"

    assert repair_urls(session, apply=True).folded == [], "a second pass finds nothing"


def test_the_kept_reading_stands_where_both_copies_state_a_figure(session):
    """The rule `tracker merge` applies to a shared citation, with the rival named."""
    project = _row(session)
    _cite(session, project, ARTICLE, at=T0, claims={"mw_planned": 300.0})
    _cite(
        session, project, f"{ARTICLE}/", at=T0 + dt.timedelta(days=1), claims={"mw_planned": 350.0}
    )

    repair_urls(session, apply=True)

    session.refresh(project)
    assert json.loads(project.sources[0].claims) == {"mw_planned": 300.0}
    assert project.mw_planned == 300.0
    assert "350.0" in (project.notes or ""), "the rival figure is disclosed"


def test_different_pages_are_not_folded(session):
    """A parameter other than tracking can select another page, so it is kept."""
    project = _row(session)
    _cite(session, project, "https://example.test/?p=1", at=T0, claims={"mw_planned": 1.0})
    _cite(session, project, "https://example.test/?p=2", at=T0, claims={"mw_planned": 2.0})

    assert repair_urls(session, apply=True).folded == []
    session.refresh(project)
    assert len(project.sources) == 2


def test_a_read_article_leaves_the_retry_pool_and_an_unread_one_stays(session):
    project = _row(session)
    _cite(session, project, ARTICLE, at=T0, claims={"mw_planned": 300.0})
    read = _queued(session, "https://www.example.test/news/campus/", "llm_error")
    unread = _queued(session, "https://example.test/news/other", "fetch_error")

    report = repair_urls(session, apply=True)

    assert report.restored == [(read.url, "llm_error")]
    assert (read.status, unread.status) == ("ok", "fetch_error")


def test_a_computed_citation_is_not_proof_the_page_was_read(session):
    """A Census or inferred row names a URL nobody's extractor read."""
    project = _row(session)
    _cite(
        session,
        project,
        "https://www2.census.gov/geo/place_by_county.txt",
        at=T0,
        claims={"county": "Racine"},
        extractor="derived:census-v1",
    )
    queued = _queued(session, "https://www2.census.gov/geo/place_by_county.txt", "llm_error")

    assert repair_urls(session, apply=True).restored == []
    assert queued.status == "llm_error"


def test_the_preview_writes_nothing(session):
    project = _row(session)
    _cite(session, project, ARTICLE, at=T0, claims={"mw_planned": 300.0})
    _cite(session, project, f"{ARTICLE}/", at=T0, claims={"customer": "OpenAI"})
    queued = _queued(session, ARTICLE, "parse_error")

    report = repair_urls(session)

    assert (len(report.folded), len(report.restored)) == (1, 1)
    session.refresh(project)
    assert len(project.sources) == 2
    assert queued.status == "parse_error"
    assert session.scalar(select(Source).where(Source.url == f"{ARTICLE}/")) is not None
