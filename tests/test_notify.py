"""Email delivery: one message per person per day, and nothing sent to anybody twice.

The load-bearing assertions are `test_one_person_gets_one_email_however_much_moved`
(the reason the feature exists), `test_an_update_is_sent_once_and_never_again` (the
reason it is consistent), and `test_a_fact_written_weeks_after_its_fetch_is_still_sent`
(the bug that made it inconsistent). Everything else protects a rule or the template.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy import select

from tracker import accounts, notify, watchlist
from tracker.feed import Digest, Signal
from tracker.models import Event, NotifyDelivery, NotifyRun, NotifySent, Project, Risk, utcnow

NOW = dt.datetime(2026, 8, 29, 9, 0)
TODAY = dt.date.today()


def no_wait(_seconds: float) -> None:
    """Stands in for `time.sleep`: the pauses are real policy, not test time."""


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
        "happened": TODAY - dt.timedelta(days=2),
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

    def send(self, *, to, subject, html_body, text_body, idempotency_key=None) -> str:
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "html": html_body,
                "text": text_body,
                "key": idempotency_key,
            }
        )
        return f"msg_{len(self.sent)}"


class Refuses(Recorder):
    """Refuses one address for good; delivers everybody else."""

    def __init__(self, refused: str) -> None:
        super().__init__()
        self.refused = refused

    def send(self, *, to, subject, html_body, text_body, idempotency_key=None) -> str:
        if to == self.refused:
            raise notify.EmailError("Resend returned HTTP 422: invalid recipient")
        return super().send(
            to=to,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            idempotency_key=idempotency_key,
        )


def send(session, transport, **kw):
    return notify.send_all(session, transport=transport, sleep=no_wait, **kw)


def _project(session, *, company="Nscale", name="Monarch", city="Point Pleasant", state="WV"):
    project = Project(
        name=name,
        company=company,
        city=city,
        state=state,
        dedup_key=f"{company.lower()}|{name.lower()}|{state}",
        phase="construction",
        confidence=2,
    )
    session.add(project)
    session.flush()
    return project


def _milestone(
    session,
    project,
    *,
    event_type="energized",
    happened: dt.date | None = None,
    recorded: dt.datetime | None = None,
    fetched: dt.datetime | None = None,
) -> Event:
    recorded = recorded or utcnow()
    row = Event(
        project_id=project.id,
        event_date=happened or TODAY - dt.timedelta(days=1),
        event_type=event_type,
        description=f"{project.name} {event_type.replace('_', ' ')}.",
        quote=f"{project.name} was {event_type.replace('_', ' ')} on Friday.",
        created_at=fetched or recorded,
        recorded_at=recorded,
    )
    # Through the relationship, so a project already loaded by an earlier run in
    # this same session sees it — each real run is its own process and session.
    project.events.append(row)
    session.flush()
    return row


def _obstacle(session, project, *, severity="material", category="permitting", **kw) -> Risk:
    row = Risk(
        project_id=project.id,
        category=category,
        severity=severity,
        status="open",
        summary=kw.pop("summary", "The county has not scheduled the rezoning vote."),
        quote="The county has not scheduled a vote on the rezoning.",
        created_at=kw.get("recorded_at", utcnow()),
        recorded_at=kw.pop("recorded_at", utcnow()),
        **kw,
    )
    project.risks.append(row)
    session.flush()
    return row


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
    _milestone(session, project)
    return project


def _reader(session, email="reader@example.com", *, watch=("Nscale",), name="Reader"):
    account = accounts.create(session, email, "correct horse", name=name)
    for entry in watch:
        watchlist.add(session, entry, account_id=account.id)
    return account


# --- the reason this exists ------------------------------------------------------


def test_one_person_gets_one_email_however_much_moved(session):
    """Fourteen updates is one email with fourteen cards.

    A channel that sends one message per change is one people filter into a
    folder, and a filtered channel protects nobody — the same argument
    `feed.notable` makes about the bar for interrupting somebody, one layer out.
    """
    _reader(session)
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
    outcomes = send(session, post)

    assert len(post.sent) == 1, f"expected one message, got {len(post.sent)}"
    assert post.sent[0]["to"] == "reader@example.com"
    assert sum(o.signals for o in outcomes) >= 14
    assert post.sent[0]["html"].count("Monarch") >= 10


def test_each_person_gets_only_their_own_watchlist(session):
    """Two accounts, two lists, two different messages — the property per-user
    watchlists exist for."""
    _reader(session, "a@example.com", watch=("Nscale",))
    _reader(session, "b@example.com", watch=("xAI",))
    _energized(
        session, company="Nscale", city="Point Pleasant", state="WV", name="Monarch", key="n|pp|WV"
    )
    _energized(session, company="xAI", city="Memphis", state="TN", name="Colossus", key="x|m|TN")

    post = Recorder()
    send(session, post)

    assert len(post.sent) == 2
    by_to = {m["to"]: m["html"] for m in post.sent}
    assert "Nscale" in by_to["a@example.com"] and "xAI" not in by_to["a@example.com"]
    assert "xAI" in by_to["b@example.com"] and "Nscale" not in by_to["b@example.com"]


# --- once, and only once ---------------------------------------------------------


def test_an_update_is_sent_once_and_never_again(session):
    """The ledger. Tomorrow's email does not repeat today's, even though the update
    is still recent enough for the catch-up rule to admit it."""
    _reader(session)
    _milestone(session, _project(session))

    post = Recorder()
    first = send(session, post, now=utcnow())
    second = send(session, post, now=utcnow() + dt.timedelta(days=1))

    assert [o.kind for o in first] == ["updates"]
    assert [o.kind for o in second] == ["quiet"], "the second morning has nothing new"
    assert "Monarch energized" not in post.sent[1]["text"]
    assert len(session.scalars(select(NotifySent)).all()) == 1


def test_a_second_run_on_the_same_morning_sends_nothing(session):
    """One email a day, whoever runs the command twice."""
    _reader(session)
    _milestone(session, _project(session))
    post = Recorder()
    send(session, post)
    again = send(session, post)
    assert len(post.sent) == 1
    assert again[0].skipped == "already emailed today"

    forced = send(session, post, force=True)
    assert forced[0].sent, "--force is how an operator re-sends on purpose"
    assert post.sent[1]["key"] != post.sent[0]["key"], (
        "a resend reusing the morning's key would be dropped by the provider as a retry"
    )


def test_a_missed_day_is_caught_up_the_next_morning(session):
    """A run that never happened loses nothing: the window starts at the last email
    actually sent, not at the clock."""
    _reader(session)
    project = _project(session)
    day0 = utcnow() - dt.timedelta(days=3)
    post = Recorder()
    send(session, post, now=day0)
    # Recorded the day after, while the host was down for two mornings; it happened
    # a month ago, so only "recorded since your last email" admits it.
    _milestone(
        session,
        project,
        happened=TODAY - dt.timedelta(days=30),
        recorded=day0 + dt.timedelta(days=1),
    )

    outcomes = send(session, post)
    assert outcomes[0].kind == "updates"
    assert "Monarch energized" in post.sent[-1]["text"]


# --- what goes in ----------------------------------------------------------------


def test_a_fact_written_weeks_after_its_fetch_is_still_sent(session):
    """The bug that made delivery inconsistent: a row carrying a three-week-old fetch
    date landed behind every window that had already run and was never sent."""
    _reader(session)
    _milestone(
        session,
        _project(session),
        happened=TODAY - dt.timedelta(days=20),
        fetched=utcnow() - dt.timedelta(days=21),
        recorded=utcnow() - dt.timedelta(hours=2),
    )
    post = Recorder()
    [outcome] = send(session, post)
    assert outcome.kind == "updates" and outcome.signals == 1


def test_nothing_that_happened_over_45_days_ago_is_sent(session):
    _reader(session)
    project = _project(session)
    _milestone(session, project, happened=TODAY - dt.timedelta(days=44))
    _milestone(
        session, project, event_type="first_customer", happened=TODAY - dt.timedelta(days=46)
    )
    post = Recorder()
    send(session, post)
    assert "Monarch energized" in post.sent[0]["text"]
    assert "first customer" not in post.sent[0]["text"]


def test_an_unsent_update_from_the_past_two_weeks_is_caught_up(session):
    """Recorded before the last email, never sent — a company just added to the
    watchlist, say. Two weeks is the catch-up; older than that stays unsent."""
    account = _reader(session, watch=("xAI",))
    nscale = _project(session)
    post = Recorder()
    send(session, post, now=utcnow() - dt.timedelta(days=1))
    _milestone(
        session,
        nscale,
        happened=TODAY - dt.timedelta(days=8),
        recorded=utcnow() - dt.timedelta(days=5),
    )
    _milestone(
        session,
        nscale,
        event_type="first_customer",
        happened=TODAY - dt.timedelta(days=20),
        recorded=utcnow() - dt.timedelta(days=5),
    )
    watchlist.add(session, "Nscale", account_id=account.id)

    send(session, post)
    text = post.sent[-1]["text"]
    assert "Monarch energized" in text
    assert "first customer" not in text


def test_a_schedule_whose_date_passed_is_not_caught_up_as_news(session):
    """ "Expected online September 20", read in June, is not an energisation on the
    21st. Only a milestone recorded on or after its own date is caught up."""
    _reader(session)
    post = Recorder()
    send(session, post, now=utcnow() - dt.timedelta(days=1))
    _milestone(
        session,
        _project(session),
        happened=TODAY - dt.timedelta(days=3),
        recorded=utcnow() - dt.timedelta(days=40),
    )
    [outcome] = send(session, post)
    assert outcome.kind == "quiet"


def test_the_choice_is_pure_and_judges_an_undated_update_by_when_we_recorded_it():
    old = signal(
        kind="obstacle_opened",
        sign="bad",
        label="permitting",
        happened=None,
        at=dt.datetime.combine(TODAY - dt.timedelta(days=60), dt.time()),
    )
    fresh = signal(
        kind="obstacle_opened",
        sign="bad",
        label="water",
        happened=None,
        at=dt.datetime.combine(TODAY, dt.time()),
    )
    since = dt.datetime.combine(TODAY - dt.timedelta(days=90), dt.time())
    chosen = notify.choose((old, fresh), since=since, sent=set())
    assert [s.label for s in chosen] == ["water"]


# --- who gets nothing ------------------------------------------------------------


def test_an_account_with_no_watchlist_is_mailed_nothing(session):
    accounts.create(session, "unconfigured@example.com", "correct horse")
    _energized(session, company="xAI", city="Memphis", state="TN", name="Colossus", key="x|m|TN")
    post = Recorder()
    [outcome] = send(session, post)
    assert post.sent == []
    assert outcome.skipped == "no watchlist"


def test_an_account_watching_everything_is_not_mailed(session):
    """Wanting the whole database on a *page* is a reasonable thing to turn on;
    having it mailed every morning is a firehose, and mail arrives uninvited."""
    account = accounts.create(session, "all@example.com", "correct horse")
    account.watch_all = True
    session.flush()
    _energized(session, company="xAI", city="Memphis", state="TN", name="Colossus", key="x|m|TN")
    post = Recorder()
    [outcome] = send(session, post)
    assert post.sent == []
    assert outcome.skipped == "watches the whole database — not mailed"


def test_a_disabled_account_is_not_mailed(session):
    account = _reader(session)
    _milestone(session, _project(session))
    accounts.set_disabled(session, account, True)
    post = Recorder()
    [outcome] = send(session, post)
    assert post.sent == [] and outcome.skipped == "account disabled"


# --- a day with no news ----------------------------------------------------------


def test_a_quiet_day_sends_what_to_watch_for(session):
    """Nothing new is still an email: every open blocker on every followed project,
    and what would move it. A quiet day must not look like a broken service."""
    _reader(session)
    project = _project(session)
    _obstacle(session, project, recorded_at=utcnow() - dt.timedelta(days=120))

    post = Recorder()
    [outcome] = send(session, post, console_url="https://console.example")

    assert outcome.kind == "quiet"
    message = post.sent[0]
    assert message["subject"].startswith("No new updates today")
    assert "rezoning vote" in message["html"]
    assert "Would clear it" in message["text"], "the milestone that would clear it"
    assert "https://console.example/watch-for" in message["html"]


def test_a_day_with_news_carries_a_short_watch_list_and_the_week(session):
    _reader(session)
    project = _project(session)
    _milestone(session, project, event_type="first_customer")
    _obstacle(session, project, recorded_at=utcnow() - dt.timedelta(days=120))

    post = Recorder()
    send(session, post, console_url="https://console.example")

    html = post.sent[0]["html"]
    assert "What to watch for" in html
    assert "https://console.example/watch-for" in html
    assert "https://console.example/updates" in html, "the week, for anybody who skipped a day"


# --- when it goes wrong ----------------------------------------------------------


def test_one_refused_person_does_not_stop_the_rest(session):
    """It used to: the first refusal ended the run, and everybody after it in the
    list got nothing."""
    _reader(session, "first@example.com")
    _reader(session, "second@example.com")
    _milestone(session, _project(session))

    post = Refuses("first@example.com")
    outcomes = send(session, post)

    assert [o.failed for o in outcomes] == [True, False]
    assert [m["to"] for m in post.sent] == ["second@example.com"]
    failed = session.scalars(select(NotifyDelivery).where(NotifyDelivery.status == "failed")).one()
    assert failed.email == "first@example.com" and "422" in failed.error


def test_a_failed_persons_updates_are_still_owed_the_next_day(session):
    _reader(session, "first@example.com")
    _milestone(session, _project(session))
    send(session, Refuses("first@example.com"))

    post = Recorder()
    [outcome] = send(session, post, now=utcnow() + dt.timedelta(days=1))
    assert outcome.kind == "updates"
    assert "Monarch energized" in post.sent[0]["text"]


def test_a_rate_limit_is_retried_with_the_same_key(session):
    """A retry must not deliver twice, so every attempt carries one idempotency key."""

    class Flaky(Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.keys: list[str] = []

        def send(self, *, to, subject, html_body, text_body, idempotency_key=None) -> str:
            self.keys.append(idempotency_key)
            if len(self.keys) < 3:
                raise notify.TransientEmailError("HTTP 429", retry_after=1)
            return super().send(
                to=to,
                subject=subject,
                html_body=html_body,
                text_body=text_body,
                idempotency_key=idempotency_key,
            )

    _reader(session)
    _milestone(session, _project(session))
    post = Flaky()
    waits: list[float] = []
    [outcome] = notify.send_all(session, transport=post, sleep=waits.append)

    assert outcome.sent and outcome.attempts == 3
    assert len(set(post.keys)) == 1 and post.keys[0].startswith("dct-")
    assert waits[:2] == [2.0, 8.0]


def test_the_administrator_is_told_when_somebody_was_not_delivered_to(session):
    admin = accounts.create(session, "admin@example.com", "correct horse")
    admin.is_admin = True
    _reader(session, "client@example.com")
    _milestone(session, _project(session))

    post = Refuses("client@example.com")
    send(session, post)

    alerts = [m for m in post.sent if m["to"] == "admin@example.com" and "problem" in m["subject"]]
    assert len(alerts) == 1
    assert "client@example.com" in alerts[0]["text"]


def test_every_run_is_recorded(session):
    _reader(session)
    _milestone(session, _project(session))
    send(session, Recorder())
    run = session.scalars(select(NotifyRun)).one()
    assert run.finished_at is not None and run.sent == 1 and run.failed == 0


def test_a_dry_run_records_nothing(session):
    _reader(session)
    _milestone(session, _project(session))
    send(session, Recorder(), record=False)
    assert session.scalars(select(NotifyDelivery)).all() == []
    assert session.scalars(select(NotifyRun)).all() == []


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

    text = notify.render_text(brief_of(*many), many)
    assert text.count("Site ") >= 40


def test_send_all_puts_every_update_in_the_one_message(session):
    """The same guarantee end to end, not only in the template."""
    _reader(session)
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
    outcomes = send(session, post)

    assert len(post.sent) == 1
    assert outcomes[0].signals == 25, "every signal is counted as sent"
    for n in range(25):
        assert f"Campus {n}" in post.sent[0]["html"]


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


def _resend(monkeypatch, response: httpx.Response) -> list[dict]:
    from pydantic import SecretStr

    from tracker.config import Settings

    calls: list[dict] = []

    def post(url, *, json, headers, timeout):
        calls.append({"url": url, "json": json, "headers": headers})
        return response

    monkeypatch.setattr(notify.httpx, "post", post)
    transport = notify.ResendTransport(
        Settings(resend_api_key=SecretStr("re_x"), notify_from="a@example.com")
    )
    calls.append(transport)
    return calls


def test_the_idempotency_key_reaches_resend(monkeypatch):
    calls = _resend(monkeypatch, httpx.Response(200, json={"id": "abc"}))
    transport = calls.pop()
    assert (
        transport.send(
            to="b@example.com", subject="s", html_body="h", text_body="t", idempotency_key="dct-1"
        )
        == "abc"
    )
    assert calls[0]["headers"]["Idempotency-Key"] == "dct-1"


def test_a_rate_limit_is_transient_and_a_bad_request_is_not(monkeypatch):
    calls = _resend(monkeypatch, httpx.Response(429, headers={"retry-after": "3"}, text="slow"))
    with pytest.raises(notify.TransientEmailError) as caught:
        calls.pop().send(to="b@example.com", subject="s", html_body="h", text_body="t")
    assert caught.value.retry_after == 3.0

    calls = _resend(monkeypatch, httpx.Response(422, text="missing field"))
    with pytest.raises(notify.EmailError) as caught:
        calls.pop().send(to="b@example.com", subject="s", html_body="h", text_body="t")
    assert not isinstance(caught.value, notify.TransientEmailError)


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
