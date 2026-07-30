"""Shared fixtures.

Every DB fixture uses a real file under `tmp_path` rather than `:memory:`.
In-memory SQLite rejects `PRAGMA journal_mode=WAL` and, more importantly, hides
file-level behaviour (the read-only `mode=ro` guard, WAL sibling files) that we
specifically want covered.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from tracker.config import get_settings
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
    * **No `.env` leakage** into settings.
    """
    monkeypatch.setenv("TRACKER_POLITENESS_DELAY_S", "0")
    monkeypatch.setenv("TRACKER_RETRY_BACKOFF_BASE_S", "0")
    monkeypatch.delenv("TRACKER_MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
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
