"""`tracker duplicates` and `tracker merge` — rows that look like one campus stored twice.

The group proposes, parks and (behind `--merge`) folds; `merge` lives beside it in
this module because it is the disposing half of the same question, while staying a
top-level command by name because it deletes rows and deserves its own. The
pipeline is drawn in `docs/workflows/duplicates.md`; touching a function named in
that page's source map means updating the page in the same commit (`CLAUDE.md` §7).
"""

from __future__ import annotations

import atexit
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from tracker.cli._shared import (
    TABLE_BOX,
    _db_path,
    _explain_db_locks,
    _fail,
    _fmt_mw,
    _location,
    _print_report_rows,
    _read_engine,
    _use_llm,
    _writable,
    app,
    console,
    duplicates_app,
    emit,
    err,
    json_mode,
)
from tracker.db import AlreadyRunning, acquire_write_lock, init_db, session_scope
from tracker.models import Project

#: The colour each evidence class is drawn in. The *words* come from
#: `capex.EVIDENCE_LABELS`, so the console and this report cannot name the same
#: evidence differently — five classes that carry very different consequences, one
#: of which permits an unattended merge and another of which is a word.
#:
#: `identity` was missing from this table while being the most common class in it —
#: 31 of 49 pairs on the live database — so the majority of the report printed a
#: bare `?` where its reason belonged. A class a reader cannot name is a class they
#: cannot act on.
_EVIDENCE_COLOUR: dict[str, str] = {
    "exact": "magenta",
    "tranche": "green",
    "party": "cyan",
    "identity": "blue",
    "name": "yellow",
}


@duplicates_app.callback(invoke_without_command=True)
def duplicates(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", help="Groups to show.")] = 30,
    weak: Annotated[
        bool,
        typer.Option(
            "--weak/--no-weak",
            help="Include pairs raised only by a shared name word. Off shows the hard evidence.",
        ),
    ] = True,
    parked_too: Annotated[
        bool,
        typer.Option("--parked", help="Show pairs already ruled out, and nothing else."),
    ] = False,
) -> None:
    """Rows that look like one campus stored several times.

    Bare `tracker duplicates` lists the suspected groups. The subcommands are the
    two answers to one: `park` records that a group is *not* one campus, `unpark`
    reopens that decision, and `parked` shows every pair already ruled out. The
    other answer is `tracker merge`, which lives outside this group because it
    deletes rows and that deserves its own name.

    One site often has a builder, a landlord and an occupier, and whichever name a
    source picks becomes its own row with its own dedup key. Every key is correct;
    the building is one. `tracker capex` defends itself — it counts one row per
    suspected group and discloses the rest — but the listing still carries every
    row, and only a merge repairs that.

    **Each pair now says what raised it**, strongest first, because "these two look
    similar" is not something a reader can check and "both hold tranche horizon-1,
    a key that appears nowhere else in the country" is. Three signals, and they are
    not equal: a shared tranche is two readings of one building, a shared operator
    is how one campus becomes four rows, and a shared name word is a word.

    **A shared word had to get stricter.** `Aligned Data Centers Phoenix` and
    `NTT Global Data Centers Americas Phoenix` were reported as one campus on the
    token "centers" — the generic list held the singular and not the plural. So was
    `Element Critical — Houston One` against `Switch — Houston Data Center Campus`,
    on a tranche both had labelled "existing". A tranche key that turns up in more
    than one town is vocabulary, not identity, and no longer pairs anything.

    Nothing here is merged. Fold a real group with `tracker merge`; rule a false
    one out for good with `tracker duplicates park`, which also stops `capex`
    holding one of the two rows out of the buyer table.
    """
    if ctx.invoked_subcommand is not None:
        return

    from tracker import capex as capex_mod
    from tracker import pairs as pairs_mod

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        if parked_too:
            _print_parked(pairs_mod.listing(session))
            return
        pairs = capex_mod.suspected_duplicates(session)
        parked_count = len(pairs_mod.parked_keys(session))
    if not weak:
        pairs = [p for p in pairs if p.kinds and p.kinds[0] != "name"]
    wasted = capex_mod.double_counted_mw(pairs)

    if json_mode():
        emit(
            {
                "double_counted_mw": wasted,
                "parked_pairs": parked_count,
                "pairs": [p.as_json() for p in pairs],
                "groups": [
                    {"ids": ids, "members": _dupe_labels(pairs, ids)}
                    for ids in capex_mod.duplicate_groups(pairs)
                ],
            }
        )
        return

    if not pairs:
        note = f" [dim]({parked_count} pair(s) already ruled out)[/dim]" if parked_count else ""
        console.print(f"[green]no suspected duplicates[/green]{note}")
        return

    groups = capex_mod.duplicate_groups(pairs)
    by_pair = {(p.a_id, p.b_id): p for p in pairs}

    console.print(
        f"[bold]{len(groups)}[/bold] suspected group(s), holding "
        f"[bold]{_fmt_mw(wasted)} MW[/bold] stored more than once — `tracker capex` "
        "skips the extra rows and says so until each group is settled."
    )
    if parked_count:
        console.print(
            f"[dim]{parked_count} pair(s) already ruled out and not shown "
            "— `tracker duplicates --parked`[/dim]"
        )
    console.print()

    for ids in groups[:limit]:
        member = {p.a_id: p for p in pairs if p.a_id in ids}
        member.update({p.b_id: p for p in pairs if p.b_id in ids})
        strongest = min(
            (by_pair[(a, b)] for a, b in by_pair if a in ids and b in ids),
            key=lambda p: p.rank,
        )
        kind = strongest.kinds[0] if strongest.kinds else ""
        colour = _EVIDENCE_COLOUR.get(kind, "white")
        label = capex_mod.EVIDENCE_LABELS.get(kind, "same locality")
        console.print(
            f"  [{colour}]{label}[/{colour}]  [dim]{escape(strongest.locality)}, "
            f"{strongest.state}[/dim]"
        )
        for project_id in ids:
            console.print(f"    #{project_id:<5} {escape(_dupe_label(pairs, project_id)[:78])}")
        # Every pair inside the group, with its own evidence: a group of three is
        # three separate questions, and one of them is often the wrong one.
        for a, b in sorted(by_pair):
            if a in ids and b in ids:
                console.print(f"    [dim]#{a} + #{b}: {escape(by_pair[(a, b)].why)}[/dim]")
        console.print(
            f"    [dim]one campus:[/dim]    tracker merge --into {ids[0]} "
            f"{' '.join(str(i) for i in ids[1:])}"
        )
        console.print(
            f"    [dim]different sites:[/dim] tracker duplicates park "
            f"{' '.join(str(i) for i in ids)}\n"
        )
    if len(groups) > limit:
        console.print(f"[dim]{len(groups) - limit} more; raise --limit[/dim]\n")
    console.print(
        "[dim]every quantitative field is recomputed from the combined citations after a "
        "merge; identity fields (name, company, locality) stay the survivor's, so pick "
        "the row whose identity should win.[/dim]"
    )


def _dupe_label(pairs, project_id: int) -> str:
    for pair in pairs:
        if pair.a_id == project_id:
            return f"{pair.a_company} — {pair.a_name}"
        if pair.b_id == project_id:
            return f"{pair.b_company} — {pair.b_name}"
    return f"#{project_id}"


def _dupe_labels(pairs, ids: list[int]) -> list[str]:
    return [_dupe_label(pairs, i) for i in ids]


def _print_parked(rows) -> None:
    if json_mode():
        emit({"parked": [r.as_json() for r in rows]})
        return
    if not rows:
        console.print(
            "[dim]no pair has been ruled out yet[/dim] — `tracker duplicates park A B` "
            "records that two rows are different sites"
        )
        return
    table = Table(
        title=f"{len(rows)} pair(s) ruled out",
        header_style="bold",
        title_justify="left",
        box=TABLE_BOX,
    )
    table.add_column("pair")
    table.add_column("rows", max_width=64)
    table.add_column("who")
    table.add_column("why", max_width=40, style="dim")
    for row in rows:
        table.add_row(
            f"#{row.a_id} + #{row.b_id}",
            f"{escape(row.a_label[:60])}\n{escape(row.b_label[:60])}",
            escape(row.decided_by),
            escape(row.reason or "—"),
        )
    console.print(table)
    console.print(
        "\n[dim]reopen one with:[/dim] tracker duplicates unpark A B\n"
        "[dim]a parked pair is dropped from the duplicates report and from the rows "
        "`tracker capex` holds out of the buyer table.[/dim]"
    )


@duplicates_app.command("park")
def duplicates_park(
    project_ids: Annotated[
        list[int], typer.Argument(help="Two or more project ids that are NOT one campus.")
    ],
    reason: Annotated[
        str, typer.Option("--reason", help="Why, for whoever reads this in six months.")
    ] = "",
) -> None:
    """Record that these rows are different sites, so the report stops asking.

    The missing half of `tracker duplicates`. Until now the only answer available
    was `merge`, so a pair that was simply wrong came back on every run — ahead of
    the real ones, because there was nothing to push it down.

    **This is not cosmetic.** `capex.rollup` reads the same suspected pairs and
    holds one row of every group out of the buyer table, disclosed in the skip
    fields. A false pair therefore takes a real campus's capacity out of a number
    somebody quotes. Parking puts it back.

    Every pair among the ids given is stored separately, so a third row appearing
    next week is still asked about. Nothing is edited or deleted; `tracker
    duplicates unpark` reopens the question.
    """
    from tracker import pairs as pairs_mod

    if len(project_ids) < 2:
        _fail("give at least two project ids — parking is a statement about a pair")

    engine = _writable("duplicates park")

    try:
        with _explain_db_locks(), session_scope(engine) as session:
            written = pairs_mod.park(session, list(project_ids), reason=reason)
            labels = {
                p.id: f"{p.company} — {p.name}"
                for p in session.scalars(
                    select(Project).where(Project.id.in_(list(project_ids)))
                ).all()
            }
    except pairs_mod.UnknownProject as exc:
        _fail(str(exc))
        raise

    if json_mode():
        emit({"parked": [{"a_id": a, "b_id": b} for a, b in written]})
        return

    for project_id in sorted(set(project_ids)):
        console.print(f"  #{project_id:<5} {escape(labels.get(project_id, '')[:78])}")
    already = len(project_ids) * (len(project_ids) - 1) // 2 - len(written)
    console.print(
        f"\n[green]{len(written)} pair(s) recorded as different sites[/green]"
        + (f" [dim]({already} already were)[/dim]" if already > 0 else "")
    )
    console.print(
        "[dim]they are gone from `tracker duplicates`, and `tracker capex` will stop "
        "holding either row out of the buyer table. Reopen with `tracker duplicates "
        "unpark`.[/dim]"
    )


@duplicates_app.command("unpark")
def duplicates_unpark(
    project_ids: Annotated[list[int], typer.Argument(help="Two or more project ids.")],
) -> None:
    """Reopen a pair somebody ruled out, putting it back in the report.

    The exact inverse of `park`, including its shape: with two ids it reopens one
    question, and with more it reopens every pair among them, so an operator
    undoing a decision does not have to work out which spellings were written.

    A reopened pair reappears in `tracker duplicates` and goes back to being one
    of the rows `tracker capex` holds out of the buyer table.
    """
    from tracker import pairs as pairs_mod

    if len(project_ids) < 2:
        _fail("give at least two project ids")

    engine = _writable("duplicates unpark")

    with _explain_db_locks(), session_scope(engine) as session:
        removed = pairs_mod.unpark(session, list(project_ids))

    if json_mode():
        emit({"unparked": [{"a_id": a, "b_id": b} for a, b in removed]})
        return
    if not removed:
        console.print("[dim]none of those pairs was parked[/dim]")
        return
    for a, b in removed:
        console.print(f"  [yellow]reopened[/yellow] #{a} + #{b}")
    console.print(f"\n{len(removed)} pair(s) back in `tracker duplicates`")


@duplicates_app.command("resolve")
def duplicates_resolve(
    llm: Annotated[
        bool,
        typer.Option("--llm/--no-llm", help="Let a reasoning model decide the ones you did not."),
    ] = True,
    ask: Annotated[
        bool,
        typer.Option(
            "--ask/--no-ask",
            help="Put each pair to you first. Off, or with no terminal, goes to the model.",
        ),
    ] = False,
    merge_them: Annotated[
        bool,
        typer.Option(
            "--merge",
            help="Also fold the pairs it rules are one campus. Deletes rows; read the rails first.",
        ),
    ] = False,
    agent: Annotated[
        bool,
        typer.Option(
            "--agent/--no-agent",
            help=(
                "Default. A model reads both rows' articles, searches if it must, and "
                "rules. Drops the cross-granularity refusal, which 28 of 47 live groups "
                "hit. Suppressed by --ask and by --no-llm."
            ),
        ),
    ] = True,
    min_confidence: Annotated[
        float,
        typer.Option("--min-confidence", help="With --agent and --merge: the floor a fold needs."),
    ] = 0.85,
    weak: Annotated[
        bool,
        typer.Option("--weak", help="Also ask about pairs raised only by a shared name word."),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="Pairs to work through.")] = 20,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Ask, report, write nothing. The calls are still paid for."),
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
    """Settle the suspected duplicates: a model decides, and the rails decide what it may do.

    `tracker duplicates` proposes and never disposes, which is right — a wrong
    merge destroys two rows and no re-crawl recovers them. The cost of that caution
    was a report nobody answered: 30 pairs on the live database, 29 of them across
    genuinely different company names, each waiting on a person to open two rows and
    read their citations. This is that first pass.

    **One call per pair, three answers, trusted unequally** — because their
    consequences are not equally reversible.

    * **different** parks the pair, recording `model (0.87)` as the decider. That
      is what `not_duplicate.decided_by` was built for, and `duplicates unpark`
      undoes it. It is also the useful half: `capex` holds one row of every
      suspected group out of the buyer table, so a false pair keeps a real campus's
      capacity out of a published number until somebody rules it out.
    * **same** merges, but only with `--merge` and only past every rail below.
    * **unclear** leaves the pair in the report, which is a real answer.

    **What `--merge` still refuses**, all of it printed per pair when it bites:
    confidence under 0.9; a pair whose only evidence is a shared name word ("a
    shared name word is a word"); a pair whose only evidence is a cross-granularity
    key match, because `dedup` has never merged a county row into a city row
    unattended and a model is not a person with a map; and two rows whose stored
    coordinates are more than 25 km apart, because geography outranks the model.

    **Which row survives is not the model's choice.** It is the row with more
    citations, then more fields filled, then the lower id — and it barely matters,
    because a merge recomputes every field from the combined claims. The model
    cannot name a survivor, cannot edit a field, and cannot merge anything the
    rails refuse.

    Spends one model call per pair. `--dry-run` still pays for the calls and writes
    nothing, which is the honest way to see what a run would do.
    """
    _use_llm(llm_provider)
    import sys as _sys

    from tracker import dupresolve

    path = _db_path()
    if not path.is_file():
        _fail(f"database not found: {path}\nRun `tracker init` first.")

    interactive = ask and _sys.stdin.isatty() and not json_mode()
    keyboard = _dupe_prompt if interactive else None

    # `--ask` means a person is deciding, so the agent stands down: paying for a
    # run whose answer is then overridden at the keyboard buys nothing. `--no-llm`
    # suppresses it too — the agent *is* a model, so "no model" has to mean no
    # model, or `--no-llm --no-ask` would spend calls instead of saying that
    # nothing can decide.
    use_agent = agent and llm and not interactive

    extractor = None
    if llm or use_agent:
        from tracker.llm import LLMUnavailable, agent_extractor, reasoning_extractor

        try:
            build = agent_extractor if use_agent else reasoning_extractor
            extractor = build()
        except LLMUnavailable as exc:
            if keyboard is None:
                _fail(str(exc))
            err.print(f"[yellow]no model available, so deciding at the keyboard[/yellow]\n{exc}")
            use_agent = False

    if extractor is None and keyboard is None:
        _fail("nothing can decide: pass --llm with a key configured, or --ask at a terminal.")

    # Writable even for `--dry-run`, exactly as `tracker merge --dry-run` is: the
    # run parks and merges inside a transaction and the dry run is the transaction
    # not being committed. A `mode=ro` connection cannot even hold those writes
    # long enough to describe them — SQLAlchemy flushes the park and SQLite
    # refuses. The write lock is taken either way, because this command can delete
    # rows and belongs under the same discipline as the merge it performs.
    engine = _writable("duplicates resolve")
    with _explain_db_locks(), session_scope(engine, commit=not dry_run) as session:
        if use_agent:
            from tracker import triage as triage_mod

            decisions = triage_mod.resolve_pairs(
                session,
                extractor=extractor,
                limit=limit,
                allow_merge=merge_them,
                weak=weak,
                min_confidence=min_confidence,
            )
        else:
            decisions = dupresolve.resolve(
                session,
                extractor=extractor,
                limit=limit,
                allow_merge=merge_them,
                weak=weak,
                ask=keyboard,
            )

    if json_mode():
        emit(
            {
                "decisions": [d.as_json() for d in decisions],
                "merged": sum(1 for d in decisions if d.action == "merged"),
                "parked": sum(1 for d in decisions if d.action == "parked"),
                "left": sum(1 for d in decisions if d.action == "left"),
                "dry_run": dry_run,
            }
        )
        return

    if not decisions:
        console.print("[green]no suspected duplicates to settle[/green]")
        return

    style = {"merged": "red", "parked": "green", "left": "dim"}
    for got in decisions:
        verdict = got.judgement.verdict if got.judgement else "no answer"
        confidence = f" {got.judgement.confidence:.2f}" if got.judgement else ""
        console.print(
            f"\n[{style[got.action]}]{got.action}[/{style[got.action]}] "
            f"[bold]{verdict}{confidence}[/bold]  {escape(got.label)}"
        )
        if got.judgement and got.judgement.reason:
            console.print(f"  [dim]{escape(got.judgement.reason)}[/dim]")
        if got.detail:
            console.print(f"  {escape(got.detail)}")

    merged = sum(1 for d in decisions if d.action == "merged")
    parked = sum(1 for d in decisions if d.action == "parked")
    left = sum(1 for d in decisions if d.action == "left")
    console.print(
        f"\n[bold]{len(decisions)} pair(s)[/bold] — {merged} merged, {parked} ruled out, "
        f"{left} left for a person"
    )
    if dry_run:
        console.print("[yellow]--dry-run: nothing was written[/yellow]")
    elif merged:
        console.print(
            "[dim]merges are recorded in the surviving rows' notes, naming the model "
            "and its reason. `tracker duplicates parked` lists what was ruled out.[/dim]"
        )
    if left and not merge_them:
        console.print("[dim]pass --merge to fold the pairs it ruled are one campus.[/dim]")


def _dupe_prompt(a, b, pair) -> str:
    """Put one pair to the operator, with both rows on screen.

    The best rung and the reason it is offered first: a person who knows the market
    settles in a second what a model has to reason its way to. Returns "same",
    "different", "skip", or "" to hand it to the model.
    """
    from tracker.dupresolve import km_apart

    console.print(f"\n[bold]{escape(pair.locality)}, {pair.state}[/bold] — {escape(pair.why)}")
    for tag, project in (("A", a), ("B", b)):
        console.print(
            f"  [{tag}] #{project.id} {escape(project.company)} — {escape(project.name)}"
            f"  [dim]{escape(_location(project))}, {_fmt_mw(project.mw_planned)} MW, "
            f"{len(project.sources)} citation(s)[/dim]"
        )
    distance = km_apart(a, b)
    if distance is not None:
        console.print(f"  [dim]{distance:.1f} km apart[/dim]")
    answer = typer.prompt(
        "  [s]ame campus / [d]ifferent / [Enter] let the model decide / [k]skip", default=""
    )
    return {"s": "same", "d": "different", "k": "skip"}.get(answer.strip().lower()[:1], "")


@duplicates_app.command("parked")
def duplicates_parked() -> None:
    """Every pair ruled out so far, who ruled it out, and why.

    `decided_by` is the column to read: "operator" and "model (0.82)" are
    different claims about how much reading happened before the question was
    closed. The same listing is available as `tracker duplicates --parked`.
    """
    from tracker import pairs as pairs_mod

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        _print_parked(pairs_mod.listing(session))


@app.command()
def merge(
    dupe_ids: Annotated[list[int], typer.Argument(help="Project ids to fold in.")],
    into: Annotated[int, typer.Option("--into", help="The project id that survives.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report without writing.")] = False,
) -> None:
    """Fold duplicate rows into one project.

    Citations, milestones and obstacles move onto the surviving row, the others
    are deleted, and every field is then recomputed from the combined set of
    claims. Nothing is hand-copied — which id you keep does not decide the values,
    only which row number survives.

    Irreversible. Run `tracker duplicates` first, and `--dry-run` to see the shape
    of it before committing.
    """
    from tracker.merge import MergeError
    from tracker.merge import merge_projects as do_merge

    engine, _ = init_db(_db_path())
    try:
        release_lock = acquire_write_lock(_db_path(), command="merge")
    except AlreadyRunning as exc:
        _fail(str(exc))
        raise
    atexit.register(release_lock)

    try:
        with _explain_db_locks(), session_scope(engine, commit=not dry_run) as session:
            before = {
                p.id: f"{p.company} — {p.name}"
                for p in session.scalars(
                    select(Project).where(Project.id.in_([into, *dupe_ids]))
                ).all()
            }
            result = do_merge(session, into, dupe_ids)
            survivor = session.get(Project, into)
            summary = (
                f"{survivor.company} — {survivor.name} "
                f"({_location(survivor)}), {_fmt_mw(survivor.mw_planned)} MW, "
                f"confidence {survivor.confidence}"
            )
    except MergeError as exc:
        _fail(str(exc))
        raise

    for project_id, label in before.items():
        marker = "[green]keep[/green]" if project_id == into else "[red]fold[/red]"
        console.print(f"  {marker} #{project_id:<5} {label[:78]}")
    console.print()
    _print_report_rows(result.as_rows(), title="merge (dry run)" if dry_run else "merge")
    console.print(f"\nsurvivor: [bold]#{into}[/bold] {summary}")
    if result.conflicts:
        console.print(
            f"[yellow]sources disagree on:[/yellow] {', '.join(result.conflicts)} "
            "[dim]— both values are kept in their own citations and disclosed in notes[/dim]"
        )
    if dry_run:
        console.print("[yellow]dry run[/yellow] — nothing written")
