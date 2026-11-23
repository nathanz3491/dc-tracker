"""The console's watch on the morning email: what the run itself cannot report.

A run that never started, one that died partway, and one whose own alert could not
be sent all say nothing. The console looks after the deadline and says it once.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from tracker import accounts, mailwatch, notify
from tracker.db import session_scope
from tracker.models import NotifyDelivery, NotifyRun

DUE = dt.time(8, 30)


def _utc(hour: int, minute: int = 0, *, days: int = 0) -> dt.datetime:
    """Today at this local time, as the naive UTC the database keeps."""
    day = dt.date.today() + dt.timedelta(days=days)
    local = dt.datetime.combine(day, dt.time(hour, minute)).astimezone()
    return local.astimezone(dt.UTC).replace(tzinfo=None)


class Recorder:
    """Stands in for Resend. Never opens a socket."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, to, subject, html_body, text_body, idempotency_key=None) -> str:
        self.sent.append({"to": to, "subject": subject, "text": text_body})
        return f"msg_{len(self.sent)}"


class Down(Recorder):
    """Resend refusing everything."""

    def send(self, *, to, subject, html_body, text_body, idempotency_key=None) -> str:
        self.sent.append({"to": to, "subject": subject, "text": text_body})
        raise notify.EmailError("Resend rejected the key (HTTP 401).")


@pytest.fixture
def admin(engine):
    with session_scope(engine) as session:
        row = accounts.create(session, "admin@example.com", "correct horse")
        row.is_admin = True


def _watch(engine, post, clock):
    return mailwatch.Watchdog(
        read=lambda: session_scope(engine, commit=False),
        write=lambda: session_scope(engine),
        due=DUE,
        transport=lambda: post,
        clock=clock,
    )


def _run(engine, *, started, finished=None):
    with session_scope(engine) as session:
        run = NotifyRun(started_at=started, finished_at=finished)
        session.add(run)
        session.flush()
        return run.id


def test_nothing_is_said_before_the_deadline(engine, admin):
    post = Recorder()
    assert _watch(engine, post, lambda: _utc(8, 15)).look() == []
    assert post.sent == []


def test_a_morning_with_no_run_is_said_once(engine, admin):
    post = Recorder()
    watch = _watch(engine, post, lambda: _utc(9, 0))

    [told] = watch.look()

    assert told.sent
    [mail] = post.sent
    assert mail["to"] == "admin@example.com"
    assert mail["subject"] == "dc-tracker: the morning email did not run today"
    assert "tracker notify send" in mail["text"]
    assert watch.look() == [], "once a day"
    assert post.sent == [mail]


def test_yesterdays_run_does_not_count_for_today(engine, admin):
    _run(engine, started=_utc(8, 0, days=-1), finished=_utc(8, 1, days=-1))
    post = Recorder()
    _watch(engine, post, lambda: _utc(9, 0)).look()
    assert [m["subject"] for m in post.sent] == ["dc-tracker: the morning email did not run today"]


def test_a_run_still_going_is_left_alone_and_one_that_died_is_said(engine, admin):
    _run(engine, started=_utc(8, 50))
    post = Recorder()
    assert _watch(engine, post, lambda: _utc(9, 0)).look() == []

    _watch(engine, post, lambda: _utc(9, 30)).look()
    [mail] = post.sent
    assert mail["subject"] == "dc-tracker: the morning email stopped partway"
    assert "08:50" in mail["text"]


def test_a_run_that_finished_and_was_told_is_left_alone(engine, admin):
    run_id = _run(engine, started=_utc(8, 0), finished=_utc(8, 1))
    post = Recorder()
    assert _watch(engine, post, lambda: _utc(9, 0)).look() == [], "nothing to report"

    with session_scope(engine) as session:
        session.add(
            NotifyDelivery(
                run_id=run_id,
                email="admin@example.com",
                kind="alert",
                status="sent",
                prepared_at=_utc(8, 1),
                message_id="m1",
            )
        )
    assert _watch(engine, post, lambda: _utc(9, 0)).look() == [], "its alert went"
    assert post.sent == []


def test_an_alert_the_run_could_not_send_is_sent_with_what_failed(engine, admin):
    run_id = _run(engine, started=_utc(8, 0), finished=_utc(8, 1))
    with session_scope(engine) as session:
        session.add_all(
            [
                NotifyDelivery(
                    run_id=run_id,
                    email="client@example.com",
                    kind="updates",
                    status="failed",
                    prepared_at=_utc(8, 0),
                    attempts=4,
                    error="Resend returned HTTP 503",
                ),
                NotifyDelivery(
                    run_id=run_id,
                    email="admin@example.com",
                    kind="alert",
                    status="failed",
                    prepared_at=_utc(8, 1),
                    attempts=4,
                    error="Resend returned HTTP 503",
                ),
            ]
        )
    post = Recorder()

    _watch(engine, post, lambda: _utc(10, 0)).look()

    [mail] = post.sent
    assert mail["subject"] == "dc-tracker: this morning's alert could not be sent"
    assert "Could not send to client@example.com" in mail["text"]
    with session_scope(engine, commit=False) as session:
        told = session.scalars(select(NotifyDelivery).where(NotifyDelivery.run_id.is_(None))).one()
        assert (told.kind, told.status) == ("alert", "sent")


def test_an_alert_that_cannot_go_is_tried_again_an_hour_later(engine, admin):
    """A Resend outage: the alert arrives once Resend is back."""
    now = [_utc(9, 0)]
    post = Down()
    watch = _watch(engine, post, lambda: now[0])

    watch.look()
    assert len(post.sent) == 1
    now[0] = _utc(9, 15)
    watch.look()
    assert len(post.sent) == 1, "not before the hour is up"

    now[0] = _utc(10, 1)
    up = Recorder()
    watch.transport = lambda: up
    [told] = watch.look()
    assert told.sent and up.sent[0]["subject"].endswith("did not run today")


def test_the_second_inbox_hears_the_watch_too(engine, admin, monkeypatch):
    from tracker.config import get_settings

    monkeypatch.setenv("TRACKER_NOTIFY_ALERT_TO", "me@other.example")
    get_settings.cache_clear()
    post = Recorder()
    _watch(engine, post, lambda: _utc(9, 0)).look()
    assert [m["to"] for m in post.sent] == ["admin@example.com", "me@other.example"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("08:30", dt.time(8, 30)),
        (" 7:05 ", dt.time(7, 5)),
        ("", None),
        ("8h30", None),
        ("25:00", None),
    ],
)
def test_the_deadline_is_hh_mm_or_nothing(text, expected):
    assert mailwatch.deadline(text) == expected
