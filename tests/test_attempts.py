"""Remembering which fields we already went looking for and did not find.

The whole point is not asking twice at ~77,000 tokens a time. The assertions that
carry the design:

* :func:`test_an_attempt_expires_when_the_row_gains_a_citation` — the escape hatch.
  A permanent skip would be a blind spot, not a saving.
* :func:`test_the_record_survives_beside_an_operator_note` — it lives in the same
  prose channel `logic.record_decision` uses, which re-ingest never erases.
"""

from __future__ import annotations

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
    row = Source(
        project_id=project.id,
        url=url,
        source_type="trade_press",
        fetched_at=utcnow(),
        **kwargs,
    )
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


def test_an_attempt_expires_when_the_row_gains_a_citation(session):
    """The escape hatch, and the reason this is a cap rather than a verdict.

    `audit.settled_codes` records what a decision that never expires costs: on
    Hyperion a settled code muzzled the detector on the row where it had most
    recently been right. "Nobody published this" is true until somebody does.
    """
    project = _project(session)
    _source(session, project, "https://trade.example/one")
    attempts.record(project, ["mw_built"])
    attempts.record(project, ["mw_built"])
    assert attempts.exhausted(project, max_attempts=2) == {"mw_built"}

    _source(session, project, "https://trade.example/two")
    session.refresh(project)
    assert attempts.exhausted(project, max_attempts=2) == set(), (
        "a new citation must reopen the question"
    )


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
