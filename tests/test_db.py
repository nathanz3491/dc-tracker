"""Migration runner, connection pragmas, and the models-vs-SQL drift gate.

The drift test is what makes it safe to define the schema twice — once in
`migrations/*.sql` (authoritative at runtime) and once in `models.py` (used for
typed queries). Without it, the two definitions silently diverge and queries
start returning wrong results against a schema that no longer matches.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from tracker.db import (
    MigrationError,
    discover_migrations,
    init_db,
    make_engine,
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


# --- Applying migrations ----------------------------------------------------


def test_init_db_applies_then_is_idempotent(db_path: Path):
    _, first = init_db(db_path)
    assert first == [1, 2]
    engine, second = init_db(db_path)
    assert second == [], "re-running init must apply nothing"
    assert schema_version(engine) == 2


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


def test_open_db_rejects_uninitialized_file(tmp_path: Path):
    stray = tmp_path / "empty.db"
    make_engine(stray).connect().close()  # create the file, apply no schema
    with pytest.raises(MigrationError, match="tracker init"):
        open_db(stray)


def test_open_db_missing_file_says_run_init(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="tracker init"):
        open_db(tmp_path / "nope.db")


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
