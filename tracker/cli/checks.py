"""`tracker checks`: the nightly spot check — a random sample of the night's changed
values, judged by a person. See `tracker/spotcheck.py` for why it exists."""

from __future__ import annotations

import datetime as dt
import getpass
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from tracker.cli._shared import (
    _fail,
    _read_engine,
    _writable,
    checks_app,
    console,
    emit,
    json_mode,
)
from tracker.db import session_scope


@checks_app.callback(invoke_without_command=True)
def checks_cmd(
    ctx: typer.Context,
    days: Annotated[
        int | None,
        typer.Option("--days", help="Only the samples of the last N nights."),
    ] = None,
) -> None:
    """How many of the sampled changes a person judged right. Reads only.

    Each night the loop draws 15 of its changed values at random; this is the share
    judged right, with its 95% interval, overall, by field, by kind of change and by
    night. Unlike `tracker answers`, which says whether a night broke a value already
    verified, this estimates how much of what the loop writes is right.
    """
    if ctx.invoked_subcommand is not None:
        return
    from tracker import spotcheck

    since = dt.date.today() - dt.timedelta(days=days) if days else None
    with session_scope(_read_engine(), commit=False) as session:
        got = spotcheck.score(session, since=since)
    if json_mode():
        emit(
            {
                "total": spotcheck.tally_dict(got.total),
                "by_field": {k: spotcheck.tally_dict(v) for k, v in got.by_field.items()},
                "by_kind": {k: spotcheck.tally_dict(v) for k, v in got.by_kind.items()},
                "by_night": {
                    k.isoformat(): spotcheck.tally_dict(v) for k, v in got.by_night.items()
                },
            }
        )
        return
    if not got.by_night:
        console.print("No samples yet: the nightly loop draws the first at the end of a night.")
        return
    console.print(f"[bold]all[/bold]  {got.total.line()}")
    for title, groups in (("by field", got.by_field), ("by kind", got.by_kind)):
        console.print(f"\n[bold]{title}[/bold]")
        for name, t in sorted(groups.items()):
            console.print(f"  {name:16} {t.line()}")
    console.print("\n[bold]by night[/bold]")
    for night, t in sorted(got.by_night.items(), reverse=True):
        console.print(f"  {night}  {t.line()}")


@checks_app.command("draw")
def draw_cmd(
    against: Annotated[
        Path,
        typer.Option(
            "--against",
            help="A copy of the database from before the night — the snapshot the nightly "
            "loop takes before round 1.",
        ),
    ],
    night: Annotated[
        str | None,
        typer.Option("--night", help="The night, YYYY-MM-DD. Read from the snapshot's name."),
    ] = None,
    size: Annotated[int, typer.Option("--size", help="How many to draw.")] = 15,
) -> None:
    """Draw a night's sample: changed values picked at random for a person to judge.

    Once per night: a night already drawn is left alone, so a judged sample is never
    replaced. The nightly loop runs this at the end of each night.
    """
    from tracker import changes as changes_mod
    from tracker import spotcheck

    if not against.is_file():
        _fail(f"no snapshot at {against}")
        return
    day = dt.date.fromisoformat(night) if night else spotcheck.night_of(against)
    if day is None:
        _fail(f"cannot tell the night from {against.name}; pass --night")
        return
    with session_scope(_writable("checks draw")) as session:
        found = changes_mod.diff(against, session)
        drawn = spotcheck.draw(session, found, day, size=size)
        lines = spotcheck.report_lines(session, drawn, today=day)
    if json_mode():
        emit(vars(drawn) | {"night": drawn.night.isoformat()})
        return
    for line in lines:
        console.print(escape(line), soft_wrap=True)


@checks_app.command("show")
def show_cmd(
    night: Annotated[
        str | None,
        typer.Argument(help="The night, YYYY-MM-DD. The latest by default."),
    ] = None,
) -> None:
    """One night's sample: each change, the sentence behind it, and its answer. Reads only."""
    from tracker import spotcheck

    with session_scope(_read_engine(), commit=False) as session:
        held = spotcheck.nights(session)
        day = dt.date.fromisoformat(night) if night else (held[0] if held else None)
        if day is None:
            console.print("No samples yet.")
            return
        rows = [spotcheck.as_dict(c) for c in spotcheck.listing(session, day)]
    if json_mode():
        emit(rows)
        return
    console.print(f"[bold]{day}[/bold]: {len(rows)} drawn")
    for r in rows:
        answer = r["verdict"] or "[yellow]not answered[/yellow]"
        console.print(f"\n  #{r['id']}  {answer}  {escape(r['label'][:70])}")
        console.print(f"    {escape(r['question'])}")
        if r["quote"]:
            console.print(f"    [dim]“{escape(' '.join(r['quote'].split())[:240])}”[/dim]")
        elif r["kind"] != "emptied":
            console.print("    [dim]no sentence stands behind it[/dim]")
        if r["url"]:
            console.print(f"    [dim]{escape(r['url'])}[/dim]")
        if r["note"]:
            console.print(f"    note: {escape(r['note'])}")


@checks_app.command("mark")
def mark_cmd(
    check_id: Annotated[int, typer.Argument(help="The check's number, from `show`.")],
    verdict: Annotated[str, typer.Argument(help="right, wrong or unsure.")],
    note: Annotated[str, typer.Option("--note", help="Why, in a line.")] = "",
    by: Annotated[str | None, typer.Option("--by", help="Who judged it.")] = None,
) -> None:
    """Record a person's answer to one check. A second answer replaces the first."""
    from tracker import spotcheck

    with session_scope(_writable("checks mark")) as session:
        try:
            check = spotcheck.mark(
                session, check_id, verdict, by=by or getpass.getuser(), note=note
            )
        except spotcheck.CheckError as exc:
            _fail(str(exc))
            return
        console.print(f"#{check.id} {check.field} on {escape(check.label[:60])}: {verdict}")
