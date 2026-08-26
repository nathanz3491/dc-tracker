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

One consequence worth stating rather than discovering. `tracker users` runs in a
*different process*, so it cannot reach into this dictionary: deleting an account
takes effect on that account's next request, because `Handler._account` resolves
the session to a row and a missing row is not a session — but **changing a
password does not sign the old cookie out**. The 12-hour TTL is what bounds that.
If a token has to die now, restart the console.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: How long a sign-in lasts. Long enough to work a session, short enough that a
#: forgotten open tab does not stay a key forever.
SESSION_TTL_S = 12 * 60 * 60

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


@dataclass
class Gate:
    """Session store and lockout for one console instance.

    Holds no password and performs no credential check — see the module
    docstring. `fail`, `succeed` and `grant` are the three things a login handler
    does, in whichever order the outcome dictates.
    """

    session_ttl: int = SESSION_TTL_S
    max_failures: int = MAX_FAILURES
    lockout_s: int = LOCKOUT_S
    global_max_failures: int = GLOBAL_MAX_FAILURES
    global_lockout_s: int = GLOBAL_LOCKOUT_S

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
        now = time.monotonic()
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
        now = time.monotonic()
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

    def grant(self, account_id: int) -> str:
        """A fresh token for one account."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._sessions[token] = _Session(account_id, time.monotonic() + self.session_ttl)
        return token

    def session_for(self, token: str | None) -> int | None:
        """Which account this token signs in as, or None if it does not.

        Returns an account id rather than a boolean because that id is what every
        route downstream needs: a watchlist read is a question about one person,
        and a handler that had to ask twice could ask two different gates.
        """
        if not token:
            return None
        with self._lock:
            found = self._sessions.get(token)
            if found is None:
                return None
            if found.expires < time.monotonic():
                del self._sessions[token]
                return None
            return found.account_id

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def _prune(self) -> None:
        now = time.monotonic()
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
    "SESSION_TTL_S",
    "Gate",
    "cookie_value",
]
