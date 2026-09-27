"""Signing up without an invite: a confirmed address, then an administrator's yes.

The properties, in the order a mistake would cost:

* nobody reads the dataset on a plain sign-up alone — it needs the mailed link
  clicked *and* an administrator's approval, and starts without the model panels;
* the forms never say which addresses have accounts — not in the answer, not in
  whether an email was sent from the form's point of view;
* a mailed link is single use, expires, is stored only hashed, is built from the
  configured console address and never from the request, and a guessed one counts
  toward the lockout;
* nobody can be mailed without limit through the forms;
* an invite code still signs somebody straight in, and every account that existed
  before this keeps full access, model panels included.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from http.client import HTTPConnection
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select, text

from tracker import accounts
from tracker.db import open_db, session_scope
from tracker.models import Account, AccountToken, utcnow
from tracker.webui.server import Console, Handler

ADMIN = "boss@example.com"
PASSWORD = "correct horse battery"
CONSOLE_URL = "https://console.example"


@pytest.fixture
def db(tmp_path, migrated_copy):
    path = migrated_copy(tmp_path / "t.db")
    with session_scope(open_db(path, readonly=False)) as session:
        row = accounts.create(session, ADMIN, PASSWORD, name="Boss")
        accounts.set_admin(session, row, True)
    return path


def _session(db):
    return session_scope(open_db(db, readonly=False))


# --- the account functions -------------------------------------------------------


def test_a_sign_up_cannot_sign_in_until_confirmed_and_approved(db):
    with _session(db) as session:
        made = accounts.sign_up(session, "New@Example.com", PASSWORD, name="New")
        row = made.pending
        assert row is not None and made.token and made.existing is None
        assert accounts.status(row) == "unconfirmed"
        assert not row.ai_allowed and row.approved_at is None
        with pytest.raises(accounts.AccountError, match="not confirmed"):
            accounts.approve(session, row)

        confirmed, first = accounts.confirm_email(session, made.token)
        assert confirmed is row and first and accounts.status(row) == "pending"
        again, first_again = accounts.confirm_email(session, made.token)
        assert again is row and not first_again, "a second click is a success, not news"

        assert accounts.approve(session, row) is True
        assert accounts.status(row) == "active" and not row.ai_allowed


def test_links_are_stored_only_as_hashes(db):
    with _session(db) as session:
        made = accounts.sign_up(session, "new@example.com", PASSWORD)
        stored = session.scalars(select(AccountToken.token_hash)).all()
    assert made.token not in stored and len(stored) == 1


def test_signing_up_again_replaces_an_unconfirmed_request(db):
    """Somebody who started a sign-up with your address cannot hold it."""
    with _session(db) as session:
        first = accounts.sign_up(session, "new@example.com", "squatter-pass")
        second = accounts.sign_up(session, "new@example.com", PASSWORD)
        assert second.pending is first.pending
        assert accounts.verify(session, "new@example.com", PASSWORD) is second.pending
        with pytest.raises(accounts.AccountError):
            accounts.confirm_email(session, first.token)
        accounts.confirm_email(session, second.token)


def test_signing_up_with_a_taken_address_changes_nothing_and_offers_a_reset(db):
    with _session(db) as session:
        made = accounts.sign_up(session, ADMIN, "some-other-pass")
        assert made.pending is None and made.existing is not None and made.existing_token
        assert accounts.verify(session, ADMIN, PASSWORD) is not None


def test_an_address_is_sent_only_so_many_links_an_hour(db):
    with _session(db) as session:
        row = accounts.sign_up(session, "new@example.com", PASSWORD).pending
        for _ in range(accounts.MAX_LINKS_PER_HOUR - 1):
            accounts.issue_link(session, row, "confirm")
        with pytest.raises(accounts.Throttled):
            accounts.issue_link(session, row, "confirm")


def test_an_expired_confirmation_link_is_refused(db):
    with _session(db) as session:
        made = accounts.sign_up(session, "new@example.com", PASSWORD)
        for token in session.scalars(select(AccountToken)):
            token.expires_at = utcnow() - dt.timedelta(minutes=1)
        with pytest.raises(accounts.AccountError, match="not usable"):
            accounts.confirm_email(session, made.token)


def test_a_reset_link_sets_the_password_once_and_ends_every_session(db):
    with _session(db) as session:
        row = accounts.require(session, ADMIN)
        before = accounts.stamp_for(row)
        _, token = accounts.request_reset(session, ADMIN)
        with pytest.raises(accounts.AccountError, match="characters"):
            accounts.finish_reset(session, token, "short")
        accounts.finish_reset(session, token, "a brand new one")
        assert accounts.verify(session, ADMIN, "a brand new one") is row
        assert accounts.stamp_for(row) != before, "the old sessions no longer match"
        with pytest.raises(accounts.AccountError, match="not usable"):
            accounts.finish_reset(session, token, "yet another one")


def test_no_reset_for_an_unknown_or_disabled_address(db):
    with _session(db) as session:
        assert accounts.request_reset(session, "nobody@example.com") is None
        accounts.set_disabled(session, accounts.require(session, ADMIN), True)
        assert accounts.request_reset(session, ADMIN) is None


def test_week_old_unconfirmed_sign_ups_are_deleted(db):
    with _session(db) as session:
        stale = accounts.sign_up(session, "stale@example.com", PASSWORD).pending
        waiting = accounts.sign_up(session, "waiting@example.com", PASSWORD)
        accounts.confirm_email(session, waiting.token)
        for row in (stale, waiting.pending):
            row.created_at = utcnow() - dt.timedelta(days=accounts.UNCONFIRMED_DAYS + 1)
        assert accounts.expire_unconfirmed(session) == 1
        assert accounts.by_email(session, "stale@example.com") is None
        assert accounts.by_email(session, "waiting@example.com") is not None


def test_an_invite_or_the_terminal_lets_somebody_straight_in_with_the_panels(db):
    with _session(db) as session:
        accounts.sign_up(session, "squat@example.com", "squatter-pass")
        _, code = accounts.mint_invite(session, note="friend")
        row = accounts.redeem(session, code, "squat@example.com", PASSWORD)
        assert accounts.status(row) == "active" and row.ai_allowed
        made = accounts.create(session, "terminal@example.com", PASSWORD)
        assert accounts.status(made) == "active" and made.ai_allowed


def test_accounts_from_before_the_migration_keep_full_access(tmp_path):
    from tracker.db import discover_migrations, make_engine, run_migrations

    engine = make_engine(tmp_path / "old.db")
    run_migrations(engine, [m for m in discover_migrations() if m.version < 30])
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO account (email, email_key, password_hash, created_at) "
                "VALUES ('old@example.com', 'old@example.com', 'x', '2026-09-01 00:00:00')"
            )
        )
    run_migrations(engine)
    with session_scope(engine, commit=False) as session:
        row = session.scalar(select(Account))
        assert accounts.status(row) == "active" and row.ai_allowed and not row.self_signup
    engine.dispose()


def test_the_morning_email_skips_an_account_nobody_approved(db):
    from tracker import notify

    with _session(db) as session:
        made = accounts.sign_up(session, "new@example.com", PASSWORD)
        accounts.confirm_email(session, made.token)
        assert notify.compose(session, made.pending) == "not approved yet"


# --- the forms, over HTTP -----------------------------------------------------------


@pytest.fixture
def mailed(monkeypatch):
    """Mail sent, captured instead of posted, and a console that can send it."""
    from tracker.config import get_settings

    monkeypatch.setenv("TRACKER_NOTIFY_CONSOLE_URL", CONSOLE_URL)
    monkeypatch.setenv("TRACKER_RESEND_API_KEY", "re_test_not_real")
    monkeypatch.setenv("TRACKER_NOTIFY_FROM", "dc-tracker <console@console.example>")
    get_settings.cache_clear()
    sent: list[tuple[str, object]] = []
    monkeypatch.setattr(
        Console, "send_mail", lambda self, to, notice, key=None: sent.append((to, notice))
    )
    yield sent
    get_settings.cache_clear()


@pytest.fixture
def live(db):
    from http.server import ThreadingHTTPServer

    console = Console(db)
    console.gate.session_confirm_s = 0
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    try:
        yield httpd.server_address, console
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


def call(address, path, method="GET", body=None, cookie=None, headers=None):
    conn = HTTPConnection(*address, timeout=30)
    sent = {"Content-Type": "application/json", **(headers or {})}
    if cookie:
        sent["Cookie"] = cookie
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=sent)
    response = conn.getresponse()
    raw = response.read().decode("utf-8")
    cookie_out = (response.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    try:
        return response.status, json.loads(raw), cookie_out
    except ValueError:
        return response.status, raw, cookie_out


def _token(notice) -> str:
    """The `t=` of the one link in a mailed notice's text part."""
    for word in notice.text_body.split():
        if "?t=" in word:
            return parse_qs(urlsplit(word).query)["t"][0]
    raise AssertionError("no link in the message")


def test_the_whole_plain_sign_up_over_http(live, mailed):
    address, _ = live
    status, body, cookie = call(
        address,
        "/api/signup",
        "POST",
        {"email": "new@example.com", "password": PASSWORD, "name": "New"},
    )
    assert status == 200 and not cookie and "Check your inbox" in body["message"]
    ((to, notice),) = mailed
    assert to == "new@example.com" and f"{CONSOLE_URL}/confirm?t=" in notice.text_body

    status, body, _ = call(
        address, "/api/login", "POST", {"email": "new@example.com", "password": PASSWORD}
    )
    assert status == 403 and "Confirm your email" in body["error"]

    status, body, _ = call(address, "/api/confirm", "POST", {"token": _token(notice)})
    assert status == 200 and body["status"] == "pending"
    assert [to for to, _ in mailed[1:]] == [ADMIN], "the administrators are told, once"
    call(address, "/api/confirm", "POST", {"token": _token(notice)})
    assert len(mailed) == 2

    status, body, _ = call(
        address, "/api/login", "POST", {"email": "new@example.com", "password": PASSWORD}
    )
    assert status == 403 and "waiting for an administrator" in body["error"]

    _, _, admin = call(address, "/api/login", "POST", {"email": ADMIN, "password": PASSWORD})
    _, listing, _ = call(address, "/api/admin/users", cookie=admin)
    new_id = next(a["id"] for a in listing["accounts"] if a["email"] == "new@example.com")
    status, body, _ = call(
        address, "/api/admin/users/approve", "POST", {"id": new_id}, cookie=admin
    )
    assert status == 200 and mailed[-1][0] == "new@example.com"

    # The approval links the sign-in page, not the root — which is the public front
    # page for somebody not yet signed in — and says where a forgotten password goes.
    approval = mailed[-1][1].text_body
    assert f"{CONSOLE_URL}/signin" in approval and f"{CONSOLE_URL}/forgot" in approval

    status, _, reader = call(
        address, "/api/login", "POST", {"email": "new@example.com", "password": PASSWORD}
    )
    assert status == 200 and reader


def test_a_taken_address_gets_the_same_answer(live, mailed):
    address, _ = live
    new = call(address, "/api/signup", "POST", {"email": "new@example.com", "password": PASSWORD})
    taken = call(address, "/api/signup", "POST", {"email": ADMIN, "password": PASSWORD})
    assert new[:2] == taken[:2]
    assert mailed[1][0] == ADMIN and "already have" in mailed[1][1].subject


def test_a_disabled_address_is_sent_nothing_and_answered_the_same(live, mailed, db):
    """A reset link a disabled account could not use would be an email that lies."""
    address, _ = live
    with _session(db) as session:
        locked = accounts.create(session, "locked@example.com", PASSWORD)
        accounts.set_disabled(session, locked, True)
    new = call(address, "/api/signup", "POST", {"email": "new@example.com", "password": PASSWORD})
    taken = call(
        address, "/api/signup", "POST", {"email": "locked@example.com", "password": PASSWORD}
    )
    assert new[:2] == taken[:2]
    assert [to for to, _ in mailed] == ["new@example.com"]


def test_a_waiting_account_is_not_told_to_just_sign_in(db):
    from tracker import account_mail

    waiting = account_mail.already_registered(CONSOLE_URL, "t", waiting=True)
    assert "waiting for an administrator" in waiting.text_body
    assert "just sign in" not in waiting.text_body
    assert "just sign in" in account_mail.already_registered(CONSOLE_URL, "t").text_body


def test_tab_goes_from_email_to_password_not_to_the_reset_link():
    """Someone typing their way through the form must not send a password to a link."""
    from tracker.webui import assets

    page = (assets.PUBLIC_ROOT / "signin.html").read_text(encoding="utf-8")
    assert page.index('id="email"') < page.index('id="password"') < page.index('href="/forgot"')


def test_an_invite_code_still_signs_straight_in(live, mailed, db):
    address, _ = live
    with _session(db) as session:
        _, code = accounts.mint_invite(session)
    status, _, cookie = call(
        address,
        "/api/signup",
        "POST",
        {"email": "friend@example.com", "password": PASSWORD, "code": code},
    )
    assert status == 200 and cookie and not mailed


def test_an_invite_code_brings_you_back_where_you_started(live, mailed, db):
    """/register carries `next` from the link that sent somebody there, as /signin
    does, so a code redeemed from a signed-out deep link lands on that page."""
    address, _ = live
    with _session(db) as session:
        _, code = accounts.mint_invite(session)
    status, body, cookie = call(
        address,
        "/api/signup",
        "POST",
        {"email": "friend@example.com", "password": PASSWORD, "code": code, "next": "/watch-for"},
    )
    assert status == 200 and body["next"] == "/watch-for" and cookie


def test_forgot_password_answers_the_same_and_the_link_signs_in(live, mailed):
    address, _ = live
    _, _, old_session = call(address, "/api/login", "POST", {"email": ADMIN, "password": PASSWORD})
    unknown = call(address, "/api/forgot", "POST", {"email": "nobody@example.com"})
    known = call(address, "/api/forgot", "POST", {"email": ADMIN})
    assert unknown[:2] == known[:2] and len(mailed) == 1
    ((_, notice),) = mailed
    assert f"{CONSOLE_URL}/reset?t=" in notice.text_body

    status, body, cookie = call(
        address, "/api/reset", "POST", {"token": _token(notice), "password": "a new one here"}
    )
    assert status == 200 and body["signed_in"] and cookie
    assert call(address, "/api/dataset", cookie=old_session)[0] == 401, "other sessions end"
    assert (
        call(address, "/api/login", "POST", {"email": ADMIN, "password": "a new one here"})[0]
        == 200
    )


def test_links_come_from_the_setting_never_from_the_request(live, mailed):
    address, _ = live
    call(address, "/api/forgot", "POST", {"email": ADMIN}, headers={"Host": "attacker.example"})
    assert "attacker.example" not in mailed[0][1].text_body


def test_without_mail_set_up_the_forms_say_so(live, monkeypatch, db):
    """And why, as a `reason` the page acts on — it moves to the invite-code field,
    the one way in that still works — rather than a sentence it would have to match."""
    from tracker.config import get_settings

    get_settings.cache_clear()
    address, _ = live
    for route, body in (
        ("/api/signup", {"email": "a@example.com", "password": PASSWORD}),
        ("/api/forgot", {"email": ADMIN}),
    ):
        status, payload, _ = call(address, route, "POST", body)
        assert status == 503 and "can't send email" in payload["error"]
        assert payload["reason"] == "mail_off"

    # Mail works, but nobody could approve a sign-up.
    monkeypatch.setenv("TRACKER_NOTIFY_CONSOLE_URL", CONSOLE_URL)
    monkeypatch.setenv("TRACKER_RESEND_API_KEY", "re_test_not_real")
    monkeypatch.setenv("TRACKER_NOTIFY_FROM", "dc-tracker <console@console.example>")
    monkeypatch.setattr(Console, "send_mail", lambda self, to, notice, key=None: None)
    get_settings.cache_clear()
    try:
        with _session(db) as session:
            accounts.set_admin(session, accounts.require(session, ADMIN), False)
        status, payload, _ = call(
            address, "/api/signup", "POST", {"email": "a@example.com", "password": PASSWORD}
        )
        assert status == 503 and "isn't taking sign-ups" in payload["error"]
        assert payload["reason"] == "no_admin"
    finally:
        get_settings.cache_clear()


def test_one_visitor_cannot_mail_without_limit(live, mailed):
    address, console = live
    for i in range(console.gate.mail_max + 3):
        status, _, _ = call(
            address, "/api/signup", "POST", {"email": f"n{i}@example.com", "password": PASSWORD}
        )
        assert status == 200, "the answer never changes"
    assert len(mailed) == console.gate.mail_max


def test_past_the_mail_budget_a_sign_up_writes_nothing(live, mailed, db):
    address, console = live
    console.gate.mail_max = 0
    status, body, _ = call(
        address, "/api/signup", "POST", {"email": "n@example.com", "password": PASSWORD}
    )
    assert status == 200 and "Check your inbox" in body["message"]
    with _session(db) as session:
        assert accounts.by_email(session, "n@example.com") is None


def test_a_sign_up_confirmed_by_a_reset_link_still_reaches_the_admins(live, mailed, db):
    """Never clicked the confirmation, used "Forgot password?" instead."""
    address, _ = live
    with _session(db) as session:
        accounts.sign_up(session, "new@example.com", PASSWORD)
        _, token = accounts.request_reset(session, "new@example.com")
    status, body, _ = call(
        address, "/api/reset", "POST", {"token": token, "password": "a new one here"}
    )
    assert (
        status == 200
        and not body["signed_in"]
        and "waiting for an administrator" in body["message"]
    )
    assert [to for to, _ in mailed] == [ADMIN]


def test_a_disabled_admin_is_not_told_about_sign_ups(live, mailed, db):
    address, _ = live
    with _session(db) as session:
        other = accounts.create(session, "second@example.com", PASSWORD)
        accounts.set_admin(session, other, True)
        accounts.set_disabled(session, other, True)
        made = accounts.sign_up(session, "new@example.com", PASSWORD)
    call(address, "/api/confirm", "POST", {"token": made.token})
    assert [to for to, _ in mailed] == [ADMIN]


def test_a_guessed_link_counts_toward_the_lockout(live, mailed):
    address, console = live
    for _ in range(console.gate.max_failures):
        call(address, "/api/confirm", "POST", {"token": "guess"})
    status, _, _ = call(address, "/api/confirm", "POST", {"token": "guess"})
    assert status == 429


def _get(address, path, cookie=None):
    """`(status, headers, text)` of a GET, for the header assertions `call` drops."""
    conn = HTTPConnection(*address, timeout=30)
    conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
    response = conn.getresponse()
    text = response.read().decode("utf-8")
    conn.close()
    return response.status, dict(response.getheaders()), text


def test_a_mailed_link_opens_its_own_page(live):
    """Each link opens the page for what it does, and neither spends its token on
    load. The token is in the address, so the page is never cached and never sent
    on as a referrer."""
    address, _ = live
    _, _, signed_in = call(address, "/api/login", "POST", {"email": ADMIN, "password": PASSWORD})
    for path, page, words in (
        ("/reset?t=abc", "reset", "Choose a new password"),
        ("/confirm?t=abc", "confirm", "Confirm my email"),
    ):
        status, headers, body = _get(address, path)
        assert status == 200 and f'data-page="{page}"' in body and words in body, path
        assert "no-store" in headers["Cache-Control"], path
        assert headers["Referrer-Policy"] == "no-referrer", path
        # The link is somebody's whether or not this browser is signed in.
        assert _get(address, path, cookie=signed_in)[0] == 200, path

    # No token, nothing to do here: each goes to the page that mails one.
    for path, where in (("/reset", "/forgot"), ("/confirm", "/signin")):
        status, headers, _ = _get(address, path)
        assert status == 303 and headers["Location"] == where, path


def test_every_account_email_links_the_page_it_is_about():
    """Never the console's root, which is the public front page for anybody signed out."""
    from tracker import account_mail

    approved = account_mail.approved(CONSOLE_URL, "New")
    for part in (approved.text_body, approved.html_body):
        assert f"{CONSOLE_URL}/signin" in part
        assert f"{CONSOLE_URL}/forgot" in part, "the footer says where a lost password goes"
    assert f'href="{CONSOLE_URL}"' not in approved.html_body
    assert f'href="{CONSOLE_URL}/"' not in approved.html_body

    waiting = account_mail.pending_for_admin(CONSOLE_URL, "new@example.com", "New")
    assert f'href="{CONSOLE_URL}/admin"' in waiting.html_body
    assert f"{CONSOLE_URL}/confirm?t=tok" in account_mail.confirm(CONSOLE_URL, "tok").text_body
    for notice in (
        account_mail.reset(CONSOLE_URL, "tok"),
        account_mail.already_registered(CONSOLE_URL, "tok"),
    ):
        assert f"{CONSOLE_URL}/reset?t=tok" in notice.text_body, notice.subject


def test_the_panels_follow_the_account_switch(live, mailed, db):
    from tracker.models import Project

    address, _ = live
    with _session(db) as session:
        made = accounts.sign_up(session, "new@example.com", PASSWORD)
        accounts.confirm_email(session, made.token)
        accounts.approve(session, made.pending)
        new_id = made.pending.id
        project = Project(
            name="Hillsboro Campus", company="STACK", city="Hillsboro", state="OR", dedup_key="k"
        )
        session.add(project)
        session.flush()
        project_id = project.id
    _, _, cookie = call(
        address, "/api/login", "POST", {"email": "new@example.com", "password": PASSWORD}
    )
    assert call(address, "/api/dataset", cookie=cookie)[1]["allow_ai"] is False
    # No `confirm` field: a reader who may use the panels is told to re-send with
    # one (400); a reader who may not is refused before that (403).
    status, body, _ = call(address, "/api/infer", "POST", {"project_id": project_id}, cookie=cookie)
    assert status == 403 and "not switched on" in body["error"]

    _, _, admin = call(address, "/api/login", "POST", {"email": ADMIN, "password": PASSWORD})
    call(address, "/api/admin/users/update", "POST", {"id": new_id, "ai": True}, cookie=admin)
    assert call(address, "/api/dataset", cookie=cookie)[1]["allow_ai"] is True
    status, _, _ = call(address, "/api/infer", "POST", {"project_id": project_id}, cookie=cookie)
    assert status == 400
