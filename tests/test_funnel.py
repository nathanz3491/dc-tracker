"""Stage 1's funnel: what each feed cost and what it returned.

The number this exists to surface is the waste rate — URLs that reached an LLM
call and produced no project. It is 49% on the live database, and the arithmetic
behind it is what the tests below pin down.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from tracker import funnel
from tracker.models import IngestUrl, Project, Source

NOW = dt.datetime(2026, 8, 13, 12, 0, 0)


def url(session, u, *, feed="a-feed", status="ok", published_at=None):
    session.add(
        IngestUrl(
            url=u,
            run_id="r",
            status=status,
            feed=feed,
            published_at=published_at,
            first_seen_at=NOW,
            last_tried_at=NOW,
        )
    )
    session.flush()


def cite(session, u):
    project = session.scalar(select(Project))
    if project is None:
        project = Project(
            name="Hyperion",
            company="Meta",
            county="Richland Parish",
            state="LA",
            dedup_key="meta|county:richland|LA",
        )
        session.add(project)
        session.flush()
    session.add(Source(project_id=project.id, url=u, source_type="trade_press", fetched_at=NOW))
    session.flush()


# --- the waste rate ---------------------------------------------------------


def test_waste_is_calls_that_produced_nothing(session):
    url(session, "https://x.test/1", status="ok")
    url(session, "https://x.test/2", status="no_project")
    url(session, "https://x.test/3", status="no_project")
    report = funnel.survey(session)
    assert report.reached_model == 3
    assert report.no_project == 2
    assert report.waste == pytest.approx(2 / 3)


def test_unread_urls_are_not_in_the_denominator(session):
    """`discovered` has not cost a call yet, so it cannot be waste.

    Counting it would make the rate improve every time discovery ran and worsen
    every time extraction did, which is backwards.
    """
    url(session, "https://x.test/1", status="no_project")
    url(session, "https://x.test/2", status="discovered")
    url(session, "https://x.test/3", status="discovered")
    report = funnel.survey(session)
    assert report.reached_model == 1
    assert report.waste == 1.0


def test_a_fetch_failure_is_not_waste(session):
    """It never reached the model, so it cost no call."""
    url(session, "https://x.test/1", status="ok")
    url(session, "https://x.test/2", status="fetch_error")
    report = funnel.survey(session)
    assert report.reached_model == 1
    assert report.waste == 0.0
    assert report.feeds[0].failed == 1


def test_a_page_refused_as_thin_did_reach_the_gate(session):
    """`thin_content` is refused *before* the call but after the fetch.

    It belongs in `read` because it is part of what discovery handed to
    extraction, and it is not `no_project` because no model ever saw it.
    """
    url(session, "https://x.test/1", status="thin_content")
    report = funnel.survey(session)
    assert (report.reached_model, report.no_project) == (1, 0)
    assert report.feeds[0].thin == 1


# --- per feed ---------------------------------------------------------------


def test_feeds_are_reported_separately(session):
    url(session, "https://a.test/1", feed="good", status="ok")
    url(session, "https://b.test/1", feed="bad", status="no_project")
    url(session, "https://b.test/2", feed="bad", status="no_project")
    by_feed = {f.feed: f for f in funnel.survey(session).feeds}
    assert by_feed["good"].waste == 0.0
    assert by_feed["bad"].waste == 1.0
    assert by_feed["bad"].read == 2


def test_the_worst_feed_leads(session):
    """Ordered by waste, because the report exists to say what to reconsider."""
    url(session, "https://a.test/1", feed="fine", status="ok")
    url(session, "https://b.test/1", feed="wasteful", status="no_project")
    assert funnel.survey(session).ranked()[0].feed == "wasteful"


def test_a_tiny_sample_does_not_head_the_table(session):
    """One call at 100% must not outrank fifty at 100%."""
    url(session, "https://a.test/1", feed="tiny", status="no_project")
    for n in range(5):
        url(session, f"https://b.test/{n}", feed="big", status="no_project")
    assert funnel.survey(session).ranked()[0].feed == "big"


def test_urls_with_no_feed_are_grouped_not_dropped(session):
    """Search, archive and enrich all produce URLs no feed found."""
    url(session, "https://x.test/1", feed=None, status="ok")
    feeds = {f.feed for f in funnel.survey(session).feeds}
    assert "(no feed)" in feeds


# --- what actually stuck ----------------------------------------------------


def test_cited_counts_urls_backing_a_stored_value(session):
    url(session, "https://x.test/1", status="ok")
    url(session, "https://x.test/2", status="ok")
    cite(session, "https://x.test/1")
    assert funnel.survey(session).feeds[0].cited == 1


def test_an_extracted_url_that_left_no_citation_is_not_counted(session):
    """A URL can extract `ok` and leave nothing — the project may since have merged."""
    url(session, "https://x.test/1", status="ok")
    stat = funnel.survey(session).feeds[0]
    assert stat.extracted == 1
    assert stat.cited == 0


def test_dated_counts_what_the_tiebreak_can_use(session):
    url(session, "https://x.test/1", published_at=dt.datetime(2026, 7, 13))
    url(session, "https://x.test/2")
    report = funnel.survey(session)
    assert report.dated == 1
    assert report.feeds[0].dated == 1


def test_an_empty_database_reports_zero_rather_than_dividing(session):
    report = funnel.survey(session)
    assert report.total_urls == 0
    assert report.waste == 0.0
    assert report.as_json()["feeds"] == []
