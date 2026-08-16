"""Ship a consistent snapshot of the database to the production host.

The console is read-only over one SQLite file, and the database is gitignored, so
`git push` carries code and nothing else. This is the other half of a deploy.

**Never `cp` the database.** It runs in WAL mode, which means committed data lives
in `tracker.db-wal` until a checkpoint folds it back. Copying `tracker.db` alone
produces a file that opens cleanly, reports no error, and is silently out of date
— measured here at 16.3 MB of main file against a 7.9 MB WAL, so a third of the
recent history would simply be missing. That failure has already happened once in
this project: a copied database without its sibling gave a stale snapshot and the
wrong figures were reported from it before anyone noticed.

`VACUUM INTO` is the fix and the reason this is a script rather than an `scp`. It
asks SQLite itself for a single-file copy of a consistent read snapshot, WAL
content included, without stopping writers.

Two more properties worth stating, since both are the difference between a deploy
and an outage:

* the snapshot is **verified before it is sent** — `integrity_check` plus a row
  count against the source, because shipping a corrupt file is worse than not
  shipping;
* it lands **atomically** — uploaded beside the live file and then renamed, so a
  reader either sees the old database or the new one, never a half-written file.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

#: Tables whose row counts are compared before and after. Not every table — just
#: enough that a truncated copy cannot pass, cheaply.
#:
#: **Names are singular, and getting that wrong is silent.** The first version of
#: this script counted `projects`/`sources`; nothing matched, the comparison ran
#: over an empty dict, and the snapshot was reported "verified" having checked
#: nothing at all. `main` now refuses outright when none of these are found,
#: which is the guard against that recurring.
_COUNTED = ("project", "source", "event", "risk", "capacity_block", "ingest_url")

DEFAULT_HOST = "mm"
DEFAULT_REMOTE = "~/dc-tracker/data/tracker.db"


def counts(path: Path) -> dict[str, int]:
    """Row counts for the tables that exist, from a read-only handle."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {
            row[0]
            for row in con.execute("select name from sqlite_master where type='table'")
        }
        return {
            t: con.execute(f"select count(*) from {t}").fetchone()[0]
            for t in _COUNTED
            if t in present
        }
    finally:
        con.close()


def snapshot(source: Path, target: Path) -> None:
    """A consistent single-file copy, WAL content included."""
    if target.exists():
        target.unlink()
    con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        # The path is interpolated because VACUUM INTO takes no parameters. It is
        # a temporary file this process just named, never operator input.
        con.execute(f"VACUUM INTO '{target.as_posix()}'")
    finally:
        con.close()


def verify(snap: Path, expected: dict[str, int]) -> list[str]:
    """Everything wrong with the snapshot, empty when it is sound."""
    problems: list[str] = []
    con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        result = con.execute("pragma integrity_check").fetchone()[0]
        if result != "ok":
            problems.append(f"integrity_check said {result!r}")
    finally:
        con.close()
    got = counts(snap)
    for table, want in expected.items():
        # Greater is fine and expected: writes can land between the two reads.
        # Fewer means the copy lost rows, which is the failure worth catching.
        if got.get(table, 0) < want:
            problems.append(f"{table}: {got.get(table, 0)} rows, source had {want}")
    return problems


def run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"failed: {' '.join(command)}\n{result.stderr.strip() or result.stdout.strip()}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=DEFAULT_HOST, help="ssh host alias")
    parser.add_argument("--remote", default=DEFAULT_REMOTE, help="path on that host")
    parser.add_argument("--db", type=Path, default=None, help="source database")
    parser.add_argument(
        "--dry-run", action="store_true", help="snapshot and verify, send nothing"
    )
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if args.db is None:
        from tracker.config import get_settings

        args.db = get_settings().resolve_db()
    source = Path(args.db)
    if not source.is_file():
        raise SystemExit(f"no database at {source}")

    before = counts(source)
    if not before:
        raise SystemExit(
            f"none of {_COUNTED} exist in {source}. Refusing: the row-count check "
            "would pass without comparing anything, which is how a truncated "
            "copy ships as 'verified'."
        )
    print(f"source   {source}  ({source.stat().st_size / 1e6:.1f} MB)")
    print("         " + ", ".join(f"{k} {v}" for k, v in before.items()))

    work = Path(tempfile.mkdtemp(prefix="ship-db-"))
    snap = work / "tracker.db"
    try:
        started = time.perf_counter()
        snapshot(source, snap)
        print(
            f"snapshot {snap.stat().st_size / 1e6:.1f} MB "
            f"in {time.perf_counter() - started:.1f}s (VACUUM INTO, WAL folded in)"
        )

        problems = verify(snap, before)
        if problems:
            for problem in problems:
                print(f"  REFUSED: {problem}")
            return 1
        print("verified integrity_check ok, no rows lost")

        if args.dry_run:
            print("dry run: nothing sent")
            return 0

        # Uploaded beside the live file, then renamed. A reader sees one database
        # or the other, never a partial one.
        incoming = f"{args.remote}.incoming"
        run(["scp", "-q", str(snap), f"{args.host}:{incoming}"])
        run(["ssh", args.host, f"mv {incoming} {args.remote}"])
        print(f"shipped  {args.host}:{args.remote}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
