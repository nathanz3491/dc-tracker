"""The gate in front of the console: sessions, and how fast anyone may knock.

With no accounts at all the console needs no sign-in: reaching loopback already
means having the machine, and `tracker serve` should need no setup. The moment an
account exists — or the console is published through a tunnel or a proxy — that
stops being true, and everything behind it is a real database that can spend LLM
tokens on a model panel.

So the rules here are deliberately not "good enough for localhost":

* **Everything is behind it.** Not just the page — every API route, every static
  asset. An unauthenticated request gets one of the public pages, one of the files
  on the public list, or a 401, and nothing else.
* **A lockout, not just a check.** A published URL means an unattended login form.
  A short human-memorable password is only safe if guessing is slow, so failures
  are counted and the gate closes for a while.
* **Session tokens are random and server-side.** The cookie carries no claim the
  server has to trust — it is a lookup key, revocable, and it expires.

**This module knows nothing about passwords, and that is on purpose.** It imports
nothing from this project and touches no database. The credential check lives in
`tracker/accounts.py`, where the hashing is; the server asks that module whether a
pair is right and then asks this one for a token. So a token is the only thing the
gate can hand out, and the only thing it can be asked about is which account a
token belongs to.

**Sessions outlive a restart.** They are held in memory, and the host's deployer
restarts this process whenever a commit lands — eleven times on the night of
2026-11-09, each one signing every reader out. So a gate may be handed a
`SessionStore`: each session granted is also written there, and a cookie the gate
does not recognise is looked up there once before it is refused. The store is given
the token's SHA-256, never the token, so whatever it writes signs nobody in; the
console's is a table in the database (`console_session`, migration 0054). A store
that cannot write just now costs only that: the session works until the next
restart, and a sign-in never waits on it.

**A session is only as good as the account behind it**, and that has to be asked
rather than assumed. `tracker users` runs in a *different process*, so it cannot
reach into this dictionary: a session that remembered only an account id kept
working after `tracker users rm` for the rest of its 12-hour life, on every route
but the one that happened to look the row up — measured on a copy of production,
`/api/projects`, `/api/project`, `/api/claims`, `/api/updates` and `/api/articles`
all answered 200 for an account that no longer existed.

So a session also carries a `stamp` — whatever the granting code says identifies
the credential it was granted against; the console uses a digest of the stored
password hash — and `session_for` takes a `confirm` question to put to the
database, asked at most once every `SESSION_CONFIRM_S` per session. Deleting an
account, changing its password, or SQLite handing a deleted account's id to the
next account created (it is a plain `INTEGER PRIMARY KEY`, so it does) all end the
old sessions within those few seconds, on every route, without a restart. The gate
still touches no database itself: it is handed the question, not the connection.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)

#: How long a sign-in lasts. Long enough to work a session, short enough that a
#: forgotten open tab does not stay a key forever.
SESSION_TTL_S = 12 * 60 * 60

#: How long a session's account may go without being re-read.
#:
#: Every request is behind the gate, including every static file, so asking the
#: database on each one would be a dozen reads per page load for an answer that
#: changes about never. Five seconds is shorter than it takes to switch from the
#: terminal that ran `tracker users rm` to a browser and reload — the same
#: reasoning, and the same number, as `server.AUTH_CACHE_S`.
SESSION_CONFIRM_S = 5.0

#: Failures before the gate closes on one client, and for how long. Eight is
#: generous for a typo and ruinous for a guesser.
MAX_FAILURES = 8
LOCKOUT_S = 15 * 60

#: The same again, but counted across every client at once.
#:
#: Per-client lockout is the wrong shape on its own against a published URL: the
#: counter keys on `CF-Connecting-IP`, and an attacker with a thousand addresses
#: gets a thousand budgets. The global counter is what makes the rate a property
#: of the gate rather than of the attacker's address pool. Set higher than the
#: per-client limit so that one person fumbling their password does not lock
#: everyone out, but low enough that distributed guessing gains nothing.
#:
#: The arithmetic that makes a short password safe: 40 attempts per 15 minutes is
#: ~3,800 a day. A 7-character lowercase-and-digits password is 36^7 ≈ 7.8e10
#: combinations, so an exhaustive search is ~57 million years. Length is not what
#: is protecting this; the rate limit is.
#:
#: **"Per 15 minutes" is a window, and a correct password does not reset it.** It
#: used to be a counter that only a successful sign-in cleared, so the arithmetic
#: above was false in both directions: unrelated typos over a week added up to a
#: lockout, and anybody with an account of their own could guess seven times,
#: sign in as themselves, and repeat without ever reaching a limit. A failure now
#: counts for `GLOBAL_WINDOW_S` and then ages out, and a success forgets only the
#: succeeding client's own failures (`succeed`).
#:
#: **Nothing is counted per email**, and that is a decision rather than an
#: omission. A per-address counter lets anyone who knows an address lock its owner
#: out, and the global counter already bounds the rate without handing out that
#: lever.
GLOBAL_MAX_FAILURES = 40
GLOBAL_WINDOW_S = 15 * 60
GLOBAL_LOCKOUT_S = 15 * 60

#: Emails one client may cause in an hour, and all clients together.
#:
#: The sign-up and forgot-password forms answer anybody and send mail, so without
#: a budget they are a way to make this server mail strangers — as often as a
#: script can press the button — and to spend the sending domain's reputation
#: doing it. Per client first; the global figure is what a distributed attempt
#: meets. Each address is also limited on its own (`accounts.MAX_LINKS_PER_HOUR`).
MAIL_MAX_PER_CLIENT = 5
MAIL_WINDOW_S = 60 * 60
GLOBAL_MAIL_MAX = 60

#: The cookie holds a lookup key, never a claim. Named for the app so it cannot
#: collide with anything else on localhost.
COOKIE = "dc_console_session"

#: How long a cookie the store did not know is answered from memory. Every request
#: carries the cookie, static files included, so an expired or forged one would
#: otherwise be a database read per request for as long as the browser keeps it.
UNKNOWN_FOR_S = 60.0


def token_key(token: str) -> str:
    """What a store is given for a token: its SHA-256, so the store signs nobody in."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionStore(Protocol):
    """Where sessions are kept beyond this process, so a restart keeps them.

    Keyed by `token_key`, never the token. `expires_at` is wall-clock seconds since
    the epoch: the gate's own clock is monotonic, which starts again with the
    process. Any method may raise; the gate logs it and carries on in memory.
    """

    def save(self, key: str, account_id: int, stamp: str, expires_at: float) -> None: ...

    def load(self, key: str) -> tuple[int, str, float] | None: ...

    def restamp(self, key: str, stamp: str) -> None: ...

    def delete(self, key: str) -> None: ...


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0


@dataclass
class _Session:
    """One signed-in account, and when its token stops working."""

    account_id: int
    expires: float
    #: Opaque to the gate: what the granting code said identifies the credential.
    #: `confirm` is handed it back, which is how a changed password or a reused id
    #: is told apart from the account the session was actually granted for.
    stamp: str = ""
    #: When `confirm` last said this session's account still holds. Granting counts,
    #: because the sign-in that asked for the token has just read the row.
    confirmed_at: float = 0.0


@dataclass
class Gate:
    """Session store and lockout for one console instance.

    Holds no password and performs no credential check — see the module
    docstring. `fail`, `succeed` and `grant` are the three things a login handler
    does, in whichever order the outcome dictates.
    """

    session_ttl: int = SESSION_TTL_S
    session_confirm_s: float = SESSION_CONFIRM_S
    max_failures: int = MAX_FAILURES
    lockout_s: int = LOCKOUT_S
    global_max_failures: int = GLOBAL_MAX_FAILURES
    global_window_s: int = GLOBAL_WINDOW_S
    global_lockout_s: int = GLOBAL_LOCKOUT_S
    mail_max: int = MAIL_MAX_PER_CLIENT
    mail_window_s: int = MAIL_WINDOW_S
    global_mail_max: int = GLOBAL_MAIL_MAX
    #: Injectable so a test can move time rather than wait it out. Monotonic, so a
    #: wall-clock change on the host cannot extend a session or end a lockout.
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    #: Where sessions outlive this process; None keeps them in memory only.
    store: SessionStore | None = field(default=None, repr=False)
    #: The wall clock, for what the store keeps — only when a session expires.
    wall: Callable[[], float] = field(default=time.time, repr=False)

    _sessions: dict[str, _Session] = field(default_factory=dict, repr=False)
    #: Tokens the store did not know, and until when that answer is trusted.
    _unknown: dict[str, float] = field(default_factory=dict, repr=False)
    _attempts: dict[str, _Attempts] = field(default_factory=dict, repr=False)
    #: When each failure inside the current global window happened, oldest first.
    _failures: deque[float] = field(default_factory=deque, repr=False)
    _global_locked_until: float = field(default=0.0, repr=False)
    #: When each email inside the current window was caused, per client and overall.
    _mails: dict[str, deque[float]] = field(default_factory=dict, repr=False)
    _all_mails: deque[float] = field(default_factory=deque, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- attempts ---------------------------------------------------------

    def locked_for(self, client: str) -> int:
        """Seconds remaining before this client may try again, 0 if it may now.

        The larger of the client's own lockout and the global one. A client that
        has never failed is still held back while the gate as a whole is closed —
        that is the point of the global counter, and without it an attacker just
        rotates addresses.
        """
        now = self.clock()
        with self._lock:
            record = self._attempts.get(client)
            until = max(
                record.locked_until if record else 0.0,
                self._global_locked_until,
            )
        remaining = until - now
        return int(remaining) + 1 if remaining > 0 else 0

    def fail(self, client: str) -> None:
        """Count one refused attempt, and close the gate if that was enough."""
        now = self.clock()
        with self._lock:
            record = self._attempts.setdefault(client, _Attempts())
            record.count += 1
            if record.count >= self.max_failures:
                record.locked_until = now + self.lockout_s
                record.count = 0
                log.warning("console: locking out %s for %ds", client, self.lockout_s)

            self._failures.append(now)
            self._age_out(now)
            if len(self._failures) >= self.global_max_failures:
                self._global_locked_until = now + self.global_lockout_s
                # Cleared, so the gate reopens with a whole window to fill rather
                # than one failure away from closing again — as the counter did.
                self._failures.clear()
                log.warning(
                    "console: %d failed sign-ins across all clients within %ds; "
                    "closing the gate for %ds",
                    self.global_max_failures,
                    self.global_window_s,
                    self.global_lockout_s,
                )

    def succeed(self, client: str) -> None:
        """Forget this client's failures — and only this client's.

        It used to clear the global counter too, on the reasoning that a correct
        password says the traffic is not an attack. It says that about one client,
        and nothing about the rest: an account holder could guess seven times at
        other people's passwords, sign in as themselves, and repeat indefinitely,
        never reaching either limit. Old failures leave the global count by ageing
        out of `global_window_s` instead, which keeps the thing the reset was for —
        one person's typos never closing the gate on everybody — without the hole.
        """
        with self._lock:
            self._attempts.pop(client, None)

    def recent_failures(self) -> int:
        """Failures across every client inside the current global window."""
        now = self.clock()
        with self._lock:
            self._age_out(now)
            return len(self._failures)

    def _age_out(self, now: float) -> None:
        horizon = now - self.global_window_s
        while self._failures and self._failures[0] <= horizon:
            self._failures.popleft()

    def may_mail(self, client: str) -> bool:
        """Whether this client may cause one more email now; counts it if so.

        Asked before a form sends anything. A refusal is not a failure for the
        lockout — nothing was guessed — and the form still answers as it always does.
        """
        now = self.clock()
        horizon = now - self.mail_window_s
        with self._lock:
            if len(self._mails) > 1000:
                # Clients whose last email left the window, so a stream of one-off
                # addresses cannot grow this without bound.
                for key in [k for k, q in self._mails.items() if not q or q[-1] <= horizon]:
                    del self._mails[key]
            mine = self._mails.setdefault(client, deque())
            for queue in (mine, self._all_mails):
                while queue and queue[0] <= horizon:
                    queue.popleft()
            if len(mine) >= self.mail_max or len(self._all_mails) >= self.global_mail_max:
                return False
            mine.append(now)
            self._all_mails.append(now)
            return True

    # --- sessions ---------------------------------------------------------

    def grant(self, account_id: int, *, stamp: str = "") -> str:
        """A fresh token for one account, remembering the credential it was granted on.

        `stamp` is opaque here and is handed back to `session_for`'s `confirm`.
        Left empty, a session can only ever be confirmed by an account id — which
        is exactly the weakness `stamp` exists to close, so the console always
        passes one.
        """
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._prune(now)
            self._sessions[token] = _Session(
                account_id, now + self.session_ttl, stamp=stamp, confirmed_at=now
            )
        self._keep("save", token_key(token), account_id, stamp, self.wall() + self.session_ttl)
        return token

    def _keep(self, what: str, *args: object) -> None:
        """Pass one change to the store, if there is one. Never raises."""
        if self.store is None:
            return
        try:
            getattr(self.store, what)(*args)
        except Exception as exc:  # the store is a convenience; the session is not
            log.warning("console: could not %s a session in the store: %s", what, exc)

    def _restore(self, token: str, now: float) -> _Session | None:
        """A session the store kept from before a restart, or None.

        Restored unconfirmed, so its account is read again before it opens anything:
        the account may have been deleted, or its password changed, while the
        console was down.
        """
        if self.store is None:
            return None
        key = token_key(token)
        with self._lock:
            if self._unknown.get(key, 0.0) > now:
                return None
        try:
            kept = self.store.load(key)
        except Exception as exc:
            log.warning("console: could not read a session from the store: %s", exc)
            return None
        if kept is None:
            with self._lock:
                if len(self._unknown) > 1000:
                    self._unknown = {k: t for k, t in self._unknown.items() if t > now}
                self._unknown[key] = now + UNKNOWN_FOR_S
            return None
        account_id, stamp, expires_at = kept
        left = expires_at - self.wall()
        if left <= 0:
            self._keep("delete", key)
            return None
        restored = _Session(account_id, now + left, stamp=stamp, confirmed_at=float("-inf"))
        with self._lock:
            return self._sessions.setdefault(token, restored)

    def session_for(
        self,
        token: str | None,
        *,
        confirm: Callable[[int, str], bool | None] | None = None,
    ) -> int | None:
        """Which account this token signs in as, or None if it does not.

        Returns an account id rather than a boolean because that id is what every
        route downstream needs: a watchlist read is a question about one person,
        and a handler that had to ask twice could ask two different gates.

        **`confirm` is how a session stays as good as the account behind it.** The
        gate cannot see the database, so the caller hands it the question — does
        account `id` still hold the credential `stamp` names? — and it is asked at
        most once per `session_confirm_s` per token, outside the lock, because it
        is a database read. Its three answers mean three different things:

        * **True** — carry on, and do not ask again for a while;
        * **False** — the account is gone, its password changed, or its id now
          belongs to somebody else: the session is dropped, not merely refused;
        * **None** — it could not be answered just now: this request is refused
          and the session kept, because signing everybody out over one unreadable
          moment would be a failure of its own.

        Without `confirm` the answer is the table's alone, which is what a caller
        with no database — a test of the lockout, say — wants.
        """
        if not token:
            return None
        now = self.clock()
        with self._lock:
            found = self._sessions.get(token)
        if found is None:
            found = self._restore(token, now)
            if found is None:
                return None
        with self._lock:
            if self._sessions.get(token) is not found:
                return None  # signed out meanwhile
            if found.expires < now:
                del self._sessions[token]
                expired = True
            else:
                expired = False
                if confirm is None or now - found.confirmed_at < self.session_confirm_s:
                    return found.account_id
        if expired:
            self._keep("delete", token_key(token))
            return None

        verdict = confirm(found.account_id, found.stamp)
        if verdict is None:
            return None
        if not verdict:
            self.revoke(token)
            log.info(
                "console: dropped a session for account %d — the account was deleted, "
                "its password changed, or its id was given to someone else",
                found.account_id,
            )
            return None
        with self._lock:
            if self._sessions.get(token) is not found:
                return None  # signed out while the question was being asked
            found.confirmed_at = now
        return found.account_id

    def restamp(self, token: str | None, stamp: str) -> bool:
        """Keep one session valid across a change to its own credential.

        A password change stales every session's stamp, which is how the other
        devices get signed out. The one the change was made in is the exception:
        it is handed the new stamp here, so the person who just chose a password is
        not sent straight to a sign-in form to type it. False if it is not a live
        session.
        """
        if not token:
            return False
        with self._lock:
            found = self._sessions.get(token)
            if found is None:
                return False
            found.stamp = stamp
            found.confirmed_at = self.clock()
        self._keep("restamp", token_key(token), stamp)
        return True

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)
        self._keep("delete", token_key(token))

    def _prune(self, now: float) -> None:
        for token in [t for t, s in self._sessions.items() if s.expires < now]:
            del self._sessions[token]


def cookie_value(header: str | None, name: str = COOKIE) -> str | None:
    """Pull one cookie out of a Cookie header without importing http.cookies.

    `SimpleCookie` raises on a malformed header, and a malformed header from the
    open internet must mean "not logged in" rather than a 500.
    """
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip() or None
    return None


__all__ = [
    "COOKIE",
    "GLOBAL_LOCKOUT_S",
    "GLOBAL_MAX_FAILURES",
    "GLOBAL_WINDOW_S",
    "LOCKOUT_S",
    "MAX_FAILURES",
    "SESSION_CONFIRM_S",
    "SESSION_TTL_S",
    "UNKNOWN_FOR_S",
    "Gate",
    "SessionStore",
    "cookie_value",
    "token_key",
]
