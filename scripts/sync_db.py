"""Move the database between this machine and the production host.

**The mini is the writer.** It is always on, which is what a job measured in
hours wants, and it already serves the console from the live file. Every command
that writes — `ingest`, `enrich`, `merge`, `infer`, `backfill` — runs there.
This machine develops against a copy.

So the default direction is **pull**, and that is a reversal: `ship_db.py`, which
this replaces, pushed dev to prod because prod held no keys and never wrote.
Once the mini started ingesting, pushing meant overwriting the only copy of work
that existed. There is no merge — a whole file replaces a whole file — so the
direction has to be a decision, not a habit.

`--push` still exists for seeding a new host or restoring one from a backup. It
refuses when the far end holds rows this database does not, because that is what
losing an ingest looks like from here.

**Neither direction copies the file.** SQLite runs in WAL mode, so committed data
sits in `tracker.db-wal` until a checkpoint folds it back; copying `tracker.db`
alone yields a file that opens cleanly and is silently out of date — 16.3 MB of
main file against a 7.9 MB WAL, measured here. `VACUUM INTO` asks SQLite for a
consistent single-file snapshot instead, and runs on whichever machine is the
source.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

#: Tables whose row counts are compared. Not every table — just enough that a
#: truncated copy cannot pass, cheaply.
#:
#: **Names are singular, and getting that wrong is silent.** An earlier version
#: counted `projects`/`sources`; nothing matched, the comparison ran over an
#: empty dict, and the snapshot was reported "verified" having checked nothing.
#: Both directions now refuse outright when no table is recognised.
_COUNTED = ("project", "source", "event", "risk", "capacity_block", "ingest_url")

DEFAULT_HOST = "mm"
DEFAULT_REMOTE = "~/dev/tracker/repo/data/tracker.db"


def counts(path: Path) -> dict[str, int]:
    """Row counts for the tables that exist, from a read-only handle."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {
            row[0] for row in con.execute("select name from sqlite_master where type='table'")
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
        # Interpolated because VACUUM INTO takes no parameters. The path is a
        # temporary file this process just named, never operator input.
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
        # Greater is fine: writes can land between the two reads. Fewer means the
        # copy lost rows, which is the failure worth catching.
        if got.get(table, 0) < want:
            problems.append(f"{table}: {got.get(table, 0)} rows, source had {want}")
    return problems


def _ssh_python(host: str, code: str) -> str:
    result = subprocess.run(
        ["ssh", host, f"python3 -c {shlex.quote(code)}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(f"remote python failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def remote_counts(host: str, remote: str) -> dict[str, int]:
    """Row counts on the far end, read over ssh rather than by fetching 16 MB."""
    code = (
        "import sqlite3,os,json;"
        f"p=os.path.expanduser({remote!r});"
        "print(json.dumps({})) if not os.path.exists(p) else None;"
        "c=sqlite3.connect('file:'+p+'?mode=ro',uri=True);"
        "t={r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")};"
        f"print(json.dumps({{n:c.execute('select count(*) from '+n).fetchone()[0] for n in {list(_COUNTED)!r} if n in t}}))"
    )
    out = _ssh_python(host, code)
    try:
        return json.loads(out.splitlines()[-1]) if out else {}
    except (ValueError, IndexError):
        return {}


def remote_snapshot(host: str, remote: str) -> str:
    """Ask the far end for a consistent snapshot; return its path there."""
    code = (
        "import sqlite3,os,tempfile;"
        f"p=os.path.expanduser({remote!r});"
        "d=tempfile.mkdtemp(prefix='sync-db-');t=os.path.join(d,'tracker.db');"
        "c=sqlite3.connect('file:'+p+'?mode=ro',uri=True);"
        'c.execute("VACUUM INTO \'"+t+"\'");c.close();print(t)'
    )
    return _ssh_python(host, code)


def run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"failed: {' '.join(command)}\n{result.stderr.strip() or result.stdout.strip()}"
        )


def pull(args, local: Path) -> int:
    """Bring the authoritative database here, replacing the local copy."""
    theirs = remote_counts(args.host, args.remote)
    if not theirs:
        raise SystemExit(f"no readable database at {args.host}:{args.remote}")
    print(f"remote   {args.host}:{args.remote}")
    print("         " + ", ".join(f"{k} {v}" for k, v in theirs.items()))

    if local.is_file():
        mine = counts(local)
        ahead = {t: n for t, n in mine.items() if n > theirs.get(t, 0)}
        if ahead and not args.force:
            print("  REFUSED: this database holds rows the remote does not.")
            for table, n in ahead.items():
                print(f"    {table}: {n} here, {theirs.get(table, 0)} there")
            print(
                "  The mini is the writer; work here was not expected. Use --force to discard it."
            )
            return 1

    started = time.perf_counter()
    there = remote_snapshot(args.host, args.remote)
    work = Path(tempfile.mkdtemp(prefix="sync-db-"))
    incoming = work / "tracker.db"
    try:
        run(["scp", "-q", f"{args.host}:{there}", str(incoming)])
        subprocess.run(["ssh", args.host, f"rm -rf {shlex.quote(os.path.dirname(there))}"])
        print(
            f"snapshot {incoming.stat().st_size / 1e6:.1f} MB "
            f"in {time.perf_counter() - started:.1f}s (VACUUM INTO on the mini)"
        )

        problems = verify(incoming, theirs)
        if problems:
            for problem in problems:
                print(f"  REFUSED: {problem}")
            return 1
        print("verified integrity_check ok, no rows lost")

        if args.dry_run:
            print("dry run: local database untouched")
            return 0

        local.parent.mkdir(parents=True, exist_ok=True)
        os.replace(incoming, local)
        # The old file's WAL and shared-memory siblings describe a database that
        # no longer exists here. Left behind, SQLite reads them against the new
        # file, which is the corrupt-looking failure this whole script exists to
        # avoid.
        for suffix in ("-wal", "-shm"):
            Path(str(local) + suffix).unlink(missing_ok=True)
        print(f"pulled   {local}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def push(args, local: Path) -> int:
    """Send this database to the far end, replacing what is there."""
    if not local.is_file():
        raise SystemExit(f"no database at {local}")
    mine = counts(local)
    if not mine:
        raise SystemExit(
            f"none of {_COUNTED} exist in {local}. Refusing: the row-count check "
            "would pass without comparing anything."
        )
    print(f"source   {local}  ({local.stat().st_size / 1e6:.1f} MB)")
    print("         " + ", ".join(f"{k} {v}" for k, v in mine.items()))

    theirs = remote_counts(args.host, args.remote)
    ahead = {t: n for t, n in theirs.items() if n > mine.get(t, 0)}
    if ahead and not args.force:
        print("  REFUSED: the remote holds rows this database does not.")
        for table, n in ahead.items():
            print(f"    {table}: {n} there, {mine.get(table, 0)} here")
        print("  The mini is the writer. Pull first, or --force to overwrite its work.")
        return 1

    work = Path(tempfile.mkdtemp(prefix="sync-db-"))
    snap = work / "tracker.db"
    try:
        started = time.perf_counter()
        snapshot(local, snap)
        print(
            f"snapshot {snap.stat().st_size / 1e6:.1f} MB "
            f"in {time.perf_counter() - started:.1f}s (VACUUM INTO, WAL folded in)"
        )
        problems = verify(snap, mine)
        if problems:
            for problem in problems:
                print(f"  REFUSED: {problem}")
            return 1
        print("verified integrity_check ok, no rows lost")
        if args.dry_run:
            print("dry run: nothing sent")
            return 0
        # Uploaded beside the live file, then renamed, so a reader sees one
        # database or the other and never a half-written one.
        run(["scp", "-q", str(snap), f"{args.host}:{args.remote}.incoming"])
        run(["ssh", args.host, f"mv {args.remote}.incoming {args.remote}"])
        print(f"pushed   {args.host}:{args.remote}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=DEFAULT_HOST, help="ssh host alias")
    parser.add_argument("--remote", default=DEFAULT_REMOTE, help="path on that host")
    parser.add_argument("--db", type=Path, default=None, help="database on this machine")
    parser.add_argument(
        "--push",
        action="store_true",
        help="send this database to the host instead of fetching from it",
    )
    parser.add_argument("--dry-run", action="store_true", help="check, change nothing")
    parser.add_argument(
        "--force", action="store_true", help="proceed even when the destination is ahead"
    )
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if args.db is None:
        from tracker.config import get_settings

        args.db = get_settings().resolve_db()
    local = Path(args.db)
    return push(args, local) if args.push else pull(args, local)


if __name__ == "__main__":
    raise SystemExit(main())
