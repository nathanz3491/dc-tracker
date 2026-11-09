"""`tracker loop`: where the nightly loop has got to, drawn with bars.

Read-only: it reads the loop's log, its lock and the night's spend ledger beside the
database (`data/runs/`), so it answers on the host, where the loop runs. The parse
and the drawing are `tracker.loopstatus`.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Annotated

import typer

from tracker import loopstatus
from tracker.cli._shared import _db_path, console, emit, json_mode, loop_app


def _ascii_only() -> bool:
    """Bars and ticks in ASCII where the console cannot encode the block characters."""
    try:
        "█░▶✓●".encode(console.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return True
    return False


def _payload(night: loopstatus.Night, state: loopstatus.Live) -> dict:
    current = night.current_phase() if state.running else None
    return {
        "running": state.running,
        "started": night.started.isoformat(),
        "ended": night.ended.isoformat() if night.ended else None,
        "stop": night.stop,
        "phase": current.name if current else None,
        "rounds": len(night.rounds),
        "ceilings": {
            "hours": night.hours,
            "rounds": night.rounds_cap,
            "cny": night.cny_cap,
            "tokens": night.tokens_cap,
            "searches": night.searches_cap,
        },
        "spent": {"cny": state.cny, "tokens": state.tokens, "searches": state.searches},
        "at_start": vars(night.at_start) if night.at_start else None,
        "now": vars(night.latest) if night.latest else None,
        "chosen_by": night.chosen_by,
        "fell_back": night.fell_back or None,
        "stop_why": night.stop_why or None,
        "referee": night.referee,
        "accuracy": night.accuracy or None,
        "rules": night.rules or None,
        "director_cny": state.director_cny,
        "steps": [r.as_json() for r in state.steps],
    }


@loop_app.callback(invoke_without_command=True)
def loop_cmd(
    watch: Annotated[
        int,
        typer.Option(
            "--watch", "-w", help="Redraw every this many seconds until ctrl-c (0: once)."
        ),
    ] = 0,
) -> None:
    """The nightly loop at a glance: money, tokens and time used, rounds, phases, past nights."""
    runs = _db_path().parent / "runs"
    log = runs / "overnight.log"
    if not log.exists():
        console.print(
            f"[yellow]no nightly log at {log}[/yellow] — the loop runs on the host: "
            "ssh $PROD 'tracker loop'"
        )
        raise typer.Exit(1)
    ascii_only = _ascii_only()
    while True:
        nights = loopstatus.read_log(log)
        state = loopstatus.live(nights[-1], runs) if nights else loopstatus.Live(running=False)
        if json_mode():
            emit(_payload(nights[-1], state) if nights else {"running": False})
            return
        if watch:
            console.clear()
        console.print(
            loopstatus.render(nights, state, now=dt.datetime.now(), ascii_only=ascii_only)
        )
        if not watch:
            return
        console.print(f"[grey50]redrawn every {watch}s — ctrl-c to stop[/grey50]")
        try:
            time.sleep(watch)
        except KeyboardInterrupt:
            return
