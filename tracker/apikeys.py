"""Keys for the read-only public API: making them, listing them, revoking them,
and checking one that arrives on a request.

A key is `dct_` followed by 43 URL-safe characters (256 random bits). Its owner
sees it once, when it is made; the database keeps its sha256 and the first few
characters (`prefix`), so the owner can tell their keys apart and nobody holding
a copy of the database holds a working key. See migration 0036.

**A key reads as its account.** It is refused when the account is disabled, not
yet approved, or gone, checked on every request rather than when the key was
made — so switching an account off at the admin page switches its keys off at
once, without anybody having to remember they exist.

**API access is a separate switch on the account** (`api_allowed`, 0037). With it
off the keys still verify, so the caller can be told *why* it is refused rather
than that its key is wrong, and they work again the moment it is switched back
on. The account cannot make new keys meanwhile.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker import accounts
from tracker.models import ApiKey, utcnow

#: What every key starts with, so one pasted somewhere it should not be is
#: recognisable as ours by a person or a secret scanner.
KEY_PREFIX = "dct_"

#: Random bytes per key: 256 bits, so a plain sha256 is a safe way to store it.
_KEY_BYTES = 32

#: Characters of the key kept in the clear for display — `dct_` and four more.
PREFIX_SHOWN = 8

#: Active keys one account may hold at a time. Enough for a laptop, a server and
#: a spare while rotating; few enough that a list of them stays readable.
MAX_ACTIVE = 5

#: Longest name a key may be given.
MAX_NAME = 60

#: How stale `last_used_at` may get before a request writes it again. Every
#: request writing it would put a write on every read, against a database that
#: takes one writer at a time; to the minute is more than a person needs.
TOUCH_EVERY = dt.timedelta(minutes=10)


class ApiKeyError(ValueError):
    """A key request refused, with the reason a person can act on."""


@dataclass(frozen=True)
class Caller:
    """Who a verified key speaks for."""

    key_id: int
    account_id: int
    name: str
    prefix: str
    last_used_at: dt.datetime | None = None
    #: The account's API access (`api_allowed`). A key whose account has it off
    #: is still recognised, and refused with that reason.
    allowed: bool = True


def _hash(key: str) -> str:
    return hashlib.sha256((key or "").strip().encode("utf-8")).hexdigest()


def as_json(row: ApiKey) -> dict[str, object]:
    """A key as its owner's list shows it. Never the key itself."""
    iso = lambda value: value.isoformat() if value else None  # noqa: E731
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": iso(row.created_at),
        "last_used_at": iso(row.last_used_at),
        "revoked_at": iso(row.revoked_at),
    }


def listed(session: Session, account_id: int) -> list[ApiKey]:
    """Every key of the account, active ones first, newest first within each."""
    return list(
        session.scalars(
            select(ApiKey)
            .where(ApiKey.account_id == account_id)
            .order_by(ApiKey.revoked_at.is_not(None), ApiKey.created_at.desc(), ApiKey.id.desc())
        )
    )


def create(session: Session, account_id: int, name: str) -> tuple[ApiKey, str]:
    """Make a key for the account. Returns the row and the key, which is not stored.

    Refused for an account that may not sign in or has no API access, for a
    blank or overlong name, and past `MAX_ACTIVE` active keys.
    """
    account = accounts.by_id(session, account_id)
    if account is None or accounts.status(account) != "active":
        raise ApiKeyError("only an active account can have API keys")
    if not account.api_allowed:
        raise ApiKeyError("API access is off for this account; an administrator can turn it on")
    clean = " ".join(str(name or "").split())
    if not clean:
        raise ApiKeyError("give the key a name, so you can tell it apart later")
    if len(clean) > MAX_NAME:
        raise ApiKeyError(f"a key's name can be at most {MAX_NAME} characters")
    active = session.scalar(
        select(func.count())
        .select_from(ApiKey)
        .where(ApiKey.account_id == account_id, ApiKey.revoked_at.is_(None))
    )
    if (active or 0) >= MAX_ACTIVE:
        raise ApiKeyError(f"an account can have at most {MAX_ACTIVE} active keys; revoke one first")
    key = KEY_PREFIX + secrets.token_urlsafe(_KEY_BYTES)
    row = ApiKey(
        account_id=account_id,
        name=clean,
        prefix=key[:PREFIX_SHOWN],
        key_hash=_hash(key),
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row, key


def revoke(session: Session, account_id: int, key_id: int) -> ApiKey:
    """Turn one of the account's keys off. Revoking a revoked key changes nothing."""
    row = session.get(ApiKey, key_id)
    if row is None or row.account_id != account_id:
        raise ApiKeyError("no such key on this account")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
    return row


def verify(session: Session, key: str | None) -> Caller | None:
    """The caller a presented key speaks for, or None if it may not be used.

    None for a key that is malformed, unknown, revoked, or whose account is not
    active — one answer for all four, so a caller cannot probe which. A key whose
    account has API access off is returned with `allowed` false: only its owner
    holds it, so saying why it is refused tells nobody else anything.
    """
    if not key or not key.startswith(KEY_PREFIX) or len(key) > 200:
        return None
    row = session.scalar(select(ApiKey).where(ApiKey.key_hash == _hash(key)))
    if row is None or row.revoked_at is not None:
        return None
    account = accounts.by_id(session, row.account_id)
    if account is None or accounts.status(account) != "active":
        return None
    return Caller(
        key_id=row.id,
        account_id=row.account_id,
        name=row.name,
        prefix=row.prefix,
        last_used_at=row.last_used_at,
        allowed=bool(account.api_allowed),
    )


def needs_touch(last_used_at: dt.datetime | None, now: dt.datetime | None = None) -> bool:
    """Whether `last_used_at` is stale enough to be written again."""
    now = now or utcnow()
    return last_used_at is None or now - last_used_at >= TOUCH_EVERY


def touch(session: Session, key_id: int, now: dt.datetime | None = None) -> None:
    """Record that the key was used, if the last record is older than `TOUCH_EVERY`."""
    row = session.get(ApiKey, key_id)
    if row is not None and needs_touch(row.last_used_at, now):
        row.last_used_at = now or utcnow()


__all__ = [
    "KEY_PREFIX",
    "MAX_ACTIVE",
    "MAX_NAME",
    "ApiKeyError",
    "Caller",
    "as_json",
    "create",
    "listed",
    "needs_touch",
    "revoke",
    "touch",
    "verify",
]
