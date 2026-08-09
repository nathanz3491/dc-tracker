"""Shared fixtures.

Every DB fixture uses a real file under `tmp_path` rather than `:memory:`.
In-memory SQLite rejects `PRAGMA journal_mode=WAL` and, more importantly, hides
file-level behaviour (the read-only `mode=ro` guard, WAL sibling files) that we
specifically want covered.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import Engine

from tracker.config import Settings, get_settings
from tracker.db import init_db, session_scope

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _fast_and_keyless_settings(monkeypatch):
    """Isolate every test from the operator's real environment.

    Three things this guarantees:

    * **No politeness sleep.** `fetch_all` waits between requests to avoid
      hammering a newsroom. Against a fake fetcher that is pure dead time, and it
      dominated the suite's runtime.
    * **No API key**, even if the developer has one exported. Tests that reach the
      LLM must do so through an injected fake, and this makes a missed injection
      fail loudly rather than quietly spending money.
    * **No `.env` leakage** into settings, by any route.
    """
    # Every TRACKER_* variable goes, not a hand-listed few.
    #
    # **Why the list was not enough.** Neutralizing `env_file` below only stops
    # pydantic reading the file; it cannot stop something else having already
    # copied it into `os.environ`, which pydantic always consults. Installing the
    # `[crawl]` extra does exactly that: `import crawl4ai` pulls in a litellm fork
    # that calls `load_dotenv()` at import time, so the developer's whole `.env` —
    # search keys, provider pin, tunnel hostname, and the API key itself — lands
    # in the process environment. Measured: `TRACKER_SERPER_API_KEY` absent before
    # the import and present after it.
    #
    # Four tests then failed, all of them asserting "nothing is configured", and
    # they failed only on a machine that had both the extra and a real `.env`.
    # That is the same class of bug as the colour probes reading the developer's
    # own database: a suite whose result depends on the operator's setup.
    for name in [key for key in os.environ if key.startswith("TRACKER_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    monkeypatch.setenv("TRACKER_POLITENESS_DELAY_S", "0")
    monkeypatch.setenv("TRACKER_RETRY_BACKOFF_BASE_S", "0")

    # Deleting the environment variables is not enough: `.env` is also read, and
    # once a real one exists on the developer's machine the suite silently starts
    # depending on it — a test asserting "no key configured" passed on CI and
    # failed locally. Neutralize the file itself so tests see defaults only.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tracker.db"


@pytest.fixture
def engine(db_path: Path) -> Engine:
    """A fully migrated, empty database."""
    eng, applied = init_db(db_path)
    assert applied, "expected migrations to be applied to a fresh database"
    return eng


@pytest.fixture
def session(engine: Engine):
    with session_scope(engine) as s:
        yield s


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def logical_snapshot():
    """Every row of every table, for comparing data rather than bytes.

    Raw file bytes are the wrong instrument for "did this write?": SQLite in WAL
    mode checkpoints the write-ahead log into the main file at times of its own
    choosing, so the file legitimately changes without any data changing. A byte
    comparison then passes or fails depending on when garbage collection closed
    the previous connection.

    A fixture rather than a module-level helper so the CLI tests and the console
    tests share one definition of the guarantee.
    """
    import sqlite3
    from contextlib import closing

    def snapshot(db: Path) -> dict[str, list[tuple]]:
        # `sqlite3.connect` as a context manager commits but does NOT close,
        # which leaks the handle and raises ResourceWarning under pytest.
        with closing(sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            return {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in sorted(tables)}

    return snapshot


@pytest.fixture(autouse=True)
def _clear_the_overview_cache():
    """The briefing cache is module-global and keyed on (project id, content hash).

    Correct in production — one process, one database, and the hash is what makes
    reuse safe. In a test run it means one test's briefing is served to the next
    test's project #1, because the fixtures build the same row and the hash
    matches. Cleared between tests so a cache hit is never accidental.
    """
    from tracker import overview

    overview._cache.clear()
    yield
    overview._cache.clear()
