"""Email delivery: one message per person, and a template that survives a client.

The load-bearing assertion here is
`test_one_person_gets_one_email_however_much_moved`. Everything else in this file
protects the template; that one protects the reason the feature exists.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker import accounts, notify, watchlist
from tracker.feed import Digest, Signal
from tracker.models import Event, Project

NOW = dt.datetime(2026, 8, 29, 9, 0)


def signal(**kw) -> Signal:
    base = {
        "kind": "milestone",
        "sign": "good",
        "project_id": 1,
        "company": "Nscale",
        "project": "Monarch Compute Campus",
        "label": "energized",
        "detail": "Powered up.",
        "at": NOW,
        "happened": dt.date.today() - dt.timedelta(days=2),
        "weight": 4,
    }
    base.update(kw)
    return Signal(**base)


def brief_of(*signals: Signal) -> Digest:
    return Digest(since=NOW - dt.timedelta(days=1), signals=tuple(signals))


class Recorder:
    """Stands in for Resend. Never opens a socket."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, to, subject, html_body, text_body) -> str:
        self.sent.append({"to": to, "subject": subject, "html": html_body, "text": text_body})
        return f"msg_{len(self.sent)}"


def _energized(session, *, company: str, city: str, state: str, name: str, key: str) -> Project:
    project = Project(
        name=name,
        company=company,
        city=city,
        state=state,
        dedup_key=key,
        phase="construction",
        confidence=2,
    )
    session.add(project)
    session.flush()
    session.add(
        Event(
            project_id=project.id,
            event_date=dt.date.today() - dt.timedelta(days=1),
            event_type="energized",
            description=f"{name} energized.",
            quote=f"{name} was energized on Friday.",
            created_at=dt.datetime.now(),
        )
    )
    session.flush()
    return project


# --- the reason this exists ------------------------------------------------------


def test_one_person_gets_one_email_however_much_moved(session):
    """Fourteen updates is one email with fourteen cards.

    A channel that sends one message per change is one people filter into a
    folder, and a filtered channel protects nobody — the same argument
    `feed.notable` makes about the bar for interrupting somebody, one layer out.
    """
    account = accounts.create(session, "reader@example.com", "correct horse", name="Reader")
    watchlist.add(session, "Nscale", account_id=account.id)
    for n in range(14):
        _energized(
            session,
            company="Nscale",
            city=f"Town{n}",
            state="WV",
            name=f"Monarch {n}",
            key=f"nscale|city:town{n}|WV",
        )

    post = Recorder()
    outcomes = notify.send_all(session, transport=post, days=2)

    assert len(post.sent) == 1, f"expected one message, got {len(post.sent)}"
    assert post.sent[0]["to"] == "reader@example.com"
    assert sum(o.signals for o in outcomes) >= 14
    assert post.sent[0]["html"].count("Monarch") >= 10


def test_a_quiet_window_sends_nothing(session):
    account = accounts.create(session, "quiet@example.com", "correct horse")
    watchlist.add(session, "Nscale", account_id=account.id)

    post = Recorder()
    outcomes = notify.send_all(session, transport=post, days=1)

    assert post.sent == []
    assert outcomes and outcomes[0].skipped == "nothing worth sending"
    assert not outcomes[0].sent


def test_an_account_with_no_watchlist_is_skipped_not_mailed_everything(session):
    """`digest` falls back to the whole database when nobody has said what they
    care about. Right for a page, wrong for mail."""
    accounts.create(session, "unconfigured@example.com", "correct horse")
    _energized(
        session,
        company="xAI",
        city="Memphis",
        state="TN",
        name="Colossus",
        key="xai|city:memphis|TN",
    )

    post = Recorder()
    outcomes = notify.send_all(session, transport=post, days=2)

    assert post.sent == [], "an empty watchlist must not become a firehose"
    assert outcomes[0].skipped == "no watchlist"


def test_each_person_gets_only_their_own_watchlist(session):
    """Two accounts, two lists, two different messages — the property per-user
    watchlists exist for."""
    a = accounts.create(session, "a@example.com", "correct horse")
    b = accounts.create(session, "b@example.com", "correct horse")
    watchlist.add(session, "Nscale", account_id=a.id)
    watchlist.add(session, "xAI", account_id=b.id)
    _energized(
        session,
        company="Nscale",
        city="Point Pleasant",
        state="WV",
        name="Monarch",
        key="nscale|city:pp|WV",
    )
    _energized(
        session,
        company="xAI",
        city="Memphis",
        state="TN",
        name="Colossus",
        key="xai|city:memphis|TN",
    )

    post = Recorder()
    notify.send_all(session, transport=post, days=2)

    assert len(post.sent) == 2
    by_to = {m["to"]: m["html"] for m in post.sent}
    assert "Nscale" in by_to["a@example.com"] and "xAI" not in by_to["a@example.com"]
    assert "xAI" in by_to["b@example.com"] and "Nscale" not in by_to["b@example.com"]


# --- the template ----------------------------------------------------------------


def test_the_message_carries_both_dates():
    """Same rule as the page: "new" means new to us, so a milestone we read
    yesterday must not read as yesterday's news."""
    sig = signal(happened=dt.date(2026, 6, 1), at=dt.datetime(2026, 8, 28, 9, 0))
    body = notify.render(brief_of(sig), (sig,))
    assert "2026-06-01" in body
    assert "2026-08-28" in body


def test_everything_from_an_article_is_escaped():
    """A project name is extracted from a page, and a page can contain anything.
    Unescaped, one article's markup rewrites the message."""
    sig = signal(project="<script>alert(1)</script>", detail='5" & rising')
    body = notify.render(brief_of(sig), (sig,))
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body
    assert "&amp; rising" in body


def test_the_palette_is_meridians_and_says_which_way_a_signal_cuts():
    good = signal(sign="good")
    bad = signal(sign="bad", kind="obstacle_opened", label="permitting")
    assert notify.TOKENS["success"] in notify.render(brief_of(good), (good,))
    assert notify.TOKENS["danger"] in notify.render(brief_of(bad), (bad,))
    # The canvas is Meridian's cream, not a default white.
    assert notify.TOKENS["background"] in notify.render(brief_of(good), (good,))


def test_the_message_needs_no_network_to_render_correctly():
    """Clients block remote content by default and many refuse web fonts, so a
    message depending on either is broken on first open."""
    sig = signal()
    body = notify.render(brief_of(sig), (sig,), console_url="https://console.example")
    assert "<img" not in body, "no images: they are blocked by default"
    assert "<script" not in body
    assert "fonts.googleapis" not in body
    assert "@font-face" not in body
    assert "<link" not in body


def test_the_message_is_never_truncated():
    """A reader works the message. One ending "…and 3 more, not listed" sends them
    somewhere else to find the rest, which is the workflow this exists to save — so
    the email carries every update, however long that makes it.

    `digest --notify` still caps its *terminal* output, and that stays: a stream
    scrolling past is a different thing from a document somebody opens.
    """
    many = tuple(signal(project=f"Site {n}", project_id=n) for n in range(40))
    body = notify.render(brief_of(*many), many)

    for n in range(40):
        assert f"Site {n}" in body, f"Site {n} is missing from the message"
    assert "not listed" not in body
    assert "more in this window" not in body

    text = notify.render_text(brief_of(*many), many)
    assert text.count("Site ") >= 40


def test_send_all_puts_every_update_in_the_one_message(session):
    """The same guarantee end to end, not only in the template."""
    account = accounts.create(session, "all@example.com", "correct horse")
    watchlist.add(session, "Nscale", account_id=account.id)
    for n in range(25):
        _energized(
            session,
            company="Nscale",
            city=f"Ville{n}",
            state="WV",
            name=f"Campus {n}",
            key=f"nscale|city:ville{n}|WV",
        )

    post = Recorder()
    outcomes = notify.send_all(session, transport=post, days=2)

    assert len(post.sent) == 1
    assert outcomes[0].signals == 25, "every signal is counted as sent"
    for n in range(25):
        assert f"Campus {n}" in post.sent[0]["html"]
    assert "not listed" not in post.sent[0]["html"]


def test_there_is_always_a_text_part():
    """What a screen reader reads, and what a spam filter scores."""
    text = notify.render_text(brief_of(signal()), (signal(),))
    assert "Monarch Compute Campus" in text
    assert "<" not in text


def test_the_subject_names_the_lead_rather_than_only_counting():
    """A bare count is a number somebody defers; a sentence is one they open."""
    sig = signal(sign="bad", kind="obstacle_opened", label="grid_capacity")
    subject = notify.subject_for(brief_of(sig), (sig,))
    assert "Nscale" in subject
    assert "grid capacity" in subject
    assert len(subject) <= 150


# --- the transport ---------------------------------------------------------------


def test_sending_without_a_key_fails_before_anything_is_built():
    """Discovering a missing credential after building forty messages wastes the
    work and says nothing useful."""
    from tracker.config import Settings

    with pytest.raises(notify.EmailError, match="TRACKER_RESEND_API_KEY"):
        notify.ResendTransport(Settings(resend_api_key=None, notify_from="a@example.com"))


def test_sending_without_a_sender_is_refused():
    """No default in a public repo: a sending address is a real domain, and the
    same rule that keeps the production hostname out of tracked files applies."""
    from pydantic import SecretStr

    from tracker.config import Settings

    with pytest.raises(notify.EmailError, match="TRACKER_NOTIFY_FROM"):
        notify.ResendTransport(Settings(resend_api_key=SecretStr("re_x"), notify_from=""))
