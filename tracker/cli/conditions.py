"""`tracker conditions`: rules that apply to a place, not one campus.

`read` and `match` write, so they run on the host, outside the nightly loop's hours;
the nightly loop runs both itself. Bare `tracker conditions` and `show` only read.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

import typer
from rich import box
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from tracker.cli._shared import (
    _db_path,
    _explain_db_locks,
    _fail,
    _model,
    _print_ceiling,
    _read_engine,
    _use_llm,
    _workers,
    _writable,
    conditions_app,
    console,
    emit,
    json_mode,
)
from tracker.config import cache_dir as article_cache
from tracker.db import session_scope

_STATUS_STYLE = {
    "proposed": "yellow",
    "pledged": "yellow",
    "passed": "red",
    "in_force": "bold red",
    "blocked": "cyan",
    "lifted": "green",
    "rejected": "green",
}


def _status(status: str) -> str:
    style = _STATUS_STYLE.get(status, "white")
    return f"[{style}]{status.replace('_', ' ')}[/{style}]"


@conditions_app.callback(invoke_without_command=True)
def conditions_list(
    ctx: typer.Context,
    state: Annotated[
        str | None, typer.Option("--state", help="Only this state (two letters).")
    ] = None,
    cleared: Annotated[
        bool, typer.Option("--cleared", help="Also rules lifted or voted down.")
    ] = False,
) -> None:
    """The rules held, newest report first, with how many projects each reaches.

    A rule for the whole country or a whole-industry input reaches no project and is
    listed under "market". Reads only.
    """
    if ctx.invoked_subcommand is not None:
        return
    from tracker import conditions

    with session_scope(_read_engine(), commit=False) as session:
        rows = conditions.listing(session, state=state, cleared=cleared)
        counts = conditions.reach_counts(session)
        if json_mode():
            emit([conditions.as_json(c, reach=counts.get(c.id, 0)) for c in rows])
            return
        if not rows:
            console.print("no rules held" + (f" for {state.upper()}" if state else ""))
            return
        table = Table(title="conditions", box=box.SIMPLE_HEAVY, title_style="bold")
        for column in ("#", "where", "rule", "need", "status", "projects", "reported"):
            table.add_column(column, justify="right" if column in ("#", "projects") else "left")
        for c in rows:
            reach = "market" if c.scope not in conditions.PLACED else str(counts.get(c.id, 0))
            table.add_row(
                str(c.id),
                escape(conditions.where_words(c)),
                escape(c.title),
                c.channel.replace("_", " "),
                _status(c.status),
                reach,
                c.last_reported.isoformat() if c.last_reported else "—",
            )
        console.print(table)


@conditions_app.command("read")
def conditions_read(
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Pages read by the news step since this date (YYYY-MM-DD). "
            "Default: the last two days.",
            show_default=False,
        ),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(
            "--until",
            help="Only pages read before this date (YYYY-MM-DD): a past week, a day at a time.",
            show_default=False,
        ),
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="Read this one saved page.", show_default=False)
    ] = None,
    behind: Annotated[
        str | None,
        typer.Option(
            "--behind",
            help="Read again the pages behind every rule now holding this status "
            "(e.g. pledged), whenever read: for a prompt that changed what it means.",
            show_default=False,
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Pages to read. One call each.")] = 150,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Read at full cost and write nothing.")
    ] = False,
    again: Annotated[
        bool, typer.Option("--again", help="Also pages already read under this prompt.")
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
    """Find rules that apply to a place in the pages the news step saved.

    **Which pages.** Every page the news step read is saved, including the ones it
    found no campus in. A free word test keeps those that may state a rule —
    moratorium, ban, pause, ordinance, ballot, governor and the like, a jurisdiction,
    and data centers — and each is put to a model once per prompt.

    **What is kept.** A rule is kept only when its sentences are the article's own,
    name the place it is filed under (or the headline does), word its status, and
    state any size threshold. Two readings of one rule are one condition; the newest
    article sets its status. Then `tracker conditions match` (run at the end of this
    command too) works out which projects each reaches.
    """
    _use_llm(llm_provider)
    from tracker import conditions
    from tracker.models import Condition

    path = _db_path()
    if not path.is_file():
        _fail(f"database not found: {path}\nRun `tracker init` first.")
    try:
        start = dt.datetime.fromisoformat(since) if since else conditions.since_default()
        end = dt.datetime.fromisoformat(until) if until else None
    except ValueError:
        _fail("--since and --until must be dates, YYYY-MM-DD")
        return
    extractor = _model("judgement")
    engine = _read_engine() if dry_run else _writable("conditions read")

    def announce(result) -> None:
        if json_mode():
            return
        if result.error:
            console.print(f"  [yellow]failed[/yellow] {escape(result.title[:90])}")
            console.print(f"      [dim]{escape(result.error[:170])}[/dim]")
            return
        if not result.kept and not result.refused:
            return
        console.print(f"  [bold]{escape(result.title[:100])}[/bold]")
        for cid, title, status, new in result.kept:
            mark = "new" if new else f"#{cid}"
            console.print(f"      [green]{mark:>5}[/green] {_status(status)} {escape(title[:110])}")
        for title, why in result.refused:
            console.print(
                f"      [yellow]refused[/yellow] {escape(title[:80])} [dim]— {escape(why)}[/dim]"
            )

    with _explain_db_locks(), session_scope(engine, commit=not dry_run) as session:
        items, looked = conditions.pages(
            session,
            since=start,
            until=end,
            cache_dir=article_cache("articles"),
            again=again or bool(behind),
            url=url,
            behind=behind,
        )
        if not items:
            console.print(
                f"no saved page since {start:%Y-%m-%d} may state a rule ({looked} looked at)"
                if not url
                else "that page is not saved or not settled"
            )
            return
        if not json_mode():
            console.print(
                f"[bold]{min(limit, len(items))}[/bold] of {len(items)} page(s) may state a "
                f"rule, of {looked} read since {start:%Y-%m-%d} — one model call each."
                + (" [dim]Dry run: nothing is written.[/dim]" if dry_run else "")
                + "\n"
            )
        run = conditions.read(
            session,
            items,
            extractor=extractor,
            limit=limit,
            workers=_workers("judgement"),
            commit=not dry_run,
            on_each=announce,
        )
        folded = None
        if not dry_run:
            # Readings of one rule filed apart are merged before matching, so a
            # project is not shown one rule four times (`conditions.fold`).
            touched = {cid for r in run.results for cid, *_ in r.kept}
            keys = session.scalars(
                select(Condition.match_key).where(Condition.id.in_(sorted(touched)))
            ).all()
            folded = conditions.fold(session, extractor, keys=keys)
        matched = None if dry_run else conditions.rebuild(session)
    results = run.results
    kept = sum(len(r.kept) for r in results)
    new = sum(1 for r in results for *_, n in r.kept if n)
    refused = sum(len(r.refused) for r in results)
    failed = sum(1 for r in results if r.error)
    if json_mode():
        emit(
            {
                "read": len(results),
                "kept": kept,
                "new": new,
                "refused": refused,
                "failed": failed,
                "exposures": None
                if matched is None
                else {"added": matched.added, "kept": matched.kept, "dropped": matched.dropped},
                "pages": [
                    {
                        "url": r.url,
                        "kept": [
                            {"id": cid, "title": t, "status": s, "new": n}
                            for cid, t, s, n in r.kept
                        ],
                        "refused": [{"title": t, "why": w} for t, w in r.refused],
                        "error": r.error or None,
                    }
                    for r in results
                ],
            }
        )
    else:
        console.print(
            f"\n{len(results)} page(s) read: {kept} rule(s) kept ({new} new), "
            f"{refused} refused, {failed} failed."
        )
        if folded is not None and folded.merged:
            console.print(
                f"{folded.merged} reading(s) of {folded.groups} rule(s) filed apart, merged."
            )
        if matched is not None:
            console.print(
                f"projects affected: {matched.added} newly, {matched.kept} still, "
                f"{matched.dropped} no longer."
            )
    _print_ceiling(run)


@conditions_app.command("fold")
def conditions_fold(
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Merge readings of one rule that were filed as separate rules, then match again.

    Every place-and-need group holding more than one rule is put to a model, without
    reasoning, which names the lines that are the same bill, order, measure or
    person's pledge; each such group is merged into its oldest rule, its articles
    kept. `conditions read` does this itself for the groups it touched; this runs it
    over all of them, and settles every rule's status again first (a candidate's
    promise read as a pledge becomes a proposal).
    """
    _use_llm(llm_provider)
    from tracker import conditions

    extractor = _model("judgement")
    engine = _writable("conditions fold")
    with _explain_db_locks(), session_scope(engine) as session:
        changed = conditions.resettle(session)
        session.commit()
        folded = conditions.fold(session, extractor)
        matched = conditions.rebuild(session)
    if json_mode():
        emit(
            {
                "resettled": changed,
                "asked": folded.asked,
                "groups": folded.groups,
                "merged": folded.merged,
                "exposures": {
                    "added": matched.added,
                    "kept": matched.kept,
                    "dropped": matched.dropped,
                },
            }
        )
        return
    console.print(
        f"{changed} status(es) settled again; {folded.asked} place-and-need group(s) "
        f"asked, {folded.merged} reading(s) of {folded.groups} rule(s) merged."
    )
    console.print(
        f"projects affected: {matched.added} newly, {matched.kept} still, "
        f"{matched.dropped} no longer."
    )
    if folded.ceiling:
        console.print(f"[yellow]stopped early[/yellow] [dim]— {escape(folded.ceiling)}[/dim]")


@conditions_app.command("match")
def conditions_match(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Say what would change and write nothing.")
    ] = False,
) -> None:
    """Work out again which projects each rule reaches. Code only, no model, free.

    By place (state, county, town, or grid region), the stages the rule covers, and
    its size threshold. Run every round by the nightly loop, so a project placed,
    merged or moved to a new stage during the night is matched by morning.
    """
    from tracker import conditions

    engine = _read_engine() if dry_run else _writable("conditions match")
    with _explain_db_locks(), session_scope(engine, commit=not dry_run) as session:
        matched = conditions.rebuild(session, write=not dry_run)
    if json_mode():
        emit({"added": matched.added, "kept": matched.kept, "dropped": matched.dropped})
        return
    console.print(
        f"projects affected: {matched.added} newly, {matched.kept} still, "
        f"{matched.dropped} no longer, by {len(matched.reach)} rule(s)."
        + (" [dim]Dry run: nothing written.[/dim]" if dry_run else "")
    )


@conditions_app.command("show")
def conditions_show(
    condition_id: Annotated[int, typer.Argument(help="The rule's number.")],
) -> None:
    """One rule: where it stands, each article and its sentences, the projects it reaches."""
    from tracker import conditions
    from tracker.models import Condition, Exposure, Project

    with session_scope(_read_engine(), commit=False) as session:
        c = session.get(Condition, condition_id)
        if c is None:
            _fail(f"no rule #{condition_id}")
            return
        reached = list(
            session.scalars(
                select(Exposure).where(Exposure.condition_id == c.id).order_by(Exposure.project_id)
            )
        )
        names = {
            pid: f"{company} — {name}"
            for pid, company, name in session.execute(
                select(Project.id, Project.company, Project.name).where(
                    Project.id.in_([r.project_id for r in reached])
                )
            )
        }
        sources = sorted(c.sources, key=lambda s: s.published_at or s.read_at, reverse=True)
        if json_mode():
            emit(
                conditions.as_json(c, reach=len(reached))
                | {
                    "sources": [
                        {
                            "url": s.url,
                            "title": s.title,
                            "published_at": s.published_at.isoformat() if s.published_at else None,
                            "status": s.status,
                            "summary": s.summary,
                            "quotes": conditions.quotes_of(s),
                        }
                        for s in sources
                    ],
                    "exposures": [
                        {"project_id": r.project_id, "fit": r.fit, "why": r.why} for r in reached
                    ],
                }
            )
            return
        console.print(f"[bold]#{c.id} {escape(c.title)}[/bold]  {_status(c.status)}")
        console.print(
            f"[dim]{escape(conditions.where_words(c))} · {c.kind.replace('_', ' ')} · "
            f"{c.channel.replace('_', ' ')} · applies to {c.applies_to} projects"
            + (f" of {c.min_mw:g} MW and up" if c.min_mw else "")
            + "[/dim]"
        )
        console.print(escape(c.summary))
        for s in sources:
            when = (s.published_at or s.read_at).date().isoformat()
            console.print(
                f"\n  {when} {_status(s.status)} [link={s.url}]{escape(s.title or s.url)}[/link]"
            )
            for what, sentence in conditions.quotes_of(s).items():
                console.print(f"    [dim]{what}:[/dim] “{escape(sentence[:240])}”")
        if c.scope not in conditions.PLACED:
            console.print("\n[dim]market context: reaches no project by itself[/dim]")
            return
        console.print(f"\n[bold]{len(reached)}[/bold] project(s) affected")
        for r in reached:
            console.print(
                f"  #{r.project_id} {escape(names.get(r.project_id, ''))} "
                f"[dim]{r.fit} · {escape(r.why)}[/dim]"
            )


@conditions_app.command("drop")
def conditions_drop(
    condition_id: Annotated[int, typer.Argument(help="The rule's number.")],
) -> None:
    """Delete a rule read wrongly, with its articles and the projects it reached.

    Its pages stay marked as read, so the same reading is not paid for again; a new
    prompt (`conditions.STAMP`) reads them afresh.
    """
    from tracker import conditions

    engine = _writable("conditions drop")
    with _explain_db_locks(), session_scope(engine) as session:
        gone = conditions.drop(session, condition_id)
    if gone is None:
        _fail(f"no rule #{condition_id}")
    console.print(f"dropped #{condition_id} {escape(gone.title)}")
