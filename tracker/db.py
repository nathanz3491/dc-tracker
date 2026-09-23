"""Engine, connection pragmas, and the raw-SQL migration runner.

No Alembic (PRD decision: three tables, raw SQL is enough). Migrations are
numbered `.sql` files under `migrations/`, applied in order, tracked in a
`schema_version` table, and idempotent — re-running `init` applies nothing.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from tracker.config import package_root

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
    """The migrations that ship with the package.

    One answer, not a search. They live at `tracker/migrations/` precisely so this
    can be a single path: the previous version looked beside the package and then
    fell back to the current directory, which worked only in a checkout — a wheel
    carries the package alone, so `site-packages/migrations` never existed and every
    command died here with "migrations directory not found".
    """
    return package_root() / "migrations"


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
    DB at the last complete version rather than in an undefined state — see
    `_transactional_ddl` for why that took more than `engine.begin()`.
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
    if not pending:
        return applied
    with engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        for m in pending:
            if _apply(conn, m):
                applied.append(m.version)
    return applied


def _apply(conn: Connection, m: Migration) -> bool:
    """One migration and its version row, as one transaction. False if already there.

    `BEGIN IMMEDIATE` takes the write lock before anything is read, which is what
    makes the re-check below mean something. Every writing command runs `init_db`,
    and so does the deployer, so two processes can find the same migration pending
    at once; the second's transaction cannot begin until the first's has
    committed, and then it finds the version row and applies nothing, instead of
    dying on "already exists" as it starts. A deferred `BEGIN` would not do: its
    first read would pin a snapshot from before the other commit.
    """
    statements = split_sql(m.sql)
    if not statements:
        raise MigrationError(f"{m.path.name} contains no executable statements")
    with _transactional_ddl(conn):
        done = conn.execute(
            text("SELECT 1 FROM schema_version WHERE version = :v"), {"v": m.version}
        ).first()
        if done:
            log.info("migration %04d_%s was applied by another process", m.version, m.name)
            return False
        log.info("applying migration %04d_%s (%d statements)", m.version, m.name, len(statements))
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
    return True


@contextmanager
def _transactional_ddl(conn: Connection) -> Iterator[None]:
    """A real SQLite transaction around everything inside it, DDL included.

    **`engine.begin()` did not provide one for schema changes.** Python's sqlite3
    driver opens a transaction itself, and only before INSERT, UPDATE and DELETE,
    so a CREATE TABLE or ALTER TABLE ran in autocommit and was permanent the
    instant it ran. A migration whose second statement failed left its first
    behind with no version row, every later `tracker init` died on "already
    exists", and the console — which refuses a database behind on migrations —
    stayed down until somebody undid the half by hand.

    This is SQLAlchemy's documented pysqlite recipe ("Serializable isolation /
    Savepoints / Transactional DDL"), scoped to this one connection: the caller
    puts it in AUTOCOMMIT, which is pysqlite's `isolation_level = None` and stops
    the driver issuing BEGIN and COMMIT of its own, and the statements here are
    the only ones there are. Safe for every migration so far because none uses a
    statement SQLite refuses inside a transaction — no VACUUM, and no PRAGMA that
    a transaction would silently ignore.
    """
    conn.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        # Some failures (disk full, I/O) make SQLite roll back by itself, and a
        # second ROLLBACK would raise over the error that explains what happened.
        if conn.connection.dbapi_connection.in_transaction:
            conn.exec_driver_sql("ROLLBACK")
        raise
    conn.exec_driver_sql("COMMIT")


class AlreadyRunning(RuntimeError):
    """Another writing command holds the lock. Message is operator-facing."""


#: An empty lock file younger than this is a lock being taken, not an abandoned one.
#: The file is created in one call and its holder written in the next, so a racing
#: reader can catch it empty; that gap is microseconds. A file still empty this long
#: after it appeared lost its creator inside the gap and is reclaimed like any other.
_UNWRITTEN_GRACE_S = 10.0

#: A ceiling on waiting out contention that is not a live holder: another process
#: reclaiming a dead one's lock, or Windows refusing to open or delete a file that
#: some other process has open for the microseconds it takes to read it.
_CONTENTION_TIMEOUT_S = 10.0
_CONTENTION_POLL_S = 0.005

#: Only Windows raises PermissionError for a file that is merely busy. Everywhere
#: else it means this user may not write there, which no amount of waiting fixes
#: and which the operator needs to see as itself, at once.
_WINDOWS = sys.platform == "win32"

#: One step, as far as every other process can tell: create the file, or learn it
#: is already there. `O_BINARY` stops Windows translating the holder text.
_EXCLUSIVE_CREATE = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)


def _lock_path(db_path: Path | str) -> Path:
    return Path(str(db_path) + ".lock")


def _pid_alive(pid: int) -> bool:
    """Whether a process id is still running, without signalling it."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def acquire_write_lock(db_path: Path | str, *, command: str = "sync") -> Callable[[], None]:
    """Refuse to start a second writing run against the same database.

    SQLite allows one writer, so two overlapping `tracker sync` runs produce a raw
    "database is locked" traceback partway through — after the second run has
    already paid for its LLM calls. Observed live: a 100-article run was still
    going when another was started, and the newcomer died on its first insert
    having spent a call to get there.

    The lock is a file beside the database holding the owner's pid, command and
    start time, and a lock whose process has died is reclaimed rather than
    blocking forever.

    Returns a release function rather than being a context manager because the CLI
    commands have several early returns, where wrapping the whole body in `with`
    would mean re-indenting it. Callers register it with `atexit` so every exit
    path releases, including `typer.Exit`. Releasing twice is harmless, and a
    release never deletes a lock this call did not take: `sync` releases explicitly
    and then again from `atexit`, and another run may hold the file by the second.
    """
    path = _lock_path(db_path)
    mine = _claim(path, command)
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        if _read_lock(path, patient=True) == mine:
            _unlink(path)

    return release


def _claim(path: Path, command: str) -> str:
    """Create the lock file, reclaiming a dead holder's first. Returns what it holds.

    **An exclusive create is the whole mechanism.** The previous version asked
    whether the file existed and then wrote it, and every process that asked
    before any had written went on to write its own: six processes released
    together all came away holding the lock, in five trials of five. `O_EXCL`
    makes "create it unless it exists" one step the operating system arbitrates,
    so exactly one of any number of simultaneous callers can succeed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mine = f"{os.getpid()} {command} {utcnow_text()}"
    deadline = time.monotonic() + _CONTENTION_TIMEOUT_S
    refused: PermissionError | None = None
    while True:
        try:
            fd = os.open(path, _EXCLUSIVE_CREATE, 0o644)
        except FileExistsError:
            refused = None
            raw = _read_lock(path)
            if raw is not None:
                if _held(path, raw):
                    raise AlreadyRunning(
                        f"another tracker run is already writing to this database.\n"
                        f"  lock:    {path}\n"
                        f"  holder:  {raw or '(starting — its pid is being written)'}\n\n"
                        "Wait for it to finish, or stop that process. Running two writing "
                        "commands at once fails partway through and wastes the LLM calls "
                        "the second one already paid for."
                    ) from None
                if _reclaim(path, raw):
                    continue
            # Otherwise it changed hands under us, or vanished between the create
            # and the read because its holder was releasing it: look again shortly.
        except PermissionError as exc:
            # Windows refuses to open a name another process is deleting, which
            # passes; anywhere else it is the real answer, and so is it on Windows
            # if it is still the answer when the wait runs out.
            if not _WINDOWS:
                raise
            refused = exc
        else:
            try:
                os.write(fd, mine.encode("utf-8"))
            except BaseException:
                os.close(fd)
                _unlink(path)
                raise
            os.close(fd)
            return mine
        if time.monotonic() > deadline:
            if refused is not None:
                raise refused
            raise AlreadyRunning(
                f"could not take the write lock at {path} within "
                f"{_CONTENTION_TIMEOUT_S:.0f}s: it kept changing hands or could not be read."
            )
        time.sleep(_CONTENTION_POLL_S)


def _read_lock(path: Path, *, patient: bool = False) -> str | None:
    """The holder text, or None when there is no file to read right now.

    `patient` rides out Windows refusing to open a file mid-deletion, for a caller
    with no loop of its own to come back round — a release that read nothing would
    leave its own lock behind for the next run to reclaim.
    """
    deadline = time.monotonic() + _CONTENTION_TIMEOUT_S
    while True:
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except FileNotFoundError:
            return None
        except PermissionError:
            if not (patient and _WINDOWS) or time.monotonic() > deadline:
                return None
            time.sleep(_CONTENTION_POLL_S)


def _held(path: Path, raw: str) -> bool:
    """Whether the lock text names a holder that is still there."""
    if not raw:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age < _UNWRITTEN_GRACE_S
    pid_text = raw.split()[0]
    return pid_text.isdigit() and _pid_alive(int(pid_text))


def _reclaim(path: Path, stale: str) -> bool:
    """Delete a dead holder's lock — only if it is still that same lock.

    Returns whether it did, so the caller knows the name is free to race for.

    **The reclaim was a second race, inside the first.** Every contender reads the
    dead pid, decides the lock is stale and deletes the file; one that deletes it
    *after* another has already replaced it with a live lock deletes that one, and
    both carry on as holders. With a stale lock in place, six processes released
    together produced between three and six holders.

    So "is it still the lock I judged, and if so delete it" happens under a guard
    only one process can hold at a time. That makes it safe: the file can only be
    deleted by its owner's release — and its owner is dead — or by a reclaimer
    holding the guard. Identical text is the same lock, because it names a dead pid
    and the second it started, and a dead process takes no second lock.
    """
    with _reclaim_guard(path):
        current = _read_lock(path)
        if current != stale:
            return False  # released, or reclaimed and retaken by somebody else
        if not current and _held(path, current):
            return False  # empty again, but a new file: somebody's lock being written
        pid_text = stale.split()[0] if stale else ""
        log.warning("reclaiming a stale lock from pid %s", pid_text or "?")
        _unlink(path)
        return True


@contextmanager
def _reclaim_guard(path: Path) -> Iterator[None]:
    """Hold an operating-system lock on a file beside the lock, for one reclaim.

    Not another create-exclusive file: that would need its own staleness rule for a
    process that died while holding it, and reclaiming *that* is the same race
    again. An OS-level lock is released by the kernel when its holder exits,
    however it exits, which is the one property a lock file cannot have. The guard
    file is created once and never deleted — deleting a file other processes are
    locking lets a newcomer create a fresh one and lock that instead.
    """
    guard = path.with_name(path.name + ".guard")
    fd = os.open(guard, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o644)
    try:
        if _WINDOWS:
            import msvcrt

            def take() -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

            def drop() -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

        else:
            import fcntl

            def take() -> None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def drop() -> None:
                fcntl.flock(fd, fcntl.LOCK_UN)

        deadline = time.monotonic() + _CONTENTION_TIMEOUT_S
        while True:
            try:
                take()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise AlreadyRunning(
                        f"another tracker run has been reclaiming the write lock at {path} "
                        f"for over {_CONTENTION_TIMEOUT_S:.0f}s. A reclaim takes milliseconds, "
                        "so the process doing it is stuck: stop it, then run this again."
                    ) from None
                time.sleep(_CONTENTION_POLL_S)
        try:
            yield
        finally:
            drop()
    finally:
        os.close(fd)


def _unlink(path: Path) -> None:
    """Delete the lock file, waiting out Windows refusing while a reader has it open."""
    deadline = time.monotonic() + _CONTENTION_TIMEOUT_S
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if not _WINDOWS or time.monotonic() > deadline:
                raise
            time.sleep(_CONTENTION_POLL_S)


def utcnow_text() -> str:
    from tracker.models import utcnow

    return utcnow().isoformat(sep=" ")


def init_db(
    db_path: Path | str, *, migrations: list[Migration] | None = None
) -> tuple[Engine, list[int]]:
    """Create or upgrade the database. Returns (engine, versions applied)."""
    engine = make_engine(db_path)
    return engine, run_migrations(engine, migrations)


def open_db(db_path: Path | str, *, readonly: bool = True) -> Engine:
    """Open an existing database, verifying it has been initialized and upgraded."""
    engine = make_engine(db_path, readonly=readonly)
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project'")
        ).first()
    if not exists:
        raise MigrationError(f"{db_path} exists but has no `project` table. Run `tracker init`.")

    # Being behind on migrations is not a hypothetical: read commands query tables
    # that a older database does not have, and without this the operator gets a raw
    # "no such table: risk" traceback out of SQLAlchemy instead of the one-word fix.
    # A read command opens the database `mode=ro`, so it cannot migrate on the
    # operator's behalf even if that were desirable.
    current = schema_version(engine)
    try:
        latest = max((m.version for m in discover_migrations()), default=current)
    except MigrationError:
        # No migrations directory to compare against — an unusual install layout,
        # not a reason to refuse a read.
        return engine
    if current < latest:
        raise MigrationError(
            f"{db_path} is at schema version {current}, but version {latest} is available.\n"
            "Run `tracker init` to upgrade it. Existing data is preserved."
        )
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
