"""Remembering which fields we already went looking for and did not find.

The whole point is not asking twice at ~77,000 tokens a time. The assertions that
carry the design:

* :func:`test_an_attempt_expires_when_the_row_gains_a_citation` — the escape hatch.
  A permanent skip would be a blind spot, not a saving.
* :func:`test_an_old_article_attached_later_does_not_reopen_a_field` — the escape
  hatch opens on news, not on the crawler reaching back: an article already out
  there when the search gave up is not a reason to search again.
* :func:`test_the_record_survives_beside_an_operator_note` — it lives in the same
  prose channel `logic.record_decision` uses, which re-ingest never erases.
"""

from __future__ import annotations

import datetime as dt

from tracker import attempts
from tracker.models import Project, Source, utcnow


def _project(session, **kwargs) -> Project:
    row = Project(
        name="Hillsboro Campus",
        company="STACK Infrastructure",
        city="Hillsboro",
        state="OR",
        dedup_key="stack|city:hillsboro|OR",
        **kwargs,
    )
    session.add(row)
    session.flush()
    return row


def _source(session, project, url: str, **kwargs) -> Source:
    kwargs.setdefault("fetched_at", utcnow())
    row = Source(project_id=project.id, url=url, source_type="trade_press", **kwargs)
    session.add(row)
    session.flush()
    return row


def test_a_field_is_not_exhausted_until_the_cap_is_reached(session):
    project = _project(session)
    attempts.record(project, ["mw_built"])
    assert attempts.exhausted(project, max_attempts=2) == set(), "one attempt is not enough"

    attempts.record(project, ["mw_built"])
    assert attempts.exhausted(project, max_attempts=2) == {"mw_built"}


def test_attempts_accumulate_on_one_line_per_field(session):
    """Three askings leave one line saying "attempt 3", not three lines to count."""
    project = _project(session)
    for _ in range(3):
        attempts.record(project, ["expected_online"])

    assert attempts.attempts(project)["expected_online"].attempts == 3
    lines = [ln for ln in project.notes.splitlines() if "expected_online" in ln]
    assert len(lines) == 1, f"one line per field, got {lines}"


#: When the citations a row already held were fetched: before any attempt below.
_EARLIER = dt.datetime(2026, 10, 1)


def _asked_twice_on(project, day: str, citations: int) -> None:
    """Two fruitless askings about `mw_built`, the last on `day`, as `record` writes it."""
    project.notes = (
        f"{day} agent found nothing for `mw_built`: attempt 2, {citations} citation(s) at the time"
    )


def test_an_attempt_expires_when_the_row_gains_a_citation(session):
    """The escape hatch, and the reason this is a cap rather than a verdict.

    `audit.settled_codes` records what a decision that never expires costs: on
    Hyperion a settled code muzzled the detector on the row where it had most
    recently been right. "Nobody published this" is true until somebody does.
    """
    project = _project(session)
    _source(session, project, "https://trade.example/one", fetched_at=_EARLIER)
    attempts.record(project, ["mw_built"])
    attempts.record(project, ["mw_built"])
    assert attempts.exhausted(project, max_attempts=2) == {"mw_built"}

    _source(session, project, "https://trade.example/two", published_at=utcnow())
    session.refresh(project)
    assert attempts.exhausted(project, max_attempts=2) == set(), (
        "a newly published citation must reopen the question"
    )


def test_an_old_article_attached_later_does_not_reopen_a_field(session):
    """The news watch reaches a year back on its first look at a campus.

    An article published before the search gave up was already out there to be
    found, and the extractor has since read it without finding the figure. On the
    2026-10-28 copy of production, 46 of the 85 reopened fields were reopened only by
    articles like this, and each dropped its row out of T2 until it was asked again.
    """
    project = _project(session)
    _source(session, project, "https://trade.example/one", fetched_at=_EARLIER)
    _asked_twice_on(project, "2026-10-20", citations=1)
    assert attempts.exhausted(project, max_attempts=2) == {"mw_built"}

    _source(
        session,
        project,
        "https://trade.example/last-spring",
        published_at=dt.datetime(2026, 4, 2),
    )
    session.refresh(project)
    assert attempts.exhausted(project, max_attempts=2) == {"mw_built"}, (
        "an article older than the search is not news"
    )


def test_an_article_published_since_reopens_a_field(session):
    """The case the escape hatch exists for: somebody published after we looked."""
    project = _project(session)
    _source(session, project, "https://trade.example/one", fetched_at=_EARLIER)
    _asked_twice_on(project, "2026-10-20", citations=1)

    _source(
        session,
        project,
        "https://trade.example/this-week",
        published_at=dt.datetime(2026, 10, 24),
    )
    session.refresh(project)
    assert attempts.exhausted(project, max_attempts=2) == set()


def test_an_article_published_the_day_it_was_asked_reopens_it(session):
    """Same day counts as since: a search engine may not have had it yet."""
    project = _project(session)
    _source(session, project, "https://trade.example/one", fetched_at=_EARLIER)
    _asked_twice_on(project, "2026-10-20", citations=1)

    _source(
        session, project, "https://trade.example/that-day", published_at=dt.datetime(2026, 10, 20)
    )
    session.refresh(project)
    assert attempts.exhausted(project, max_attempts=2) == set()


def test_an_undated_article_counts_by_when_it_was_fetched(session):
    """A page nobody can date is not ignored: fetched since is possibly new."""
    project = _project(session)
    _source(session, project, "https://trade.example/one", fetched_at=_EARLIER)
    _asked_twice_on(project, "2026-10-20", citations=1)

    _source(session, project, "https://trade.example/undated", fetched_at=dt.datetime(2026, 9, 1))
    session.refresh(project)
    assert attempts.exhausted(project, max_attempts=2) == {"mw_built"}, (
        "fetched before the search, so the search could have read it"
    )

    # The harvest re-reads a row's own pages minutes before the agent searches, so an
    # undated page fetched that day was in hand when the search gave up.
    _source(
        session, project, "https://trade.example/refreshed", fetched_at=dt.datetime(2026, 10, 20)
    )
    session.refresh(project)
    assert attempts.exhausted(project, max_attempts=2) == {"mw_built"}

    _source(
        session, project, "https://trade.example/undated-2", fetched_at=dt.datetime(2026, 10, 25)
    )
    session.refresh(project)
    assert attempts.exhausted(project, max_attempts=2) == set()


def test_a_line_that_does_not_say_when_falls_back_to_any_new_citation(session):
    """Prose a person wrote without a day keeps the rule from before the day was read."""
    project = _project(session)
    _source(session, project, "https://trade.example/one")
    project.notes = "agent found nothing for `mw_built`: attempt 2, 1 citation(s) at the time"
    assert attempts.exhausted(project, max_attempts=2) == {"mw_built"}

    _source(session, project, "https://trade.example/old", published_at=dt.datetime(2020, 1, 1))
    session.refresh(project)
    assert attempts.attempts(project)["mw_built"].day is None
    assert attempts.exhausted(project, max_attempts=2) == set()


def test_the_record_carries_the_day_it_was_written(session):
    """`record` stamps the day the expiry test reads back."""
    project = _project(session)
    attempts.record(project, ["mw_built"])

    assert attempts.attempts(project)["mw_built"].day == utcnow().date()


def test_a_derived_or_placeholder_citation_does_not_reopen_anything(session):
    """Neither is testimony about the campus, so neither is new evidence.

    `confidence.compute` excludes both for the same reason. Counting them would
    reopen a field on the strength of nothing — a Census row confirming a county
    says nothing about when a campus goes online.
    """
    project = _project(session)
    _source(session, project, "https://trade.example/one")
    attempts.record(project, ["mw_built"])
    attempts.record(project, ["mw_built"])

    _source(session, project, "https://census.example/PLACEHOLDER/x")
    _source(session, project, "https://census.example/y", extractor="derived:census")
    session.refresh(project)
    assert attempts.exhausted(project, max_attempts=2) == {"mw_built"}


def test_the_record_survives_beside_an_operator_note(session):
    """Written into the prose channel, so an existing note is not disturbed."""
    project = _project(session, notes="2026-01-01 operator resolved `x`: checked by hand")
    attempts.record(project, ["customer"])

    assert "resolved `x`" in project.notes, "an operator note must not be touched"
    assert "found nothing for `customer`" in project.notes


def test_max_attempts_zero_asks_every_time(session):
    """The off switch. `--max-attempts 0` restores the old always-ask behaviour."""
    project = _project(session)
    for _ in range(5):
        attempts.record(project, ["mw_built"])
    assert attempts.exhausted(project, max_attempts=0) == set()


def test_recording_nothing_writes_nothing(session):
    project = _project(session)
    attempts.record(project, [])
    assert not (project.notes or "")
