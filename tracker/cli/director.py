"""`tracker director`: steer the next directed night, and read what nights did.

Everything here reads or writes files beside the database (`data/runs/`), so it is
run on the host, where the loop runs. Only `note` and `backfill --write` write, and
neither touches the database.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from tracker.cli._shared import _db_path, _fail, console, director_app, emit, json_mode
from tracker.night import record as record_mod


def _runs() -> Path:
    return _db_path().parent / "runs"


@director_app.command("note")
def note_cmd(
    text: Annotated[
        str | None,
        typer.Argument(
            help="What the next directed night should know, in plain words.", show_default=False
        ),
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Remove the note.")] = False,
) -> None:
    """Show, set or clear the note the next directed night's director is briefed with.

    It steers within the limits ("tonight, duplicates first"); it cannot lift a
    ceiling or a floor. Read at the start of the night and cleared after a directed
    night, so it is said once.
    """
    from tracker.night import director

    runs = _runs()
    if clear:
        gone = director.clear_note(runs)
        console.print("note cleared" if gone else "there was no note")
        return
    if text is None:
        current = director.read_note(runs)
        console.print(escape(current) if current else "no note for the next directed night")
        return
    if len(text) > 500:
        _fail("a note is at most 500 characters; the briefing cuts anything longer")
    path = director.write_note(runs, text)
    console.print(f"saved for the next directed night: {path}")


def _nights(records: list[record_mod.StepRecord]) -> list[str]:
    return sorted({r.night for r in records})


@director_app.command("steps")
def steps_cmd(
    night: Annotated[
        str | None,
        typer.Option("--night", help="A night's stamp (YYYYMMDD-HHMMSS); default the latest."),
    ] = None,
) -> None:
    """What each step of one night cost and did. Reads `data/runs/steps.jsonl`."""
    records = record_mod.read(_runs() / record_mod.FILENAME)
    if not records:
        console.print("no steps recorded yet")
        return
    night = night or _nights(records)[-1]
    mine = [r for r in records if r.night == night]
    if json_mode():
        emit([r.as_json() for r in mine])
        return
    if not mine:
        _fail(f"no steps recorded for {night}; recorded nights: {', '.join(_nights(records)[-7:])}")
    console.print(f"{night}: {mine[0].mode}, {len(mine)} step(s)")
    console.print(
        "round  step        size  origin    ¥        searches  minutes  result (expected)"
    )
    for r in mine:
        expected = r.expect.get("result")
        console.print(
            f"{r.round:>5}  {r.kind:<10} {r.size:>5}  {r.origin:<8} {r.cny:>7.2f}  {r.searches:>8}  "
            f"{r.minutes:>7.1f}  {'?' if r.result is None else r.result}"
            + (f" ({expected:g})" if expected is not None else "")
            + (" FAILED" if r.failed else "")
        )


@director_app.command("prices")
def prices_cmd(
    nights: Annotated[
        int, typer.Option("--nights", help="Nights to read.")
    ] = record_mod.PRICE_NIGHTS,
) -> None:
    """Each step's measured price per unit — the list the director is briefed with."""
    prices = record_mod.prices(record_mod.read(_runs() / record_mod.FILENAME), nights)
    if json_mode():
        emit({k: vars(v) for k, v in prices.items()})
        return
    if not prices:
        console.print(
            "no steps recorded yet — `tracker director backfill --write` rebuilds past nights' costs"
        )
        return
    for price in prices.values():
        console.print(escape(price.line()))


@director_app.command("backfill")
def backfill_cmd(
    log: Annotated[
        Path | None, typer.Option("--log", help="The loop's log (default data/runs/overnight.log).")
    ] = None,
    since: Annotated[
        str, typer.Option("--since", help="Only nights from this stamp (YYYYMMDD).")
    ] = "",
    write: Annotated[
        bool,
        typer.Option(
            "--write", help="Append them to the record. Without it, only says what it found."
        ),
    ] = False,
) -> None:
    """Rebuild cost and time per step for nights before the record existed.

    From the loop's log (phase lines and their times) and each night's spend ledger.
    Results are left unknown: a round's counts cannot be split between its steps
    after the fact. Nights the record already holds are skipped.
    """
    runs = _runs()
    path = runs / record_mod.FILENAME
    have = {r.night for r in record_mod.read(path)}
    tz = dt.datetime.now().astimezone().tzinfo
    found = [
        r
        for r in record_mod.backfill(log or runs / "overnight.log", runs, tz, since=since)
        if r.night not in have
    ]
    nights = sorted({r.night for r in found})
    console.print(f"{len(found)} step(s) over {len(nights)} night(s) not yet in the record")
    if write:
        for r in found:
            record_mod.append(path, r)
        console.print(f"appended to {path}")


@director_app.command("guard")
def guard_cmd(
    against: Annotated[
        Path, typer.Option("--against", help="A snapshot from before the change (tonight's first).")
    ],
    week: Annotated[
        Path | None, typer.Option("--week", help="A snapshot about a week older, for flip-backs.")
    ] = None,
) -> None:
    """The accuracy check a directed night runs each round. Reads only."""
    from tracker.night import guard

    if not against.is_file():
        _fail(f"no snapshot at {against}")
    found = guard.check(_db_path(), against, week)
    if json_mode():
        emit(found.as_json())
        return
    console.print(escape(found.summary()))
    for line in found.detail[:40]:
        console.print(f"  {escape(line)}")


@director_app.command("plan")
def plan_cmd(
    night: Annotated[
        str | None, typer.Option("--night", help="A night's stamp; default the latest.")
    ] = None,
) -> None:
    """What a night's director was told and decided: check-ins, plans, verdicts, fallbacks."""
    from tracker.night import director

    runs = _runs()
    files = sorted(runs.glob(f"{director.PLAN_PREFIX}*.jsonl"))
    if night:
        files = [f for f in files if night in f.name]
    if not files:
        console.print("no director plan files")
        return
    entries = director.PlanFile.read(files[-1])
    if json_mode():
        emit(entries)
        return
    console.print(files[-1].name)
    for entry in entries:
        kind = entry.get("kind")
        at = entry.get("at", "")[11:19]
        if kind == "checkin":
            console.print(f"{at} check-in {entry.get('number')}")
        elif kind == "reply":
            args = entry.get("arguments") or {}
            steps = ", ".join(
                f"{s.get('step')} {s.get('size', '')}".strip() for s in args.get("steps") or []
            )
            console.print(
                escape(f"{at}   {entry.get('tool')}: {steps or '-'} — {args.get('why', '')}")
            )
        elif kind == "verdict":
            console.print(escape(f"{at}   referee: {entry.get('text', '').splitlines()[0]}"))
        elif kind in ("fallback", "invalid", "folded"):
            console.print(
                escape(
                    f"{at}   {kind}: {entry.get('why') or entry.get('errors') or entry.get('prompt_tokens')}"
                )
            )
