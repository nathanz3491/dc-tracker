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
    """Who may sign in to the console. `add`, `passwd`, `rm` and `invite` change it.

    **Zero accounts is a legitimate state**, and it is the one a fresh install is
    in: the console then opens with no sign-in, exactly as it did with no
    `TRACKER_CONSOLE_PASSWORD`, because reaching loopback already means having the
    machine. What refuses is publishing — `serve --tunnel` will not put a page with
    no way to gate it on the open internet.

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
                "[dim]no accounts, so the console opens without a sign-in and refuses "
                "to publish.\nMake one with `tracker users add you@example.com`.[/dim]"
            )
        else:
            table = Table(header_style="bold", title_justify="left", box=TABLE_BOX)
            table.add_column("email")
            table.add_column("name")
            table.add_column("watches", justify="right")
            table.add_column("last seen")
            for row in rows:
                table.add_row(
                    row.email,
                    row.name or "",
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

    **Live sessions survive this.** They are held in the console's memory and this
    is a different process, so an old cookie keeps working until it expires — 12
    hours at most. Restart the console if one has to die now.
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
    console.print("[dim]any session already open stays valid until it expires.[/dim]")


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
            "[yellow]that was the last account[/yellow][dim] — the console is open "
            "again on loopback, and will refuse to publish.[/dim]"
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
