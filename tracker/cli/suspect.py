"""`tracker suspect`: the suspicion score that aims the nightly re-checks.

None of these commands writes to the database. `score` writes the ranked list to
`data/runs/suspicion.jsonl` and `train` the models to `data/models/`; the values
themselves are only ever changed by the pass the ranking aims (`logic resolve
--suspects N`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from tracker.cli._shared import (
    TABLE_BOX,
    _db_path,
    _fail,
    _read_engine,
    console,
    emit,
    json_mode,
    suspect_app,
)
from tracker.db import session_scope


@suspect_app.callback(invoke_without_command=True)
def suspect_cmd(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", help="Rows of the ranking to show.")] = 20,
) -> None:
    """Tonight's ranking: the stored values most likely to be wrong, first."""
    if ctx.invoked_subcommand is not None:
        return
    from tracker import suspect

    rows = suspect.ranked(_db_path())
    if json_mode():
        emit({"ranked": rows[:limit], "total": len(rows)})
        return
    if not rows:
        console.print("no ranking yet — `tracker suspect score` makes one")
        return
    table = Table(header_style="bold", box=TABLE_BOX)
    for column in ("rank", "row", "field", "held", "p wrong", "driven by"):
        table.add_column(column)
    for r in rows[:limit]:
        table.add_row(
            str(r["rank"]),
            f"#{r['project_id']}",
            r["field"],
            escape(str(r["held"])),
            f"{r['p_wrong']:.0%}",
            escape(", ".join(r.get("why") or [])),
        )
    console.print(table)
    console.print(f"[dim]{len(rows)} value(s) scored on {rows[0]['night']}[/dim]")


@suspect_app.command("score")
def score_cmd() -> None:
    """Rank every stored MW, investment and online date by how likely it is wrong."""
    from tracker import suspect

    path = _db_path()
    with session_scope(_read_engine(), commit=False) as session:
        if suspect.load(path) is None:
            _fail("no model yet — `tracker suspect train` fits one from the answer key")
        rows = suspect.score(session, path)
    if json_mode():
        emit({"scored": len(rows), "top": rows[:10]})
        return
    console.print(f"scored {len(rows)} value(s); `tracker suspect` lists them")


@suspect_app.command("train")
def train_cmd(
    snapshots: Annotated[
        Path | None,
        typer.Option("--snapshots", help="The nightly snapshot directory, for value history."),
    ] = None,
) -> None:
    """Refit both models; each is kept only if it ranks the answer key well enough."""
    from tracker import suspect, suspect_transformer

    if snapshots is not None and not snapshots.is_dir():
        _fail(f"--snapshots: {snapshots} is not a directory")
    path = _db_path()
    with session_scope(_read_engine(), commit=False) as session:
        logistic = suspect.train_logistic(session, path)
        transformer = (
            suspect_transformer.train(suspect.entries(session, path, snapshots=snapshots), path)
            if suspect_transformer.available()
            else {"kept": "skipped: PyTorch is not installed (the `head` extra)"}
        )
    if json_mode():
        emit({"logistic": logistic, "transformer": transformer})
        return
    for name, report in (("logistic", logistic), ("transformer", transformer)):
        detail = ", ".join(f"{k} {v}" for k, v in report.items() if k != "kept")
        console.print(f"{name}: {report['kept']}" + (f"  [dim]({detail})[/dim]" if detail else ""))


@suspect_app.command("row")
def row_cmd(project_id: Annotated[int, typer.Argument(help="The row to check.")]) -> None:
    """Both models' view of one row, field by field: P(wrong) and what drives it."""
    from tracker import suspect

    path = _db_path()
    with session_scope(_read_engine(), commit=False) as session:
        view = suspect.check_row(session, path, project_id)
    if not view["fields"]:
        _fail(f"no row #{project_id}")
    if json_mode():
        emit(view)
        return
    table = Table(header_style="bold", box=TABLE_BOX)
    for column in ("field", "held", "logistic", "transformer", "driven by"):
        table.add_column(column)
    for field, v in view["fields"].items():
        lp, tp = v.get("logistic_p_wrong"), v.get("transformer_p_wrong")
        table.add_row(
            field + ("" if v["trusted_field"] else " [dim](untrusted)[/dim]"),
            escape(str(v["held"])),
            "-" if lp is None else f"{lp:.0%}",
            "-" if tp is None else f"{tp:.0%}",
            escape(", ".join(n for n, c in v.get("logistic_why", []) if c > 0)),
        )
    console.print(table)
    console.print(f"[dim]{view['note']}[/dim]")
