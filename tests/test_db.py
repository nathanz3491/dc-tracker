"""Migration runner, connection pragmas, and the models-vs-SQL drift gate.

The drift test is what makes it safe to define the schema twice — once in
`migrations/*.sql` (authoritative at runtime) and once in `models.py` (used for
typed queries). Without it, the two definitions silently diverge and queries
start returning wrong results against a schema that no longer matches.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import re
import time
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from tracker.config import install_root
from tracker.db import (
    AlreadyRunning,
    MigrationError,
    acquire_write_lock,
    discover_migrations,
    init_db,
    make_engine,
    migrations_dir,
    open_db,
    run_migrations,
    schema_version,
    split_sql,
)
from tracker.models import Base

#: Bookkeeping table owned by db.py, deliberately absent from models.py.
_RUNTIME_ONLY_TABLES = {"schema_version"}


# --- Migration discovery and splitting -------------------------------------


def test_discover_migrations_is_ordered_and_contiguous():
    migrations = discover_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "init"
    assert all(m.sql.strip() for m in migrations)


def test_migrations_are_found_from_an_unrelated_directory(tmp_path: Path, monkeypatch):
    """`tracker init` must work from anywhere once the CLI is on PATH.

    Migrations ship with the code, so they are located relative to the installed
    package. A CWD-relative lookup sent `init` hunting for a `migrations/` folder
    in whatever directory the operator happened to be standing in.
    """
    monkeypatch.chdir(tmp_path)
    found = migrations_dir()
    assert found.is_dir(), f"{found} should exist regardless of cwd"
    expected = [m.version for m in discover_migrations()]
    assert expected, "no migrations discovered"

    engine, applied = init_db(tmp_path / "elsewhere.db")
    assert applied == expected
    assert schema_version(engine) == expected[-1]


def test_project_dotenv_is_read_from_any_directory(tmp_path: Path, monkeypatch):
    """The API key lives in the project's .env, and `tracker` runs from anywhere.

    pydantic-settings resolves a relative `env_file` against the CURRENT
    directory, so a bare ".env" was invisible outside the project root — which is
    the normal case now that the CLI is on PATH.
    """
    from tracker.config import Settings, get_settings

    # conftest neutralizes env_file for every test; this one is *about* env_file,
    # so put the real setting back for the duration.
    monkeypatch.setitem(Settings.model_config, "env_file", (install_root() / ".env", ".env"))

    env_file = install_root() / ".env"
    existed = env_file.exists()
    original = env_file.read_bytes() if existed else None
    env_file.write_text("TRACKER_DEEPSEEK_MODEL=FromProjectDotenv\n", encoding="utf-8")
    try:
        monkeypatch.chdir(tmp_path)
        get_settings.cache_clear()
        assert Settings().deepseek_model == "FromProjectDotenv"
    finally:
        if original is None:
            env_file.unlink(missing_ok=True)
        else:
            env_file.write_bytes(original)
        get_settings.cache_clear()


def test_install_root_is_independent_of_cwd(tmp_path: Path, monkeypatch):
    before = install_root()
    monkeypatch.chdir(tmp_path)
    assert install_root() == before
    assert (install_root() / "tracker").is_dir()


def test_discover_migrations_rejects_bad_filename(tmp_path: Path):
    (tmp_path / "1_init.sql").write_text("CREATE TABLE a (id INTEGER);", encoding="utf-8")
    with pytest.raises(MigrationError, match="NNNN_lower_snake"):
        discover_migrations(tmp_path)


def test_discover_migrations_rejects_version_gap(tmp_path: Path):
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE a (id INTEGER);", encoding="utf-8")
    (tmp_path / "0003_c.sql").write_text("CREATE TABLE c (id INTEGER);", encoding="utf-8")
    with pytest.raises(MigrationError, match="contiguous"):
        discover_migrations(tmp_path)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1;", 1),
        ("SELECT 1; SELECT 2;", 2),
        ("-- just a comment\n", 0),
        ("SELECT 1; -- trailing comment", 1),
        ("/* block */ SELECT 1;", 1),
        # A semicolon inside a string literal must not split the statement.
        ("INSERT INTO t VALUES ('a;b');", 1),
        # Doubled quotes are an escaped quote, not the end of the literal.
        ("INSERT INTO t VALUES ('it''s; fine');", 1),
    ],
)
def test_split_sql(sql: str, expected: int):
    assert len(split_sql(sql)) == expected


def test_migrations_split_into_expected_statement_counts():
    """Guards against a stray semicolon silently merging or splitting DDL."""
    by_name = {m.name: split_sql(m.sql) for m in discover_migrations()}
    # 0001: project + 5 indexes, source + 2 indexes, ingest_url + 1 index
    assert len(by_name["init"]) == 11
    # 0002: event + 2 indexes
    assert len(by_name["add_events"]) == 3
    # 0003: rebuild ingest_url (create, copy, drop, rename) + 2 indexes
    assert len(by_name["discovery_queue"]) == 6


# --- Applying migrations ----------------------------------------------------


def test_init_db_applies_then_is_idempotent(db_path: Path):
    # Derived rather than hardcoded, so adding a migration does not require
    # editing this test -- contiguity and ordering are asserted separately.
    expected = [m.version for m in discover_migrations()]
    _, first = init_db(db_path)
    assert first == expected
    engine, second = init_db(db_path)
    assert second == [], "re-running init must apply nothing"
    assert schema_version(engine) == expected[-1]


def test_modified_applied_migration_is_refused(tmp_path: Path):
    """Migrations are immutable once applied; editing one must fail loudly."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    path = mig_dir / "0001_init.sql"
    path.write_text("CREATE TABLE a (id INTEGER PRIMARY KEY);", encoding="utf-8")

    engine = make_engine(tmp_path / "t.db")
    assert run_migrations(engine, discover_migrations(mig_dir)) == [1]

    path.write_text("CREATE TABLE a (id INTEGER PRIMARY KEY, extra TEXT);", encoding="utf-8")
    with pytest.raises(MigrationError, match="modified after it was applied"):
        run_migrations(engine, discover_migrations(mig_dir))


@pytest.mark.parametrize(
    ("broken", "fixed", "leftover"),
    [
        (
            "CREATE TABLE b (id INTEGER PRIMARY KEY);\nCREATE TABLE a (id INTEGER PRIMARY KEY);",
            "CREATE TABLE b (id INTEGER PRIMARY KEY);\nCREATE TABLE c (id INTEGER PRIMARY KEY);",
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'b'",
        ),
        (
            "ALTER TABLE a ADD COLUMN note TEXT;\nALTER TABLE a ADD COLUMN note TEXT;",
            "ALTER TABLE a ADD COLUMN note TEXT;\nALTER TABLE a ADD COLUMN seen TEXT;",
            "SELECT 1 FROM pragma_table_info('a') WHERE name = 'note'",
        ),
    ],
    ids=["create-table", "add-column"],
)
def test_a_failed_migration_leaves_no_trace_and_the_fixed_one_applies(
    tmp_path: Path, broken: str, fixed: str, leftover: str
):
    """A migration is one transaction, DDL included.

    Python's sqlite3 driver opens a transaction only before INSERT, UPDATE and
    DELETE, so a CREATE TABLE or ALTER TABLE ran outside `engine.begin()` and
    committed on the spot. A migration whose second statement failed left its
    first behind with no version row, and every later `tracker init` died on
    "already exists" — with the console refusing the database meanwhile, because
    it is behind on migrations, until somebody undid the half by hand.
    """
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    first = mig_dir / "0001_init.sql"
    first.write_text("CREATE TABLE a (id INTEGER PRIMARY KEY);", encoding="utf-8")
    second = mig_dir / "0002_second.sql"
    second.write_text(broken, encoding="utf-8")
    engine = make_engine(tmp_path / "t.db")

    with pytest.raises(OperationalError):
        run_migrations(engine, discover_migrations(mig_dir))
    with engine.connect() as conn:
        assert conn.execute(text(leftover)).first() is None, "the half that ran must not stay"
    assert schema_version(engine) == 1

    second.write_text(fixed, encoding="utf-8")
    assert run_migrations(engine, discover_migrations(mig_dir)) == [2]
    assert schema_version(engine) == 2


def test_a_migration_another_process_applied_meanwhile_is_not_applied_twice(
    tmp_path: Path, monkeypatch
):
    """Every writing command runs `init_db`, and so does the deployer, so two can
    find the same migration pending at once. The second used to run it anyway and
    die on "already exists" as it started. Pending is now re-read inside the write
    transaction, which cannot begin until the first has committed."""
    import tracker.db as db_mod

    db = tmp_path / "t.db"
    engine, applied = init_db(db)
    assert applied, "applied by the first process"
    # The second process's view, taken before the first committed.
    monkeypatch.setattr(db_mod, "applied_versions", lambda _engine: {})

    assert run_migrations(make_engine(db)) == []
    assert schema_version(engine) == max(applied)


def test_0003_upgrades_an_existing_database_without_losing_rows(tmp_path: Path):
    """0003 rebuilds ingest_url via DROP TABLE, so the copy step must be right.

    Exercises the real upgrade path — apply up to v2, write a row, then migrate —
    rather than only the fresh-install path that every other test takes.
    """
    migrations = discover_migrations()
    engine = make_engine(tmp_path / "upgrade.db")
    run_migrations(engine, [m for m in migrations if m.version <= 2])

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ingest_url (url, run_id, status, attempts, error) "
                "VALUES ('https://a.test/1', 'r1', 'ok', 2, 'none')"
            )
        )

    # Everything from 3 up is pending here, so compute it rather than pinning a
    # literal that every later migration would have to come back and edit.
    assert run_migrations(engine, migrations) == [m.version for m in migrations if m.version > 2]

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT url, run_id, status, attempts, error, title FROM ingest_url")
        ).all()
        assert rows == [("https://a.test/1", "r1", "ok", 2, "none", None)]

        # Indexes are dropped along with the old table and must be recreated.
        indexes = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='ingest_url' AND name NOT LIKE 'sqlite_%'"
                )
            )
        }
        assert indexes == {"ix_ingest_url_status", "ix_ingest_url_published_at"}

    # And the whole point of the migration: the new status is now accepted.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ingest_url (url, run_id, status, title, feed) "
                "VALUES ('https://b.test/2', 'r2', 'discovered', 'A headline', 'dcd')"
            )
        )


def test_discovered_status_was_rejected_before_0003(tmp_path: Path):
    """Confirms the migration is what enables it, not something else."""
    migrations = discover_migrations()
    engine = make_engine(tmp_path / "old.db")
    run_migrations(engine, [m for m in migrations if m.version <= 2])
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO ingest_url (url, run_id, status) "
                "VALUES ('https://c.test/3', 'r3', 'discovered')"
            )
        )


def test_open_db_rejects_uninitialized_file(tmp_path: Path):
    stray = tmp_path / "empty.db"
    make_engine(stray).connect().close()  # create the file, apply no schema
    with pytest.raises(MigrationError, match="tracker init"):
        open_db(stray)


def test_open_db_missing_file_says_run_init(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="tracker init"):
        open_db(tmp_path / "nope.db")


def test_open_db_refuses_a_database_behind_on_migrations(tmp_path: Path):
    """Read commands query tables an older database does not have.

    Observed with a real v3 database once `risk` landed: `tracker risks` produced a
    raw "no such table" traceback out of SQLAlchemy. A read command opens the file
    `mode=ro`, so it cannot migrate on the operator's behalf either — the only
    useful thing it can do is say which command will.
    """
    migrations = discover_migrations()
    db = tmp_path / "behind.db"
    engine = make_engine(db)
    run_migrations(engine, migrations[:-1])
    engine.dispose()

    with pytest.raises(MigrationError, match="tracker init"):
        open_db(db)


def test_open_db_accepts_a_fully_migrated_database(tmp_path: Path):
    db = tmp_path / "current.db"
    engine = make_engine(db)
    run_migrations(engine)
    engine.dispose()
    assert open_db(db) is not None


# --- Pragmas and guarantees -------------------------------------------------


def test_foreign_keys_are_enforced(engine: Engine):
    """SQLite ignores FKs unless the pragma is set on every connection."""
    with engine.begin() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO source (project_id, url, source_type) "
                    "VALUES (99999, 'https://example.com', 'manual')"
                )
            )


def test_readonly_engine_refuses_writes(engine: Engine, db_path: Path):
    del engine  # ensure the file exists and is migrated
    ro = open_db(db_path)
    with ro.connect() as conn, pytest.raises(OperationalError, match="readonly"):
        conn.execute(
            text(
                "INSERT INTO project (name, company, city, state, dedup_key) "
                "VALUES ('n', 'c', 'ci', 'WI', 'k')"
            )
        )


def test_project_requires_a_city_or_county(engine: Engine):
    """ck_project_locality: an ISO row has county, a news row has city, but a
    row with neither has no location at all and must be rejected."""
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO project (name, company, state, dedup_key) VALUES ('n', 'c', 'WI', 'k')"
            )
        )


def test_excerpt_length_is_capped(engine: Engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO project (id, name, company, city, state, dedup_key) "
                "VALUES (1, 'n', 'c', 'ci', 'WI', 'k')"
            )
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO source (project_id, url, source_type, excerpt) "
                    "VALUES (1, 'https://example.com', 'manual', :e)"
                ),
                {"e": "x" * 501},
            )


def test_finding_a_citation_by_its_url_uses_an_index(engine: Engine):
    """`backfill dates` asks, for every undated queue row, whether any citation
    quotes that URL. The only index holding `source.url` leads with `project_id`
    (it is the UNIQUE constraint's), so each question scanned the whole table:
    909 ms on a copy of the live database, 5,535 rows asking of 3,421 sources.

    Asserted on the plan of the SQL the function really sends, captured as it
    runs, so a rewrite of the query that can no longer use the index fails here too.
    """
    from sqlalchemy import event

    from tracker import dates
    from tracker.db import session_scope

    sent: list[tuple[str, object]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        sent.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with session_scope(engine, commit=False) as session:
            dates.undated_urls(session)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    statement, parameters = next((s, p) for s, p in sent if "FROM ingest_url" in s)
    with engine.connect() as conn:
        plan = [
            row[3] for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)
        ]
    assert any("SEARCH source USING COVERING INDEX ix_source_url" in step for step in plan), plan


def test_event_is_idempotent_per_type_and_date(engine: Engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO project (id, name, company, city, state, dedup_key) "
                "VALUES (1, 'n', 'c', 'ci', 'WI', 'k')"
            )
        )
        ins = text(
            "INSERT INTO event (project_id, event_date, event_type, description) "
            "VALUES (1, '2025-03-01', 'announced', 'd')"
        )
        conn.execute(ins)
        with pytest.raises(IntegrityError):
            conn.execute(ins)


# --- The single-writer lock -------------------------------------------------
#
# Real processes, not threads: the lock is a file shared between separate
# `tracker` invocations, and a pid is how it tells a live holder from a dead one.
# Every contender waits at one barrier, so they all reach the lock in the same
# instant rather than a process start-up apart — 300 ms apart, even the old
# check-then-write lock let exactly one through, which is why the race survived.


def _contend(db_path: str, barrier, outcomes, done) -> None:
    """One process racing the others for the lock; reports what it got."""
    from tracker.db import AlreadyRunning, acquire_write_lock

    try:
        barrier.wait(timeout=60)
        try:
            release = acquire_write_lock(db_path, command="contender")
        except AlreadyRunning:
            outcomes.put("refused")
            return
        outcomes.put("acquired")
        # Held until every contender has answered, so a late one cannot slip in
        # after the winner lets go and be counted as a second holder.
        done.wait(timeout=60)
        release()
    except Exception as exc:  # reported, not raised: a dead child cannot fail the test
        outcomes.put(f"error: {exc!r}")


def _exit_at_once() -> None:
    """A process that exists only to leave a pid nothing is running under."""


def _race(db_path: Path, contenders: int) -> list[str]:
    ctx = multiprocessing.get_context("spawn")
    barrier, outcomes, done = ctx.Barrier(contenders), ctx.Queue(), ctx.Event()
    procs = [
        ctx.Process(target=_contend, args=(str(db_path), barrier, outcomes, done))
        for _ in range(contenders)
    ]
    for proc in procs:
        proc.start()
    try:
        return sorted(outcomes.get(timeout=120) for _ in procs)
    finally:
        done.set()
        for proc in procs:
            proc.join(timeout=60)
            if proc.is_alive():
                proc.terminate()


def _dead_pid() -> int:
    proc = multiprocessing.get_context("spawn").Process(target=_exit_at_once)
    proc.start()
    proc.join(timeout=60)
    return proc.pid


def test_only_one_of_several_simultaneous_writers_gets_the_lock(tmp_path: Path):
    """Six at once, one holder. The old lock checked for the file and then wrote it,
    and every process that looked before any had written went on to write its own:
    six started together all "acquired" it, in five trials of five."""
    outcomes = _race(tmp_path / "tracker.db", contenders=6)
    assert outcomes == ["acquired"] + ["refused"] * 5


def test_a_stale_lock_is_reclaimed_by_exactly_one_of_several_writers(tmp_path: Path):
    """The reclaim is a check-then-act of its own: each contender reads the dead pid,
    and a contender that deletes the file *after* another has already replaced it
    deletes a live lock. Only one may come out holding it."""
    db = tmp_path / "tracker.db"
    lock = Path(f"{db}.lock")
    stale = f"{_dead_pid()} sync 2026-01-01 00:00:00"
    lock.write_text(stale, encoding="utf-8")

    outcomes = _race(db, contenders=6)

    assert outcomes == ["acquired"] + ["refused"] * 5
    assert not lock.exists(), "the winner released on the way out"


def test_a_dead_holders_lock_is_reclaimed_and_says_so(tmp_path: Path, caplog):
    """The line the production logs carry, `reclaiming a stale lock from pid N`."""
    db = tmp_path / "tracker.db"
    dead = _dead_pid()
    Path(f"{db}.lock").write_text(f"{dead} sync 2026-01-01 00:00:00", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="tracker.db"):
        release = acquire_write_lock(db, command="sync")
    try:
        assert f"reclaiming a stale lock from pid {dead}" in caplog.text
        assert Path(f"{db}.lock").read_text(encoding="utf-8").split()[0] == str(os.getpid())
    finally:
        release()


def test_a_live_holder_is_refused_by_name(tmp_path: Path):
    db = tmp_path / "tracker.db"
    release = acquire_write_lock(db, command="merge")
    try:
        with pytest.raises(AlreadyRunning, match="merge"):
            acquire_write_lock(db, command="sync")
    finally:
        release()
    acquire_write_lock(db, command="sync")()


def test_releasing_twice_does_not_delete_the_next_runs_lock(tmp_path: Path):
    """`sync` releases explicitly and again from `atexit`. Between the two, another
    run may take the lock — and an unconditional delete on the second call removed
    that run's lock out from under it, letting a third one in beside it."""
    db = tmp_path / "tracker.db"
    lock = Path(f"{db}.lock")
    release = acquire_write_lock(db, command="sync")
    release()

    theirs = f"{os.getpid()} enrich 2026-01-01 00:00:00"
    lock.write_text(theirs, encoding="utf-8")
    release()

    assert lock.read_text(encoding="utf-8") == theirs


def test_a_lock_it_is_not_permitted_to_create_is_reported_as_that(tmp_path: Path, monkeypatch):
    """Windows refuses to open a name another process is still deleting, which is
    worth waiting out. Anywhere else a PermissionError is the real answer — a data
    directory this user cannot write — and waiting ten seconds to then blame
    another run would hide it. On Windows too, once the wait is up, the error
    that explains it is the one raised."""
    import tracker.db as db_mod

    real_open = os.open

    def refuse(path, flags, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse)
    monkeypatch.setattr(db_mod, "_WINDOWS", False, raising=False)
    started = time.monotonic()
    with pytest.raises(PermissionError):
        acquire_write_lock(tmp_path / "tracker.db")
    assert time.monotonic() - started < 2, "a permission problem is not contention"

    monkeypatch.setattr(db_mod, "_WINDOWS", True, raising=False)
    monkeypatch.setattr(db_mod, "_CONTENTION_TIMEOUT_S", 0.05)
    with pytest.raises(PermissionError):
        acquire_write_lock(tmp_path / "tracker.db")


def test_an_empty_lock_file_is_a_lock_being_taken_until_it_is_old(tmp_path: Path):
    """An exclusive create and the write of the holder's pid are two calls, so a
    racing reader can catch the file empty. Reclaiming it then would delete the
    winner's lock between its two calls; a file still empty long after is one
    whose creator died there, and is reclaimed like any other stale lock."""
    db = tmp_path / "tracker.db"
    lock = Path(f"{db}.lock")
    lock.write_text("", encoding="utf-8")

    with pytest.raises(AlreadyRunning):
        acquire_write_lock(db)

    long_ago = time.time() - 3600
    os.utime(lock, (long_ago, long_ago))
    acquire_write_lock(db)()


# --- The drift gate ---------------------------------------------------------


def _affinity(declared: str) -> str:
    """SQLite type affinity, per the rules in the SQLite docs.

    Compared alongside the declared type so that a change from e.g. REAL to
    FLOAT (same affinity, different keyword) is reported as a type-name
    mismatch rather than passing silently.
    """
    t = declared.upper()
    if "INT" in t:
        return "INTEGER"
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in t or not t:
        return "BLOB"
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _columns(conn, table: str) -> dict[str, tuple[str, str, int, int, str | None]]:
    out = {}
    for row in conn.execute(text(f"PRAGMA table_info('{table}')")).mappings():
        pk = int(row["pk"])
        # `INTEGER PRIMARY KEY` (rowid alias) reports notnull=0 while
        # SQLAlchemy's table-level `PRIMARY KEY (id)` reports notnull=1. Both
        # are non-nullable in practice, so normalize.
        notnull = 1 if pk else int(row["notnull"])
        out[row["name"]] = (
            row["type"].upper(),
            _affinity(row["type"]),
            notnull,
            pk,
            row["dflt_value"],
        )
    return out


def _indexes(conn, table: str) -> dict[str, tuple[int, tuple[str, ...]]]:
    out = {}
    for row in conn.execute(text(f"PRAGMA index_list('{table}')")).mappings():
        cols = tuple(
            r["name"] for r in conn.execute(text(f"PRAGMA index_info('{row['name']}')")).mappings()
        )
        out[row["name"]] = (int(row["unique"]), cols)
    return out


def _foreign_keys(conn, table: str) -> set[tuple[str, str, str, str, str]]:
    return {
        (r["from"], r["table"], r["to"], r["on_update"], r["on_delete"])
        for r in conn.execute(text(f"PRAGMA foreign_key_list('{table}')")).mappings()
    }


def _check_names(conn, table: str) -> set[str]:
    sql = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table}
    ).scalar_one()
    return set(re.findall(r"CONSTRAINT\s+(\w+)\s+CHECK", sql, flags=re.IGNORECASE))


@pytest.fixture
def models_engine(tmp_path: Path) -> Engine:
    """A database built from models.py instead of the migrations."""
    eng = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'models.db').as_posix()}", future=True)
    Base.metadata.create_all(eng)
    return eng


def test_models_match_migrations(engine: Engine, models_engine: Engine):
    """HARD GATE: models.py and migrations/*.sql must describe one schema.

    The SQL is authoritative at runtime, so any mismatch means queries written
    against the models are wrong. Fix the models, not this test — unless the SQL
    itself is what changed.
    """
    with engine.connect() as mig, models_engine.connect() as mod:
        mig_tables = {
            r[0]
            for r in mig.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            )
        } - _RUNTIME_ONLY_TABLES
        mod_tables = {
            r[0]
            for r in mod.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            )
        }
        assert mig_tables == mod_tables, "table sets differ"

        for table in sorted(mig_tables):
            assert _columns(mig, table) == _columns(mod, table), f"columns differ on {table!r}"
            assert _indexes(mig, table) == _indexes(mod, table), f"indexes differ on {table!r}"
            assert _foreign_keys(mig, table) == _foreign_keys(mod, table), (
                f"foreign keys differ on {table!r}"
            )
            assert _check_names(mig, table) == _check_names(mod, table), (
                f"CHECK constraint names differ on {table!r}"
            )


def test_every_check_constraint_is_named(engine: Engine):
    """An unnamed CHECK is invisible to the drift test, so require names."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ).all()
    for name, sql in rows:
        total = len(re.findall(r"\bCHECK\s*\(", sql, flags=re.IGNORECASE))
        named = len(re.findall(r"CONSTRAINT\s+\w+\s+CHECK", sql, flags=re.IGNORECASE))
        assert total == named, f"{name} has {total - named} unnamed CHECK constraint(s)"
