"""Console sign-ins kept in the database, so a restart does not sign anybody out.

The gate (`webui.auth.Gate`) holds sessions in memory and touches no database; this is
the `SessionStore` the console hands it. One row per session in `console_session`
(migration 0054), keyed by the token's SHA-256 — the gate never passes the token —
so a copy of the database signs nobody in.

**A write never holds a sign-in up.** Each is its own short transaction on the
console's write engine with a quarter-second busy timeout, the same as the sign-in's
`last_seen_at` stamp: while the nightly loop or a CLI command holds SQLite's write
lock, the write fails, the gate logs it, and the session works as before — it just
would not survive a restart that night.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, update

from tracker.models import ConsoleSession, utcnow

if TYPE_CHECKING:
    from tracker.webui.server import Console

#: How long a session write waits for SQLite's write lock.
WRITE_WAIT_MS = 250


def _utc(seconds: float) -> dt.datetime:
    """Wall-clock seconds as the schema's naive UTC."""
    return dt.datetime.fromtimestamp(seconds, dt.UTC).replace(tzinfo=None)


class DatabaseSessions:
    """`auth.SessionStore` on the console's database."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def _write(self) -> Any:
        return self.console.write_session(busy_timeout_ms=WRITE_WAIT_MS)

    def save(self, key: str, account_id: int, stamp: str, expires_at: float) -> None:
        now = utcnow()
        with self._write() as session:
            # Expired rows go as new ones arrive, so the table holds about one row
            # per signed-in reader and nothing has to sweep it.
            session.execute(delete(ConsoleSession).where(ConsoleSession.expires_at < now))
            session.merge(
                ConsoleSession(
                    token_hash=key,
                    account_id=account_id,
                    stamp=stamp,
                    expires_at=_utc(expires_at),
                    created_at=now,
                )
            )

    def load(self, key: str) -> tuple[int, str, float] | None:
        with self.console.read_session() as session:
            row = session.get(ConsoleSession, key)
            if row is None:
                return None
            return row.account_id, row.stamp, row.expires_at.replace(tzinfo=dt.UTC).timestamp()

    def restamp(self, key: str, stamp: str) -> None:
        with self._write() as session:
            session.execute(
                update(ConsoleSession).where(ConsoleSession.token_hash == key).values(stamp=stamp)
            )

    def delete(self, key: str) -> None:
        with self._write() as session:
            session.execute(delete(ConsoleSession).where(ConsoleSession.token_hash == key))


__all__ = ["WRITE_WAIT_MS", "DatabaseSessions"]
