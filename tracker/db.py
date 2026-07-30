"""Engine, connection pragmas, and the raw-SQL migration runner.

No Alembic (PRD decision: three tables, raw SQL is enough). Migrations are
numbered `.sql` files under `migrations/`, applied in order, tracked in a
`schema_version` table, and idempotent — re-running `init` applies nothing.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from tracker.config import find_project_root

log = logging.getLogger(__name__)

_MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

#: Bookkeeping table, created before any migration runs. Deliberately absent
#: from models.py — the drift test excludes it by name.
_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    name        TEXT     NOT NULL,
    checksum    TEXT     NOT NULL,
    applied_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


class MigrationError(RuntimeError):
    """A migration file is malformed, missing, or was modified after being applied."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        """SHA-256 of the file's normalized text.

        Line endings are normalized so a CRLF checkout does not appear to be a
        different migration than an LF one — this is why .gitattributes forces
        `*.sql text eol=lf`, but belt and braces.
        """
        return hashlib.sha256(self.sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def migrations_dir() -> Path:
    return find_project_root() / "migrations"


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """All migrations sorted by version, validating names and numbering.

    A gap or duplicate in the numbering is an error rather than something to
    quietly tolerate: it almost always means two branches both added `0003_`.
    """
    directory = directory or migrations_dir()
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")

    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise MigrationError(
                f"migration filename must be NNNN_lower_snake.sql, got {path.name!r}"
            )
        found.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                path=path,
                sql=path.read_text(encoding="utf-8"),
            )
        )

    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions: {versions}")
    if versions and versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration versions must be contiguous from 1, got {versions}")
    return found


def split_sql(sql: str) -> list[str]:
    """Split a migration into executable statements.

    `sqlite3.executescript` would be simpler but it issues an implicit COMMIT,
    which would defeat running a migration inside a transaction. So we split on
    semicolons, ignoring those inside string literals or comments — SQLite has
    no procedural blocks, so semicolon-splitting is sufficient here.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            buf.append(ch)
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                buf.append("*/")
                i += 2
                continue
            buf.append(ch)
        elif in_string:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":  # escaped quote inside a literal
                    buf.append(nxt)
                    i += 2
                    continue
                in_string = False
        elif ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append("--")
            i += 2
            continue
        elif ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append("/*")
            i += 2
            continue
        elif ch == "'":
            in_string = True
            buf.append(ch)
        elif ch == ";":
            statements.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1

    statements.append("".join(buf))
    return [s for s in (stmt.strip() for stmt in statements) if _has_sql(s)]


def _has_sql(stmt: str) -> bool:
    """True if the statement contains anything besides comments and whitespace."""
    stripped = re.sub(r"--[^\n]*", "", stmt)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    return bool(stripped.strip())


# --- Engine ----------------------------------------------------------------


def _apply_pragmas(dbapi_conn, _record) -> None:
    """Per-connection pragmas.

    `foreign_keys=ON` is the important one: SQLite silently ignores every
    foreign key by default, and this entire design rests on source.project_id
    and event.source_id actually being enforced.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON")
    cur.execute("PRAGMA busy_timeout = 5000")
    # In-memory and some read-only databases reject WAL; it is an optimization,
    # not a correctness requirement, so a refusal is fine.
    with contextlib.suppress(sqlite3.OperationalError):
        cur.execute("PRAGMA journal_mode = WAL")
    cur.close()


def make_engine(db_path: Path | str, *, readonly: bool = False, echo: bool = False) -> Engine:
    """Engine for a SQLite file (or `":memory:"`).

    `readonly=True` opens the file with SQLite's `mode=ro`, so a bug in a read
    command raises rather than mutating data. That turns the PRD's "never modify
    the DB except for ingest and review" from a convention into a guarantee.
    """
    if str(db_path) == ":memory:":
        url = "sqlite+pysqlite:///:memory:"
    elif readonly:
        resolved = Path(db_path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"database not found: {resolved}\nRun `tracker init` first.")
        url = f"sqlite+pysqlite:///file:{resolved.as_posix()}?mode=ro&uri=true"
    else:
        resolved = Path(db_path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+pysqlite:///{resolved.as_posix()}"

    engine = create_engine(url, echo=echo, future=True)
    event.listen(engine, "connect", _apply_pragmas)
    return engine


@contextmanager
def session_scope(engine: Engine, *, commit: bool = True) -> Iterator[Session]:
    """Transactional session: commit on clean exit, roll back on any exception."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
        if commit:
            session.commit()
        else:
            session.rollback()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- Migrations ------------------------------------------------------------


def applied_versions(engine: Engine) -> dict[int, str]:
    """version -> checksum for every migration already applied."""
    with engine.begin() as conn:
        conn.execute(text(_SCHEMA_VERSION_DDL))
        rows = conn.execute(text("SELECT version, checksum FROM schema_version")).all()
    return {int(v): c for v, c in rows}


def run_migrations(engine: Engine, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied.

    Each migration runs in its own transaction, so a failure half-way leaves the
    DB at the last complete version rather than in an undefined state.
    """
    migrations = migrations if migrations is not None else discover_migrations()
    already = applied_versions(engine)

    for m in migrations:
        if m.version in already and already[m.version] != m.checksum:
            raise MigrationError(
                f"migration {m.version:04d}_{m.name} was modified after it was applied.\n"
                f"  expected checksum {already[m.version][:12]}, file is {m.checksum[:12]}\n"
                "Migrations are immutable once applied: add a new one instead, or delete "
                "the database and re-run `tracker init` to rebuild from scratch."
            )

    pending = [m for m in migrations if m.version not in already]
    applied: list[int] = []
    for m in pending:
        statements = split_sql(m.sql)
        if not statements:
            raise MigrationError(f"{m.path.name} contains no executable statements")
        log.info("applying migration %04d_%s (%d statements)", m.version, m.name, len(statements))
        with engine.begin() as conn:
            for stmt in statements:
                # exec_driver_sql, NOT text(): text() scans for `:name` bind
                # parameters even inside SQL comments, so a comment containing
                # something like "row=1274" preceded by a colon becomes a
                # phantom required bind. Migrations are literal SQL by
                # definition and must never be parameterized.
                conn.exec_driver_sql(stmt)
            conn.execute(
                text("INSERT INTO schema_version (version, name, checksum) VALUES (:v, :n, :c)"),
                {"v": m.version, "n": m.name, "c": m.checksum},
            )
        applied.append(m.version)
    return applied


def init_db(
    db_path: Path | str, *, migrations: list[Migration] | None = None
) -> tuple[Engine, list[int]]:
    """Create or upgrade the database. Returns (engine, versions applied)."""
    engine = make_engine(db_path)
    return engine, run_migrations(engine, migrations)


def open_db(db_path: Path | str, *, readonly: bool = True) -> Engine:
    """Open an existing database, verifying it has been initialized."""
    engine = make_engine(db_path, readonly=readonly)
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project'")
        ).first()
    if not exists:
        raise MigrationError(f"{db_path} exists but has no `project` table. Run `tracker init`.")
    return engine


def schema_version(engine: Engine) -> int:
    """Highest applied migration version, or 0 for an empty database."""
    with engine.connect() as conn:
        has_table = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'")
        ).first()
        if not has_table:
            return 0
        row = conn.execute(text("SELECT MAX(version) FROM schema_version")).first()
    return int(row[0]) if row and row[0] is not None else 0
