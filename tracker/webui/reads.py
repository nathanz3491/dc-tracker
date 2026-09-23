"""The console's read path: one engine for the life of the process, and a cache of
the expensive answers that a commit anywhere invalidates.

**One engine, not one per request.** Every request used to call `db.open_db`,
which builds an engine, runs three metadata queries and re-reads every migration
file to check that none is pending — about 6 ms before the request's own first
query. And the engines were never disposed, so their pooled connections stayed
open until the garbage collector found them: twelve SQLite connections after a
hundred requests, measured. Now the file is opened and its migrations checked
once, and the pool is bounded.

**The file's identity is part of the engine.** `scripts/sync_db.py` replaces a
database by renaming a new file over the old one. A connection opened before the
rename goes on reading the old file — on the host it stays readable after it is
unlinked — so a long-lived engine would have served the replaced database for as
long as the console ran, where a per-request engine picked up the new file on the
next request. So every use checks the path's `(st_dev, st_ino)`, and a different
file gets a new engine. That is one `stat` per request.

**The cache is keyed on `PRAGMA data_version`, not on a timer.** The console is
not the writer: `tracker` commands in another process commit whenever they like,
so a timed cache is either stale or pointless. SQLite answers the exact question.
On one connection, `data_version` changes when *another* connection has
committed and not otherwise — measured here: a commit from this process, one from
another process, and a WAL checkpoint each move it, and a rollback does not. One
long-lived read-only connection is kept to ask, and a value computed while the
answer was V is served until the answer is not V. A checkpoint therefore costs
one needless recompute, which is the right direction to be wrong in.

A value is stored under the version read *before* it was computed, so a commit
that lands during the computation leaves an entry the next request will not
match: the worst case is computing twice, never serving an answer older than the
data.

**What must never be cached here is anything about the reader.** A cached value
is served to every account on the console, so the callers put only
reader-independent payloads in it — the shell index, the capex rollup, the
publisher survey, the citations list at rest and per publisher — and add the
reader's own fields (who is signed in, whether they may edit a watchlist) per
request, outside it. Watchlists and the Updates page are per account and are not
cached at all.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from tracker.db import open_db, schema_version, session_scope

log = logging.getLogger(__name__)

T = TypeVar("T")

#: How many cached answers to keep. The fixed routes need four; the rest is room
#: for the citations list's per-publisher expansions, least recently used dropped
#: first, so reading every publisher in turn cannot grow the process.
CACHE_ENTRIES = 64

#: A database file, as the operating system identifies it. None when absent.
Identity = tuple[int, int]


def file_identity(path: Path) -> Identity | None:
    """`(st_dev, st_ino)` for `path`, which changes when the file is replaced."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


def _release(held: dict[str, Any]) -> None:
    """Close whatever a `ReadSide` still holds. Safe to call more than once."""
    probe = held.pop("probe", None)
    if probe is not None:
        probe.close()
    engine = held.pop("engine", None)
    if engine is not None:
        engine.dispose()


class ReadSide:
    """One console's database reads: the engine, the version probe, the cache.

    Thread-safe, because `ThreadingHTTPServer` gives every connection a thread.
    Each of the three has its own lock, and none is held while a query runs except
    the probe's, whose one query is a pragma. A cache miss takes a per-key lock
    around the computation, so twenty readers opening the capex view at once after
    a crawl commits cost one rollup, not twenty.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        #: What is open, in a dict the finaliser can reach without reaching `self`,
        #: so a console that is never closed — a test's — still releases its
        #: connections when it is collected rather than warning about them.
        self._held: dict[str, Any] = {}
        self._finalizer = weakref.finalize(self, _release, self._held)
        self._engine_identity: Identity | None = None
        self._probe_identity: Identity | None = None
        self._schema_version: int | None = None
        self._engine_lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache: OrderedDict[Hashable, tuple[Any, Any]] = OrderedDict()
        self._computing: dict[Hashable, threading.Lock] = {}

    # --- the engine ---------------------------------------------------------

    def engine(self) -> Engine:
        """The read-only engine for the file at `db_path` now.

        `open_db` raises `FileNotFoundError` or `MigrationError` when the file is
        missing or behind on migrations, and that is passed up rather than kept:
        nothing is remembered about a failure, so the request after `tracker init`
        opens cleanly.
        """
        identity = file_identity(self.db_path)
        with self._engine_lock:
            current = self._held.get("engine")
            if current is not None and identity == self._engine_identity:
                return current
            if current is not None:
                log.info("console: %s was replaced; reopening it", self.db_path)
                self._held.pop("engine").dispose()
            engine = open_db(self.db_path)
            self._schema_version = schema_version(engine)
            self._held["engine"] = engine
            self._engine_identity = identity
            return engine

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A read-only session on the shared engine."""
        with session_scope(self.engine(), commit=False) as session:
            yield session

    @property
    def schema_version(self) -> int:
        return self._schema_version or 0

    # --- what the cache is valid for ------------------------------------------

    def generation(self) -> tuple[Identity, int] | None:
        """The file, and how many commits the probe has seen on it. None if unknown.

        None means "do not cache": a missing file, or a probe that could not be
        opened or asked. The caller then computes the answer every time, which is
        what it did before any of this existed.
        """
        identity = file_identity(self.db_path)
        if identity is None:
            return None
        with self._probe_lock:
            probe = self._held.get("probe")
            if probe is not None and self._probe_identity != identity:
                self._held.pop("probe").close()
                probe = None
            try:
                if probe is None:
                    uri = f"file:{quote(self.db_path.resolve().as_posix(), safe='/:')}?mode=ro"
                    # Autocommit, so the probe never holds a read transaction open:
                    # a pinned snapshot would stop checkpoints from finishing and let
                    # the WAL grow for as long as the console runs.
                    probe = sqlite3.connect(
                        uri, uri=True, check_same_thread=False, isolation_level=None
                    )
                    self._held["probe"] = probe
                    self._probe_identity = identity
                (version,) = probe.execute("PRAGMA data_version").fetchone()
            except sqlite3.Error as exc:
                log.debug("console: could not read data_version (%s); not caching", exc)
                stale = self._held.pop("probe", None)
                if stale is not None:
                    stale.close()
                return None
        return (identity, int(version))

    # --- the cache ------------------------------------------------------------

    def cached(self, key: Hashable, compute: Callable[[], T]) -> T:
        """`compute()`, or the answer it gave while the database was as it is now.

        The value is shared by every request that asks for `key`, so it is treated
        as read-only by the callers — and it must not depend on who is asking.
        """
        generation = self.generation()
        if generation is None:
            return compute()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] == generation:
                self._cache.move_to_end(key)
                return hit[1]
            gate = self._computing.setdefault(key, threading.Lock())
        with gate:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit is not None and hit[0] == generation:
                    return hit[1]
            value = compute()
            with self._cache_lock:
                self._cache[key] = (generation, value)
                self._cache.move_to_end(key)
                while len(self._cache) > CACHE_ENTRIES:
                    dropped, _ = self._cache.popitem(last=False)
                    self._computing.pop(dropped, None)
        return value

    def close(self) -> None:
        """Dispose of the engine and close the probe. The next use reopens them."""
        with self._cache_lock:
            self._cache.clear()
        with self._engine_lock, self._probe_lock:
            _release(self._held)


__all__ = ["CACHE_ENTRIES", "ReadSide", "file_identity"]
