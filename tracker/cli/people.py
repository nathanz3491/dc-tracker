"""Who reads the console, what they asked to be told about, and how they are told.

Accounts are made here and nowhere else — a browser can only create one by
redeeming an invite minted here. A watchlist belongs to an account, and `digest`
and `notify` are the two ways its movements reach a person.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich import box
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from tracker.accounts import DEFAULT_INVITE_DAYS
from tracker.cli._shared import (
    TABLE_BOX,
    _db_path,
    _explain_db_locks,
    _fail,
    _read_engine,
    app,
    console,
    emit,
    err,
    json_mode,
    notify_app,
    users_app,
    watch_app,
)
from tracker.config import get_settings
from tracker.db import MigrationError, open_db, session_scope
from tracker.models import Project


def _print_notify_rows(rows: list[tuple[str, str, str]], *, title: str) -> None:
    """One line per account: who, how much, what happened to it.

    Its own renderer rather than `_print_report_rows`, which counts outcomes of a
    single run in two columns. This is a roster — every account appears, including
    the ones that got nothing, because "who was skipped and why" is most of what
    an operator needs from a send.
    """
    table = Table(title=title, box=box.SIMPLE_HEAVY, title_style="bold")
    table.add_column("account")
    table.add_column("updates", justify="right")
    table.add_column("outcome")
    for account, count, outcome in rows:
        style = "green" if outcome.startswith("sent") else "dim"
        table.add_row(escape(account), count, f"[{style}]{escape(outcome)}[/{style}]")
    console.print(table)


# --- Updates -----------------------------------------------------------------


#: The flag that names whose watchlist a command is about. One definition, because
#: `watch`, `watch add`, `watch rm` and `digest` all take it and a help string that
#: drifted between them would be four different explanations of one thing.
_USER_OPTION = typer.Option(
    "--user", help="Whose watchlist, by email. Every account's, if omitted."
)


def _account_id(session, email: str | None, *, for_write: bool) -> int | None:
    """Resolve `--user` to an account id. None means "every account".

    `for_write=True` refuses None. Reading across everybody is what a terminal on
    the host wants — it is looking at the database rather than at one person — but
    *writing* without naming an owner has no honest meaning now that there is no
    shared list, and picking one would put an entry on somebody's page that they
    did not ask for.
    """
    from tracker.accounts import AccountError, count, require

    if email:
        try:
            return require(session, email).id
        except AccountError as exc:
            _fail(str(exc))
    if not for_write:
        return None
    if count(session) == 0:
        _fail(
            "a watchlist belongs to an account and there are none yet.\n"
            "Make one with `tracker users add you@example.com`."
        )
    _fail("say whose list this is with --user. `tracker users` lists them.")
    return None  # unreachable; _fail raises


@watch_app.callback(invoke_without_command=True)
def watch(
    ctx: typer.Context,
    user: Annotated[str | None, _USER_OPTION] = None,
) -> None:
    """Companies and projects the digest is about. Read-only; `add` and `rm` edit.

    A watch is a company ("xAI") or one project of one company
    ("xAI | Colossus"), and it covers what that company is building *and* what
    others are building for it — `tracker.watchlist` has the reasoning, and the
    listing says which way each project matched.

    **Every entry belongs to an account, and this reads all of them.** A terminal on
    the host is looking at the database, so the owner is a column rather than a
    filter; `--user` narrows to one person's list, which is what the console shows
    them. With no accounts at all there are no entries, and `tracker digest` then
    reads the whole database — a legitimate state, not a warning.
    """
    if ctx.invoked_subcommand is not None:
        return

    from tracker import watchlist

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        account_id = _account_id(session, user, for_write=False)
        entities = watchlist.watched(session, account_id=account_id)

        if json_mode():
            emit(
                {
                    "user": user,
                    "watching": [
                        {**e.as_json(), "owner": e.owner} if account_id is None else e.as_json()
                        for e in entities
                    ],
                }
            )
            return

        if not entities:
            whose = f"{user} is watching nothing" if user else "nothing is being watched"
            console.print(
                f"[dim]{whose}, so the digest reads the whole database.\n"
                'Add one with `tracker watch add "xAI" --user you@example.com`.[/dim]'
            )
            return

        table = Table(header_style="bold", title_justify="left", box=TABLE_BOX)
        # Only when it means something. One account's list has one owner, and a
        # column repeating the address on every row is noise.
        if account_id is None:
            table.add_column("owner")
        table.add_column("entry")
        table.add_column("projects", justify="right")
        table.add_column("matched")
        table.add_column("note")
        for entity in entities:
            vias = sorted(set(entity.matches.values()))
            row = [
                entity.entry,
                str(len(entity.matches)),
                ", ".join(v.replace("_", " ") for v in vias) or "[yellow]nothing yet[/yellow]",
                entity.note or "",
            ]
            table.add_row(*([entity.owner or "?", *row] if account_id is None else row))
        console.print(table)


def _watch_engine():
    """A writable engine for a watchlist edit, without the single-writer lock.

    Deliberately not `_writable()`. That takes the lock file, which is held for the
    whole of a crawl and is what stops two ingests colliding — a rule about derived
    data, which a `watch` row is not: nothing reads it but the digest, and no
    ingest touches it. Blocking somebody from changing which companies they are
    told about because tonight's crawl is still running would be a worse answer
    than letting the two writes interleave, which SQLite serialises anyway.

    `Handler._watch` reached the same conclusion for the console, and this is the
    same operation. SQLite's `busy_timeout` covers the contention; a genuine
    collision surfaces as a message rather than a traceback.
    """

    try:
        return open_db(_db_path(), readonly=False)
    except (FileNotFoundError, MigrationError) as exc:
        _fail(str(exc))
        raise  # unreachable; _fail always raises


@watch_app.command("add")
def watch_add(
    entry: Annotated[
        str,
        typer.Argument(help='A company ("xAI"), or a company and a project ("xAI | Colossus").'),
    ],
    note: Annotated[
        str | None, typer.Option("--note", help="Why, in a few words. Shown on the digest.")
    ] = None,
    user: Annotated[str | None, _USER_OPTION] = None,
) -> None:
    """Start watching a company, or one of its projects, on one account's list.

    Idempotent on the normalized company key *per account*, so adding "Microsoft"
    when "Microsoft Corporation" is already watched updates the note instead of
    creating a second row for the same company — and two people watching Microsoft
    are two rows rather than a collision.

    `--user` is required here, unlike on the listing. Reading across everybody is
    what a terminal wants; writing without naming an owner would put an entry on
    somebody's page that they did not ask for.
    """
    from tracker import watchlist

    engine = _watch_engine()
    with _explain_db_locks(), session_scope(engine) as session:
        account_id = _account_id(session, user, for_write=True)
        try:
            row, created = watchlist.add(session, entry, account_id=account_id, note=note)
        except watchlist.WatchError as exc:
            _fail(str(exc))
            raise
        entry_text = row.entry
        projects = session.scalars(select(Project)).all()
        matched = len(watchlist.resolve([row], projects)[0].matches)

    if json_mode():
        emit({"entry": entry_text, "user": user, "created": created, "projects": matched})
        return
    verb = "watching" if created else "already watching"
    console.print(
        f"[green]{verb}[/green] {escape(entry_text)} for {escape(user or '')} "
        f"— {matched} project(s) match today"
    )
    if not matched:
        console.print(
            "[dim]nothing matches yet. A watch set before the project is tracked is "
            "fine — it starts reporting as soon as one appears.[/dim]"
        )


@watch_app.command("rm")
def watch_rm(
    entry: Annotated[str, typer.Argument(help="The entry to stop watching.")],
    user: Annotated[str | None, _USER_OPTION] = None,
) -> None:
    """Stop watching a company or project, on one account's list.

    Scoped to `--user` for the same reason `add` is: one person's list is not
    another's to edit, and a `rm` that swept every account would be a way to delete
    somebody else's work by typing a company name.
    """
    from tracker import watchlist

    engine = _watch_engine()
    with _explain_db_locks(), session_scope(engine) as session:
        account_id = _account_id(session, user, for_write=True)
        try:
            dropped = watchlist.remove(session, entry, account_id=account_id)
        except watchlist.WatchError as exc:
            _fail(str(exc))
            raise

    if json_mode():
        emit({"entry": entry, "user": user, "removed": dropped})
        return
    if dropped:
        console.print(f"[green]stopped watching[/green] {escape(entry)}")
    else:
        console.print(
            f"[yellow]{escape(entry)} was not on {escape(user or 'that')}'s list[/yellow]"
        )


@watch_app.command("all")
def watch_all_cmd(
    on: Annotated[
        bool,
        typer.Option("--on/--off", help="Read the whole database, or only this list."),
    ] = True,
    user: Annotated[str | None, _USER_OPTION] = None,
) -> None:
    """Watch every project, or go back to watching only what is on the list.

    **Off is the default and an empty list means nothing.** It used to mean
    *everything*: an account that had named nothing was shown all 456 projects, so
    "watching" depended on a row count nobody could see and two people who had
    asked for nothing saw identical pages. Wanting all of it is a legitimate thing
    to want, so it became a thing somebody turns on. Migration 0022 has the
    argument.

    The console's watchlist panel has the same toggle; this is the terminal's.
    """
    from tracker import watchlist
    from tracker.models import Account

    engine = _watch_engine()
    with _explain_db_locks(), session_scope(engine) as session:
        account_id = _account_id(session, user, for_write=True)
        account = session.get(Account, account_id)
        if account is None:  # pragma: no cover - _account_id resolved it a line ago
            _fail("that account no longer exists")
            return
        account.watch_all = bool(on)
        session.flush()
        # Read off the row before the session closes; a detached instance raises.
        email = account.email
        watching = len(watchlist.entries(session, account_id=account_id))

    if json_mode():
        emit({"user": email, "watch_all": bool(on), "entries": watching})
        return
    if on:
        console.print(f"[green]{escape(email)} now watches every project[/green]")
    elif watching:
        console.print(
            f"[green]{escape(email)} now watches only its {watching} entr"
            f"{'y' if watching == 1 else 'ies'}[/green]"
        )
    else:
        console.print(
            f"[yellow]{escape(email)} now watches nothing[/yellow] "
            '[dim]— name something with `tracker watch add "Nscale"`[/dim]'
        )


# --- Accounts ----------------------------------------------------------------


def _ask_password(prompt: str = "Password") -> str:
    """Read a password from the terminal, twice, without echoing it.

    **Never a flag.** A password passed as an argument lands in shell history and
    in `ps` on a multi-user host, and neither of those is a place a credential
    survives being useful.

    This is also why every `users` command is in `catalog.BLOCKED`: both the console
    and the TUI spawn commands through `webui/runner.py` with no stdin, so a prompt
    there would hang the single run slot until the timeout with nothing on screen.
    """
    import getpass

    from tracker.accounts import AccountError, check_password_length

    try:
        first = getpass.getpass(f"{prompt}: ")
        second = getpass.getpass("Again: ")
    except (EOFError, KeyboardInterrupt):
        _fail("no password given.")
        raise
    if first != second:
        _fail("those did not match.")
    try:
        check_password_length(first)
    except AccountError as exc:
        _fail(str(exc))
    return first


@users_app.callback(invoke_without_command=True)
def users(ctx: typer.Context) -> None:
    """Who may sign in to the console, and everything an operator can change about them.

    `add`, `invite`, `show`, `edit`, `passwd`, `disable`, `enable`, `signout`,
    `admin`, `rm`, and `notify` to tell somebody how their account is now set up.
    An admin can do the same from the console's admin page, except grant admin.

    **Zero accounts is a legitimate state**, and it is the one a fresh install is
    in: the console then opens with no sign-in, exactly as it did with no
    `TRACKER_CONSOLE_PASSWORD`, because reaching loopback already means having the
    machine. What refuses is publishing — `serve --tunnel` will not put a page with
    no way to gate it on the open internet — and a console *already* published
    goes on requiring a sign-in if its last account is deleted, so it refuses
    everyone rather than opening.

    Adding the first account therefore *changes what the console does*, and only
    ever in the safe direction: every route starts asking for a session, and each
    account gets its own watchlist. It takes effect within a few seconds on a
    running console, with no restart — see `webui/server.py::Console.auth_required`.
    """
    if ctx.invoked_subcommand is not None:
        return

    from tracker import accounts, watchlist

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        rows = accounts.listing(session)
        watches = {}
        for entry in watchlist.entries(session):
            watches[entry.account_id] = watches.get(entry.account_id, 0) + 1
        pending = accounts.outstanding(session)

        if json_mode():
            emit(
                {
                    "accounts": [
                        {
                            "email": row.email,
                            "name": row.name,
                            "admin": bool(row.is_admin),
                            "disabled": row.disabled_at is not None,
                            "watches": watches.get(row.id, 0),
                            "created_at": row.created_at.isoformat() if row.created_at else None,
                            "last_seen_at": (
                                row.last_seen_at.isoformat() if row.last_seen_at else None
                            ),
                        }
                        for row in rows
                    ],
                    "invites_outstanding": [
                        {"note": i.note, "expires_at": i.expires_at.isoformat()} for i in pending
                    ],
                }
            )
            return

        if not rows:
            console.print(
                "[dim]no accounts, so a console on loopback opens without a sign-in, a "
                "published one refuses everyone, and publishing is refused.\n"
                "Make one with `tracker users add you@example.com`.[/dim]"
            )
        else:
            table = Table(header_style="bold", title_justify="left", box=TABLE_BOX)
            table.add_column("email")
            table.add_column("name")
            table.add_column("role")
            table.add_column("status")
            table.add_column("watches", justify="right")
            table.add_column("last seen")
            for row in rows:
                table.add_row(
                    row.email,
                    row.name or "",
                    "admin" if row.is_admin else "",
                    "[red]disabled[/red]" if row.disabled_at is not None else "active",
                    str(watches.get(row.id, 0)),
                    row.last_seen_at.strftime("%Y-%m-%d")
                    if row.last_seen_at
                    else "[dim]never[/dim]",
                )
            console.print(table)

        if pending:
            console.print(
                f"\n[dim]{len(pending)} unredeemed invite(s): "
                + ", ".join(
                    f"{i.note or 'no note'} (expires {i.expires_at:%Y-%m-%d})" for i in pending
                )
                + "[/dim]"
            )


@users_app.command("add")
def users_add(
    email: Annotated[str, typer.Argument(help="The address they sign in with.")],
    name: Annotated[
        str | None, typer.Option("--name", help="Display name. Optional; the email is the label.")
    ] = None,
) -> None:
    """Create an account, prompting for its password.

    The other way in is `tracker users invite`, which lets somebody set their own
    password in the browser. Use that when you are not the person who will be
    typing it — a password you chose and sent them is a password in a chat log.
    """
    from tracker import accounts

    engine = _watch_engine()
    password = _ask_password()
    with _explain_db_locks(), session_scope(engine) as session:
        first = not accounts.any_exist(session)
        try:
            row = accounts.create(session, email, password, name=name)
        except accounts.AccountError as exc:
            _fail(str(exc))
            raise
        created = row.email

    if json_mode():
        emit({"email": created, "created": True, "first": first})
        return
    console.print(f"[green]created[/green] {escape(created)}")
    if first:
        console.print(
            "[dim]this is the first account, so the console now asks everyone to sign "
            "in — within a few seconds, without a restart.[/dim]"
        )


@users_app.command("passwd")
def users_passwd(
    email: Annotated[str, typer.Argument(help="Whose password to change.")],
) -> None:
    """Change one account's password.

    **Every session signed in with the old one ends**, within a few seconds, on a
    running console and without a restart. Sessions live in the console's memory
    and this is a different process, so it cannot reach them — but each one
    remembers a digest of the credential it was granted on, and the console
    re-checks that against this row every few seconds per session.
    """
    from tracker import accounts

    engine = _watch_engine()
    password = _ask_password("New password")
    with _explain_db_locks(), session_scope(engine) as session:
        try:
            row = accounts.set_password(session, email, password)
        except accounts.AccountError as exc:
            _fail(str(exc))
            raise
        changed = row.email

    if json_mode():
        emit({"email": changed, "changed": True})
        return
    console.print(f"[green]password changed[/green] for {escape(changed)}")
    console.print("[dim]sessions signed in with the old password end within a few seconds.[/dim]")


@users_app.command("rm")
def users_rm(
    email: Annotated[str, typer.Argument(help="Whose account to delete.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation. For scripts.")] = False,
) -> None:
    """Delete an account and, with it, that person's watchlist.

    The watchlist goes because it is a statement of *their* interest and means
    nothing without them — `ON DELETE CASCADE`, decided in migration 0021. Nothing
    about the dataset changes: an account has never owned a project, a citation or
    a figure.

    Their open sessions end within a few seconds on a running console, on every
    route: each request re-checks its session against this row.

    **The last account can be deleted like any other, with no extra flag**, and
    that is a decision. It used to be the dangerous one — a published console
    opened within seconds of it — but a published console now refuses everyone
    instead (`webui/server.py::Console.published`). What is left is an outage that
    `tracker users add` undoes in seconds, for an operation that is rare and
    already confirmed; a second flag would be friction on the one path that
    already asks. So the prompt says it is the last account, and the result says
    what that did.
    """
    from tracker import accounts, watchlist

    engine = _watch_engine()
    with _explain_db_locks(), session_scope(engine) as session:
        try:
            row = accounts.require(session, email)
        except accounts.AccountError as exc:
            _fail(str(exc))
            raise
        target, held = row.email, len(watchlist.entries(session, account_id=row.id))
        if not yes and not json_mode():
            if accounts.count(session) == 1:
                console.print(
                    "[yellow]this is the last account.[/yellow][dim] A published console "
                    "will refuse every sign-in until `tracker users add` makes another; "
                    "one on loopback will open without a sign-in.[/dim]"
                )
            note = f" and {held} watchlist entr{'y' if held == 1 else 'ies'}" if held else ""
            typer.confirm(f"delete {target}{note}?", abort=True)
        accounts.delete(session, email)
        remaining = accounts.count(session)

    if json_mode():
        emit({"email": target, "removed": True, "watches_dropped": held})
        return
    console.print(f"[green]deleted[/green] {escape(target)}")
    if not remaining:
        console.print(
            "[yellow]that was the last account[/yellow][dim] — a published console now "
            "refuses every sign-in until `tracker users add` makes another; one on "
            "loopback opens without a sign-in, and cannot be published.[/dim]"
        )


def _one_account(session, email: str):
    """`accounts.require`, with the refusal printed the CLI's way."""
    from tracker import accounts

    try:
        return accounts.require(session, email)
    except accounts.AccountError as exc:
        _fail(str(exc))
        raise


def _print_detail(detail: dict) -> None:
    def when(value: str | None) -> str:
        return escape(value[:16].replace("T", " ")) if value else "[dim]never[/dim]"

    table = Table(show_header=False, box=TABLE_BOX)
    table.add_column("field", style="dim")
    table.add_column("value")
    status = (
        f"[red]disabled[/red] since {when(detail['disabled_at'])}"
        if detail["disabled"]
        else "active"
    )
    count = detail["watches"]
    for label, value in (
        ("email", escape(detail["email"])),
        ("name", escape(detail["name"]) if detail["name"] else "[dim]none[/dim]"),
        ("role", "admin" if detail["admin"] else "reader"),
        ("status", status),
        ("sees", "the whole database" if detail["watch_all"] else "its watchlist only"),
        ("watchlist", f"{count} entr{'y' if count == 1 else 'ies'}"),
        ("joined", escape(detail["joined"])),
        ("created", when(detail["created_at"])),
        ("last signed in", when(detail["last_seen_at"])),
        ("last changed", when(detail["updated_at"])),
    ):
        table.add_row(label, value)
    console.print(table)


@users_app.command("show")
def users_show(
    email: Annotated[str, typer.Argument(help="Whose account to show.")],
) -> None:
    """Everything about one account: role, status, reach, how it joined, when it was used."""
    from tracker import accounts

    with session_scope(_read_engine(), commit=False) as session:
        detail = accounts.detail(session, _one_account(session, email))
    if json_mode():
        emit(detail)
        return
    _print_detail(detail)


@users_app.command("edit")
def users_edit(
    email: Annotated[str, typer.Argument(help="Whose account to change.")],
    new_email: Annotated[
        str | None,
        typer.Option("--email", help="Their new sign-in address.", show_default=False),
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", help="Their display name.", show_default=False)
    ] = None,
    clear_name: Annotated[bool, typer.Option("--clear-name", help="Remove the name.")] = False,
    see_all: Annotated[
        bool | None,
        typer.Option(
            "--see-all/--watchlist-only",
            help="Whether they read the whole database or only their watchlist.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Change an account's sign-in address, display name, or what it sees.

    Their sessions survive: a session is bound to the password, not the address.
    Nobody is emailed — `tracker users notify` does that, to whichever address you
    type, which after an address change is usually the old one.
    """
    from tracker import accounts

    if new_email is None and name is None and not clear_name and see_all is None:
        _fail("nothing to change. Pass --email, --name, --clear-name or --see-all.")
    with _explain_db_locks(), session_scope(_watch_engine()) as session:
        row = _one_account(session, email)
        try:
            changes = accounts.update(
                session, row, email=new_email, name=name, clear_name=clear_name, watch_all=see_all
            )
        except accounts.AccountError as exc:
            _fail(str(exc))
            raise
        detail = accounts.detail(session, row)
    if json_mode():
        emit({"account": detail, "changes": changes})
        return
    if not changes:
        console.print("[dim]nothing changed — those are already its settings.[/dim]")
        return
    for change in changes:
        console.print(f"[green]changed[/green] {escape(change)}")
    console.print(
        f"[dim]to tell them: tracker users notify <address> --about {escape(detail['email'])}[/dim]"
    )


def _switch(email: str, *, disable: bool) -> None:
    from tracker import accounts

    with _explain_db_locks(), session_scope(_watch_engine()) as session:
        row = _one_account(session, email)
        changed = accounts.set_disabled(session, row, disable)
        target = row.email
    verb = "disabled" if disable else "enabled"
    if json_mode():
        emit({"email": target, verb: True, "changed": changed})
        return
    if not changed:
        console.print(f"[dim]{escape(target)} is already {verb}.[/dim]")
        return
    console.print(f"[green]{verb}[/green] {escape(target)}")
    if disable:
        console.print(
            "[dim]it keeps its watchlist and cannot sign in; its open sessions end within "
            "a few seconds. `tracker users enable` switches it back on.[/dim]"
        )


@users_app.command("disable")
def users_disable(email: Annotated[str, typer.Argument(help="Whose account to lock.")]) -> None:
    """Lock an account without deleting it or its watchlist. `enable` undoes it."""
    _switch(email, disable=True)


@users_app.command("enable")
def users_enable(email: Annotated[str, typer.Argument(help="Whose account to unlock.")]) -> None:
    """Switch a disabled account back on."""
    _switch(email, disable=False)


@users_app.command("signout")
def users_signout(
    email: Annotated[str, typer.Argument(help="Whose sessions to end.")],
) -> None:
    """End every session an account has open, without changing its password.

    Within a few seconds on a running console: each session re-checks its account
    against the row, and this changes what it is checked against.
    """
    from tracker import accounts

    with _explain_db_locks(), session_scope(_watch_engine()) as session:
        row = _one_account(session, email)
        accounts.sign_out_everywhere(session, row)
        target = row.email
    if json_mode():
        emit({"email": target, "signed_out": True})
        return
    console.print(f"[green]signed out everywhere[/green] {escape(target)}")


@users_app.command("admin")
def users_admin(
    email: Annotated[str, typer.Argument(help="Whose role to change.")],
    revoke: Annotated[bool, typer.Option("--revoke", help="Take admin away instead.")] = False,
) -> None:
    """Grant an account the console's admin page, or take it away.

    **Only here, never from the console.** The admin page can edit, lock and delete
    accounts, but it cannot make an admin, so a stolen admin session cannot make
    itself permanent. Takes effect on the next request.
    """
    from tracker import accounts

    with _explain_db_locks(), session_scope(_watch_engine()) as session:
        row = _one_account(session, email)
        changed = accounts.set_admin(session, row, not revoke)
        target, left = row.email, len(accounts.admins(session))
    if json_mode():
        emit({"email": target, "admin": not revoke, "changed": changed})
        return
    state = "no longer an admin" if revoke else "an admin"
    if not changed:
        console.print(f"[dim]{escape(target)} is already {state}.[/dim]")
        return
    console.print(f"[green]{escape(target)}[/green] is now {state}")
    if revoke and not left:
        console.print("[dim]no admins remain, so nobody can open the admin page.[/dim]")


@users_app.command("notify")
def users_notify(
    to: Annotated[
        str, typer.Option("--to", help="Where to send it. Typed by you, never looked up.")
    ],
    about: Annotated[
        str | None,
        typer.Option(
            "--about",
            help="The account it describes, by its current sign-in email. Defaults to --to.",
            show_default=False,
        ),
    ] = None,
    subject: Annotated[
        str | None,
        typer.Option(
            "--subject", help="The subject line. Has a sensible default.", show_default=False
        ),
    ] = None,
    message: Annotated[
        str | None,
        typer.Option(
            "--message",
            "--note",
            help="A paragraph of your own, shown above the settings.",
            show_default=False,
        ),
    ] = None,
    old_email: Annotated[
        str | None,
        typer.Option(
            "--old-email",
            help="The address the account moved away from. The email then opens by "
            "saying the sign-in changed from this to the current one.",
            show_default=False,
        ),
    ] = None,
    new_password: Annotated[
        str | None,
        typer.Option(
            "--new-password",
            help="Set this as the account's password AND include it in the email. It lands "
            "in your shell history; --ask-password avoids that.",
            show_default=False,
        ),
    ] = None,
    ask_password: Annotated[
        bool,
        typer.Option("--ask-password", help="Like --new-password, but typed hidden, twice."),
    ] = False,
    preview: Annotated[
        bool,
        typer.Option("--preview", help="Print the message instead of sending it. Changes nothing."),
    ] = False,
) -> None:
    """Email somebody how an account is now set up, with whatever you add to it.

    **You choose the address.** After changing someone's sign-in email, the person
    to tell is at the old one, which the account no longer records — so this sends
    wherever `--to` points, and nothing sends it on its own. The email lists the
    account's current settings (sign-in email, name, status, what it sees, role),
    after your `--message` if you give one.

    **A password is included only if you pass one**, and passing one also sets it
    on the account, so the email and the account cannot disagree. If the send
    fails, the password change is undone. The email tells them to change it after
    signing in.

    \b
    Examples:
      tracker users notify --to old@x.com --about new@x.com --old-email old@x.com
      tracker users notify --to ann@x.com --ask-password --message "Welcome aboard."
    """
    from tracker import account_notice, accounts
    from tracker import notify as notify_mod

    if new_password is not None and ask_password:
        _fail("pass --new-password or --ask-password, not both.")
    try:
        recipient = accounts.normalize_email(to)
    except accounts.AccountError as exc:
        _fail(str(exc))
        raise
    password = _ask_password("New password") if ask_password else new_password
    if password is not None:
        try:
            accounts.check_password_length(password)
        except accounts.AccountError as exc:
            _fail(str(exc))
            raise
    settings = get_settings()

    def compose(detail: dict) -> account_notice.Notice:
        return account_notice.render(
            detail,
            note=message,
            console_url=settings.notify_console_url or None,
            subject=subject,
            old_email=old_email,
            new_password=password,
        )

    if preview:
        with session_scope(_read_engine(), commit=False) as session:
            notice = compose(accounts.detail(session, _one_account(session, about or to)))
        if json_mode():
            emit({"to": recipient, "subject": notice.subject, "text": notice.text_body})
            return
        console.print(f"[bold]to[/bold] {escape(recipient)}")
        console.print(f"[bold]subject[/bold] {escape(notice.subject)}\n")
        console.print(escape(notice.text_body))
        if password is not None:
            console.print("\n[dim]preview only: the password was not set.[/dim]")
        return

    try:
        transport = notify_mod.ResendTransport(settings)
    except notify_mod.EmailError as exc:
        _fail(str(exc))
        raise
    # One transaction around the password change and the send: a send that fails
    # raises out of it, so the account keeps the password it had rather than one
    # that was never delivered to anybody.
    try:
        with _explain_db_locks(), session_scope(_watch_engine()) as session:
            row = _one_account(session, about or to)
            if password is not None:
                accounts.reset_password(session, row, password)
            notice = compose(accounts.detail(session, row))
            message_id = transport.send(
                to=recipient,
                subject=notice.subject,
                html_body=notice.html_body,
                text_body=notice.text_body,
            )
            described = row.email
    except notify_mod.EmailError as exc:
        _fail(f"{exc}\nNothing was changed.")
        raise
    if json_mode():
        emit(
            {
                "to": recipient,
                "about": described,
                "sent": True,
                "id": message_id,
                "password_set": password is not None,
            }
        )
        return
    console.print(f"[green]sent[/green] to {escape(recipient)} about {escape(described)}")
    if password is not None:
        console.print(
            "[dim]its password is now the one in the email, and its open sessions end "
            "within a few seconds.[/dim]"
        )


@users_app.command("invite")
def users_invite(
    note: Annotated[
        str | None, typer.Option("--note", help="Who it is for. Shown on the outstanding list.")
    ] = None,
    days: Annotated[
        int, typer.Option("--days", help="How long it stays usable.")
    ] = DEFAULT_INVITE_DAYS,
) -> None:
    """Mint a single-use code somebody can redeem for an account in the browser.

    **The code is printed once and is not recoverable.** Only its sha256 is stored,
    because this database is copied between machines and kept in backups, where a
    plaintext code would be a live credential in every copy.

    They redeem it on the console's own login page, where they choose their own
    email and password. That is the point of an invite over `users add`: a password
    you picked and sent them is a password in a chat log.
    """
    from tracker import accounts

    engine = _watch_engine()
    with _explain_db_locks(), session_scope(engine) as session:
        try:
            row, code = accounts.mint_invite(session, note=note, days=days)
        except accounts.AccountError as exc:
            _fail(str(exc))
            raise
        expires = row.expires_at

    if json_mode():
        emit({"code": code, "note": note, "expires_at": expires.isoformat()})
        return
    console.print(f"[bold]{escape(code)}[/bold]")
    console.print(
        f"[dim]single use, expires {expires:%Y-%m-%d %H:%M} UTC. Shown once — "
        "only its hash is stored. They redeem it on the console's sign-in page.[/dim]"
    )


@app.command()
def digest(
    days: Annotated[int, typer.Option("--days", help="How far back to look, in days.")] = 7,
    since: Annotated[
        str | None,
        typer.Option("--since", help="An ISO date or datetime, instead of --days."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Signals to print. Unlimited by default.")
    ] = None,
    held: Annotated[
        bool,
        typer.Option("--held/--no-held", help="Include signals whose evidence is unconfirmed."),
    ] = False,
    notify: Annotated[
        bool,
        typer.Option(
            "--notify",
            help="Only what is worth interrupting somebody for, and nothing at all if nothing is.",
        ),
    ] = False,
    whole_database: Annotated[
        bool,
        typer.Option(
            "--whole-database",
            help="With --notify, allow an empty watchlist to mean every project.",
        ),
    ] = False,
    markdown: Annotated[
        bool, typer.Option("--markdown", help="Emit Markdown, for pasting or mailing.")
    ] = False,
    user: Annotated[str | None, _USER_OPTION] = None,
) -> None:
    """What changed on the watchlist, good and bad, since a date.

    The same reading the console's landing page renders, in a form that can be
    sent: `tracker digest --markdown --days 1` is the nightly note. Reads only, so
    it is safe on either machine.

    **`--user` is what makes it the same reading.** Without it this runs over every
    account's watchlist at once, which is what a terminal on the host wants;
    `--user alice@example.com` reproduces exactly the page alice sees, which is the
    form to schedule if the nightly note is going to *her*.

    **The window is on when we learned a fact, not when it happened.** A crawl
    reads one article and imports a project's whole back-history, so filtering on
    the milestone's own date would report 2022 every morning. Every line carries
    both dates for exactly that reason — `tracker/feed.py` has the argument.

    **`--notify` is the form to schedule.** It prints only what `feed.notable`
    admits — the blocker moving, a decisive milestone, a dated slip, an obstacle of
    `material` severity or worse opening or clearing — and prints *nothing at all*
    when none of that happened, so a nightly job piped into a mailer sends on the
    nights that earn it and stays quiet otherwise. Silence is the useful default
    for a channel somebody is meant to keep trusting.

    Three things bound what it can send, and all three exist because a mailer is
    read *after* it has interrupted somebody:

    * **It must have happened recently**, not merely been learned recently
      (`feed.NOTIFY_MAX_AGE_DAYS`). The window is on `created_at`, and a crawl
      imports a whole back-history at once — measured live, 107 of 354 notifiable
      signals in a month described something over three years old.
    * **An empty watchlist is refused**, because the fallback that makes the *page*
      useful — show everything until somebody configures it — makes the mail a
      firehose. `--whole-database` says you meant it.
    * **A burst is capped** at `feed.NOTIFY_MAX_ITEMS` and the remainder is
      counted, never silently dropped. One sync produced 135 in a night.

    It also exits 1 when it printed nothing, so a shell can tell "quiet night"
    from "we sent something" without parsing the output:

        tracker digest --notify --markdown --days 1 | mail -s "dc-tracker" you@…
    """
    import datetime as dt

    from tracker import feed

    when: dt.datetime | None = None
    if since:
        try:
            when = dt.datetime.fromisoformat(since)
        except ValueError:
            _fail(f"--since must be an ISO date or datetime, not {since!r}")

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        brief = feed.digest(
            session,
            since=when,
            days=days,
            limit=limit,
            account_id=_account_id(session, user, for_write=False),
        )

    if notify:
        # Nothing printed on a quiet night, in any format: an empty digest that
        # still prints a header is exactly the mail somebody starts filtering.
        # **A mailer may not fall back to the whole database.** `digest` shows
        # everything when no watchlist is set, which is right for a page — a blank
        # page teaches nobody what it is for — and wrong for a channel that arrives
        # uninvited. Measured on the live database: no watch rows at all, so every
        # account would have been mailed about all 193 projects that moved.
        #
        # Nothing goes to stdout, so a cron piped into a mailer sends no mail
        # rather than sending an explanation nobody asked for; the reason goes to
        # stderr, where a person running it by hand still sees it.
        if brief.watching_everything and not whole_database:
            err.print(
                "[yellow]--notify with an empty watchlist[/yellow] would mail about every "
                f"project that moved ({brief.projects_watched} are being read).\n"
                "Name what you want to hear about:\n"
                '  tracker watch add "Nscale" --user you@example.com\n'
                "Or say you meant it: --whole-database"
            )
            raise typer.Exit(2)

        sending = brief.notifying
        if json_mode():
            # Not capped: a program can page, and truncating a payload is how a
            # consumer silently under-reports.
            emit({"notify": [s.as_json() for s in sending], "since": brief.since.isoformat()})
        elif sending:
            shown, held_back = sending[: feed.NOTIFY_MAX_ITEMS], sending[feed.NOTIFY_MAX_ITEMS :]
            lines = _notify_markdown(brief, shown) if markdown else [_signal_line(s) for s in shown]
            for line in lines:
                print(line)
            if held_back:
                # Counted, never silently dropped. A cap that hides its own effect
                # reads as "that was everything", which is the one thing a
                # notification must not imply.
                print(
                    f"\n…and {len(held_back)} more this window, not listed. "
                    f"See them all with `tracker digest --days {days}`."
                )
        if not sending:
            raise typer.Exit(1)
        return

    if json_mode():
        emit(brief.as_json())
        return
    if markdown:
        for line in digest_markdown(brief, held=held):
            print(line)
        return
    _print_digest(brief, held=held)


def _digest_scope(brief) -> str:
    if brief.watching_everything:
        return f"the whole database ({brief.projects_watched} projects)"
    entries = ", ".join(e.entry for e in brief.entities)
    return f"{entries} — {brief.projects_watched} project(s)"


_SIGN_STYLE = {"good": "green", "bad": "red", "neutral": "dim"}
_SIGN_MARK = {"good": "+", "bad": "-", "neutral": "."}


def _signal_line(signal) -> str:
    """One signal as a sentence, with both of its dates."""
    when = signal.happened.isoformat() if signal.happened else "undated"
    learned = f", learned {signal.at.date().isoformat()}" if signal.at else ""
    tail = f" [{signal.publisher}]" if signal.publisher else ""
    return (
        f"{signal.company} — {signal.project}: {signal.headline} "
        f"({when}{learned}). {signal.detail}{tail}"
    )


def _print_digest(brief, *, held: bool) -> None:
    console.print(f"[bold]since {brief.since.date().isoformat()}[/bold] — {_digest_scope(brief)}")
    if brief.last_crawl:
        console.print(f"[dim]last citation fetched {brief.last_crawl.isoformat(sep=' ')}[/dim]")
    else:
        console.print("[yellow]nothing has ever been fetched into this database[/yellow]")

    for entity in brief.entities:
        extra = f", {entity.held} unconfirmed" if entity.held else ""
        console.print(
            f"  {escape(entity.entry)}: [bold]{entity.total}[/bold] update(s) — "
            f"{entity.good} good, {entity.bad} bad{extra}"
        )

    sending = brief.notifying
    if brief.signals:
        console.print(
            f"[dim]{len(sending)} of {len(brief.signals)} would notify "
            "(`--notify` for those alone)[/dim]"
        )

    if not brief.signals:
        console.print("\n[dim]nothing new in this window.[/dim]")
    for signal in brief.signals:
        style = _SIGN_STYLE.get(signal.sign, "dim")
        mark = _SIGN_MARK.get(signal.sign, ".")
        bell = " [bold]!​[/bold]" if signal.notify else ""
        console.print(f"\n[{style}]{mark}[/{style}]{bell} {escape(_signal_line(signal))}")
        if signal.unblocks:
            console.print(f"  [bold]{escape(signal.effect or '')}[/bold]")
        elif signal.effect:
            console.print(f"  [dim]{escape(signal.effect)}[/dim]")
        if signal.quote:
            console.print(f'  [dim]"{escape(signal.quote)}"[/dim]')

    if held and brief.held:
        console.print(
            f"\n[yellow]{len(brief.held)} signal(s) nobody could quote[/yellow] "
            "— shown because --held was passed, and not counted above."
        )
        for signal in brief.held:
            console.print(f"  [dim]? {escape(_signal_line(signal))} ({signal.unconfirmed})[/dim]")


def _notify_markdown(brief, sending) -> list[str]:
    """The notification itself: what happened, and nothing about what did not.

    Deliberately not `digest_markdown` with a filter. That renders the whole
    reading — scope, tallies, the last crawl — which is right for a page somebody
    opened and wrong for a message that arrives unasked. A notification says the
    thing and gets out of the way.
    """
    lines = [f"# {len(sending)} update(s) worth telling you about", ""]
    for signal in sending:
        mark = _SIGN_MARK.get(signal.sign, ".")
        lines.append(f"### {mark} {_signal_line(signal)}")
        if signal.effect:
            lines.append(f"*{signal.effect}*")
        if signal.quote:
            lines.append(f"> {signal.quote}")
        if signal.source_url:
            lines.append(f"[source]({signal.source_url})")
        lines.append("")
    lines.append(f"[since {brief.since.date().isoformat()}]")
    return lines


def digest_markdown(brief, *, held: bool = False) -> list[str]:
    """The same reading as Markdown, for pasting into a mail or a message.

    Public because the nightly note is the point of `--markdown`: whatever ends up
    sending it should not have to re-render the digest itself.
    """
    lines = [
        f"# What changed since {brief.since.date().isoformat()}",
        "",
        f"Watching: {_digest_scope(brief)}.",
    ]
    if brief.last_crawl:
        lines.append(f"Last citation fetched {brief.last_crawl.isoformat(sep=' ')}.")
    lines.append("")
    for entity in brief.entities:
        lines.append(
            f"- **{entity.entry}** — {entity.total} update(s), {entity.good} good, {entity.bad} bad"
        )
    if brief.entities:
        lines.append("")

    if not brief.signals:
        lines += ["Nothing new in this window.", ""]
    for signal in brief.signals:
        mark = _SIGN_MARK.get(signal.sign, ".")
        lines.append(f"### {mark} {_signal_line(signal)}")
        if signal.effect:
            lines.append(f"*{signal.effect}*")
        if signal.quote:
            lines.append(f"> {signal.quote}")
        if signal.source_url:
            lines.append(f"[source]({signal.source_url})")
        lines.append("")

    if held and brief.held:
        lines += [f"## {len(brief.held)} unconfirmed, not counted above", ""]
        lines += [f"- {_signal_line(s)} ({s.unconfirmed})" for s in brief.held]
        lines.append("")
    return lines


@notify_app.command("preview")
def notify_preview(
    user: Annotated[
        str | None,
        typer.Option("--user", help="Render this account's message.", show_default=False),
    ] = None,
    days: Annotated[int, typer.Option("--days", help="Window, in days.")] = 1,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the HTML here instead of to stdout."),
    ] = None,
) -> None:
    """Render what would be sent, and send nothing.

    Free and offline: no key, no network, no write lock. The template is a pure
    function of the digest, so this is the *same* HTML `notify send` would post to
    Resend rather than an approximation of it — which is the only kind of preview
    worth having.
    """
    from tracker import accounts
    from tracker import notify as notify_mod
    from tracker.feed import digest as build_digest

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        people = accounts.listing(session)
        if user:
            wanted = accounts.normalize_email(user)
            people = [a for a in people if a.email_key == wanted]
            if not people:
                _fail(f"no account for {user!r}. `tracker users` lists them.")
                return
        if not people:
            _fail("no accounts yet. Make one with `tracker users add you@example.com`.")
            return

        account = people[0]
        brief = build_digest(session, days=days, account_id=account.id)
        if brief.watching_everything:
            _fail(
                f"{account.email} has no watchlist, so there is nothing to be "
                'selective about. Add one: tracker watch add "Nscale" '
                f"--user {account.email}"
            )
            return

        sending = brief.notifying
        if not sending:
            console.print(
                f"[yellow]nothing worth sending to {escape(account.email)}[/yellow] "
                f"[dim]in the last {days} day(s). Widen it with --days.[/dim]"
            )
            raise typer.Exit(1)

        # Every update, never a truncated preview: this has to be the same bytes
        # `notify send` would post, or it is not a preview of anything.
        body = notify_mod.render(
            brief,
            sending,
            name=account.name,
            console_url=get_settings().notify_console_url or None,
        )
        subject = notify_mod.subject_for(brief, sending)
        # Read off the row before the session closes. An ORM instance is detached
        # at that point and touching it raises DetachedInstanceError — which is
        # the same hazard `parallel.map_ordered` documents about worker threads,
        # arriving here through scope rather than through concurrency.
        recipient, cards = account.email, len(sending)

    if out:
        out.write_text(body, encoding="utf-8")
        console.print(f"[green]wrote[/green] {escape(str(out))} [dim]({len(body):,} bytes)[/dim]")
        console.print(f"[dim]subject:[/dim] {escape(subject)}")
        console.print(f"[dim]to:[/dim] {escape(recipient)}  [dim]cards:[/dim] {cards}")
    else:
        print(body)


@notify_app.command("send")
def notify_send(
    days: Annotated[int, typer.Option("--days", help="Window, in days.")] = 1,
    user: Annotated[
        str | None,
        typer.Option("--user", help="Only this account. Everyone, by default.", show_default=False),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Say who would get what, and send nothing."),
    ] = False,
) -> None:
    """Send each person one email carrying everything on their watchlist that moved.

    **One message per person, never one per change.** Fourteen updates is one
    email with fourteen cards; a channel that sends fourteen separate messages is
    one people filter away, and a filtered channel protects nobody.

    Silence is the default: an account whose window is quiet gets nothing, and an
    account with no watchlist is skipped rather than mailed the whole database —
    the same rule `digest --notify` enforces, for the same reason. A page is
    opened deliberately; mail arrives uninvited.

    What crosses the bar is `feed.notable`: quote-backed, already happened,
    recently, and material. `notify preview` shows the exact message first, for
    free.
    """
    from tracker import notify as notify_mod

    settings = get_settings()
    engine = _read_engine()

    if dry_run:
        # A recorder rather than the real transport, so a dry run cannot need a
        # key and cannot reach the network — the two ways a "safe" preview
        # historically stops being safe.
        planned: list[tuple[str, str, int]] = []

        class Recorder:
            def send(self, *, to, subject, html_body, text_body):
                planned.append((to, subject, len(html_body)))
                return ""

        with session_scope(engine, commit=False) as session:
            outcomes = notify_mod.send_all(
                session, transport=Recorder(), days=days, only_email=user
            )
        rows = [
            (
                o.email,
                str(o.signals),
                o.skipped or "would send",
            )
            for o in outcomes
        ]
        _print_notify_rows(rows, title="notify (dry run)")
        for to, subject, size in planned:
            console.print(f"  [dim]{escape(to)}[/dim] — {escape(subject)} [dim]({size:,} b)[/dim]")
        if not planned:
            console.print("[dim]nothing would be sent[/dim]")
        return

    try:
        transport = notify_mod.ResendTransport(settings)
    except notify_mod.EmailError as exc:
        _fail(str(exc))
        return

    with session_scope(engine, commit=False) as session:
        try:
            outcomes = notify_mod.send_all(
                session,
                transport=transport,
                days=days,
                console_url=settings.notify_console_url or None,
                only_email=user,
            )
        except notify_mod.EmailError as exc:
            _fail(str(exc))
            return

    sent = [o for o in outcomes if o.sent]
    rows = [
        (o.email, str(o.signals), o.skipped or f"sent {o.message_id or ''}".strip())
        for o in outcomes
    ]
    _print_notify_rows(rows, title="notify")
    if not sent:
        # Exit 1 on "nothing to say", matching `digest --notify`, so a scheduled
        # job can tell a quiet night from a failure.
        raise typer.Exit(1)
