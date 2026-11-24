"""`tracker loop`: where the nightly loop has got to, drawn with meters and a time line.

Read-only: it reads the loop's log, its lock and the night's spend ledger beside the
database (`data/runs/`), so it answers on the host, where the loop runs. The parse
and the drawing are `tracker.loopstatus`.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from enum import StrEnum
from typing import Annotated

import typer
from rich.console import Console

from tracker import loopstatus, theme
from tracker.cli._shared import _db_path, console, emit, json_mode, loop_app


class Palette(StrEnum):
    auto = "auto"
    truecolor = "truecolor"
    xterm = "256"


def _ascii_only() -> bool:
    """Lines and ticks in ASCII where the console cannot encode the drawing characters."""
    try:
        "━╸─█■·→▲▼●▶✓✗╵…".encode(console.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return True
    return False


def _screen(palette: Palette) -> tuple[Console, theme.Look]:
    """The console to draw on, and the colours it can show.

    A terminal reached over ssh announces 256 colours, because the session carries no
    `COLORTERM`, though nearly every terminal it is reached from shows 24-bit colour —
    the host's login banner already relies on that. So a 256-colour terminal is drawn
    in 24-bit colour, except macOS's own Terminal, which until recently had none.
    `--palette 256` is for a terminal where that guess comes out as wrong colours.
    A pipe, `NO_COLOR` and a colourless terminal stay without colour either way.
    """
    system = console.color_system
    if palette is Palette.xterm or system not in ("256", "truecolor"):
        return console, theme.XTERM
    if system == "truecolor":
        return console, theme.MERIDIAN
    if palette is Palette.auto and os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        return console, theme.XTERM
    full = Console(
        width=console.width, soft_wrap=False, color_system="truecolor", legacy_windows=False
    )
    return full, theme.MERIDIAN


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
    palette: Annotated[
        Palette,
        typer.Option(
            "--palette",
            help="Colours to draw with: 24-bit, or the 256 every terminal has. "
            "auto: 24-bit unless the terminal is known to lack it.",
        ),
    ] = Palette.auto,
) -> None:
    """The nightly loop at a glance: money, tokens and time used, the night's time line,
    rounds, phases, past nights."""
    runs = _db_path().parent / "runs"
    log = runs / "overnight.log"
    if not log.exists():
        console.print(
            f"[yellow]no nightly log at {log}[/yellow] — the loop runs on the host: "
            "ssh $PROD 'tracker loop'"
        )
        raise typer.Exit(1)
    ascii_only = _ascii_only()
    screen, look = _screen(palette)
    while True:
        nights = loopstatus.read_log(log)
        state = loopstatus.live(nights[-1], runs) if nights else loopstatus.Live(running=False)
        if json_mode():
            emit(_payload(nights[-1], state) if nights else {"running": False})
            return
        if watch:
            screen.clear()
        screen.print(
            loopstatus.render(
                nights,
                state,
                now=dt.datetime.now(),
                ascii_only=ascii_only,
                width=screen.width,
                look=look,
            )
        )
        if not watch:
            return
        screen.print(f" redrawn every {watch}s — ctrl-c to stop", style=look.muted)
        try:
            time.sleep(watch)
        except KeyboardInterrupt:
            return
