"""`tracker answers`: the answer key, and how the database scores against it."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from tracker.cli._shared import (
    TABLE_BOX,
    _fail,
    _read_engine,
    _writable,
    answers_app,
    console,
    emit,
    json_mode,
)
from tracker.db import session_scope


@answers_app.callback(invoke_without_command=True)
def answers_cmd(
    ctx: typer.Context,
    drafts: Annotated[
        bool,
        typer.Option("--drafts", help="Score draft answers as well, labelled as such."),
    ] = False,
    against: Annotated[
        Path | None,
        typer.Option(
            "--against",
            help="A copy of the database from before a change: score both against the "
            "same answers and list every answer whose outcome moved.",
        ),
    ] = None,
) -> None:
    """How the database scores against the verified answers. Reads only.

    Nothing else here says whether a stored value is true — the quote check says a
    sentence exists, the tiers count open questions. Each answer is judged right,
    imprecise (a date that agrees at a coarser precision), wrong, or empty, and each
    field's share right is given with its 95% interval. With `--against`, the same
    answers score a snapshot too, and every answer a change moved is listed: the way
    to weigh a rule before it ships. How an answer earns "verified" is in
    docs/data-quality.md, "The answer key".
    """
    if ctx.invoked_subcommand is not None:
        return
    from tracker import answer_key

    statuses = ("verified", "draft") if drafts else ("verified",)
    if against is not None and not against.is_file():
        _fail(f"no snapshot at {against}")
        return
    with session_scope(_read_engine(), commit=False) as session:
        now = answer_key.score(session, statuses=statuses)
        before = moved = None
        if against is not None:
            rows = answer_key.snapshot_rows(against)
            before = answer_key.score(session, statuses=statuses, rows=rows)
            moved = answer_key.compare(session, rows, statuses=statuses)
    if json_mode():
        emit(
            {
                "statuses": list(statuses),
                "now": _as_json(now),
                "before": _as_json(before) if before is not None else None,
                "moved": [vars(m) for m in moved] if moved is not None else None,
            }
        )
        return
    _print_score(now, title="now" if before is not None else "score", drafts=drafts)
    if before is not None:
        _print_score(before, title=f"before ({against.name})", drafts=drafts)
        better = sum(1 for m in moved if m.now == "right")
        worse = sum(1 for m in moved if m.was == "right")
        console.print(f"{better} answer(s) became right, {worse} stopped being right")
        for m in moved:
            console.print(
                escape(
                    f"  #{m.project_id} {m.field}: {m.was} -> {m.now} ({m.was_value!r} -> {m.now_value!r})"
                )
            )


@answers_app.command("load")
def load_cmd(
    path: Annotated[
        str, typer.Argument(help="A JSON file of answers (a list, or one per line); - reads stdin.")
    ],
    replace: Annotated[
        bool,
        typer.Option(
            "--replace", help="Overwrite an answer already held for the same campus and field."
        ),
    ] = False,
) -> None:
    """Store a batch of answers. Run on the host, where the database is written.

    Every answer is checked before any is stored, so a batch with one bad answer is
    refused whole, naming it. An answer already held is kept unless `--replace`.
    """
    from tracker import answer_key

    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        records = json.loads(text)
    except json.JSONDecodeError:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(records, dict):
        records = [records]
    try:
        parsed = [answer_key.parse(r) for r in records]
    except ValueError as exc:
        _fail(str(exc))
        return
    with session_scope(_writable("answers load")) as session:
        report = answer_key.load(session, parsed, replace=replace)
    if json_mode():
        emit(vars(report))
        return
    console.print(
        f"{report.added} added, {report.replaced} replaced, {report.kept} kept as held"
        + (
            f"; no such campus: {', '.join(f'#{i}' for i in report.missing)}"
            if report.missing
            else ""
        )
    )


@answers_app.command("show")
def show_cmd(
    project: Annotated[int | None, typer.Option("--project", help="One campus only.")] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", help="verified, unsettled or draft (default: all)."),
    ] = None,
) -> None:
    """Every answer beside what its row holds now, and the verdict. Reads only."""
    from sqlalchemy import select

    from tracker import answer_key
    from tracker.models import AnswerKey, Project

    with session_scope(_read_engine(), commit=False) as session:
        query = select(AnswerKey).order_by(AnswerKey.project_id, AnswerKey.field)
        if project is not None:
            query = query.where(AnswerKey.project_id == project)
        if status is not None:
            query = query.where(AnswerKey.status == status)
        lines = []
        for answer in session.scalars(query):
            row = session.get(Project, answer.project_id)
            value, precision = answer_key.held_values(row)[answer.field] if row else (None, None)
            verdict = (
                answer_key.judge(answer, value, precision) if answer.status != "unsettled" else "-"
            )
            truth = (
                answer.value
                if answer.low is None and answer.high is None
                else f"{answer.low}..{answer.high}"
            )
            lines.append(
                {
                    "project_id": answer.project_id,
                    "field": answer.field,
                    "status": answer.status,
                    "answer": truth,
                    "precision": answer.precision,
                    "held": None if value is None else str(value),
                    "verdict": verdict,
                    "source_url": answer.source_url,
                }
            )
    if json_mode():
        emit({"answers": lines})
        return
    table = Table(header_style="bold", box=TABLE_BOX)
    for column in ("row", "field", "status", "answer", "held", "verdict"):
        table.add_column(column)
    for line in lines:
        table.add_row(
            f"#{line['project_id']}",
            line["field"],
            line["status"],
            escape(str(line["answer"])) + (f" ({line['precision']})" if line["precision"] else ""),
            escape(str(line["held"])),
            line["verdict"],
        )
    console.print(table)


def _as_json(found) -> dict:
    return {
        "fields": {
            name: {
                "outcomes": dict(one.outcomes),
                "accuracy": one.accuracy,
                "interval": one.interval,
            }
            for name, one in found.fields.items()
        },
        "absent": found.absent,
    }


def _print_score(found, *, title: str, drafts: bool) -> None:
    table = Table(
        title=title + (" — draft answers included" if drafts else ""),
        header_style="bold",
        title_justify="left",
        box=TABLE_BOX,
    )
    for column in (
        "field",
        "answers",
        "right",
        "imprecise",
        "wrong",
        "empty",
        "share right",
        "95% interval",
    ):
        table.add_column(column, justify="left" if column == "field" else "right")
    for name, one in [*found.fields.items(), ("all", found.total)]:
        interval = one.interval
        table.add_row(
            name,
            str(one.judged),
            str(one.outcomes["right"]),
            str(one.outcomes["imprecise"]),
            str(one.outcomes["wrong"]),
            str(one.outcomes["empty"]),
            "-" if one.accuracy is None else f"{one.accuracy:.0%}",
            "-" if interval is None else f"{interval[0]:.0%} to {interval[1]:.0%}",
        )
    console.print(table)
    if found.absent:
        console.print(f"  {len(found.absent)} answer(s) name a campus not in this database")
