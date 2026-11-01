"""`tracker lock`: who holds the database's write lock, and taking it back.

Every writing command takes a lock file beside the database (`tracker.db.lock`)
holding its pid, its command and when it started, because SQLite allows one writer
and a second fails partway through, after paying for its model calls. A writer that
dies leaves the file behind; the next writer reclaims it on its own, so `break` is
for a person who wants it gone now. `break --force` also removes the lock of a
process that is still running, which makes the next command a second writer beside
it: only for a holder known to be hung, and better still, stop that process instead.

The nightly loop has a lock of its own (`data/runs/overnight.lock`), which the
deployer reads; `check` shows it beside the write lock, and nothing here touches it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from tracker.cli._shared import _db_path, _fail, console, emit, err, json_mode, lock_app
from tracker.db import AlreadyRunning, WriteLock, break_write_lock, inspect_write_lock


def _night(db: Path) -> dict:
    """The nightly loop's lock: its pid, and whether that process is alive."""
    from tracker.db import _pid_alive

    pid_file = db.parent / "runs" / "overnight.lock" / "pid"
    try:
        text = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return {"held": False}
    pid = int(text) if text.isdigit() else None
    return {"held": True, "pid": pid, "alive": bool(pid and _pid_alive(pid))}


def _describe(lock: WriteLock) -> str:
    if not lock.held:
        return "free — no command is writing"
    state = (
        "[green]running[/green]" if lock.alive else "[yellow]stale: its process has died[/yellow]"
    )
    return f"held by pid {lock.pid} ({lock.command or '?'}) since {lock.since or '?'} UTC — {state}"


@lock_app.callback(invoke_without_command=True)
def lock_cmd(ctx: typer.Context) -> None:
    """Who holds the write lock (same as `tracker lock check`)."""
    if ctx.invoked_subcommand is None:
        check_cmd()


@lock_app.command("check")
def check_cmd() -> None:
    """Who holds the write lock, whether that process is still alive, and since when."""
    db = _db_path()
    lock = inspect_write_lock(db)
    night = _night(db)
    if json_mode():
        emit(
            {
                "write_lock": {
                    "path": str(lock.path),
                    "held": lock.held,
                    "alive": lock.alive,
                    "stale": lock.stale,
                    "pid": lock.pid,
                    "command": lock.command,
                    "since": lock.since,
                },
                "nightly_loop": night,
            }
        )
        return
    console.print(f"write lock    {_describe(lock)}")
    if night["held"]:
        alive = "running" if night["alive"] else "stale (the next night takes it over)"
        console.print(f"nightly loop  pid {night.get('pid') or '?'} — {alive}")
    else:
        console.print("nightly loop  not running")
    if lock.stale:
        console.print(
            "[dim]the next writing command reclaims it; `tracker lock break` removes it now[/dim]"
        )


@lock_app.command("break")
def break_cmd(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="DANGEROUS: also break a lock whose process is still running. That "
            "process keeps writing, and the next command becomes a second writer.",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="With --force: do not ask first.")] = False,
) -> None:
    """Remove the write lock of a process that has died; --force for a live one."""
    db = _db_path()
    lock = inspect_write_lock(db)
    if not lock.held:
        console.print("the write lock is free; nothing to break")
        return
    if lock.alive and not force:
        _fail(
            f"the write lock is held by a running process: pid {lock.pid} ({lock.command or '?'}) "
            f"since {lock.since or '?'} UTC.\nStop that process, or pass --force to break "
            "the lock anyway (it keeps writing, and the next command becomes a second writer)."
        )
    if lock.alive and not yes:
        if not sys.stdin.isatty():
            _fail("--force on a running holder asks first; pass --yes where nobody can answer")
        err.print(
            f"[red]pid {lock.pid} ({lock.command or '?'}) is still running and still writing.[/red]"
        )
        if not typer.confirm("Break its lock anyway?", default=False):
            _fail("left as it was", 1)
    try:
        gone = break_write_lock(db, force=force)
    except AlreadyRunning as exc:
        _fail(str(exc))
    what = "a running process's" if gone.alive else "a dead process's"
    console.print(
        f"broke {what} write lock: pid {gone.pid} ({gone.command or '?'}) since {gone.since or '?'}"
    )
