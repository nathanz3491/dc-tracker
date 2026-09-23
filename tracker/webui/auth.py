"""The gate in front of the console: sessions, and how fast anyone may knock.

With no accounts at all the console needs no sign-in: reaching loopback already
means having the machine, and `tracker serve` should need no setup. The moment an
account exists — or the console is published through a tunnel or a proxy — that
stops being true, and everything behind it is a real database that can spend LLM
tokens on a model panel.

So the rules here are deliberately not "good enough for localhost":

* **Everything is behind it.** Not just the page — every API route, every static
  asset. An unauthenticated request gets the login form or a 401 and nothing else.
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

**Sessions are in memory, so a restart signs everybody out.** That is not an
oversight: the host's poller restarts this process whenever a commit lands, and a
session that survived a restart would have to be persisted somewhere the console
can write — which is the one thing a read-only console does not have.

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

import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

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
#: **Nothing is counted per email**, and that is a decision rather than an
#: omission. A per-address counter lets anyone who knows an address lock its owner
#: out, and the global counter already bounds the rate without handing out that
#: lever.
GLOBAL_MAX_FAILURES = 40
GLOBAL_LOCKOUT_S = 15 * 60

#: The cookie holds a lookup key, never a claim. Named for the app so it cannot
#: collide with anything else on localhost.
COOKIE = "dc_console_session"


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
    global_lockout_s: int = GLOBAL_LOCKOUT_S
    #: Injectable so a test can move time rather than wait it out. Monotonic, so a
    #: wall-clock change on the host cannot extend a session or end a lockout.
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    _sessions: dict[str, _Session] = field(default_factory=dict, repr=False)
    _attempts: dict[str, _Attempts] = field(default_factory=dict, repr=False)
    _global: _Attempts = field(default_factory=_Attempts, repr=False)
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
                self._global.locked_until,
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

            self._global.count += 1
            if self._global.count >= self.global_max_failures:
                self._global.locked_until = now + self.global_lockout_s
                self._global.count = 0
                log.warning(
                    "console: %d failed sign-ins across all clients; closing the gate for %ds",
                    self.global_max_failures,
                    self.global_lockout_s,
                )

    def succeed(self, client: str) -> None:
        """Forget this client's failures, and the shared ones."""
        with self._lock:
            self._attempts.pop(client, None)
            # A correct password says the traffic is not an attack, so the global
            # counter resets too. The lockout itself is left alone: if the gate is
            # shut, `locked_for` has already refused this request.
            self._global.count = 0

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
        return token

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
                return None
            if found.expires < now:
                del self._sessions[token]
                return None
            if confirm is None or now - found.confirmed_at < self.session_confirm_s:
                return found.account_id

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

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

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
    "LOCKOUT_S",
    "MAX_FAILURES",
    "SESSION_CONFIRM_S",
    "SESSION_TTL_S",
    "Gate",
    "cookie_value",
]
