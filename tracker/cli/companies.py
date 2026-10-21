"""Companies and their roles: the registry, and whose role on a campus has ended.

`tracker company` reads and edits the company registry (`tracker/companies.py`); an
alias re-keys what it touches the way a deploy does. `tracker party` lists a campus's
parties and records a role that has ended (`parties.end_role`). Every edit records
who made it.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape

from tracker.cli._shared import (
    NA,
    _explain_db_locks,
    _fail,
    _read_engine,
    _writable,
    company_app,
    console,
    emit,
    json_mode,
    party_app,
)
from tracker.db import session_scope


@company_app.callback(invoke_without_command=True)
def company_cmd(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", help="Companies to list.")] = 40,
    search: Annotated[
        str | None, typer.Option("--search", help="Only names or spellings containing this.")
    ] = None,
) -> None:
    """Every company, most campuses first, with its kind, parent and spellings. Free.

    A company is one row per identity (migration 0043): the key every campus and
    party is filed under, the one name the console prints and who chose it, the kind
    of company the roster or the EDGAR list says it is, its parent, and every
    spelling that folds to it — the code's, the roster's, and any a person added.
    """
    if ctx.invoked_subcommand is not None:
        return
    from tracker import companies

    with session_scope(_read_engine(), commit=False) as session:
        entries = companies.listing(session)
    if search:
        needle = search.casefold()
        entries = [
            e
            for e in entries
            if needle in e.name.casefold()
            or needle in e.key
            or any(needle in s for s in e.spellings)
        ]
    if json_mode():
        emit({"companies": [e.as_json() for e in entries]})
        return
    if not entries:
        console.print("[dim]no companies — run `tracker init` to build the registry[/dim]")
        return
    console.print(f"[bold]{len(entries)}[/bold] compan{'y' if len(entries) == 1 else 'ies'}\n")
    for entry in entries[:limit]:
        extra = []
        if entry.kind:
            extra.append(entry.kind)
        if entry.parent:
            extra.append(f"part of {escape(entry.parent)}")
        if entry.name_by not in ("rows", "roster"):
            extra.append(f"named by {escape(entry.name_by)}")
        console.print(
            f"  [bold]{escape(entry.name)}[/bold] [dim]{escape(entry.key)}[/dim]  "
            f"{entry.campuses} campus(es), {entry.roles} as a party"
            + (f"  [dim]({'; '.join(extra)})[/dim]" if extra else "")
        )
        if entry.spellings:
            shown = ", ".join(entry.spellings[:6]) + (" …" if len(entry.spellings) > 6 else "")
            console.print(f"      [dim]also: {escape(shown)}[/dim]")
    if len(entries) > limit:
        console.print(f"\n[dim]{len(entries) - limit} more; raise --limit or --search.[/dim]")


@company_app.command("show")
def show(company: Annotated[str, typer.Argument(help="A name, key or spelling.")]) -> None:
    """One company: its name, kind, parent, subsidiaries, spellings and campuses."""
    from sqlalchemy import select

    from tracker import companies
    from tracker.models import CompanyAlias, Project

    with session_scope(_read_engine(), commit=False) as session:
        row = companies.find(session, company)
        if row is None:
            _fail(f"no company {company!r}; `tracker company --search` lists them")
            return
        family = companies.family(session, row.key)
        aliases = session.scalars(
            select(CompanyAlias).where(CompanyAlias.company_id == row.id)
        ).all()
        campuses = session.scalars(
            select(Project).where(Project.company_id == row.id).order_by(Project.id)
        ).all()
        if json_mode():
            emit(
                {
                    "key": row.key,
                    "name": row.name,
                    "name_by": row.name_by,
                    **family,
                    "aliases": [{"spelling": a.spelling, "by": a.decided_by} for a in aliases],
                    "campuses": [{"id": p.id, "name": p.name} for p in campuses],
                }
            )
            return
        console.print(f"[bold]{escape(row.name)}[/bold]  [dim]{escape(row.key)}[/dim]")
        console.print(f"  name chosen by: {escape(row.name_by)}")
        console.print(f"  kind:           {escape(row.kind or NA)}")
        parent = family["parent"]["name"] if family["parent"] else NA
        console.print(f"  part of:        {escape(parent)}")
        if family["subsidiaries"]:
            names = ", ".join(s["name"] for s in family["subsidiaries"])
            console.print(f"  subsidiaries:   {escape(names)}")
        for alias in aliases:
            console.print(
                f"  spelling:       {escape(alias.spelling)} [dim]({alias.decided_by})[/dim]"
            )
        console.print(f"\n  {len(campuses)} campus(es) as operator")
        for project in campuses[:20]:
            console.print(f"    #{project.id} {escape(project.name)}")


def _changed(report) -> None:
    console.print(
        f"[green]{escape(report.spelling)}[/green] now reads as "
        f"[bold]{escape(report.new_key)}[/bold] [dim](was {escape(report.old_key)})[/dim]"
    )
    console.print(
        f"  {report.keys_remembered} campus key(s) remembered, {report.parties_rebuilt} "
        f"campus(es)' parties rebuilt, {report.watches_moved} watch(es) moved"
        + (
            f"; {escape(report.removed_company)} no longer names a company"
            if report.removed_company
            else ""
        )
    )


@company_app.command("alias")
def alias(
    spelling: Annotated[str, typer.Argument(help="The spelling, as sources write it.")],
    company: Annotated[str, typer.Argument(help="The company it is: a name, key or spelling.")],
    by: Annotated[
        str, typer.Option("--by", help="Who is deciding, as the record names them.")
    ] = "",
) -> None:
    """Fold a spelling into a company: from now on it reads as that company everywhere.

    The campuses, parties, watches and Capex rows filed under the old spelling move
    with it, by the same re-keying a deploy runs. `tracker company unalias` takes it
    back.

    \b
      tracker company alias "Compute North" "NextEra" --by nathan
    """
    from tracker import companies

    if not by.strip():
        _fail("--by is required: an alias records who added it")
        return
    with _explain_db_locks(), session_scope(_writable("company alias")) as session:
        target = companies.find(session, company)
        if target is None:
            _fail(f"no company {company!r}")
            return
        try:
            report = companies.add_alias(session, spelling, target, by=by)
        except companies.CompanyError as exc:
            _fail(str(exc))
            return
    _changed(report)


@company_app.command("unalias")
def unalias(
    spelling: Annotated[str, typer.Argument(help="A spelling a person folded.")],
) -> None:
    """Take back a spelling a person folded. The code's own spellings stay."""
    from tracker import companies

    with _explain_db_locks(), session_scope(_writable("company unalias")) as session:
        try:
            report = companies.remove_alias(session, spelling)
        except companies.CompanyError as exc:
            _fail(str(exc))
            return
    _changed(report)


@company_app.command("rename")
def rename(
    company: Annotated[str, typer.Argument(help="A name, key or spelling.")],
    name: Annotated[str, typer.Argument(help="The name to print everywhere.")],
    by: Annotated[str, typer.Option("--by", help="Who is deciding.")] = "",
) -> None:
    """Print a company under a name a person chose. It stands until they change it."""
    from tracker import companies

    with _explain_db_locks(), session_scope(_writable("company rename")) as session:
        row = companies.find(session, company)
        if row is None:
            _fail(f"no company {company!r}")
            return
        try:
            companies.rename(session, row, name, by=by)
        except companies.CompanyError as exc:
            _fail(str(exc))
            return
        console.print(f"[green]renamed[/green] {escape(row.key)} → {escape(row.name)}")


@company_app.command("parent")
def parent(
    company: Annotated[str, typer.Argument(help="The subsidiary: a name, key or spelling.")],
    parent_company: Annotated[
        str | None, typer.Argument(help="Its parent. Omit with --clear to remove one.")
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="It has no parent.")] = False,
    by: Annotated[str, typer.Option("--by", help="Who is deciding.")] = "",
) -> None:
    """Record whose subsidiary a company is. Its campuses stay its own.

    The parent's page lists its subsidiaries and each subsidiary's page names its
    parent; no figure moves — a company's totals are its own campuses, as on Capex.
    """
    from tracker import companies

    if not clear and not parent_company:
        _fail("name the parent, or pass --clear")
        return
    if not by.strip():
        _fail("--by is required")
        return
    with _explain_db_locks(), session_scope(_writable("company parent")) as session:
        row = companies.find(session, company)
        above = None if clear else companies.find(session, parent_company or "")
        if row is None or (not clear and above is None):
            _fail(f"no company {company if row is None else parent_company!r}")
            return
        try:
            companies.set_parent(session, row, above, by=by)
        except companies.CompanyError as exc:
            _fail(str(exc))
            return
        console.print(
            f"[green]{escape(row.name)}[/green] "
            + (f"is part of {escape(above.name)}" if above else "has no parent")
        )


# --- parties ---------------------------------------------------------------------------


@party_app.command("list")
def party_list(
    project_id: Annotated[int, typer.Argument(help="The campus.")],
) -> None:
    """Who plays which role on one campus, the former ones marked. Free, read-only."""
    from tracker.models import Project

    with session_scope(_read_engine(), commit=False) as session:
        project = session.get(Project, project_id)
        if project is None:
            _fail(f"no project #{project_id}")
            return
        rows = sorted(project.parties, key=lambda p: (p.role, p.party_key))
        if json_mode():
            emit(
                {
                    "project_id": project.id,
                    "parties": [
                        {
                            "name": p.name,
                            "key": p.party_key,
                            "role": p.role,
                            "confirmed": p.unconfirmed is None,
                            "ended_on": p.ended_on.isoformat() if p.ended_on else None,
                            "ended_reason": p.ended_reason,
                        }
                        for p in rows
                    ],
                }
            )
            return
        console.print(f"[bold]#{project.id} {escape(project.name)}[/bold]")
        for p in rows:
            line = f"  {p.role:<11} {escape(p.name)} [dim]{escape(p.party_key)}[/dim]"
            if p.unconfirmed is not None:
                line += " [dim]待确认[/dim]"
            if p.ended_reason:
                when = f" {p.ended_on.isoformat()}" if p.ended_on else ""
                line += f"  [yellow]ended{when}[/yellow] [dim]— {escape(p.ended_reason)}[/dim]"
            console.print(line)


@party_app.command("end")
def end(
    project_id: Annotated[int, typer.Argument(help="The campus.")],
    party: Annotated[str, typer.Argument(help="The party, as named or by key.")],
    role: Annotated[str, typer.Option("--role", help="The role that ended.")] = "customer",
    on: Annotated[
        str | None, typer.Option("--on", help="When it ended: YYYY, YYYY-MM or YYYY-MM-DD.")
    ] = None,
    reason: Annotated[str, typer.Option("--reason", help="Why, in one sentence.")] = "",
    by: Annotated[str, typer.Option("--by", help="Who is deciding.")] = "",
) -> None:
    """Record that a party's role on a campus has ended: a former tenant, a sold site.

    The party stays on the page, marked former, and stays evidence of which campus
    this is; it stops being a current buyer on Capex and the company pages.

    \b
      tracker party end 6 Cyxtera --on 2023 --reason "lease rejected" --by nathan
    """
    import datetime as dt

    from tracker import parties
    from tracker.models import Project

    ended_on = None
    if on:
        parts = on.strip().split("-")
        try:
            ended_on = dt.date(
                int(parts[0]),
                int(parts[1]) if len(parts) > 1 else 1,
                int(parts[2]) if len(parts) > 2 else 1,
            )
        except (ValueError, IndexError):
            _fail("--on takes YYYY, YYYY-MM or YYYY-MM-DD")
            return
    with _explain_db_locks(), session_scope(_writable("party end")) as session:
        project = session.get(Project, project_id)
        if project is None:
            _fail(f"no project #{project_id}")
            return
        try:
            row = parties.end_role(
                session, project, party, role, reason=reason, by=by, ended_on=ended_on
            )
        except parties.TenureError as exc:
            _fail(str(exc))
            return
        console.print(
            f"[green]ended[/green] #{project_id} {escape(row.party_key)} as {row.role}"
            + (f" in {ended_on.isoformat()}" if ended_on else "")
        )


@party_app.command("resume")
def resume(
    project_id: Annotated[int, typer.Argument(help="The campus.")],
    party: Annotated[str, typer.Argument(help="The party, as named or by key.")],
    role: Annotated[str, typer.Option("--role", help="The role.")] = "customer",
) -> None:
    """Take back an ended role: the party is current again."""
    from tracker import parties
    from tracker.models import Project

    with _explain_db_locks(), session_scope(_writable("party resume")) as session:
        project = session.get(Project, project_id)
        if project is None:
            _fail(f"no project #{project_id}")
            return
        if parties.resume_role(session, project, party, role):
            console.print(f"[green]resumed[/green] #{project_id} {escape(party)} as {role}")
        else:
            console.print(f"[dim]#{project_id} {escape(party)} had no ended {role} role[/dim]")
