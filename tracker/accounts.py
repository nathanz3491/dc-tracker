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
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.models import Account, AccountToken, Invite, utcnow

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

#: 256 bits, for a link in an email. Stored as sha256 only, like an invite's code.
_TOKEN_BYTES: Final = 32

#: How long a mailed link works. A confirmation waits for somebody to find the
#: email; a reset link is a credential in a mailbox, so it lives an hour.
LINK_TTL: Final = {"confirm": dt.timedelta(hours=24), "reset": dt.timedelta(hours=1)}

#: Links of one kind one address may be sent in an hour. The sign-up and reset
#: forms answer anyone, so without this they would mail an address as often as a
#: stranger pressed the button.
MAX_LINKS_PER_HOUR: Final = 3

#: A sign-up nobody confirmed is deleted after this, freeing the address.
UNCONFIRMED_DAYS: Final = 7


class AccountError(ValueError):
    """Something an operator did wrong, with a message written for them."""


class Throttled(AccountError):
    """This address has been sent enough links for now."""


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
    """Add one trusted account — let in, with the model panels. Raises `AccountError`.

    The terminal and an invite both come through here, and both are trusted: the
    person was chosen at the host. A plain sign-up goes through `sign_up` instead.
    An address held only by a sign-up nobody confirmed is taken over, because that
    row proves nothing about who owns the address.
    """
    key = normalize_email(email)
    check_password_length(password)
    holder = session.scalar(select(Account).where(Account.email_key == key))
    if holder is not None and _unconfirmed_signup(holder):
        session.delete(holder)
        session.flush()
    elif holder is not None:
        raise AccountError(f"{key} already has an account. `tracker users passwd` changes it.")
    now = utcnow()
    row = Account(
        email=email.strip(),
        email_key=key,
        name=(name or None),
        password_hash=hash_password(password),
        created_at=now,
        approved_at=now,
        ai_allowed=True,
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


def status(account: Account) -> str:
    """`active`, `disabled`, `unconfirmed` (a sign-up whose link is unclicked) or
    `pending` (confirmed, waiting for an administrator)."""
    if is_disabled(account):
        return "disabled"
    if account.approved_at is not None:
        return "active"
    if account.self_signup and account.email_verified_at is None:
        return "unconfirmed"
    return "pending"


def _unconfirmed_signup(account: Account) -> bool:
    return status(account) == "unconfirmed"


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
    ai: bool | None = None,
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
    if ai is not None and bool(ai) != bool(account.ai_allowed):
        changes.append("model panels on" if ai else "model panels off")
        account.ai_allowed = bool(ai)
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
    if invite is not None:
        return f"redeemed an invite ({invite.note or 'no note'})"
    if account.self_signup:
        return "signed up on the sign-in page"
    return "added at the terminal"


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
        "status": status(account),
        "disabled": is_disabled(account),
        "disabled_at": when(account.disabled_at),
        "approved_at": when(account.approved_at),
        "email_verified_at": when(account.email_verified_at),
        "ai": bool(account.ai_allowed),
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


# --- sign-up, mailed links, approval -------------------------------------------
#
# The sign-in page answers anyone, so every function here is written for a caller
# that must not say which addresses have accounts: they return what to mail and to
# whom, and the route answers the same sentence whatever happened.


@dataclass(frozen=True)
class SignUp:
    """What a sign-up produced, for the route to mail.

    Exactly one of the two is set. `pending` is the new (or re-submitted) account
    and `token` its confirmation link; `existing` is an account that already holds
    the address, which is told so — with a reset link, in case it was them — while
    the form says the same thing it says to anybody.
    """

    pending: Account | None = None
    token: str | None = None
    existing: Account | None = None
    existing_token: str | None = None


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").strip().encode("utf-8")).hexdigest()


def issue_link(session: Session, account: Account, purpose: str) -> str:
    """A fresh single-use link token for this account. Raises `Throttled`.

    Any earlier unused link of the same kind stops working, so only the newest email
    is live. Past `MAX_LINKS_PER_HOUR` for this address and kind, nothing is issued.
    """
    if purpose not in LINK_TTL:
        raise ValueError(f"unknown link purpose {purpose!r}")
    now = utcnow()
    recent = session.scalar(
        select(func.count())
        .select_from(AccountToken)
        .where(
            AccountToken.account_id == account.id,
            AccountToken.purpose == purpose,
            AccountToken.created_at > now - dt.timedelta(hours=1),
        )
    )
    if (recent or 0) >= MAX_LINKS_PER_HOUR:
        raise Throttled(f"{account.email_key} was sent {recent} {purpose} link(s) this hour")
    for old in session.scalars(
        select(AccountToken).where(
            AccountToken.account_id == account.id,
            AccountToken.purpose == purpose,
            AccountToken.used_at.is_(None),
        )
    ):
        old.used_at = now
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    session.add(
        AccountToken(
            account_id=account.id,
            purpose=purpose,
            token_hash=_token_hash(token),
            created_at=now,
            expires_at=now + LINK_TTL[purpose],
        )
    )
    session.flush()
    return token


def _live_link(session: Session, token: str, purpose: str) -> AccountToken | None:
    row = session.scalar(select(AccountToken).where(AccountToken.token_hash == _token_hash(token)))
    if row is None or row.purpose != purpose:
        return None
    return row


def expire_unconfirmed(session: Session) -> int:
    """Delete sign-ups nobody confirmed within `UNCONFIRMED_DAYS`. Returns how many."""
    cutoff = utcnow() - dt.timedelta(days=UNCONFIRMED_DAYS)
    stale = list(
        session.scalars(
            select(Account).where(
                Account.self_signup.is_(True),
                Account.email_verified_at.is_(None),
                Account.approved_at.is_(None),
                Account.created_at < cutoff,
            )
        )
    )
    for row in stale:
        session.delete(row)
    session.flush()
    return len(stale)


def sign_up(session: Session, email: str, password: str, *, name: str | None = None) -> SignUp:
    """Ask for an account without an invite. Raises `AccountError` on bad input.

    The account cannot sign in until its address is confirmed and an administrator
    approves it, and it starts without the model panels. Submitting again for an
    address still unconfirmed replaces the password and sends a fresh link, so a
    sign-up somebody else started with your address cannot hold it.
    """
    key = normalize_email(email)
    check_password_length(password)
    expire_unconfirmed(session)
    holder = session.scalar(select(Account).where(Account.email_key == key))
    if holder is not None and not _unconfirmed_signup(holder):
        # The one scrypt the other branch spends, so the response time does not say
        # which addresses already have an account.
        hash_password(password)
        try:
            return SignUp(existing=holder, existing_token=issue_link(session, holder, "reset"))
        except Throttled:
            return SignUp(existing=holder)
    if holder is not None:
        holder.email = email.strip()
        holder.name = (name or "").strip() or None
        holder.password_hash = hash_password(password)
        row = holder
    else:
        row = Account(
            email=email.strip(),
            email_key=key,
            name=(name or "").strip() or None,
            password_hash=hash_password(password),
            created_at=utcnow(),
            self_signup=True,
        )
        session.add(row)
    session.flush()
    return SignUp(pending=row, token=issue_link(session, row, "confirm"))


def confirm_email(session: Session, token: str) -> tuple[Account, bool]:
    """Prove the address a confirmation link was mailed to. Raises `AccountError`.

    Returns the account and whether this click is what confirmed it — the moment to
    tell an administrator, once. Clicking a link twice is not an error, since mail
    scanners open links before people do: a used link for an address already
    confirmed answers as a success, and says it was not the first.
    """
    unusable = AccountError("that link is not usable. Sign up again for a fresh one.")
    row = _live_link(session, token, "confirm")
    if row is None:
        raise unusable
    account = session.get(Account, row.account_id)
    if account is None:
        raise unusable
    if row.used_at is not None:
        if account.email_verified_at is not None:
            return account, False
        raise unusable
    if row.expires_at <= utcnow():
        raise unusable
    row.used_at = utcnow()
    first = account.email_verified_at is None
    account.email_verified_at = account.email_verified_at or utcnow()
    session.flush()
    return account, first


def request_reset(session: Session, email: str) -> tuple[Account, str] | None:
    """A reset link for this address, or None — unknown, disabled or throttled.

    None for all three, because the route answers the same either way.
    """
    account = by_email(session, email)
    if account is None or is_disabled(account):
        return None
    try:
        return account, issue_link(session, account, "reset")
    except Throttled:
        return None


def finish_reset(session: Session, token: str, password: str) -> tuple[Account, bool]:
    """Set a new password from a reset link. Raises `AccountError`.

    The password is checked before the link is spent, so a too-short one can be
    corrected. Every session the account had ends, as any password change does,
    and the address counts as confirmed: the link could only be opened from it.
    Returns the account and whether this is what confirmed its address — a sign-up
    that never clicked its confirmation link but reset its password instead, whose
    administrators have not been told about it yet.
    """
    check_password_length(password)
    unusable = AccountError("that reset link is not usable. Ask for a fresh one.")
    row = _live_link(session, token, "reset")
    if row is None or row.used_at is not None or row.expires_at <= utcnow():
        raise unusable
    account = session.get(Account, row.account_id)
    if account is None or is_disabled(account):
        raise unusable
    row.used_at = utcnow()
    reset_password(session, account, password)
    first = account.self_signup and account.email_verified_at is None
    account.email_verified_at = account.email_verified_at or utcnow()
    session.flush()
    return account, first


def pending(session: Session) -> list[Account]:
    """Confirmed sign-ups waiting for an administrator, oldest first."""
    return [
        row
        for row in session.scalars(
            select(Account)
            .where(Account.approved_at.is_(None), Account.disabled_at.is_(None))
            .order_by(Account.created_at.asc())
        )
        if status(row) == "pending"
    ]


def approve(session: Session, account: Account) -> bool:
    """Let a waiting account sign in. Returns whether anything changed.

    Refused for a sign-up whose address is not confirmed yet: approving it would let
    in an address nobody has shown they own.
    """
    if account.approved_at is not None:
        return False
    if status(account) == "unconfirmed":
        raise AccountError(f"{account.email} has not confirmed the address yet.")
    account.approved_at = utcnow()
    _edited(account)
    session.flush()
    return True


__all__ = [
    "DEFAULT_INVITE_DAYS",
    "LINK_TTL",
    "MAX_EMAIL_LEN",
    "MAX_LINKS_PER_HOUR",
    "MAX_PASSWORD_LEN",
    "MIN_PASSWORD_LEN",
    "UNCONFIRMED_DAYS",
    "AccountError",
    "SignUp",
    "Throttled",
    "admins",
    "any_exist",
    "approve",
    "by_email",
    "by_id",
    "check_password_length",
    "confirm_email",
    "count",
    "create",
    "decoy_hash",
    "delete",
    "detail",
    "expire_unconfirmed",
    "finish_reset",
    "hash_password",
    "is_disabled",
    "issue_link",
    "joined_via",
    "listing",
    "mint_invite",
    "normalize_email",
    "outstanding",
    "pending",
    "redeem",
    "request_reset",
    "require",
    "reset_password",
    "session_stamp",
    "set_admin",
    "set_disabled",
    "set_password",
    "set_watch_all",
    "sign_out_everywhere",
    "sign_up",
    "stamp_for",
    "status",
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
