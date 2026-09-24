"""Who may sign in to the console, and how somebody new gets an account.

The console used to have one shared password read from the environment. That made
every reader the same principal, which is why the landing page could only ever
draw one watchlist — a shared list is what "no identity" looks like from the data's
end. This module is the identity: create an account, check a password, mint an
invite, redeem one.

**It is the only place in the codebase that hashes anything secret**, and it is
deliberately separate from `webui/auth.py`. That module is sessions and rate
limiting; it imports nothing from this project and touches no database, and
keeping it that way is worth a little indirection — the gate never learns what a
password is, it is only told whether one was right.

**scrypt from the standard library, and no new dependency.** A project that
vendors its entire front end rather than take a CDN should not acquire bcrypt to
hash a handful of passwords. The stored form is self-describing —
``scrypt$<n>$<r>$<p>$<salt>$<hash>`` — so the cost parameters can be raised later
without a migration, and rows written under the old ones keep verifying against
the parameters they were actually written with. `source.extractor` is a versioned
self-describing string for exactly this reason.

**An invite's code is never stored, only its sha256.** The database travels
between machines through `scripts/sync_db.py` and sits in `backups/`, so a
plaintext code in it would be a live credential in every copy. The code is printed
once by the command that mints it and is not recoverable afterwards. It is *not*
scrypt-hashed: a 160-bit random token has no guessable keyspace for a work factor
to slow anybody down in, so the salt and the cost would buy nothing a password
needs them for.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import logging
import secrets
from functools import lru_cache
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.models import Account, Invite, utcnow

log = logging.getLogger(__name__)

#: Below this a password is a typo rather than a secret. Deliberately low, and it
#: lives here rather than in `webui/auth.py` because it is a fact about an
#: identity and not about a gate. What makes a short password safe is the rate
#: limit — 40 failed sign-ins across all clients within 15 minutes closes the
#: console for 15 more, which puts even a 7-character keyspace tens of millions of
#: years out of reach. See `tracker/webui/auth.py`.
MIN_PASSWORD_LEN: Final = 6

#: Long enough that a paste cannot be a password by accident, short enough not to
#: be a denial-of-service vector: scrypt hashes whatever it is handed, so an
#: unbounded field is unbounded work on an unauthenticated route.
MAX_PASSWORD_LEN: Final = 1024

#: An address longer than this is not one. RFC 5321 caps a path at 256.
MAX_EMAIL_LEN: Final = 254

#: scrypt cost. 128 * r * n = 16 MiB of memory per hash, which is the point of
#: scrypt over PBKDF2 — memory is what a GPU cannot parallelise cheaply. Kept
#: under OpenSSL's 32 MiB default `maxmem` so no caller has to raise it.
_SCRYPT_N: Final = 1 << 14
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SCRYPT_DKLEN: Final = 32
_SALT_BYTES: Final = 16

#: 160 bits. `token_urlsafe` so it survives being pasted into a URL or a chat
#: message without escaping.
_INVITE_BYTES: Final = 20

#: How long a fresh invite is good for, unless the caller says otherwise.
DEFAULT_INVITE_DAYS: Final = 7


class AccountError(ValueError):
    """Something an operator did wrong, with a message written for them."""


# --- passwords -------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """One password, as it is stored. A fresh salt every time."""
    check_password_length(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Whether `password` produced `stored`.

    Re-derives with the parameters recorded *in* `stored`, never with the
    constants above, which is the whole reason the format carries them: raising
    the cost must not lock out every account written before the change.

    A malformed or unrecognised hash is False rather than an exception. This runs
    on an unauthenticated route, and a row that cannot be parsed must read as
    "wrong password" rather than as a 500 that says the row exists.
    """
    if not password or len(password) > MAX_PASSWORD_LEN:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_unb64(digest)),
        )
    except (ValueError, TypeError):
        log.warning("account: unparseable password hash; treating as no match")
        return False
    return hmac.compare_digest(candidate, _unb64(digest))


def check_password_length(password: str) -> None:
    """Raise `AccountError` unless the password is a plausible secret."""
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AccountError(
            f"a password under {MIN_PASSWORD_LEN} characters is short enough to be a typo "
            "rather than a secret."
        )
    if len(password) > MAX_PASSWORD_LEN:
        raise AccountError(f"a password over {MAX_PASSWORD_LEN} characters is not a password.")


# --- emails ----------------------------------------------------------------


def normalize_email(email: str) -> str:
    """``" Alice@Ex.COM "`` → ``"alice@ex.com"``. Raises `AccountError`.

    Checked here rather than only in the schema, because the schema's CHECK can
    say no but cannot say why. Deliberately not a full RFC 5322 parse: the useful
    properties are that there is exactly one `@` with something either side and no
    whitespace anywhere, and a stricter rule would refuse a valid address that
    somebody actually has.
    """
    key = (email or "").strip().lower()
    if not key:
        raise AccountError("an account needs an email address.")
    if len(key) > MAX_EMAIL_LEN:
        raise AccountError(f"that address is over {MAX_EMAIL_LEN} characters, so it is not one.")
    if any(character.isspace() for character in key):
        raise AccountError(f"{email!r} contains whitespace, so it is not an email address.")
    local, separator, domain = key.partition("@")
    if not separator or not local or not domain or "@" in domain:
        raise AccountError(
            f"{email!r} is not an email address — it needs one @ with text on both sides."
        )
    return key


# --- accounts --------------------------------------------------------------


def by_email(session: Session, email: str) -> Account | None:
    """One account by address, matched on the normalized key. None if unknown.

    A malformed address is None rather than a raise: every caller of this is
    either a lookup that legitimately misses or a sign-in attempt, and "no such
    account" is the right answer to both.
    """
    try:
        key = normalize_email(email)
    except AccountError:
        return None
    return session.scalar(select(Account).where(Account.email_key == key))


def listing(session: Session) -> list[Account]:
    """Every account, oldest first — the order they were created in."""
    return list(
        session.scalars(select(Account).order_by(Account.created_at.asc(), Account.id.asc())).all()
    )


def count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(Account)) or 0)


def any_exist(session: Session) -> bool:
    """Whether the console should ask anybody to sign in.

    Zero accounts is a legitimate state and means an open console, exactly as an
    unset `TRACKER_CONSOLE_PASSWORD` did before this: `tracker serve` on loopback
    needs no setup, and reaching loopback already means having the machine.
    Publishing is what refuses — see `cli._console_accounts` — and a console
    already published ignores this answer and requires a sign-in regardless
    (`webui/server.py::Console.published`).
    """
    return session.scalar(select(Account.id).limit(1)) is not None


def create(session: Session, email: str, password: str, *, name: str | None = None) -> Account:
    """Add one account. Raises `AccountError` on a bad address, password or clash."""
    key = normalize_email(email)
    check_password_length(password)
    if session.scalar(select(Account).where(Account.email_key == key)) is not None:
        raise AccountError(f"{key} already has an account. `tracker users passwd` changes it.")
    row = Account(
        email=email.strip(),
        email_key=key,
        name=(name or None),
        password_hash=hash_password(password),
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def set_password(session: Session, email: str, password: str) -> Account:
    """Replace one account's password. Raises `AccountError` if unknown."""
    row = by_email(session, email)
    if row is None:
        raise AccountError(_unknown(session, email))
    reset_password(session, row, password)
    return row


def delete(session: Session, email: str) -> bool:
    """Drop one account and, by cascade, their watchlist. False if unknown."""
    row = by_email(session, email)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


@lru_cache(maxsize=1)
def decoy_hash() -> str:
    """A real stored-form hash of a secret nobody holds, made once per process.

    What `verify` checks a password against when no account has the address, so
    that path costs exactly the one scrypt the wrong-password path costs. It used to
    hash a fresh random string on every miss and then verify against it — two
    scrypts — and on a copy of production a wrong password for an unknown address
    took ~109 ms against ~56 ms for a real one: the response time said which
    addresses exist, which is the one thing the shared "Wrong email or password"
    message was written not to say.

    Made with today's cost parameters, which are the ones every row is written
    with. If they are ever raised, rows written before verify at their old cost,
    and this is where the difference would reappear. `server.serve` calls it
    before listening, so the first miss after a restart is not the odd one out.
    """
    return hash_password(secrets.token_urlsafe(32))


def verify(session: Session, email: str, password: str) -> Account | None:
    """The account this pair signs in as, or None.

    **The unknown-address and wrong-password paths must cost the same**, or the
    response time says which addresses have accounts. So a miss runs the same one
    scrypt a real row does, against `decoy_hash` — a hash nothing can match.
    """
    row = by_email(session, email)
    if row is None:
        verify_password(password, decoy_hash())
        return None
    return row if verify_password(password, row.password_hash) else None


def touch(session: Session, account: Account) -> None:
    """Record that this account just signed in."""
    account.last_seen_at = utcnow()
    session.flush()


def session_stamp(password_hash: str, epoch: int = 0) -> str:
    """What a console session remembers about the credential it was granted on.

    The console's gate holds sessions in memory and `tracker users` runs in another
    process, so a session has to be re-checked against the row — and an account id
    alone cannot say whether the row is still the same account. Changing a password
    changes the stored hash; so does SQLite handing a deleted account's id to the
    next account created, because every hash carries a fresh salt. A session whose
    stamp no longer matches its row is therefore a session for a credential that no
    longer exists, whichever of those happened.

    A digest of the hash rather than the hash, so the gate's table holds nothing an
    attacker could start cracking from.

    `epoch` is the account's `session_epoch`, folded in so that raising it — "sign
    out everywhere" — stales every outstanding stamp without touching the password.
    Epoch 0 digests the hash alone, as every stamp did before migration 0028.
    """
    material = password_hash if not epoch else f"{password_hash}#{int(epoch)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def stamp_for(account: Account) -> str:
    """`session_stamp` for one row: its password hash and its session epoch."""
    return session_stamp(account.password_hash, account.session_epoch or 0)


def _unknown(session: Session, email: str) -> str:
    """The message for an address nobody holds, naming the ones somebody does.

    Listing them is the point: `--user` is typed by hand, and a silent miss looks
    exactly like an empty watchlist. Same reasoning as `logic.py`'s refusal to
    guess which of two values won.
    """
    known = [row.email for row in listing(session)]
    if not known:
        return f"no account for {email!r}, and there are none at all yet. `tracker users add` makes one."
    return f"no account for {email!r}. Known: {', '.join(known)}."


def require(session: Session, email: str) -> Account:
    """One account by address, or `AccountError` naming the ones that exist."""
    row = by_email(session, email)
    if row is None:
        raise AccountError(_unknown(session, email))
    return row


# --- managing an account -----------------------------------------------------
#
# What the operator does to somebody's account, from `tracker users` or the
# console's admin page. Both call these, so the two can never disagree about what
# an edit means. Each records `updated_at`, which the detail view shows.


def by_id(session: Session, account_id: int) -> Account | None:
    return session.get(Account, account_id)


def is_disabled(account: Account) -> bool:
    return account.disabled_at is not None


def admins(session: Session) -> list[Account]:
    """Accounts that may use the admin page, oldest first."""
    return [row for row in listing(session) if row.is_admin]


def _edited(account: Account) -> None:
    account.updated_at = utcnow()


def update(
    session: Session,
    account: Account,
    *,
    email: str | None = None,
    name: str | None = None,
    clear_name: bool = False,
    watch_all: bool | None = None,
) -> list[str]:
    """Change an account's address, display name or reach. Returns what changed.

    Each change is described in words — `email a@x -> b@y` — because both callers
    report it. Changing nothing returns an empty list rather than raising: an edit
    that restates the current value is not a mistake worth refusing.

    **A new address is checked exactly as a new account's is** — normalized, and
    refused if somebody else holds it — because it becomes the identity every
    lookup and every sign-in uses. The account's sessions survive it: they are
    bound to the credential, not the address.
    """
    changes: list[str] = []
    if email is not None:
        key = normalize_email(email)
        if key != account.email_key:
            holder = session.scalar(select(Account).where(Account.email_key == key))
            if holder is not None and holder.id != account.id:
                raise AccountError(f"{key} already has an account.")
        if email.strip() != account.email:
            changes.append(f"email {account.email} -> {email.strip()}")
            account.email = email.strip()
            account.email_key = key
    if clear_name or name is not None:
        new_name = None if clear_name else ((name or "").strip() or None)
        if new_name != account.name:
            changes.append(f"name {account.name or '(none)'} -> {new_name or '(none)'}")
            account.name = new_name
    if watch_all is not None and bool(watch_all) != bool(account.watch_all):
        reach = "the whole database" if watch_all else "only its watchlist"
        changes.append(f"reads {reach}")
        account.watch_all = bool(watch_all)
    if changes:
        _edited(account)
        session.flush()
    return changes


def reset_password(session: Session, account: Account, password: str) -> None:
    """Give one account a new password. Every session signed in with the old one ends."""
    check_password_length(password)
    account.password_hash = hash_password(password)
    _edited(account)
    session.flush()


def set_disabled(session: Session, account: Account, disabled: bool) -> bool:
    """Switch an account off or back on. Returns whether anything changed.

    Off keeps the row and its watchlist and refuses its sign-ins; its open sessions
    end within the console's confirm interval, the way a deleted account's do.
    """
    if disabled == is_disabled(account):
        return False
    account.disabled_at = utcnow() if disabled else None
    _edited(account)
    session.flush()
    return True


def sign_out_everywhere(session: Session, account: Account) -> None:
    """End every session this account has open, without changing its password."""
    account.session_epoch = (account.session_epoch or 0) + 1
    _edited(account)
    session.flush()


def set_admin(session: Session, account: Account, value: bool) -> bool:
    """Grant or revoke the admin page. Returns whether anything changed.

    Only `tracker users admin` calls this — the console has no route to it, so an
    admin session that was stolen can manage accounts but cannot make more admins.
    """
    if bool(value) == bool(account.is_admin):
        return False
    account.is_admin = bool(value)
    _edited(account)
    session.flush()
    return True


def joined_via(session: Session, account: Account) -> str:
    """How this account came to exist: an invite (named by its note) or the terminal."""
    invite = session.scalar(select(Invite).where(Invite.redeemed_by == account.id).limit(1))
    if invite is None:
        return "added at the terminal"
    return f"redeemed an invite ({invite.note or 'no note'})"


def detail(session: Session, account: Account) -> dict[str, object]:
    """Everything about one account, for `tracker users show` and the admin page.

    Never the password hash, nor anything derived from it: this is sent to a
    browser.
    """
    from tracker.models import Watch

    watches = session.scalar(
        select(func.count()).select_from(Watch).where(Watch.account_id == account.id)
    )

    def when(value: dt.datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "id": account.id,
        "email": account.email,
        "name": account.name,
        "admin": bool(account.is_admin),
        "disabled": is_disabled(account),
        "disabled_at": when(account.disabled_at),
        "watch_all": bool(account.watch_all),
        "watches": int(watches or 0),
        "created_at": when(account.created_at),
        "last_seen_at": when(account.last_seen_at),
        "updated_at": when(account.updated_at),
        "joined": joined_via(session, account),
    }


# --- invites ---------------------------------------------------------------


def _code_hash(code: str) -> str:
    return hashlib.sha256((code or "").strip().encode("utf-8")).hexdigest()


def mint_invite(
    session: Session, *, note: str | None = None, days: int = DEFAULT_INVITE_DAYS
) -> tuple[Invite, str]:
    """A fresh single-use code. Returns the row and the code, which is shown once."""
    if days < 1:
        raise AccountError("an invite has to be good for at least a day.")
    code = secrets.token_urlsafe(_INVITE_BYTES)
    row = Invite(
        code_hash=_code_hash(code),
        note=(note or None),
        created_at=utcnow(),
        expires_at=utcnow() + dt.timedelta(days=days),
    )
    session.add(row)
    session.flush()
    return row, code


def outstanding(session: Session) -> list[Invite]:
    """Unredeemed, unexpired invites, soonest to expire first."""
    now = utcnow()
    return list(
        session.scalars(
            select(Invite)
            .where(Invite.redeemed_at.is_(None), Invite.expires_at > now)
            .order_by(Invite.expires_at.asc())
        ).all()
    )


def redeem(
    session: Session, code: str, email: str, password: str, *, name: str | None = None
) -> Account:
    """Spend one code and create the account it pays for.

    Every refusal says the same thing — "that code is not usable" — rather than
    distinguishing unknown from expired from already-spent. This runs
    unauthenticated on a public URL, and the differences are only useful to
    somebody probing.

    The account is created *first* so that a bad address or a short password does
    not burn the code; the code is marked spent only once there is an account to
    attribute it to.
    """
    row = session.scalar(select(Invite).where(Invite.code_hash == _code_hash(code)))
    unusable = AccountError("that invite code is not usable. Ask for a fresh one.")
    if row is None or row.redeemed_at is not None or row.expires_at <= utcnow():
        raise unusable

    account = create(session, email, password, name=name)
    row.redeemed_at = utcnow()
    row.redeemed_by = account.id
    session.flush()
    log.info("account: %s created by invite %d", account.email_key, row.id)
    return account


__all__ = [
    "DEFAULT_INVITE_DAYS",
    "MAX_EMAIL_LEN",
    "MAX_PASSWORD_LEN",
    "MIN_PASSWORD_LEN",
    "AccountError",
    "admins",
    "any_exist",
    "by_email",
    "by_id",
    "check_password_length",
    "count",
    "create",
    "decoy_hash",
    "delete",
    "detail",
    "hash_password",
    "is_disabled",
    "joined_via",
    "listing",
    "mint_invite",
    "normalize_email",
    "outstanding",
    "redeem",
    "require",
    "reset_password",
    "session_stamp",
    "set_admin",
    "set_disabled",
    "set_password",
    "set_watch_all",
    "sign_out_everywhere",
    "stamp_for",
    "touch",
    "update",
    "verify",
    "verify_password",
]


def set_watch_all(session: Session, email: str, value: bool) -> Account:
    """Turn "read the whole database" on or off for one account.

    Off is the default and the honest reading of an empty watchlist: this person
    has said what they want, and it is nothing yet. On is for somebody who has
    decided they want all of it — see migration 0022 for why that stopped being
    what an empty list implied.
    """
    account = require(session, email)
    account.watch_all = bool(value)
    session.flush()
    return account
