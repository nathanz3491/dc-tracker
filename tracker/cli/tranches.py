"""How each campus's tranches nest: list, show, review, rule, undo.

`blocks.structure` derives what arithmetic can settle; `nesting` holds the rest — a
model's rulings from the nightly review and a person's from here or the project page.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape

from tracker.cli._shared import (
    NA,
    _db_path,
    _explain_db_locks,
    _fail,
    _model,
    _print_ceiling,
    _print_report_rows,
    _read_engine,
    _use_llm,
    _workers,
    _writable,
    again_flag,
    console,
    emit,
    json_mode,
    tranches_app,
)
from tracker.db import session_scope

#: How a link reads in a terminal.
_WORDS = {
    "part_of": "part of",
    "same_as": "same as",
    "separate": "separate",
    "superseded": "superseded",
    "elsewhere": "another campus",
}


def _who(by: str) -> str:
    if by.startswith("auto:"):
        return f"[dim]automatic ({by.split(':', 1)[1]})[/dim]"
    return "[cyan]model[/cyan]" if by == "model" else f"[green]{escape(by)}[/green]"


@tranches_app.callback(invoke_without_command=True)
def tranches_cmd(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", help="Campuses to list.")] = 30,
) -> None:
    """Campuses whose tranches still do not add up, worst first. Free, read-only.

    Sources describe a campus as a whole, again phase by phase and again building by
    building; summed as siblings those count the same megawatts several times. The
    tranches are summed as the tree they form (`blocks.structure`): what arithmetic
    can settle is settled on every read, and the rest waits for a ruling — the
    nightly `review`, or a person's `rule`. Each campus here is listed with what is
    still open: counted twice, tied to no facility, live past what the campus runs.
    """
    if ctx.invoked_subcommand is not None:
        return
    from tracker import nesting

    with session_scope(_read_engine(), commit=False) as session:
        open_ = nesting.candidates(session)
        rows = [
            {
                "project_id": p.id,
                "project": f"{p.company} — {p.name}",
                "tranches": len(p.blocks),
                **s.as_json(),
            }
            for p, s in open_
        ]
    if json_mode():
        emit({"open": rows})
        return
    if not rows:
        console.print("[green]every campus's tranches add up[/green]")
        return
    total = sum(r["overlap_mw"] + r["unplaced_mw"] + r["live_over_mw"] for r in rows)
    console.print(
        f"[bold]{len(rows)}[/bold] campus(es) whose tranches do not yet add up, "
        f"{total:,.0f} MW at stake.\n"
    )
    for row in rows[:limit]:
        parts = [
            f"{row[k]:,.0f} MW {label}"
            for k, label in (
                ("overlap_mw", "counted twice"),
                ("unplaced_mw", "tied to no facility"),
                ("live_over_mw", "live past what runs"),
            )
            if row[k] >= 0.5
        ]
        console.print(
            f"  [bold]#{row['project_id']}[/bold] {escape(row['project'][:52])} "
            f"[dim]({row['tranches']} tranches)[/dim] — {', '.join(parts)}"
        )
    if len(rows) > limit:
        console.print(f"\n[dim]{len(rows) - limit} more; raise --limit to see them.[/dim]")
    console.print(
        "\n[dim]`tracker tranches show ID` draws one; `tracker tranches review` puts them "
        "to the model.[/dim]"
    )


@tranches_app.command("show")
def show(project_id: Annotated[int, typer.Argument(help="The campus.")]) -> None:
    """One campus's tranches as the tree they form, with who placed each and why."""
    from tracker import blocks as blocks_mod
    from tracker import nesting

    with session_scope(_read_engine(), commit=False) as session:
        project = nesting.project_with_rulings(session, project_id)
        if project is None:
            _fail(f"no project #{project_id}")
            return
        nested = blocks_mod.structure(project)
        tree = blocks_mod.tranche_tree(project, nested)
        got = blocks_mod.account(project, nested)
        at_stake = nesting.stake(project)
        by_key = {b.block_key: b for b in project.blocks}

        def line(key: str, depth: int) -> None:
            block = by_key[key]
            mw = NA if block.mw is None else f"{block.mw:,.0f} MW"
            if block.mw is not None and not blocks_mod.mw_is_confirmed(block):
                mw += " 待确认"
            link = nested.link(key)
            how = f"  {_who(link.by)}" if link is not None else ""
            if link is not None and link.reason:
                how += f" [dim]— {escape(link.reason[:90])}[/dim]"
            console.print(
                f"  {'   ' * depth}{'└ ' if depth else ''}{escape(block.label[:48])} "
                f"[dim]{mw}, {block.status}[/dim]{how}"
            )
            for child in tree.children.get(key, ()):
                line(child, depth + 1)

        if json_mode():
            emit(
                {
                    "project_id": project.id,
                    "links": [link.as_json() for link in nested.links.values()],
                    "dormant": [link.as_json() for link in nested.dormant],
                    "residuals": [{"reason": r.reason, "mw": r.mw} for r in got.residuals],
                    **at_stake.as_json(),
                }
            )
            return
        console.print(
            f"[bold]#{project.id} {escape(project.company)} — {escape(project.name)}[/bold]  "
            f"[dim]{project.mw_planned or 0:,.0f} MW planned, "
            f"{project.mw_built or 0:,.0f} MW running[/dim]\n"
        )
        for root in tree.roots:
            line(root, 0)
        aside = [k for k, link in nested.links.items() if link.relation in blocks_mod.SET_ASIDE]
        if aside:
            console.print("\n[bold]set aside[/bold]")
            for key in sorted(aside):
                link = nested.links[key]
                target = by_key.get(link.target or "")
                console.print(
                    f"  {escape(by_key[key].label[:48])} — {_WORDS[link.relation]}"
                    f"{' ' + escape(target.label[:40]) if target else ''}  {_who(link.by)}"
                    + (f" [dim]— {escape(link.reason[:90])}[/dim]" if link.reason else "")
                )
        if nested.dormant:
            console.print(
                f"\n[dim]{len(nested.dormant)} ruling(s) wait for tranches no source states "
                "any more.[/dim]"
            )
        console.print()
        if at_stake.settled:
            console.print("[green]the tranches add up[/green]")
        else:
            console.print(
                "[yellow]still open[/yellow]: "
                + ", ".join(
                    f"{mw:,.0f} MW {label}"
                    for mw, label in (
                        (at_stake.overlap, "counted twice"),
                        (at_stake.unplaced, "tied to no facility"),
                        (at_stake.live_over, "live past what runs"),
                    )
                    if mw >= 0.5
                )
            )


@tranches_app.command("review")
def review(
    project_id: Annotated[
        int | None, typer.Option("--project", help="Review only this campus.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Campuses to review. One call each.")] = 15,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Review at full cost and write nothing.")
    ] = False,
    again: Annotated[
        bool, again_flag("Also review campuses already reviewed on the same tranches.")
    ] = False,
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Put the campuses whose tranches do not add up to the model, worst first.

    One call per campus, showing every tranche with its figures, its stated parent,
    the sentences quoted for it and how it is linked now. The model rules per
    tranche — part of, same as, separate, superseded, another campus — and a ruling
    is kept only when it names tranches that exist, would not make one part of
    itself, is at least 65% sure, and leaves a person's ruling alone. Its rulings on
    a campus replace its earlier ones; a person's are never touched.

    A campus that still does not add up is remembered with a hash of its tranches
    and not reviewed again until they change. `--again` reviews it anyway.
    """
    _use_llm(llm_provider)
    from tracker import nesting

    if not _db_path().is_file():
        _fail(f"database not found: {_db_path()}\nRun `tracker init` first.")
    extractor = _model("judgement")

    engine = _read_engine() if dry_run else _writable("tranches review")

    def announce(outcome) -> None:
        if json_mode():
            return
        style = {"settled": "green", "improved": "cyan", "error": "yellow"}.get(
            outcome.result, "dim"
        )
        console.print(
            f"  [{style}]{outcome.result:<9}[/{style}] #{outcome.project_id} "
            f"{escape(outcome.project[:48])} [dim]{outcome.before.total:,.0f} → "
            f"{outcome.after.total:,.0f} MW open, {outcome.written} ruling(s)[/dim]"
        )
        if outcome.note:
            console.print(f"      [dim]{escape(outcome.note[:160])}[/dim]")

    def started(run, chosen) -> None:
        if chosen and not json_mode():
            console.print(
                f"[bold]{len(chosen)}[/bold] of {len(open_)} campus(es) whose tranches do not "
                "add up, worst first — one model call each."
                + (
                    f" [dim]{run.held} already reviewed on the same tranches.[/dim]"
                    if run.held
                    else ""
                )
                + "\n"
            )

    with _explain_db_locks(), session_scope(engine, commit=not dry_run) as session:
        open_ = nesting.candidates(session, project_id=project_id)
        if not open_:
            console.print("[green]every campus's tranches add up[/green]")
            return
        # Each campus's rulings are committed as they are written; held-back campuses
        # are skipped before the limit.
        got = nesting.review_all(
            session,
            open_,
            extractor=extractor,
            limit=limit,
            again=again,
            write=not dry_run,
            workers=_workers("judgement"),
            on_each=announce,
            on_start=started,
        )
        outcomes = got.results
        if not outcomes and not got.ceiling:
            console.print(
                f"[green]nothing new to review[/green] [dim]— all {len(open_)} open campus(es) "
                "were reviewed on the tranches they hold now; --again reviews them.[/dim]"
            )
            return

    _print_ceiling(got)
    if json_mode():
        emit({"outcomes": [o.as_json() for o in outcomes], "open": len(open_)})
        return
    tally: dict[str, int] = {}
    for outcome in outcomes:
        tally[outcome.result] = tally.get(outcome.result, 0) + 1
    _print_report_rows(
        [
            ("reviewed", len(outcomes)),
            ("settled", tally.get("settled", 0)),
            ("improved", tally.get("improved", 0)),
            ("unchanged", tally.get("unchanged", 0)),
            ("rulings written", sum(o.written for o in outcomes)),
            ("rulings refused", sum(len(o.refused) for o in outcomes)),
            ("call failed", tally.get("error", 0)),
        ],
        title="tranches review (dry run)" if dry_run else "tranches review",
        warn={"call failed"},
    )
    if dry_run:
        console.print("\n[yellow]dry run[/yellow] — nothing was written")


@tranches_app.command("rule")
def rule(
    project_id: Annotated[int, typer.Argument(help="The campus.")],
    tranche: Annotated[str, typer.Argument(help="The tranche: its key or its exact label.")],
    relation: Annotated[
        str,
        typer.Argument(help="part_of, same_as, separate, superseded or elsewhere."),
    ],
    target: Annotated[
        str | None,
        typer.Argument(
            help="For part_of / same_as (and optionally superseded): the other tranche."
        ),
    ] = None,
    by: Annotated[str, typer.Option("--by", help="Who is ruling, as the record names them.")] = "",
    reason: Annotated[str, typer.Option("--reason", help="Why, in one sentence.")] = "",
) -> None:
    """Rule how one tranche relates to the rest of its campus. A person's ruling stands.

    It outranks the model's and every automatic rule, and the nightly review will not
    revisit it. `tracker tranches undo` takes it back.

    \b
      tracker tranches rule 3 "Shackelford County Site" elsewhere --by nathan
      tracker tranches rule 3 "Building 1" part_of "Abilene Campus" --by nathan
    """
    from tracker import nesting

    if not by.strip():
        _fail("--by is required: a ruling records who made it")
        return
    with _explain_db_locks(), session_scope(_writable("tranches rule")) as session:
        project = nesting.project_with_rulings(session, project_id)
        if project is None:
            _fail(f"no project #{project_id}")
            return
        key = nesting.find_key(project, tranche)
        target_key = nesting.find_key(project, target) if target else None
        if key is None:
            _fail(f"#{project_id} has no tranche keyed or labelled {tranche!r}")
            return
        if target and target_key is None:
            _fail(f"#{project_id} has no tranche keyed or labelled {target!r}")
            return
        relation = relation.strip().lower().replace("-", "_").replace(" ", "_")
        try:
            row = nesting.rule(
                session, project, key, relation, target_key, by=by.strip(), reason=reason
            )
        except nesting.RulingError as exc:
            _fail(str(exc))
            return
        console.print(
            f"[green]ruled[/green] #{project_id} {escape(key)} {_WORDS[row.relation]}"
            f"{' ' + escape(row.target_key) if row.target_key else ''} [dim]— {escape(row.decided_by)}[/dim]"
        )


@tranches_app.command("undo")
def undo(
    project_id: Annotated[int, typer.Argument(help="The campus.")],
    tranche: Annotated[str, typer.Argument(help="The tranche: its key or its exact label.")],
) -> None:
    """Take back the ruling on one tranche — a model's or a person's."""
    from tracker import nesting

    with _explain_db_locks(), session_scope(_writable("tranches undo")) as session:
        project = nesting.project_with_rulings(session, project_id)
        if project is None:
            _fail(f"no project #{project_id}")
            return
        key = nesting.find_key(project, tranche) or tranche
        if nesting.undo(session, project, key):
            console.print(f"[green]taken back[/green] #{project_id} {escape(key)}")
        else:
            console.print(f"[dim]#{project_id} {escape(key)} carried no ruling[/dim]")
