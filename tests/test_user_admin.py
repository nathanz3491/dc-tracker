"""Managing accounts: the admin role, a locked account, signing out everywhere,
changing your own password, and the notice email.

The properties that matter, in the order a mistake would cost:

* an admin route re-reads the role on every request, so a non-admin session is
  refused however the page was reached, and no route can grant admin;
* a disabled account cannot sign in and its open sessions end;
* changing your own password asks for nothing but the new one, ends every other
  session, and keeps the one it was changed in;
* the notice describes the account as it is now, and never carries a password.
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest
from typer.testing import CliRunner

from tracker import account_notice, accounts
from tracker.cli import app
from tracker.db import open_db, session_scope
from tracker.webui.server import Console, Handler

ADMIN, READER, OTHER = "boss@example.com", "reader@example.com", "other@example.com"
PASSWORD = "correct horse battery"

runner = CliRunner()


def _make(db, email, *, admin=False, name=None):
    with session_scope(open_db(db, readonly=False)) as session:
        row = accounts.create(session, email, PASSWORD, name=name)
        if admin:
            accounts.set_admin(session, row, True)
        return row.id


@pytest.fixture
def db(tmp_path, migrated_copy):
    path = migrated_copy(tmp_path / "t.db")
    _make(path, ADMIN, admin=True, name="Boss")
    _make(path, READER, name="Reader")
    _make(path, OTHER)
    return path


@pytest.fixture
def live(db):
    """A console over `db`, re-checking every session against its row on every request."""
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
        yield httpd.server_address, db
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


def call(address, path, method="GET", body=None, cookie=None):
    conn = HTTPConnection(*address, timeout=30)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    response = conn.getresponse()
    raw = response.read().decode("utf-8")
    set_cookie = response.getheader("Set-Cookie") or ""
    conn.close()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw
    return response.status, payload, set_cookie.split(";")[0]


def sign_in(address, email, password=PASSWORD):
    status, payload, cookie = call(
        address, "/api/login", "POST", {"email": email, "password": password}
    )
    return status, payload, cookie


def ids(db):
    with session_scope(open_db(db, readonly=True), commit=False) as session:
        return {row.email: row.id for row in accounts.listing(session)}


# --- the account functions ------------------------------------------------------


def test_changing_an_address_moves_the_identity_and_keeps_the_sessions(db):
    with session_scope(open_db(db, readonly=False)) as session:
        row = accounts.require(session, READER)
        before = accounts.stamp_for(row)
        changes = accounts.update(session, row, email="New.Reader@Example.com")
        assert changes == [f"email {READER} -> New.Reader@Example.com"]
        assert accounts.by_email(session, "new.reader@example.com") is row
        assert accounts.by_email(session, READER) is None
        assert accounts.stamp_for(row) == before, "a session is bound to the password"
        assert row.updated_at is not None


def test_an_address_somebody_else_holds_is_refused(db):
    with session_scope(open_db(db, readonly=False)) as session:
        row = accounts.require(session, READER)
        with pytest.raises(accounts.AccountError, match="already has an account"):
            accounts.update(session, row, email=OTHER.upper())


def test_signing_out_everywhere_stales_every_stamp_and_keeps_the_password(db):
    with session_scope(open_db(db, readonly=False)) as session:
        row = accounts.require(session, READER)
        stamp, hashed = accounts.stamp_for(row), row.password_hash
        accounts.sign_out_everywhere(session, row)
        assert accounts.stamp_for(row) != stamp
        assert row.password_hash == hashed


def test_the_detail_never_carries_the_password(db):
    with session_scope(open_db(db, readonly=True), commit=False) as session:
        detail = accounts.detail(session, accounts.require(session, READER))
    assert detail["email"] == READER and detail["joined"] == "added at the terminal"
    flat = json.dumps(detail)
    assert "scrypt" not in flat and "password" not in flat


# --- signing in, and staying signed in ---------------------------------------------


def test_a_disabled_account_cannot_sign_in_and_its_sessions_end(live):
    address, db = live
    assert sign_in(address, READER)[0] == 200
    _, _, cookie = sign_in(address, READER)
    assert call(address, "/api/dataset", cookie=cookie)[0] == 200

    with session_scope(open_db(db, readonly=False)) as session:
        accounts.set_disabled(session, accounts.require(session, READER), True)

    assert call(address, "/api/dataset", cookie=cookie)[0] == 401, "its session outlived it"
    status, payload, _ = sign_in(address, READER)
    assert status == 403 and "disabled" in payload["error"]
    assert sign_in(address, READER, "wrong password")[0] == 401, "a wrong password says nothing"


def test_changing_your_own_password_needs_only_the_new_one(live):
    address, _ = live
    _, _, here = sign_in(address, READER)
    _, _, elsewhere = sign_in(address, READER)

    status, payload, _ = call(
        address, "/api/account/password", "POST", {"password": "a brand new one"}, cookie=here
    )
    assert status == 200, payload
    assert call(address, "/api/dataset", cookie=here)[0] == 200, "the device it was changed on"
    assert call(address, "/api/dataset", cookie=elsewhere)[0] == 401, "every other device ends"
    assert sign_in(address, READER)[0] == 401
    assert sign_in(address, READER, "a brand new one")[0] == 200


def test_a_short_password_is_refused_and_nothing_changes(live):
    address, _ = live
    _, _, cookie = sign_in(address, READER)
    status, _, _ = call(address, "/api/account/password", "POST", {"password": "x"}, cookie=cookie)
    assert status == 400
    assert sign_in(address, READER)[0] == 200


# --- the admin page -------------------------------------------------------------------


def test_only_an_admin_reaches_the_admin_routes(live):
    address, db = live
    _, _, reader = sign_in(address, READER)
    target = ids(db)[OTHER]
    assert call(address, "/api/admin/users", cookie=reader)[0] == 403
    for verb in ("update", "password", "disable", "enable", "signout", "delete"):
        status, _, _ = call(
            address,
            f"/api/admin/users/{verb}",
            "POST",
            {"id": target, "password": "x" * 8},
            cookie=reader,
        )
        assert status == 403, verb
    assert sign_in(address, OTHER)[0] == 200, "nothing a refused call asked for happened"


def test_the_admin_listing_is_every_account_without_a_hash(live):
    address, _ = live
    _, _, cookie = sign_in(address, ADMIN)
    status, payload, _ = call(address, "/api/admin/users", cookie=cookie)
    assert status == 200
    assert {a["email"] for a in payload["accounts"]} == {ADMIN, READER, OTHER}
    assert "scrypt" not in json.dumps(payload)


def test_an_admin_edits_locks_signs_out_and_deletes(live):
    address, db = live
    _, _, admin = sign_in(address, ADMIN)
    _, _, theirs = sign_in(address, READER)
    target = ids(db)[READER]

    status, payload, _ = call(
        address,
        "/api/admin/users/update",
        "POST",
        {"id": target, "email": "renamed@example.com", "name": "", "watch_all": True},
        cookie=admin,
    )
    assert status == 200, payload
    assert payload["account"]["email"] == "renamed@example.com"
    assert payload["account"]["name"] is None and payload["account"]["watch_all"] is True
    assert len(payload["changes"]) == 3
    assert call(address, "/api/dataset", cookie=theirs)[0] == 200, (
        "an address change keeps sessions"
    )

    assert call(address, "/api/admin/users/signout", "POST", {"id": target}, cookie=admin)[0] == 200
    assert call(address, "/api/dataset", cookie=theirs)[0] == 401

    assert call(address, "/api/admin/users/disable", "POST", {"id": target}, cookie=admin)[0] == 200
    assert sign_in(address, "renamed@example.com")[0] == 403
    assert call(address, "/api/admin/users/enable", "POST", {"id": target}, cookie=admin)[0] == 200
    assert sign_in(address, "renamed@example.com")[0] == 200

    status, payload, _ = call(
        address,
        "/api/admin/users/password",
        "POST",
        {"id": target, "password": "set by the admin"},
        cookie=admin,
    )
    assert status == 200 and sign_in(address, "renamed@example.com", "set by the admin")[0] == 200

    status, payload, _ = call(
        address, "/api/admin/users/delete", "POST", {"id": target}, cookie=admin
    )
    assert status == 200 and payload["deleted"] == "renamed@example.com"
    assert "renamed@example.com" not in ids(db)


def test_an_admin_cannot_lock_or_delete_themselves_from_the_page(live):
    address, db = live
    _, _, admin = sign_in(address, ADMIN)
    me = ids(db)[ADMIN]
    for verb in ("disable", "delete"):
        status, payload, _ = call(
            address, f"/api/admin/users/{verb}", "POST", {"id": me}, cookie=admin
        )
        assert status == 400 and "terminal" in payload["error"], verb
    assert call(address, "/api/dataset", cookie=admin)[0] == 200


def test_signing_yourself_out_everywhere_keeps_this_device(live):
    address, db = live
    _, _, here = sign_in(address, ADMIN)
    _, _, elsewhere = sign_in(address, ADMIN)
    me = ids(db)[ADMIN]
    assert call(address, "/api/admin/users/signout", "POST", {"id": me}, cookie=here)[0] == 200
    assert call(address, "/api/dataset", cookie=here)[0] == 200
    assert call(address, "/api/dataset", cookie=elsewhere)[0] == 401


def test_revoking_admin_at_the_terminal_takes_effect_on_the_next_request(live):
    address, db = live
    _, _, admin = sign_in(address, ADMIN)
    assert call(address, "/api/admin/users", cookie=admin)[0] == 200
    with session_scope(open_db(db, readonly=False)) as session:
        accounts.set_admin(session, accounts.require(session, ADMIN), False)
    assert call(address, "/api/admin/users", cookie=admin)[0] == 403


def test_no_route_grants_admin(live):
    address, db = live
    _, _, admin = sign_in(address, ADMIN)
    target = ids(db)[READER]
    call(address, "/api/admin/users/update", "POST", {"id": target, "admin": True}, cookie=admin)
    with session_scope(open_db(db, readonly=True), commit=False) as session:
        assert not accounts.require(session, READER).is_admin


def test_the_account_and_admin_pages_have_their_own_urls(live):
    address, _ = live
    _, _, cookie = sign_in(address, READER)
    for view in ("account", "admin"):
        status, body, _ = call(address, f"/{view}", cookie=cookie)
        assert status == 200 and f'window.DC_VIEW="{view}"' in body


def test_the_page_is_told_who_is_an_admin(live):
    address, _ = live
    _, _, admin = sign_in(address, ADMIN)
    _, _, reader = sign_in(address, READER)
    assert call(address, "/api/dataset", cookie=admin)[1]["account"]["admin"] is True
    assert call(address, "/api/dataset", cookie=reader)[1]["account"]["admin"] is False


# --- the CLI ----------------------------------------------------------------------


def invoke(db, *args):
    return runner.invoke(app, ["--db", str(db), *args])


def test_the_cli_edits_locks_and_shows(db):
    assert invoke(db, "users", "edit", READER, "--email", "moved@example.com").exit_code == 0
    assert invoke(db, "users", "disable", "moved@example.com").exit_code == 0
    shown = json.loads(invoke(db, "--json", "users", "show", "moved@example.com").output)
    assert shown["disabled"] is True and shown["email"] == "moved@example.com"
    assert invoke(db, "users", "enable", "moved@example.com").exit_code == 0
    assert invoke(db, "users", "admin", "moved@example.com").exit_code == 0
    listed = json.loads(invoke(db, "--json", "users").output)["accounts"]
    moved = next(a for a in listed if a["email"] == "moved@example.com")
    assert moved["admin"] is True and moved["disabled"] is False


def test_an_edit_with_nothing_to_change_is_refused(db):
    result = invoke(db, "users", "edit", READER)
    assert result.exit_code != 0 and "nothing to change" in result.output


# --- the notice email -------------------------------------------------------------------


def test_the_notice_describes_the_account_as_it_is_now_and_escapes_the_note():
    detail = {
        "email": "new@example.com",
        "name": "Ann",
        "disabled": False,
        "watch_all": True,
        "admin": False,
    }
    message = account_notice.render(
        detail, note="<b>moved</b>", console_url="https://console.example"
    )
    for text in (message.html_body, message.text_body):
        assert "new@example.com" in text and "The whole database" in text
        assert "never contains a password" in text
    assert (
        "&lt;b&gt;moved&lt;/b&gt;" in message.html_body and "<b>moved</b>" not in message.html_body
    )
    assert "https://console.example" in message.html_body


def test_notify_sends_to_the_address_typed_not_the_account(db, monkeypatch):
    from tracker import notify

    sent = []

    class Recorder:
        def __init__(self, settings=None):
            pass

        def send(self, *, to, subject, html_body, text_body):
            sent.append((to, subject, text_body))
            return "id-1"

    monkeypatch.setattr(notify, "ResendTransport", Recorder)
    invoke(db, "users", "edit", READER, "--email", "moved@example.com")
    result = invoke(db, "users", "notify", READER, "--about", "moved@example.com", "--note", "hi")
    assert result.exit_code == 0, result.output
    assert [(to, subject) for to, subject, _ in sent] == [(READER, account_notice.SUBJECT)]
    assert "moved@example.com" in sent[0][2] and "hi" in sent[0][2]


def test_notify_preview_sends_nothing(db, monkeypatch):
    from tracker import notify

    def refuse(*args, **kwargs):
        raise AssertionError("a preview reached the transport")

    monkeypatch.setattr(notify, "ResendTransport", refuse)
    result = invoke(db, "users", "notify", "someone@example.com", "--about", READER, "--preview")
    assert result.exit_code == 0 and READER in result.output


def test_show_prints_its_markup_rather_than_the_tags(db):
    result = invoke(db, "users", "show", OTHER)
    assert result.exit_code == 0
    assert "[dim]" not in result.output and "none" in result.output
