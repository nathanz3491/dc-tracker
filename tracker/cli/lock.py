"""`tracker lock`: who holds the database's write lock, and taking it back.

Every writing command takes a lock file beside the database (`tracker.db.lock`)
holding its pid, its command and when it started, because SQLite allows one writer
and a second fails partway through, after paying for its model calls. A writer that
dies leaves the file behind; the next writer reclaims it on its own, so `break` is
for a person who wants it gone now. `break --force` also removes the lock of a
process that is still running, which makes the next command a second writer beside
it: only for a holder known to be hung, and better still, stop that process instead.
So the first `--force` only warns, naming the process, and a second on the same lock
within ten minutes breaks it (`tracker.db.lock.force-armed` remembers the warning).
`--stop-process` ends the holder and then breaks its lock, in the same two steps; and
when the holder is one of the nightly loop's phases, the warning says so, loudly.

The nightly loop has a lock of its own (`data/runs/overnight.lock`), which the
deployer reads; `check` shows it beside the write lock, and nothing here touches it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import typer

from tracker.cli._shared import _db_path, _fail, console, emit, err, json_mode, lock_app
from tracker.db import (
    AlreadyRunning,
    WriteLock,
    break_write_lock,
    inspect_write_lock,
    stop_lock_holder,
)


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


#: How long a warning stays armed: the same flag again within this acts, a later one
#: warns again. Long enough to read the warning and decide.
ARMED_FOR_S = 600.0


def _armed_path(lock: WriteLock, action: str) -> Path:
    return lock.path.with_name(f"{lock.path.name}.{action}-armed")


def _armed_for(lock: WriteLock, action: str) -> bool:
    """Whether a warning about `action` was given for this very lock, recently."""
    path = _armed_path(lock, action)
    try:
        armed = path.read_text(encoding="utf-8")
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return armed == lock.raw and age <= ARMED_FOR_S


def _ancestors(pid: int) -> list[int]:
    """The process's parent, grandparent and so on (empty where that cannot be read)."""
    import subprocess
    import sys

    if sys.platform == "win32":
        return []
    out: list[int] = []
    current = pid
    for _ in range(20):
        try:
            text = subprocess.run(
                ["ps", "-p", str(current), "-o", "ppid="],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        except OSError:  # `ps` refused: the ancestry is unknown, which reads as the loop's
            return []
        if not text.isdigit() or int(text) <= 1:
            break
        current = int(text)
        out.append(current)
    return out


def _of_the_night(lock: WriteLock, db: Path) -> bool:
    """Whether the holder is one of the nightly loop's phases.

    A descendant of the loop's process. Where the ancestry cannot be read, a holder
    while the loop runs is treated as the loop's: the louder warning is the safe guess.
    """
    night = _night(db)
    if not (night["held"] and night["alive"] and lock.pid):
        return False
    family = _ancestors(lock.pid)
    return not family or night["pid"] in family


def _warn(lock: WriteLock, db: Path, action: str) -> None:
    """The first of two steps: say what the second will do, arm it, break nothing."""
    _armed_path(lock, action).write_text(lock.raw or "", encoding="utf-8")
    holder = (
        f"pid {lock.pid} ({lock.command or '?'}), holding the lock since {lock.since or '?'} UTC"
    )
    if action == "force":
        what = (
            "Breaking its lock does not stop it: the next command to start becomes a second "
            "writer beside it, and one of them fails partway through, after paying for its "
            "model calls. Stopping that process is almost always the better fix "
            "(`--stop-process`)."
        )
    else:
        what = (
            "Stopping it ends that command now: what it has not committed is rolled back, "
            "and the model calls it paid for that are not yet written are lost. It is asked "
            "to stop and killed after fifteen seconds."
        )
    if _of_the_night(lock, db):
        night = _night(db)
        err.print(
            "[bold white on red]" + "!" * 76 + "[/bold white on red]\n"
            "[bold white on red]  THE NIGHTLY LOOP IS RUNNING, AND THIS LOCK IS ONE OF ITS PHASES  "
            "        [/bold white on red]\n"
            "[bold white on red]" + "!" * 76 + "[/bold white on red]\n"
            f"[bold red]The loop (pid {night['pid']}) owns {holder}.[/bold red]\n"
            "The loop goes on to its next phase whatever happens to this one, and its "
            "progress counts and spend ledger will read this phase as finished or failed. "
            "A forced break makes the loop's own phase a second writer; stopping it throws "
            "away that phase's paid, unwritten work. Ending the night is a decision about "
            "the whole loop (`scripts/overnight.sh --status`), not about one lock."
        )
    else:
        err.print(f"[bold red]WARNING[/bold red]: {holder}, is still running and still writing.")
    err.print(
        f"{what}\nNothing was done. Run `tracker lock break --{action.replace('_', '-')}` "
        "again within ten minutes to go ahead with this lock."
    )
    raise typer.Exit(1)


@lock_app.command("break")
def break_cmd(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="DANGEROUS: also break a lock whose process is still running. The first "
            "--force only warns; a second within ten minutes, on the same lock, breaks it.",
        ),
    ] = False,
    stop_process: Annotated[
        bool,
        typer.Option(
            "--stop-process",
            help="End the process holding the lock (asked to stop, killed after 15 s), "
            "then break its lock. The first only warns; a second within ten minutes acts.",
        ),
    ] = False,
) -> None:
    """Remove the write lock of a process that has died; --force or --stop-process for a live one.

    For a running holder both work in two steps: the first run prints a warning and
    does nothing, and the same flag again on the same lock within ten minutes acts. The
    warning names the process, so the second run is a decision about that one: a lock
    that changed hands in between is warned about anew. When the holder is one of the
    nightly loop's phases, the warning says so, loudly.

    `--stop-process` ends the holder instead of leaving it running (asked to stop,
    killed after fifteen seconds), then removes its lock, stale by then. Safer than
    `--force`, which leaves it writing beside whatever starts next. It refuses a pid
    that is no longer a tracker command, since pids are reused.
    """
    if force and stop_process:
        _fail("choose one: --force leaves the holder running, --stop-process ends it")
    db = _db_path()
    lock = inspect_write_lock(db)
    if not lock.held:
        console.print("the write lock is free; nothing to break")
        return
    action = "stop_process" if stop_process else "force" if force else ""
    if lock.alive and not action:
        _fail(
            f"the write lock is held by a running process: pid {lock.pid} ({lock.command or '?'}) "
            f"since {lock.since or '?'} UTC.\nStop it with --stop-process, or pass --force to "
            "break the lock anyway (it keeps writing, and the next command becomes a second writer)."
        )
    if lock.alive and not _armed_for(lock, action):
        _warn(lock, db, action)
    if lock.alive and stop_process:
        try:
            how = stop_lock_holder(lock)
        except AlreadyRunning as exc:
            _fail(str(exc))
    try:
        gone = break_write_lock(db, force=force)
    except AlreadyRunning as exc:
        _fail(str(exc))
    if action:
        _armed_path(lock, action).unlink(missing_ok=True)
    if lock.alive and stop_process:
        console.print(
            f"{how} pid {lock.pid} ({lock.command or '?'}) and broke its write lock "
            f"(held since {gone.since or lock.since or '?'} UTC)"
        )
        return
    what = "a running process's" if gone.alive else "a dead process's"
    console.print(
        f"broke {what} write lock: pid {gone.pid} ({gone.command or '?'}) since {gone.since or '?'}"
    )
