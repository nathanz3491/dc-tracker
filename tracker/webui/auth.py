"""The gate in front of the console.

On loopback the console needs no password: reaching it already means having the
machine. The moment it is published — a Cloudflare tunnel, a reverse proxy,
anything — that stops being true, and what is behind it is a process that runs
shell-free but real commands against a real database and can spend money.

So the rules here are deliberately not "good enough for localhost":

* **Everything is behind it.** Not just the page — every API route, every static
  asset. An unauthenticated request gets the login form or a 401 and nothing else.
* **Constant-time comparison**, so the password cannot be recovered a character at
  a time from response timing.
* **A lockout, not just a check.** A published URL means an unattended login form.
  A short human-memorable password is only safe if guessing is slow, so failures
  are counted and the gate closes for a while.
* **Session tokens are random and server-side.** The cookie carries no claim the
  server has to trust — it is a lookup key, revocable, and it expires.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: How long a login lasts. Long enough to work a session, short enough that a
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
GLOBAL_MAX_FAILURES = 40
GLOBAL_LOCKOUT_S = 15 * 60

#: Below this a password is a typo rather than a secret. Deliberately low: the
#: rate limit above is the defence, and an arbitrary length rule mostly persuades
#: people to write the password on a note.
MIN_PASSWORD_LEN = 6

#: The cookie holds a lookup key, never a claim. Named for the app so it cannot
#: collide with anything else on localhost.
COOKIE = "dc_console_session"


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0


@dataclass
class Gate:
    """Password check, session store and lockout for one console instance."""

    password: str | None = None
    session_ttl: int = SESSION_TTL_S
    max_failures: int = MAX_FAILURES
    lockout_s: int = LOCKOUT_S
    global_max_failures: int = GLOBAL_MAX_FAILURES
    global_lockout_s: int = GLOBAL_LOCKOUT_S

    _sessions: dict[str, float] = field(default_factory=dict, repr=False)
    _attempts: dict[str, _Attempts] = field(default_factory=dict, repr=False)
    _global: _Attempts = field(default_factory=_Attempts, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def required(self) -> bool:
        """False when no password is configured — loopback-only, open console."""
        return bool(self.password)

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

    def _fail(self, client: str) -> None:
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

    def _succeed(self, client: str) -> None:
        with self._lock:
            self._attempts.pop(client, None)
            # A correct password says the traffic is not an attack, so the global
            # counter resets too. The lockout itself is left alone: if the gate is
            # shut, `locked_for` has already refused this request.
            self._global.count = 0

    # --- the check --------------------------------------------------------

    def attempt(self, offered: str, *, client: str) -> str | None:
        """Return a fresh session token, or None if the password is wrong.

        Raises nothing on lockout — the caller checks `locked_for` first, so that
        a locked client is told how long rather than being told "wrong password"
        and left guessing about which.
        """
        if not self.required:
            return self.issue()
        # compare_digest on bytes: it is only constant-time over equal-length
        # inputs, and encoding first avoids a unicode fast path.
        ok = hmac.compare_digest((offered or "").encode("utf-8"), self.password.encode("utf-8"))
        if not ok:
            self._fail(client)
            return None
        self._succeed(client)
        return self.issue()

    # --- sessions ---------------------------------------------------------

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._sessions[token] = time.monotonic() + self.session_ttl
        return token

    def valid(self, token: str | None) -> bool:
        if not self.required:
            return True
        if not token:
            return False
        with self._lock:
            expires = self._sessions.get(token)
            if expires is None:
                return False
            if expires < time.monotonic():
                del self._sessions[token]
                return False
            return True

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def _prune(self) -> None:
        now = time.monotonic()
        for token in [t for t, exp in self._sessions.items() if exp < now]:
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
    "MIN_PASSWORD_LEN",
    "SESSION_TTL_S",
    "Gate",
    "cookie_value",
]
