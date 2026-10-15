"""API keys: made once and shown once, stored only as a hash, and only as good as
the account behind them.

The properties, in the order a mistake would cost:

* nothing stored is a usable key, so a copy of the database grants no access;
* a key stops working the moment its account is disabled, unapproved or gone, and
  the moment it is revoked;
* one account cannot revoke, or see, another's keys;
* the limits a person meets — a name, five active keys — say what to do.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker import accounts, apikeys
from tracker.models import ApiKey


@pytest.fixture
def person(session):
    return accounts.create(session, "reader@example.com", "a-long-password").id


def test_a_key_is_shown_once_and_stored_only_as_its_hash(session, person):
    row, key = apikeys.create(session, person, "laptop")
    assert key.startswith("dct_") and len(key) > 40
    assert row.prefix == key[:8]
    stored = session.get(ApiKey, row.id)
    assert key not in (stored.key_hash, stored.prefix, stored.name)
    assert "key" not in apikeys.as_json(row) and key not in str(apikeys.as_json(row))


def test_a_key_verifies_as_its_account(session, person):
    row, key = apikeys.create(session, person, "server")
    caller = apikeys.verify(session, key)
    assert caller is not None and caller.account_id == person and caller.key_id == row.id
    assert apikeys.verify(session, key + "x") is None
    assert apikeys.verify(session, "not-a-key") is None
    assert apikeys.verify(session, None) is None


def test_a_revoked_key_stops_working_and_stays_listed(session, person):
    row, key = apikeys.create(session, person, "old")
    apikeys.revoke(session, person, row.id)
    assert apikeys.verify(session, key) is None
    listed = apikeys.listed(session, person)
    assert [r.id for r in listed] == [row.id] and listed[0].revoked_at is not None
    apikeys.revoke(session, person, row.id)  # twice is harmless


def test_a_disabled_account_has_no_working_keys(session, person):
    _row, key = apikeys.create(session, person, "server")
    account = accounts.by_id(session, person)
    account.disabled_at = dt.datetime(2026, 10, 15)
    assert apikeys.verify(session, key) is None
    account.disabled_at = None
    assert apikeys.verify(session, key) is not None


def test_only_an_active_account_can_make_a_key(session, person):
    accounts.by_id(session, person).approved_at = None
    with pytest.raises(apikeys.ApiKeyError, match="active account"):
        apikeys.create(session, person, "laptop")


def test_one_account_cannot_revoke_anothers_key(session, person):
    other = accounts.create(session, "other@example.com", "another-long-password").id
    row, key = apikeys.create(session, person, "mine")
    with pytest.raises(apikeys.ApiKeyError, match="no such key"):
        apikeys.revoke(session, other, row.id)
    assert apikeys.verify(session, key) is not None
    assert apikeys.listed(session, other) == []


def test_names_and_the_active_limit(session, person):
    with pytest.raises(apikeys.ApiKeyError, match="name"):
        apikeys.create(session, person, "   ")
    with pytest.raises(apikeys.ApiKeyError, match="at most"):
        apikeys.create(session, person, "x" * 61)
    made = [apikeys.create(session, person, f"key {n}")[0] for n in range(apikeys.MAX_ACTIVE)]
    with pytest.raises(apikeys.ApiKeyError, match="revoke one first"):
        apikeys.create(session, person, "one too many")
    apikeys.revoke(session, person, made[0].id)
    assert apikeys.create(session, person, "now fits")[0].name == "now fits"


def test_last_used_is_written_at_most_every_ten_minutes(session, person):
    row, _key = apikeys.create(session, person, "server")
    t0 = dt.datetime(2026, 10, 15, 12, 0)
    apikeys.touch(session, row.id, now=t0)
    assert row.last_used_at == t0
    apikeys.touch(session, row.id, now=t0 + dt.timedelta(minutes=5))
    assert row.last_used_at == t0
    apikeys.touch(session, row.id, now=t0 + dt.timedelta(minutes=10))
    assert row.last_used_at == t0 + dt.timedelta(minutes=10)
