"""Accounts: hashing, identity, and the invite that lets somebody make their own.

Three things here are worth more than the rest and are asserted first: a stored
hash never contains the password, verification re-derives with the parameters
*in the row* rather than today's constants, and an invite is single-use. The rest
of the file is the ordinary CRUD and the refusals that have to have readable
messages, because every one of them reaches an operator at a terminal or a
stranger at a login form.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker import accounts, watchlist
from tracker.models import Account, Invite, utcnow

PASSWORD = "correct horse"


def _make(session, email="alice@example.com", password=PASSWORD, **kw) -> Account:
    return accounts.create(session, email, password, **kw)


# --- hashing ---------------------------------------------------------------


def test_the_password_is_not_in_what_gets_stored():
    """The one property the whole scheme exists for."""
    stored = accounts.hash_password(PASSWORD)
    assert PASSWORD not in stored
    assert "horse" not in stored


def test_the_same_password_hashes_differently_every_time():
    """A fresh salt, so two people who pick the same password do not look alike."""
    assert accounts.hash_password(PASSWORD) != accounts.hash_password(PASSWORD)


def test_a_hash_verifies_against_itself_and_nothing_else():
    stored = accounts.hash_password(PASSWORD)
    assert accounts.verify_password(PASSWORD, stored)
    assert not accounts.verify_password(PASSWORD + " ", stored)
    assert not accounts.verify_password("", stored)


def test_the_stored_form_carries_its_own_parameters():
    """Self-describing, so the cost can be raised without a migration.

    The format is `scrypt$n$r$p$salt$hash`. If this ever stops being true,
    `verify_password` starts re-deriving with today's constants and every row
    written under the old ones stops verifying — which locks everybody out at
    once, silently, on a deploy.
    """
    scheme, n, r, p, salt, digest = accounts.hash_password(PASSWORD).split("$")
    assert scheme == "scrypt"
    assert (int(n), int(r), int(p)) == (16384, 8, 1)
    assert salt and digest


def test_verification_uses_the_row_s_parameters_not_the_current_ones():
    """A row written at a lower cost keeps working after the cost is raised."""
    import base64
    import hashlib

    salt = b"0123456789abcdef"
    weak = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=1024, r=8, p=1, dklen=32)
    b64 = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")  # noqa: E731
    stored = f"scrypt$1024$8$1${b64(salt)}${b64(weak)}"
    assert accounts.verify_password(PASSWORD, stored)


@pytest.mark.parametrize(
    "stored",
    ["", "not-a-hash", "scrypt$only$four$parts", "bcrypt$16384$8$1$aaaa$bbbb", "scrypt$x$y$z$a$b"],
)
def test_an_unparseable_hash_reads_as_wrong_password_not_a_crash(stored):
    """This runs on an unauthenticated route, so a bad row must not 500."""
    assert accounts.verify_password(PASSWORD, stored) is False


def test_an_absurdly_long_password_is_refused_before_it_is_hashed():
    """scrypt hashes whatever it is handed, so an unbounded field is a DoS."""
    assert not accounts.verify_password("x" * 5000, accounts.hash_password(PASSWORD))
    with pytest.raises(accounts.AccountError):
        accounts.hash_password("x" * 5000)


# --- emails ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("alice@example.com", "alice@example.com"),
        ("  Alice@Example.COM  ", "alice@example.com"),
        ("A.B+tag@sub.example.co.uk", "a.b+tag@sub.example.co.uk"),
    ],
)
def test_an_address_is_trimmed_and_lowercased(given, expected):
    assert accounts.normalize_email(given) == expected


@pytest.mark.parametrize(
    "given", ["", "   ", "alice", "@example.com", "alice@", "a b@c.d", "a@b@c"]
)
def test_a_thing_that_is_not_an_address_is_refused_with_a_reason(given):
    with pytest.raises(accounts.AccountError) as caught:
        accounts.normalize_email(given)
    assert str(caught.value), "the message is what the schema CHECK cannot give"


def test_case_does_not_make_a_second_account(session):
    _make(session, "Alice@Example.com")
    with pytest.raises(accounts.AccountError, match="already has an account"):
        _make(session, "alice@EXAMPLE.COM")


def test_the_address_is_kept_as_typed_and_matched_folded(session):
    row = _make(session, "  Alice@Example.COM  ")
    assert row.email == "Alice@Example.COM", "display keeps their spelling"
    assert row.email_key == "alice@example.com", "matching does not"
    assert accounts.by_email(session, "ALICE@example.com").id == row.id


# --- accounts --------------------------------------------------------------


def test_zero_accounts_is_a_legitimate_state(session):
    """It is what a fresh install is in, and it means an open console."""
    assert accounts.any_exist(session) is False
    assert accounts.count(session) == 0
    assert accounts.listing(session) == []


def test_creating_one_flips_it(session):
    _make(session)
    assert accounts.any_exist(session) is True


def test_a_short_password_is_refused_at_creation(session):
    with pytest.raises(accounts.AccountError, match="typo rather than a secret"):
        _make(session, password="abc")
    assert accounts.count(session) == 0, "and nothing was written"


def test_verify_returns_the_account_or_none(session):
    row = _make(session)
    assert accounts.verify(session, "alice@example.com", PASSWORD).id == row.id
    assert accounts.verify(session, "alice@example.com", "wrong") is None
    assert accounts.verify(session, "nobody@example.com", PASSWORD) is None


def test_an_unknown_address_still_spends_a_hash(session, monkeypatch):
    """Otherwise the response time says which addresses have accounts.

    Asserted on the call rather than on a clock: timing a scrypt in a test suite
    is how you get a flake on a loaded machine, and the property that matters is
    that the work happens at all.
    """
    calls = []
    real = accounts.verify_password
    monkeypatch.setattr(accounts, "verify_password", lambda p, s: (calls.append(s), real(p, s))[1])
    _make(session)
    accounts.verify(session, "nobody@example.com", PASSWORD)
    assert len(calls) == 1, "a miss must hash too"


def test_setting_a_password_replaces_the_hash(session):
    row = _make(session)
    before = row.password_hash
    accounts.set_password(session, "alice@example.com", "a new one")
    assert row.password_hash != before
    assert accounts.verify(session, "alice@example.com", "a new one") is not None
    assert accounts.verify(session, "alice@example.com", PASSWORD) is None


def test_touch_records_the_sign_in(session):
    row = _make(session)
    assert row.last_seen_at is None
    accounts.touch(session, row)
    assert row.last_seen_at is not None


def test_deleting_an_account_takes_its_watchlist(session):
    """`ON DELETE CASCADE`: a watch is a statement of that person's interest."""
    row = _make(session)
    watchlist.add(session, "xAI", account_id=row.id)
    assert len(watchlist.entries(session)) == 1

    assert accounts.delete(session, "alice@example.com") is True
    session.expire_all()
    assert accounts.count(session) == 0
    assert watchlist.entries(session) == [], "the list went with them"


def test_deleting_an_unknown_account_is_false_not_an_error(session):
    assert accounts.delete(session, "nobody@example.com") is False


def test_an_unknown_user_is_named_along_with_the_ones_that_exist(session):
    """A silent miss on `--user` looks exactly like an empty watchlist."""
    _make(session, "alice@example.com")
    _make(session, "bob@example.com")
    with pytest.raises(accounts.AccountError) as caught:
        accounts.require(session, "carol@example.com")
    message = str(caught.value)
    assert "alice@example.com" in message and "bob@example.com" in message


def test_with_no_accounts_the_message_says_how_to_make_one(session):
    with pytest.raises(accounts.AccountError, match="tracker users add"):
        accounts.require(session, "alice@example.com")


# --- invites ---------------------------------------------------------------


def test_the_code_is_not_stored(session):
    """Only its sha256. The database travels between machines and into backups."""
    row, code = accounts.mint_invite(session, note="carol")
    assert code not in row.code_hash
    assert len(row.code_hash) == 64, "sha256, hex"


def test_redeeming_creates_the_account_and_spends_the_code(session):
    _, code = accounts.mint_invite(session)
    row = accounts.redeem(session, code, "carol@example.com", PASSWORD, name="Carol")
    assert row.email_key == "carol@example.com"
    assert row.name == "Carol"

    invite = session.query(Invite).one()
    assert invite.redeemed_at is not None
    assert invite.redeemed_by == row.id


def test_a_code_cannot_be_used_twice(session):
    _, code = accounts.mint_invite(session)
    accounts.redeem(session, code, "carol@example.com", PASSWORD)
    with pytest.raises(accounts.AccountError, match="not usable"):
        accounts.redeem(session, code, "dave@example.com", PASSWORD)


def test_an_expired_code_is_refused(session):
    row, code = accounts.mint_invite(session)
    row.expires_at = utcnow() - dt.timedelta(seconds=1)
    session.flush()
    with pytest.raises(accounts.AccountError, match="not usable"):
        accounts.redeem(session, code, "carol@example.com", PASSWORD)


def test_an_unknown_code_is_refused(session):
    with pytest.raises(accounts.AccountError, match="not usable"):
        accounts.redeem(session, "no-such-code", "carol@example.com", PASSWORD)


def test_every_refusal_reads_the_same(session):
    """Unknown, expired and spent are one message on purpose.

    This route is unauthenticated on a public URL, and the difference between
    "that code never existed" and "that code was used on Tuesday" is only useful
    to somebody probing.
    """
    _used, used_code = accounts.mint_invite(session)
    accounts.redeem(session, used_code, "carol@example.com", PASSWORD)
    expired, expired_code = accounts.mint_invite(session)
    expired.expires_at = utcnow() - dt.timedelta(days=1)
    session.flush()

    messages = set()
    for code in ("never-existed", used_code, expired_code):
        with pytest.raises(accounts.AccountError) as caught:
            accounts.redeem(session, code, "dave@example.com", PASSWORD)
        messages.add(str(caught.value))
    assert len(messages) == 1, f"three different refusals leak which is which: {messages}"


def test_a_bad_password_does_not_burn_the_code(session):
    """The account is created first, so a typo costs nothing but a retry."""
    _, code = accounts.mint_invite(session)
    with pytest.raises(accounts.AccountError):
        accounts.redeem(session, code, "carol@example.com", "abc")
    # Still good.
    assert accounts.redeem(session, code, "carol@example.com", PASSWORD) is not None


def test_a_bad_address_does_not_burn_the_code(session):
    _, code = accounts.mint_invite(session)
    with pytest.raises(accounts.AccountError):
        accounts.redeem(session, code, "not-an-address", PASSWORD)
    assert accounts.redeem(session, code, "carol@example.com", PASSWORD) is not None


def test_outstanding_skips_the_spent_and_the_expired(session):
    accounts.mint_invite(session, note="live")
    _spent, spent_code = accounts.mint_invite(session, note="spent")
    accounts.redeem(session, spent_code, "carol@example.com", PASSWORD)
    stale, _ = accounts.mint_invite(session, note="stale")
    stale.expires_at = utcnow() - dt.timedelta(days=1)
    session.flush()

    assert [i.note for i in accounts.outstanding(session)] == ["live"]


def test_an_invite_must_last_at_least_a_day(session):
    with pytest.raises(accounts.AccountError, match="at least a day"):
        accounts.mint_invite(session, days=0)


def test_deleting_the_redeemer_leaves_the_invite_readable(session):
    """`ON DELETE SET NULL`, and no paired CHECK, or this delete would fail.

    `redeemed_at` alone is the spent flag precisely so that the audit link can go
    missing without making the row inconsistent.
    """
    _, code = accounts.mint_invite(session)
    row = accounts.redeem(session, code, "carol@example.com", PASSWORD)
    accounts.delete(session, row.email)
    session.expire_all()

    invite = session.query(Invite).one()
    assert invite.redeemed_by is None
    assert invite.redeemed_at is not None, "still spent"
