"""`python -m tracker <subcommand>` / `tracker <subcommand>`.

Typer + Rich: the PRD asks for formatted tables on stdout, and both are already
available. Read commands open the database **read-only**, which turns the PRD's
"never modify the DB except for ingest and review" from a convention into a
guarantee enforced by SQLite itself.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from rich import box
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.table import Table
from sqlalchemy import func, select

from tracker import __version__
from tracker.config import get_settings, install_root
from tracker.db import (
    AlreadyRunning,
    MigrationError,
    acquire_write_lock,
    init_db,
    open_db,
    schema_version,
    session_scope,
)
from tracker.gaps import DEFAULTED, DERIVED, INFERRED, UNCONFIRMED, basis
from tracker.gaps import measure as measure_gaps
from tracker.gaps import worst as worst_gaps
from tracker.models import Project, Risk, Source
from tracker.vocab import (
    OPEN_RISK_STATUS,
    PHASES,
    RISK_CATEGORIES,
    RISK_SEVERITIES,
    severity_rank,
)

app = typer.Typer(
    name="tracker",
    help="Track US data center construction projects, with every fact cited.",
    no_args_is_help=True,
    add_completion=False,
)
ingest_app = typer.Typer(name="ingest", help="Load projects from a source.", no_args_is_help=True)
app.add_typer(ingest_app)

#: Finding contradictions and settling the one kind that can be settled are two
#: halves of one job, so they live under one name rather than as strangers in a
#: flat list. Same shape as `ingest`.
logic_app = typer.Typer(
    name="logic",
    help="Find values that contradict each other, and settle the ones that can be.",
    no_args_is_help=True,
)
app.add_typer(logic_app)

#: `duplicates`, `audit`, `risks` and `queue` are groups whose bare form still runs
#: the listing they have always run — `invoke_without_command=True` on the
#: callback, and the report in the callback body. A report is only half a tool if
#: there is nowhere to put the answer, and each of them grew one: `duplicates park`
#: records a *no*, `audit resolve` works through the implausible figures, `risks
#: confirm` reads the article behind an unquoted obstacle, `queue prune` throws out
#: what the filter no longer wants. Making them groups is what lets the answer live
#: next to the question instead of becoming `tracker park-duplicates`, two commands
#: away in an alphabetical list.
#:
#: **None of them passes `help=`, and that is deliberate.** Typer takes the group's
#: help from that string when it is given and from the callback's docstring when it
#: is not — so passing one replaces a full explanation with a single line, and
#: `tracker duplicates --help` becomes the only command in this CLI that will not
#: tell you what it does. The docstrings say it; the first line of each is what the
#: top-level listing shows, exactly as for every ordinary command.
duplicates_app = typer.Typer(name="duplicates", invoke_without_command=True)
app.add_typer(duplicates_app)

audit_app = typer.Typer(name="audit", invoke_without_command=True)
app.add_typer(audit_app)

#: The listing and the confirmation pass over obstacles, under one name.
risks_app = typer.Typer(name="risks", invoke_without_command=True)
app.add_typer(risks_app)

#: Tables use ASCII borders, not Rich's default box-drawing characters.
#: This machine's console codepage is cp936, where Unicode box characters render
#: as mojibake — and the PRD's whole output story is redirection (`tracker export
#: md > file`, pasting tables into chat). ASCII survives every codepage.
TABLE_BOX = box.ASCII2


def _width() -> int | None:
    """Console width: honour COLUMNS, auto-detect a terminal, else stay wide.

    Rich defaults a non-terminal to 80 columns and then *truncates* cell text to
    fit. For a tool whose output is routinely piped, silently cutting "Microsoft"
    down to "Microso…" loses data, so redirected output gets a generous width.
    """
    env = os.environ.get("COLUMNS")
    if env and env.isdigit():
        return int(env)
    return None if sys.stdout.isatty() else 200


def _utf8(stream) -> None:
    """Emit UTF-8 regardless of the console codepage.

    Without this, writing an excerpt containing a typographic quote to a cp936
    stdout raises UnicodeEncodeError and takes the whole command down.
    """
    # A captured or already-detached stream cannot be reconfigured; that is fine,
    # it just keeps whatever encoding it has.
    with contextlib.suppress(AttributeError, ValueError):
        stream.reconfigure(encoding="utf-8", errors="replace")


def _forced_colour() -> dict[str, object]:
    """Console kwargs for when the caller has asked for colour down a pipe.

    `FORCE_COLOR` alone is not enough on Windows, and the failure is silent.
    Rich honours it — `is_terminal` becomes True — but then
    `_detect_color_system` takes the Windows branch, finds `legacy_windows`, and
    picks `ColorSystem.WINDOWS`, which paints by calling the console API instead
    of writing escape sequences. Down a pipe that API does nothing, so the output
    arrives with the markup stripped and no colour in its place. `COLORTERM` is
    only ever read on the POSIX side, so it cannot help either.

    Naming an ANSI dialect and switching legacy mode off is what actually
    produces escapes. Worth having for anyone piping this on Windows, not only
    for `tracker serve`, which is what surfaced it.

    `NO_COLOR` still wins: https://no-color.org says an application should not
    emit colour when it is set, whatever else it was told.
    """
    if os.environ.get("NO_COLOR") is not None or not os.environ.get("FORCE_COLOR"):
        return {}
    return {"color_system": "truecolor", "legacy_windows": False}


_utf8(sys.stdout)
_utf8(sys.stderr)

console = Console(width=_width(), soft_wrap=False, **_forced_colour())
err = Console(stderr=True, width=_width(), **_forced_colour())

#: Rendered for a NULL field. Deliberately ASCII, for the same reason as TABLE_BOX.
NA = "-"

#: Shown when a fetch was blocked. The backslash escapes the bracket for Rich,
#: which would otherwise read "[crawl]" as a style tag and delete the extra's
#: name from the very message telling the operator what to install.
BROWSER_HINT = (
    "[dim]some fetches failed. Several trade-press sites block plain HTTP; "
    r'retry with --browser after: pip install -e ".\[crawl]" && crawl4ai-setup[/dim]'
)

LOCKED_HELP = """the database is locked by another process.

Something else is writing to it, most often a `tracker sync` still running in
another window. SQLite allows one writer at a time.

Find it with:
  Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Select-Object ProcessId, CommandLine

Wait for it to finish, or stop it, then run again. Nothing already committed is
lost: ingestion is idempotent, so a re-run resumes where this one stopped."""

#: Set by the top-level --db option and consumed by each subcommand.
_state: dict[str, object] = {"db": None}


def json_mode() -> bool:
    """True when the caller asked for machine-readable output."""
    return bool(_state.get("json"))


def emit(payload: object) -> None:
    """Write the JSON payload for a command, once, on stdout.

    Deterministic like `tracker export`: sorted keys and a trailing newline, so a
    run can be diffed and piped. `ensure_ascii=False` because project names and the
    待确认 marker are not ASCII and escaping them helps nobody.
    """
    console.print_json(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str))


def _fail(message: str, code: int = 2) -> None:
    """Print an operator-facing error and exit without a traceback.

    In JSON mode the error is emitted as JSON too — a script that pipes stdout to a
    parser should not get prose on one path and a payload on the other.

    The message is escaped, because it is data. Rich reads `[...]` as a style tag
    and silently drops what it does not recognise, which had been quietly
    corrupting the one message where it hurts most: the crawl4ai error tells you
    to run `pip install -e ".[crawl]"` and it was printing `pip install -e "."` —
    the instruction for fixing the problem, broken by the same mechanism.
    """
    if json_mode():
        emit({"error": message})
    else:
        err.print(f"[bold red]error[/bold red] {escape(message)}")
    raise typer.Exit(code)


def _db_path() -> Path:
    override = _state.get("db")
    return get_settings().resolve_db(Path(override) if override else None)


def _read_engine():
    """Open the database read-only, with an actionable message if it is missing."""
    try:
        return open_db(_db_path())
    except (FileNotFoundError, MigrationError) as exc:
        _fail(str(exc))
        raise  # unreachable; _fail always raises typer.Exit


def _writable(command: str):
    """Open the database for writing, holding the single-writer lock.

    The lock is the thing: two commands deleting queued rows at once, or one
    deleting while a `sync` is mid-crawl, is the failure mode SQLite reports as a
    lock timeout twenty seconds later and this reports by name immediately.
    """
    engine, _ = init_db(_db_path())
    try:
        release = acquire_write_lock(_db_path(), command=command)
    except AlreadyRunning as exc:
        _fail(str(exc))
        raise
    atexit.register(release)
    return engine


@app.callback()
def main_callback(
    db: Annotated[
        Path | None,
        typer.Option(
            "--db", help="Database file. Defaults to data/tracker.db.", show_default=False
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show debug logging.")] = False,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit machine-readable JSON on stdout instead of tables.",
        ),
    ] = False,
) -> None:
    _state["db"] = db
    _state["json"] = as_json
    logging.basicConfig(
        # Logs go to stderr, so `tracker --json list | jq` stays clean even at
        # --verbose. Warnings are raised to ERROR in JSON mode because an
        # incidental warning is noise to a script that only wants the payload.
        level=logging.DEBUG if verbose else (logging.ERROR if as_json else logging.INFO),
        format="%(message)s",
        handlers=[RichHandler(console=err, show_time=False, show_path=verbose, markup=False)],
    )


@contextlib.contextmanager
def _explain_db_locks():
    """Turn SQLite's "database is locked" into an actionable message.

    The lock FILE prevents two tracker runs from starting, but it cannot see a
    writer that predates it, nor any other process holding the file. Without this
    the operator gets a forty-line SQLAlchemy traceback whose actual meaning --
    "something else is writing, try later" -- appears only on the last line.
    """
    from sqlalchemy.exc import OperationalError

    try:
        yield
    except OperationalError as exc:
        if "database is locked" not in str(exc).lower():
            raise
        _fail(LOCKED_HELP)


@app.command()
def version() -> None:
    """Print the version."""
    if json_mode():
        emit({"name": "dc-tracker", "version": __version__})
        return
    console.print(f"dc-tracker {__version__}")


@app.command()
def init() -> None:
    """Create or upgrade the database, then recompute the cached derived values."""
    from tracker.upsert import recompute_blocks, recompute_confidence, recompute_h200

    path = _db_path()
    engine, applied = init_db(path)
    with session_scope(engine) as session:
        rescored = recompute_confidence(session)
        # Both of these are restatements of stored facts rather than facts, so
        # they are recomputed rather than migrated: the conversion ratio lives in
        # settings and a SQL backfill would freeze one copy of it here.
        resized = recompute_h200(session)
        reblocked = recompute_blocks(session)

    console.print(f"database: [bold]{path}[/bold]")
    if applied:
        console.print(f"applied migrations: {', '.join(f'{v:04d}' for v in applied)}")
    else:
        console.print("schema already current, nothing to apply")
    console.print(f"schema version: {schema_version(engine)}")
    if rescored:
        console.print(f"recomputed confidence on {rescored} project(s)")
    if reblocked:
        console.print(f"rebuilt capacity blocks on {reblocked} project(s)")
    if resized:
        from tracker.compute import kw_per_h200

        console.print(
            f"recomputed H200-equivalents on {resized} project(s) "
            f"[dim]at {kw_per_h200()} kW each[/dim]"
        )


@app.command()
def serve(
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8765,
    host: Annotated[
        str, typer.Option("--host", help="Interface to bind. Loopback unless overridden.")
    ] = "127.0.0.1",
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open a browser when the server starts.")
    ] = True,
    run: Annotated[
        bool,
        typer.Option(
            "--run/--no-run",
            help="Allow the page to execute commands. --no-run makes it read-only.",
        ),
    ] = True,
    allow_remote: Annotated[
        bool,
        typer.Option("--allow-remote", help="Permit a non-loopback --host. Read the warning."),
    ] = False,
    tunnel: Annotated[
        bool,
        typer.Option(
            "--tunnel",
            help="Publish through a Cloudflare quick tunnel. Requires TRACKER_CONSOLE_PASSWORD.",
        ),
    ] = False,
) -> None:
    """Open the console: the database as a live page, with the commands as buttons.

    Different from `tracker export html`, and both are worth having. The export is
    one self-contained file you can email, frozen at the moment it was written.
    This reads the database on every request and can run the commands that change
    it — which is also why it binds loopback and refuses anything else without
    `--allow-remote`. Anyone who can reach this port can start a `sync`.

    Set `TRACKER_CONSOLE_PASSWORD` to put a password in front of it. On loopback
    that is optional; with `--tunnel` it is required, because publishing makes it
    a public URL and what is behind it runs commands.

    `--tunnel` uses the tunnel configured in `TRACKER_TUNNEL_NAME` /
    `TRACKER_TUNNEL_HOSTNAME` if there is one, and an anonymous quick tunnel
    otherwise. `tracker cloudflare` is the same thing with a readiness check and
    flags to override either.
    """
    settings = get_settings()
    _run_console(
        port=port,
        host=host,
        open_browser=open_browser,
        run=run,
        allow_remote=allow_remote,
        publish=(("named" if settings.tunnel_name else "quick") if tunnel else None),
        tunnel_name=settings.tunnel_name if tunnel else None,
        hostname=settings.tunnel_hostname if tunnel else None,
    )


def _console_password() -> str | None:
    """The console password, checked for the one thing worth checking."""
    from tracker.webui.auth import MIN_PASSWORD_LEN

    secret = get_settings().console_password
    password = secret.get_secret_value() if secret else None
    # A floor against a typo, not a strength policy. What makes a short password
    # safe here is the rate limit — 40 failed attempts across all clients closes
    # the gate for 15 minutes, which puts even a 7-character keyspace tens of
    # millions of years out of reach. See tracker/webui/auth.py.
    if password and len(password) < MIN_PASSWORD_LEN:
        _fail(
            f"TRACKER_CONSOLE_PASSWORD is under {MIN_PASSWORD_LEN} characters. "
            "That is short enough to be a typo rather than a secret."
        )
    return password


def _console_preflight(password: str | None) -> Path:
    """Everything that can be checked before a socket is opened. Returns the db path."""
    from tracker.webui import assets

    missing = assets.missing_vendor()
    if missing:
        _fail(
            "the console's vendored front-end files are missing:\n  "
            + "\n  ".join(missing)
            + f"\n\nExpected under {assets.STATIC_ROOT}. This is an incomplete "
            "install rather than a configuration problem."
        )

    path = _db_path()
    if not path.is_file():
        _fail(f"database not found: {path}\nRun `tracker init` first.")
    return path


def _explain_tunnel_failure(message: str, publish: str | None, used_proxy: bool) -> str:
    """Add the one thing the raw cloudflared log does not say.

    `context deadline exceeded` on the quick-tunnel API is not a bug in this
    project and not something a retry reliably fixes, so dumping the log and
    stopping leaves the operator with no move. What it usually means is that the
    request is slower than cloudflared's fixed budget — measured at 13-25s
    against roughly ten on a filtered link — and the two things that actually
    help are a proxy it will use and a named tunnel, which never calls that
    endpoint at all.
    """
    from tracker.webui.tunnel import detect_proxy

    if publish != "quick" or "deadline exceeded" not in message:
        return message

    hint = [
        "",
        "This is the quick-tunnel API being slower than cloudflared's own timeout,",
        "not a fault in the tunnel itself. Two things help:",
        "",
    ]
    proxy = detect_proxy()
    if not used_proxy:
        hint.append("  * drop --no-proxy: the API request can be routed through a proxy")
    elif proxy:
        hint.append(f"  * the proxy at {proxy} was already used and still timed out")
    else:
        hint.append("  * --proxy http://host:port, if you have one — cloudflared's own")
        hint.append("    client ignores HTTPS_PROXY for this request, so the console")
        hint.append("    relays it for you")
    hint += [
        "  * a named tunnel, which never calls that endpoint:",
        "      cloudflared tunnel login",
        "      cloudflared tunnel create dc-console",
        "      cloudflared tunnel route dns dc-console console.example.com",
        "      tracker cloudflare --name dc-console --hostname console.example.com",
    ]
    return message + "\n" + "\n".join(hint)


def _run_console(
    *,
    port: int,
    host: str,
    open_browser: bool,
    run: bool,
    allow_remote: bool = False,
    publish: str | None = None,
    tunnel_name: str | None = None,
    hostname: str | None = None,
    proxy: str | None = None,
    use_proxy: bool = True,
) -> None:
    """Start the console, optionally behind cloudflared. Shared by `serve` and `cloudflare`.

    One implementation rather than two, because the interesting part is the
    refusals — no password behind a public URL, a non-loopback bind without
    `--allow-remote` — and a second copy of those is a second place for one of
    them to quietly go missing.

    `publish` is None, "quick" or "named".
    """
    from tracker.webui.server import serve as run_server
    from tracker.webui.tunnel import (
        CloudflaredMissing,
        TunnelFailed,
        TunnelNotFound,
        named_tunnel,
        quick_tunnel,
    )

    password = _console_password()

    if host not in {"127.0.0.1", "localhost", "::1"} and not allow_remote:
        _fail(
            f"--host {host} would expose the command runner to the network.\n"
            "Anyone who can reach that address could start a run that spends LLM "
            "tokens and writes to the database. Pass --allow-remote if that is "
            "genuinely what you want."
        )

    # A tunnel is a public URL handed to anyone who learns it, in front of a
    # process that runs commands. Refusing rather than warning is the point: a
    # warning scrolls past, and there is no safe reading of "published and open".
    if publish and not password:
        _fail(
            "publishing this console puts it on a public URL, and it has no password.\n\n"
            "Set one first — in .env (gitignored) or the environment:\n"
            "  TRACKER_CONSOLE_PASSWORD=...\n\n"
            "Anything that reaches the URL can otherwise run `sync`, spend LLM "
            "tokens and write to the database."
        )

    path = _console_preflight(password)

    console.print(f"database: [bold]{path}[/bold]")
    console.print(f"local:    [bold]http://{host}:{port}/[/bold]")
    console.print(
        "[green]password protected[/green]"
        if password
        else "[yellow]no password[/yellow] — fine on loopback, never publish it like this"
    )
    if not run:
        console.print("[dim]read-only: the page cannot execute commands[/dim]")

    public = None
    if publish:
        try:
            public = (
                named_tunnel(port, tunnel_name or "", hostname=hostname)
                if publish == "named"
                else quick_tunnel(port, proxy=proxy, use_proxy=use_proxy)
            )
        except (CloudflaredMissing, TunnelFailed, TunnelNotFound, TimeoutError) as exc:
            _fail(_explain_tunnel_failure(str(exc), publish, use_proxy))

        if public.via_proxy:
            console.print(f"[dim]quick-tunnel API routed via {escape(public.via_proxy)}[/dim]")
        if public.url:
            console.print(f"public:   [bold]{public.url}[/bold]")
        else:
            # A named tunnel's hostname lives in your Cloudflare DNS, not in the
            # tunnel's output. Saying "unknown" is honest; inventing one is not.
            console.print(
                f"public:   [bold]tunnel {tunnel_name!r} is up[/bold] "
                "[dim]— at whichever hostname you routed to it. "
                "Pass --hostname to have it printed here.[/dim]"
            )
        if not public.confirmed:
            console.print(
                "[yellow]could not confirm the edge connection[/yellow] "
                "[dim]— cloudflared is still running and may well be fine; "
                "run with -v to read its log[/dim]"
            )
        if public.kind == "quick":
            console.print(
                "[yellow]that URL is reachable by anyone who has it.[/yellow] "
                "It stops working when this command does, and a new one is issued "
                "next time."
            )
        else:
            console.print(
                "[yellow]that hostname is reachable by anyone who has it[/yellow] "
                "while this command runs."
            )

    console.print("[dim]stop with Ctrl-C[/dim]")
    try:
        run_server(
            path,
            host=host,
            port=port,
            open_browser=open_browser and not publish,
            allow_write=run,
            password=password,
        )
    finally:
        if public is not None:
            public.stop()


@app.command()
def cloudflare(
    port: Annotated[int, typer.Option("--port", help="Local port the tunnel points at.")] = 8765,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help="Run this named tunnel instead of an anonymous one. Needs a Cloudflare account.",
            show_default=False,
        ),
    ] = None,
    hostname: Annotated[
        str | None,
        typer.Option(
            "--hostname",
            help="The hostname routed to --name, so it can be printed. Display only.",
            show_default=False,
        ),
    ] = None,
    quick: Annotated[
        bool,
        typer.Option(
            "--quick",
            help="Force an anonymous throwaway tunnel, ignoring the configured one.",
        ),
    ] = False,
    run: Annotated[
        bool,
        typer.Option(
            "--run/--no-run",
            help="Allow the page to execute commands. --no-run publishes it read-only.",
        ),
    ] = True,
    proxy: Annotated[
        str | None,
        typer.Option(
            "--proxy",
            help="Route the quick-tunnel API request through this proxy. Auto-detected.",
            show_default=False,
        ),
    ] = None,
    use_proxy: Annotated[
        bool,
        typer.Option("--proxy-api/--no-proxy", help="Use a proxy for the quick-tunnel API call."),
    ] = True,
    check: Annotated[
        bool,
        typer.Option("--check", help="Report whether publishing would work, then exit."),
    ] = False,
) -> None:
    """Publish the console on the internet through cloudflared.

    The console itself still binds loopback. cloudflared makes an *outbound*
    connection to Cloudflare and relays traffic back down it, so nothing is
    exposed on your network and no firewall or router changes are involved.

    Two shapes. A *named* tunnel is one you created once on your own account: the
    hostname is yours and survives a restart, which is what you want if the link
    is going to anybody else — re-sending a fresh URL every session is how one
    ends up written down somewhere it should not be. A *quick* tunnel is
    anonymous: a random `*.trycloudflare.com`, no account needed, and a different
    URL every time.

    Set `TRACKER_TUNNEL_NAME` and `TRACKER_TUNNEL_HOSTNAME` in `.env` and this
    needs no arguments. They are properties of the machine, not of the run — the
    credentials are already in your home directory and the DNS record is already
    in your zone — so retyping them on every publish is bookkeeping the
    environment can do. `--name` and `--hostname` override them, together;
    `--quick` ignores them and gets a throwaway URL.

    `TRACKER_CONSOLE_PASSWORD` is required either way and this refuses to start
    without it. The URL is public and the page can run commands that spend money
    and write to the database; a random hostname is obscurity, not access control.

    `--check` runs every test short of opening the tunnel and prints what it
    found, which is the cheap way to discover that cloudflared is a truncated
    download before you need the link to work.
    """
    from tracker.webui.tunnel import CloudflaredMissing, find_cloudflared

    if quick and (name or hostname):
        _fail("--quick means an anonymous tunnel; --name and --hostname describe a named one.")

    # The configured pair is taken together, and only when neither flag was
    # given. Filling a configured hostname in behind an explicitly different
    # --name would print a URL that does not point at the tunnel being run.
    settings = get_settings()
    if not quick and name is None and hostname is None:
        name, hostname = settings.tunnel_name, settings.tunnel_hostname

    if hostname and not name:
        _fail(
            "--hostname describes a named tunnel; pass --name as well, or drop both. "
            "(TRACKER_TUNNEL_HOSTNAME is set without TRACKER_TUNNEL_NAME.)"
            if settings.tunnel_hostname and not settings.tunnel_name
            else "--hostname describes a named tunnel; pass --name as well, or drop both."
        )

    if check:
        _cloudflare_check(name, hostname)
        return

    # Fail on a missing binary before the socket is opened, so the operator is not
    # told the console is up and then told it is not published.
    try:
        find_cloudflared()
    except CloudflaredMissing as exc:
        _fail(str(exc))

    _run_console(
        port=port,
        host="127.0.0.1",
        open_browser=False,
        run=run,
        publish="named" if name else "quick",
        tunnel_name=name,
        hostname=hostname,
        proxy=proxy,
        use_proxy=use_proxy,
    )


def _cloudflare_check(name: str | None, hostname: str | None = None) -> None:
    """Print a publish-readiness report and exit non-zero if it would fail."""
    from tracker.webui import assets
    from tracker.webui.auth import MIN_PASSWORD_LEN
    from tracker.webui.tunnel import (
        CloudflaredMissing,
        detect_proxy,
        find_cloudflared,
        named_tunnels,
        version,
    )

    rows: list[tuple[bool, str, str]] = []

    secret = get_settings().console_password
    password = secret.get_secret_value() if secret else None
    if not password:
        rows.append(
            (False, "password", "TRACKER_CONSOLE_PASSWORD is not set — required to publish")
        )
    elif len(password) < MIN_PASSWORD_LEN:
        rows.append((False, "password", f"set, but under {MIN_PASSWORD_LEN} characters"))
    else:
        rows.append((True, "password", f"set, {len(password)} characters"))

    try:
        binary = find_cloudflared()
        rows.append((True, "cloudflared", f"{version(binary)} at {binary}"))
        runnable = True
    except CloudflaredMissing as exc:
        rows.append((False, "cloudflared", str(exc).splitlines()[0]))
        runnable = False

    if runnable and name:
        available = named_tunnels()
        if name in available:
            rows.append((True, "tunnel", f"{name!r} exists on this account"))
        elif available:
            rows.append((False, "tunnel", f"no {name!r}; this account has {', '.join(available)}"))
        else:
            rows.append(
                (False, "tunnel", "no named tunnels — run `cloudflared tunnel login` first")
            )
        # Say where it will publish, and say that the DNS route is not checked
        # here. `cloudflared tunnel route dns` writes a CNAME this command cannot
        # see, so a hostname pointing at a tunnel that no longer exists reports
        # clean and then fails at the edge with a 1033.
        rows.append(
            (True, "hostname", f"https://{hostname} — DNS route not verified from here")
            if hostname
            else (
                True,
                "hostname",
                "not set; the tunnel's own config decides where it answers. "
                "Set TRACKER_TUNNEL_HOSTNAME to have the URL printed.",
            )
        )

    # Not pass/fail — no proxy is the ordinary case. It is reported because when
    # the quick tunnel does time out, this line is the first thing worth knowing.
    if not name:
        proxy = detect_proxy()
        rows.append(
            (
                True,
                "proxy",
                f"{proxy} — the quick-tunnel API call is relayed through it, because "
                "cloudflared's own client ignores HTTPS_PROXY"
                if proxy
                else "none configured; cloudflared will reach the API directly",
            )
        )

    path = _db_path()
    rows.append(
        (path.is_file(), "database", str(path) if path.is_file() else f"{path} does not exist")
    )
    missing = assets.missing_vendor()
    rows.append(
        (not missing, "front end", "vendored" if not missing else f"{len(missing)} file(s) missing")
    )

    for ok, label, detail in rows:
        mark = "[green]ok[/green]  " if ok else "[red]no[/red]  "
        console.print(f"{mark}[bold]{label:<12}[/bold]{escape(detail)}")

    if all(ok for ok, _, _ in rows):
        # Echo the shape that was actually checked. Printing the bare command
        # after a `--quick` check would suggest the anonymous tunnel is what runs
        # by default, which it is not once one is configured.
        invocation = "tracker cloudflare" if name else "tracker cloudflare --quick"
        console.print(f"\n[green]ready to publish[/green] — run `{invocation}`")
    else:
        _fail("not ready to publish; see above")


# --- ingest -----------------------------------------------------------------


@ingest_app.command("manual")
def ingest_manual(
    json_path: Annotated[Path, typer.Option("--json", help="Seed JSON file.", exists=True)],
    allow_placeholders: Annotated[
        bool,
        typer.Option(
            "--allow-placeholders",
            help="Ingest a file that still has PLACEHOLDER values (stored as NULL). Smoke tests only.",
        ),
    ] = False,
    lenient: Annotated[
        bool,
        typer.Option("--lenient", help="Skip invalid records instead of aborting the whole file."),
    ] = False,
    force_new: Annotated[
        bool,
        typer.Option("--force-new", help="Insert even when a possible duplicate is detected."),
    ] = False,
) -> None:
    """Load hand-curated projects from a JSON file."""
    from tracker.ingest import manual

    engine, _ = init_db(_db_path())
    try:
        with session_scope(engine) as session:
            report = manual.run(
                session,
                json_path,
                allow_placeholders=allow_placeholders,
                strict=not lenient,
                force_new=force_new,
            )
    except manual.ManualError as exc:
        _fail(str(exc))
        return
    _print_report(report, title=f"ingest manual: {json_path.name}")


@ingest_app.command("crawl")
def ingest_crawl(
    urls: Annotated[
        Path | None,
        typer.Option("--urls", help="File with one article URL per line; # comments allowed."),
    ] = None,
    url: Annotated[
        list[str] | None,
        typer.Option("--url", help="A single article URL. Repeatable."),
    ] = None,
    from_queue: Annotated[
        bool,
        typer.Option(
            "--from-queue", help="Crawl what `tracker discover` queued instead of a file."
        ),
    ] = False,
    stale_prompt: Annotated[
        bool,
        typer.Option(
            "--stale-prompt",
            help="Re-read sources extracted by an older version of the prompt.",
        ),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit", help="With --from-queue or --stale-prompt, take at most this many."
        ),
    ] = None,
    prompt_name: Annotated[
        str, typer.Option("--prompt", help="Prompt name or path, e.g. extract-v1.")
    ] = "extract-v1",
    check: Annotated[
        bool,
        typer.Option("--check", help="Test LLM connectivity and exit. Ingests nothing."),
    ] = False,
    browser: Annotated[
        bool,
        typer.Option(
            "--browser",
            help="Allow escalation to Crawl4AI for pages plain HTTP cannot read. Needs the 'crawl' extra.",
        ),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Re-process URLs a previous run completed.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report without writing.")] = False,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Ignore the on-disk article cache.")
    ] = False,
) -> None:
    """Extract projects from news articles with an LLM.

    Every non-null value must be backed by a verbatim quote that really appears in
    the fetched article; unsupported values are dropped and the drop is recorded
    in the project's notes.

    `--stale-prompt` re-reads articles this database has already read, using the
    prompt as it stands today. The gate has been tightened repeatedly and each
    improvement only ever applied to rows written after it landed — so a row
    extracted before migration `0007` has no per-field quote, not because nothing
    supported it but because there was nowhere to record the sentence. Those rows
    read as established ever since. Served from the article cache by default,
    which makes it a re-read rather than a re-fetch: `--no-cache` would confound
    "the prompt improved" with "the page changed".
    """
    from tracker.ingest import crawl
    from tracker.llm import DeepSeekExtractor, MissingApiKey

    settings = get_settings()

    if check:
        try:
            info = DeepSeekExtractor(settings).check()
        except MissingApiKey as exc:
            _fail(str(exc))
            return
        except Exception as exc:
            _fail(f"LLM check failed: {exc}")
            return
        for key, value in info.items():
            console.print(f"{key:18} {value}")
        console.print("[green]ok[/green]")
        return

    chosen = [
        name
        for name, given in (
            ("--urls", urls),
            ("--url", url),
            ("--from-queue", from_queue),
            ("--stale-prompt", stale_prompt),
        )
        if given
    ]
    if len(chosen) > 1:
        _fail(
            "pass only one of --urls, --url, --from-queue or --stale-prompt "
            f"(got {', '.join(chosen)})"
        )
        return
    if not chosen:
        _fail(
            "pass --urls FILE, --url URL, --from-queue, --stale-prompt, "
            "or --check to test connectivity"
        )
        return

    engine, _ = init_db(_db_path())
    source_label = "queue"

    if url:
        # Deduped like `read_urls` does, so passing the same link twice costs one
        # call rather than two.
        url_list = list(dict.fromkeys(u.strip() for u in url if u.strip()))
        bad = [u for u in url_list if not u.lower().startswith(("http://", "https://"))]
        if bad:
            _fail(f"not an http(s) URL: {bad[0]}")
            return
        source_label = url_list[0] if len(url_list) == 1 else f"{len(url_list)} URLs"
        # Same reasoning as the queue path below: a URL named explicitly is one
        # the operator wants read now, and the skip rule would otherwise make a
        # second attempt at an already-seen link do nothing at all.
        force = True
    elif from_queue:
        from tracker.ingest import discover as disc

        with session_scope(engine, commit=False) as session:
            url_list = [row.url for row in disc.pending(session, limit=limit)]
        if not url_list:
            console.print(
                "[green]queue is empty[/green] — run `tracker discover` to look for articles"
            )
            return
        # A queued URL is `discovered`, not `ok`, so crawl would process it
        # anyway; forcing makes that explicit and independent of the skip rule.
        force = True
    elif stale_prompt:
        from tracker.prompts import load_prompt

        stamp = load_prompt(prompt_name).stamp
        with session_scope(engine, commit=False) as session:
            url_list = crawl.stale_by_prompt(session, stamp=stamp, limit=limit)
        if not url_list:
            console.print(
                f"[green]every extracted source is on {stamp}[/green] — nothing to re-read"
            )
            return
        source_label = f"prompts superseded by {stamp}"
        # Every one of these is already `ok`, so the skip rule would drop all of
        # them. Re-reading an article we have already read, under a prompt that
        # has since been tightened, is the entire point of this selector.
        force = True
    else:
        if not urls.is_file():
            _fail(f"no such file: {urls}")
            return
        url_list = crawl.read_urls(urls)
        if not url_list:
            _fail(f"{urls} contains no http(s) URLs")
            return
        source_label = urls.name

    # Resolved before any fetching, so a missing key fails immediately rather
    # than after paying for a page load per URL.
    try:
        extractor = DeepSeekExtractor(settings)
    except MissingApiKey as exc:
        _fail(str(exc))
        return

    from tracker.ingest.fetch import Crawl4AIFetcher, MissingDependency, escalation_ladder

    if browser:
        # Fail on the flag, not twenty pages in. `__aenter__` holds the import,
        # so nothing before this point would have noticed the extra was absent.
        try:
            Crawl4AIFetcher.ensure_available()
        except MissingDependency as exc:
            _fail(str(exc))
            return
    # A ladder, cheapest rung first. curl_cffi needs no flag: it costs one
    # ordinary request and clears the WAFs that fingerprint TLS rather than
    # reading the User-Agent. Chromium stays behind --browser.
    escalate = escalation_ladder(settings, browser=browser)

    cache_dir = None if no_cache else install_root() / ".cache" / "articles"
    console.print(f"[dim]crawling {len(url_list)} URL(s) from {source_label}[/dim]")

    with session_scope(engine) as session:
        report = crawl.run(
            session,
            url_list,
            prompt_name=prompt_name,
            extractor=extractor,
            escalate=escalate,
            settings=settings,
            dry_run=dry_run,
            force=force,
            cache_dir=cache_dir,
        )

    _print_report(report, title=f"ingest crawl: {source_label}{' (dry run)' if dry_run else ''}")


@ingest_app.command("pjm")
def ingest_iso(
    csv_path: Annotated[
        Path, typer.Option("--csv", help="Queue export: CSV, XLSX or JSON.", exists=True)
    ],
    iso: Annotated[str, typer.Option("--iso", help="pjm | miso | ercot | caiso")] = "pjm",
    filter_mode: Annotated[
        str,
        typer.Option(
            "--filter",
            help=(
                'How to identify data centers: "heuristic" (name keywords, caps '
                'confidence at 1), "column:NAME=REGEX" for a real load-type column, '
                'or "none".'
            ),
        ),
    ] = "heuristic",
    trust_gen_mw: Annotated[
        bool,
        typer.Option(
            "--trust-gen-mw",
            help=(
                "Write the queue's generator nameplate MW to mw_planned. OFF by "
                "default because generator capacity is NOT data-center load."
            ),
        ),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report without writing.")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="Stop after N rows.")] = None,
    rejects_out: Annotated[
        Path | None, typer.Option("--rejects-out", help="Append rejected rows here as JSONL.")
    ] = None,
    map_override: Annotated[
        str | None,
        typer.Option("--map-override", help='JSON, e.g. \'{"gen_mw": ["Max Summer MW"]}\''),
    ] = None,
) -> None:
    """Load an ISO interconnection queue export.

    These are GENERATOR queues: they list proposed power plants and have no
    data-center column. Matching is therefore a keyword heuristic, confidence
    caps at 1, and queue MW is recorded as a disclosure rather than as the
    project's capacity. Use it for candidate generation and corroboration.

    `--csv` takes a file you download, and deliberately does not fetch one. Each
    ISO publishes behind a different report portal — a session cookie here, a
    subscription key there, an .aspx that returns HTML to anything without a
    browser — so a built-in fetcher would be four scrapers to maintain against
    four sites that change. Downloading it is a two-minute job done a few times a
    year:

      PJM     https://www.pjm.com/planning/service-requests/services-request-status
      MISO    https://www.misoenergy.org/planning/resource-utilization/GI_Queue/
      ERCOT   https://www.ercot.com/gridinfo/resource  (Generation Interconnection
              Status report; the Large Load queue is the one to want)
      CAISO   https://www.caiso.com/generation-transmission/interconnection

    Save the export anywhere and point `--csv` at it. XLSX and JSON work too.
    """
    import json as _json

    from tracker.ingest import pjm as iso_ingest

    overrides = None
    if map_override:
        try:
            overrides = _json.loads(map_override)
        except _json.JSONDecodeError as exc:
            _fail(f"--map-override is not valid JSON: {exc}")

    engine, _ = init_db(_db_path())
    try:
        with session_scope(engine) as session:
            report = iso_ingest.run(
                session,
                csv_path,
                iso=iso,
                filter_mode=filter_mode,
                trust_gen_mw=trust_gen_mw,
                dry_run=dry_run,
                limit=limit,
                rejects_out=rejects_out,
                map_override=overrides,
            )
    except KeyError as exc:
        _fail(str(exc).strip("\"'"))
        return
    except (iso_ingest.HeaderError, iso_ingest.IsoIngestError) as exc:
        _fail(str(exc))
        return

    _print_report(report, title=f"ingest {iso}: {csv_path.name}{' (dry run)' if dry_run else ''}")
    if not trust_gen_mw:
        console.print(
            "[dim]note: queue MW is generator nameplate, not data-center load, so "
            "mw_planned was left unset. See each project's notes.[/dim]"
        )


# --- queries ----------------------------------------------------------------


def _filtered(stmt, company, state, phase, min_confidence, risk=None, severity=None):
    if company:
        stmt = stmt.where(func.lower(Project.company).like(f"%{company.lower()}%"))
    if state:
        stmt = stmt.where(Project.state == state.upper())
    if phase:
        stmt = stmt.where(Project.phase == phase)
    if min_confidence is not None:
        stmt = stmt.where(Project.confidence >= min_confidence)
    if risk or severity:
        # EXISTS rather than a join, so a project with three matching risks is one
        # row rather than three. Restricted to open risks: filtering on an obstacle
        # that has been resolved would answer a question nobody asked.
        sub = select(Risk.id).where(Risk.project_id == Project.id, Risk.status == OPEN_RISK_STATUS)
        if risk:
            sub = sub.where(Risk.category == risk)
        if severity:
            sub = sub.where(Risk.severity == severity)
        stmt = stmt.where(sub.exists())
    return stmt


def _location(project: Project) -> str:
    """City if known, else the county with its granularity made visible."""
    if project.city:
        return f"{project.city}, {project.state}"
    if project.county:
        return f"{project.county}, {project.state}"
    return project.state


def _fmt_mw(value: float | None) -> str:
    return NA if value is None else f"{value:,.0f}"


def _fmt_usd(value: int | None) -> str:
    if value is None:
        return NA
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    return f"${value:,}"


@app.command("list")
def list_projects(
    company: Annotated[str | None, typer.Option("--company", help="Substring match.")] = None,
    state: Annotated[str | None, typer.Option("--state", help="2-letter code.")] = None,
    phase: Annotated[
        str | None, typer.Option("--phase", help=f"One of: {', '.join(PHASES)}")
    ] = None,
    min_confidence: Annotated[int | None, typer.Option("--min-confidence", min=0, max=3)] = None,
    risk: Annotated[
        str | None,
        typer.Option("--risk", help=f"Open risk category. One of: {', '.join(RISK_CATEGORIES)}"),
    ] = None,
    severity: Annotated[
        str | None,
        typer.Option(
            "--severity", help=f"Open risk severity. One of: {', '.join(RISK_SEVERITIES)}"
        ),
    ] = None,
    sort: Annotated[
        str, typer.Option("--sort", help="mw | investment | date | confidence | name")
    ] = "mw",
    limit: Annotated[
        int | None, typer.Option("--limit", help="Show at most this many rows.")
    ] = None,
) -> None:
    """List projects as a table."""
    if phase and phase not in PHASES:
        _fail(f"--phase must be one of: {', '.join(PHASES)}")
    if risk and risk not in RISK_CATEGORIES:
        _fail(f"--risk must be one of: {', '.join(RISK_CATEGORIES)}")
    if severity and severity not in RISK_SEVERITIES:
        _fail(f"--severity must be one of: {', '.join(RISK_SEVERITIES)}")

    order = {
        "mw": (Project.mw_planned.desc().nullslast(),),
        "investment": (Project.investment_usd.desc().nullslast(),),
        "date": (Project.first_announced.desc().nullslast(),),
        "confidence": (Project.confidence.desc(),),
        "name": (Project.company.asc(), Project.name.asc()),
    }.get(sort)
    if order is None:
        _fail("--sort must be one of: mw, investment, date, confidence, name")

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        stmt = _filtered(select(Project), company, state, phase, min_confidence, risk, severity)
        total = session.scalar(
            select(func.count()).select_from(
                _filtered(
                    select(Project.id), company, state, phase, min_confidence, risk, severity
                ).subquery()
            )
        )
        stmt = stmt.order_by(*order, Project.id.asc())
        if limit:
            stmt = stmt.limit(limit)
        projects = session.scalars(stmt).all()

        if json_mode():
            # The same per-project shape `tracker export json` emits, so a consumer
            # does not have to learn two schemas for the same object.
            from tracker.export import to_json_object

            emit(
                {
                    "count": len(projects),
                    "total": total,
                    "projects": [to_json_object(p) for p in projects],
                }
            )
            return

        if not projects:
            console.print("[yellow]no projects match[/yellow]")
            return

        shown = (
            f"{len(projects)} of {total} project(s)"
            if total > len(projects)
            else f"{total} project(s)"
        )
        table = Table(title=shown, header_style="bold", box=TABLE_BOX)
        table.add_column("id", justify="right")
        table.add_column("company")
        table.add_column("name")
        table.add_column("location")
        table.add_column("phase")
        table.add_column("MW", justify="right")
        table.add_column("investment", justify="right")
        table.add_column("conf", justify="center")
        table.add_column("src", justify="right")

        for p in projects:
            table.add_row(
                str(p.id),
                escape(p.company),
                escape(p.name),
                escape(_location(p)),
                p.phase,
                _fmt_mw(p.mw_planned),
                _fmt_usd(p.investment_usd),
                _confidence_cell(p.confidence),
                str(len(p.sources)),
            )
        console.print(table)


def _confidence_cell(value: int) -> str:
    colour = {0: "red", 1: "yellow", 2: "green", 3: "bold green"}.get(value, "white")
    return f"[{colour}]{value}[/{colour}]"


def _severity_style(severity: str) -> str:
    return {"watch": "yellow", "material": "bright_red", "blocking": "bold red"}.get(
        severity, "white"
    )


#: What an uncited obstacle needs, in the words of the work it implies. Naming
#: the reason is the whole point of storing one: "nobody quoted it" sends you
#: looking for another source, while "the sentence does not say that" sends you
#: to correct the category on a source you already have.
_UNCITED_BECAUSE = {
    "no_quote": "uncited — the source named it but quoted nothing; confirm in `tracker review`",
    "quote_unverified": (
        "uncited — the quote offered for it is not in the article; confirm in `tracker review`"
    ),
    "quote_off_target": (
        "uncited — the quoted sentence does not state this category; "
        "recategorise in `tracker review`"
    ),
}


def _why_uncited(risk_row) -> str:
    """The line under an obstacle with no quote behind it."""
    return _UNCITED_BECAUSE.get(risk_row.unconfirmed or "", "uncited — confirm in `tracker review`")


def _open_risk_count(project: Project) -> int:
    return sum(1 for r in project.risks if r.status == OPEN_RISK_STATUS)


def _ordered_risks(risks) -> list:
    """Most severe first, then open before settled, then by category.

    Stable and content-based rather than by id, so the same data renders the same
    way whatever order the rows happened to be written in.
    """
    return sorted(
        risks,
        key=lambda r: (
            -severity_rank(r.severity),
            r.status != OPEN_RISK_STATUS,
            r.category,
            str(r.first_seen or ""),
        ),
    )


def _print_standing(project) -> None:
    """Per-track position, the binding blocker, and what to watch for.

    This is the PRD's central ask — 判断一个项目究竟走到了哪一步 — which a single
    `phase` enum cannot answer. Nothing here is stored: it is derived from the
    project's own dated, cited events and open risks, so it cannot disagree with
    the evidence.
    """
    from tracker.tracks import TRACK_LABELS, standing

    stand = standing(project.id, project.events, project.risks)
    table = Table(title="where it stands", header_style="bold", box=TABLE_BOX, title_justify="left")
    table.add_column("track")
    table.add_column("reached")
    table.add_column("blocked by")
    for state in stand.tracks:
        if state.complete:
            reached = "[green]complete[/green]"
        elif state.reached:
            reached = f"[yellow]{state.status}[/yellow]"
        else:
            reached = f"[dim]{state.status}[/dim]"
        if state.only_implied:
            # Deduced from a later milestone, not read anywhere. A built site must
            # hold its land and permits; saying so beats printing "unknown", but a
            # deduction is not a citation and must not look like one.
            reached += " [dim](implied)[/dim]"
        blocked = ", ".join(state.blockers)
        if blocked and state.blocker_severity:
            blocked += f" [dim]({state.blocker_severity})[/dim]"
        table.add_row(TRACK_LABELS[state.track], reached, blocked or NA)
    console.print()
    console.print(table)

    binding = stand.binding_blocker
    if binding:
        console.print(
            f"binding constraint: [red]{TRACK_LABELS[binding.track]}[/red] — "
            f"{', '.join(binding.blockers)}"
        )
    if stand.watch_for:
        # The PRD's final question, in `tracker.tracks`: what signal proves the
        # project is still advancing.
        console.print(f"[bold]watch for:[/bold] {stand.watch_for}")


#: Block statuses coloured by how far along they are, so a mixed campus reads at a
#: glance. `serving` and `energized` are the two that mean megawatts are delivering.
_BLOCK_STYLE: dict[str, str] = {
    "serving": "green",
    "energized": "green",
    "shell_complete": "yellow",
    "under_construction": "yellow",
    "permitting": "cyan",
    "planned": "dim",
    "paused": "red",
    "cancelled": "red",
}


def _print_blocks(project: Project) -> None:
    """The campus broken into the tranches that have their own state.

    The reason this table exists: the rows above it can only say one phase, one
    capacity, one customer. This is where a campus gets to say it is 100 MW serving
    one buyer beside 150 MW still going up for another — which is what most of these
    projects actually are.

    A capacity no quote confirmed is marked 待确认 *and* stated to be outside the
    campus total, because those are two different facts and a reader who sees only
    the first will assume the number is in `MW planned`.
    """
    blocks = list(getattr(project, "blocks", ()) or ())
    if not blocks:
        return

    from tracker import blocks as blocks_mod

    got = blocks_mod.rollup(blocks)
    table = Table(
        title=f"capacity blocks ({len(blocks)})",
        header_style="bold",
        box=TABLE_BOX,
        title_justify="left",
    )
    table.add_column("block")
    table.add_column("MW", justify="right")
    table.add_column("status")
    table.add_column("customer")
    table.add_column("online")

    for block in sorted(blocks, key=lambda b: b.block_key):
        style = _BLOCK_STYLE.get(block.status, "")
        status = f"[{style}]{block.status}[/{style}]" if style else block.status
        # The hedge the block's own quote put on this figure. A block carries no
        # `claim_meta`, so there is no stored axis to read — but it does store the
        # verbatim sentence, and "Each exceeds 350 MW" is a floor whether or not
        # anything recorded that at ingest time.
        from tracker.export import _mw_bound

        mw = with_bound(_fmt_mw(block.mw), _mw_bound(block))
        if block.mw is not None and not blocks_mod.mw_is_confirmed(block):
            mw = f"[red]{mw} 待确认[/red]"
        when = block.energized_on or block.expected_online
        label = escape(block.label)
        if block.parent:
            label = f"{escape(block.parent)} / {label}"
        table.add_row(label, mw, status, escape(block.customer or NA), str(when or NA))

    # The residual lines, then the total. Without these the tranches visibly fail to
    # add up to `MW planned` — measured on 70 of 118 itemised projects — and a reader
    # who cannot make the arithmetic work stops trusting the rest of the row too.
    account = blocks_mod.account(project)
    if account.residuals:
        table.add_section()
        for residual in account.residuals:
            table.add_row(
                f"[yellow]{residual.reason}[/yellow]",
                f"[yellow]{_fmt_mw(residual.mw)}[/yellow]",
                f"[dim]{escape(residual.note)}[/dim]",
                "",
                "",
            )
    if account.total is not None:
        table.add_section()
        caveat = (
            " [dim](floor — no source states a campus total)[/dim]"
            if (account.total_is_floor)
            else ""
        )
        table.add_row(
            "[bold]accounted for[/bold]",
            f"[bold]{_fmt_mw(account.total)}[/bold]",
            caveat,
            "",
            "",
        )

    console.print()
    console.print(table)

    for note in blocks_mod.reconcile_notes(got):
        console.print(f"  [dim]{escape(note)}[/dim]")


def _print_itemisation(project: Project) -> None:
    """Why a campus shows no tranches. Printed only when it shows none.

    A bare row and an unread row looked identical, so 88 bare rows beside 118
    detailed ones read as uneven research. Most of those campuses really are one
    undivided thing, and saying so is the difference between a gap and an answer.
    """
    if getattr(project, "blocks", None):
        return

    from tracker import blocks as blocks_mod

    state = blocks_mod.itemisation(project)
    console.print(
        f"\n[bold]capacity blocks[/bold] [dim]— {blocks_mod.ITEMISATION_NOTES[state]}[/dim]"
    )


#: How a hedged quantity reads. The article either qualified the number or it did
#: not, and until migration 0015 there was nowhere to record which — prompt RULE 4
#: said `"500-700 MW" -> 500 (the LOWER bound; say so in "notes")`, so the hedge
#: went into prose nothing could read back.
#:
#: One glyph rather than a column. These are qualifiers on a number, not facts of
#: their own, and a `bound` column would be empty on most rows and would push the
#: figures out of alignment on the rest.
#: `at_least` is a SUFFIX — "350+" rather than "≥350" — because that is how a
#: reader outside this codebase writes "or more", and the floor is the case that
#: matters most: Fairwater's 350 MW rests on "Each exceeds 350 MW". The console
#: uses the same two tables, so the two surfaces cannot drift apart.
_BOUND_PREFIX: dict[str, str] = {"approximate": "~", "at_most": "≤"}
_BOUND_SUFFIX: dict[str, str] = {"at_least": "+"}


def with_bound(rendered: str, bound: str | None) -> str:
    """A rendered quantity carrying the hedge its own source used."""
    if not rendered or rendered == NA or not bound or bound == "exact":
        return rendered
    return f"{_BOUND_PREFIX.get(bound, '')}{rendered}{_BOUND_SUFFIX.get(bound, '')}"


#: A date stated to a year rendered as `2024-01-01` asserts a precision the
#: article never gave. `normalize.parse_date` has always known the difference.
_DATE_FORMAT: dict[str, str] = {"year": "%Y", "half": "%Y", "quarter": "%Y", "month": "%Y-%m"}

#: Suffix naming the bucket, where dropping the digits alone would lose it.
_DATE_SUFFIX: dict[str, str] = {"half": " (H1/H2)", "quarter": " (quarter)"}


def _qualified(project, field: str, rendered: str) -> str:
    """A quantity carrying the hedge its own source used, if any.

    The stored axis wins, and where it says `exact` the quote is read directly.
    That fallback is doing most of the work today for two reasons: the `bound`
    axis reached only 32% of claims, and `exceeds` — the commonest hedge in this
    corpus, and the one under Fairwater's own `mw_built` — was missing from the
    marker list until it moved into `vocab`. Rows extracted before that say
    `exact` and will keep saying it until they are re-read, so a display that
    trusted the axis alone would report "Each exceeds 350 MW" as a point value.
    """
    from tracker.gaps import provenance
    from tracker.vocab import bound_from_quote

    prov = provenance(project, field)
    if prov is None:
        return rendered
    bound = prov.bound
    if bound == "exact" and prov.quote:
        bound = bound_from_quote(prov.quote, getattr(project, field, None))
    return with_bound(rendered, bound)


def _fmt_date(project, field: str) -> str:
    """A date at the precision the source actually offered.

    "Q3 2025" and "2025-07-01" are stored identically and mean very different
    things; the row used to print the second whatever the article said. This is
    the rare display change that makes the output *shorter* and more honest at
    once — a year-precision date renders as `2024`, four characters instead of
    ten, and stops claiming a day nobody published.
    """
    value = getattr(project, field, None)
    if value is None:
        return NA
    precision = getattr(project, f"{field}_precision", None)
    fmt = _DATE_FORMAT.get(precision or "")
    if not fmt:
        return str(value)
    return f"{value.strftime(fmt)}{_DATE_SUFFIX.get(precision, '')}"


@app.command()
def show(project_id: Annotated[int, typer.Argument(help="Project id.")]) -> None:
    """Show one project in full, with every citation."""
    from tracker.confidence import compute_for_project

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        project = session.get(Project, project_id)
        if project is None:
            _fail(f"no project with id {project_id}", code=1)
            return

        if json_mode():
            from tracker.export import to_json_object

            emit(to_json_object(project))
            return

        facts = Table(show_header=False, box=None)
        facts.add_column(style="dim")
        facts.add_column()

        def cell(field: str, rendered: object) -> str:
            """Mark a value the PRD would call 待确认.

            Red because the tier has to be impossible to miss: an unconfirmed value
            is one the model produced and no quote in any fetched article supports.
            It is shown rather than deleted — deleting cost 194 values across 92 of
            124 projects — but a reader must never mistake it for a fact.

            `defaulted` is deliberately quieter than 待确认 and says something
            different: nobody claimed anything, and the NOT NULL column is showing
            its schema default. Printing that in red as "待确认" asserted that a
            source had offered "announced" and failed to prove it, which was untrue
            of every project whose phase no article mentions.
            """
            # Escaped here rather than at `add_row`, because the strings this
            # returns are markup by design and escaping them afterwards would
            # print the `[dim]` tags literally. A project name really can carry a
            # bracket — "Stargate (Phase [2])" — and Rich would eat it.
            rendered = _qualified(project, field, escape(str(rendered)))
            tier = basis(project, field)
            if tier == UNCONFIRMED:
                return f"[red]{rendered}[/red] [red]待确认[/red]"
            if tier == DEFAULTED:
                return f"[dim]{rendered}[/dim] [dim](default — no source states it)[/dim]"
            if tier == DERIVED:
                return f"{rendered} [dim](derived)[/dim]"
            if tier == INFERRED:
                return f"[magenta]{rendered}[/magenta] [magenta](inferred)[/magenta]"
            return rendered

        for label, value in [
            ("name", cell("name", project.name)),
            ("company", cell("company", project.company)),
            ("customer", cell("customer", project.customer or NA)),
            ("location", escape(_location(project))),
            ("county", cell("county", project.county or NA)),
            (
                "coordinates",
                cell("lat", f"{project.lat}, {project.lon}") if project.lat else NA,
            ),
            ("phase", cell("phase", project.phase)),
            ("MW planned", cell("mw_planned", _fmt_mw(project.mw_planned))),
            ("MW built", cell("mw_built", _fmt_mw(project.mw_built))),
            ("H200-equiv", _h200_cell(project)),
            ("investment", cell("investment_usd", _fmt_usd(project.investment_usd))),
            ("first announced", cell("first_announced", _fmt_date(project, "first_announced"))),
            ("expected online", cell("expected_online", _fmt_date(project, "expected_online"))),
            ("blocker", cell("blocker", project.blocker or NA)),
            ("open risks", str(_open_risk_count(project) or NA)),
            ("confidence", _confidence_cell(project.confidence)),
            ("created", str(project.created_at)),
            ("updated", str(project.updated_at)),
            ("last verified", str(project.last_verified_at or "never")),
            ("dedup key", escape(project.dedup_key)),
        ]:
            facts.add_row(label, str(value))
        console.print(facts)

        score = compute_for_project(project, project.sources)
        console.print(f"\n[dim]why confidence {score.value}:[/dim] {'; '.join(score.reasons)}")

        _print_standing(project)
        # Above the sources on purpose: on a partly-built campus this table is the
        # answer to "what state is it in", and the scalars above it are a summary of
        # this rather than the other way round.
        _print_blocks(project)
        _print_itemisation(project)

        if project.notes:
            console.print("\n[bold]notes[/bold]")
            console.print(project.notes)

        console.print(f"\n[bold]sources[/bold] ({len(project.sources)})")
        for s in sorted(project.sources, key=lambda x: x.url):
            console.print(f"  [cyan]{s.source_type}[/cyan]  {s.url}")
            console.print(f"    fetched {s.fetched_at}  supports: {s.fields or NA}")
            if s.excerpt:
                console.print(f'    "{s.excerpt}"', style="dim")
            if s.extractor:
                console.print(f"    via {s.extractor}", style="dim")

        if project.risks:
            console.print(f"\n[bold]risks[/bold] ({len(project.risks)})")
            for r in _ordered_risks(project.risks):
                state = "" if r.status == OPEN_RISK_STATUS else f" [dim]({r.status})[/dim]"
                dates = str(r.first_seen or NA)
                if r.resolved_at:
                    dates += f" → {r.resolved_at}"
                delay = f"  [red]+{r.delay_days}d[/red]" if r.delay_days else ""
                console.print(
                    f"  [cyan]{r.category}[/cyan] [{_severity_style(r.severity)}]"
                    f"{r.severity}[/{_severity_style(r.severity)}]{state}  {dates}{delay}"
                )
                console.print(f"    {escape(r.summary)}")
                # The quote, not the summary, is the evidence: the summary is
                # allowed to be a paraphrase and the quote is verified verbatim.
                if r.quote:
                    console.print(f'    "{escape(r.quote)}"', style="dim")
                else:
                    console.print("    [yellow]uncited[/yellow]", style="dim")

        if project.events:
            console.print(f"\n[bold]events[/bold] ({len(project.events)})")
            for e in sorted(project.events, key=lambda x: x.event_date):
                console.print(f"  {e.event_date}  [cyan]{e.event_type}[/cyan]  {e.description}")


@risks_app.callback(invoke_without_command=True)
def risks(
    ctx: typer.Context,
    category: Annotated[
        str | None, typer.Option("--category", help=f"One of: {', '.join(RISK_CATEGORIES)}")
    ] = None,
    severity: Annotated[
        str | None, typer.Option("--severity", help=f"One of: {', '.join(RISK_SEVERITIES)}")
    ] = None,
    state: Annotated[str | None, typer.Option("--state", help="2-letter code.")] = None,
    uncited: Annotated[
        bool,
        typer.Option("--uncited", help="Only the 待确认 ones — the obstacles nobody could quote."),
    ] = False,
    all_statuses: Annotated[
        bool, typer.Option("--all", help="Include resolved and superseded risks.")
    ] = False,
    detail: Annotated[
        bool,
        typer.Option("--detail/--summary", help="Per-obstacle listing under each kind."),
    ] = True,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit", help="Obstacles to list, across all kinds. The table is unaffected."
        ),
    ] = None,
) -> None:
    """Obstacles across the database, grouped by kind, with the MW behind each.

    Bare `tracker risks` is the listing. The one subcommand, `confirm`, is what
    answers the uncomfortable line at the bottom of it: a third of these obstacles
    rest on no quote that stands up, and `confirm` reads the article behind each
    one and settles it.

    This is the query the single `blocker` column could not answer: one sentence
    per project cannot be counted, and counting is what carries the read-through
    to chip, cloud and power companies.

    **The evidence is a column now, not a footnote.** The old layout printed every
    obstacle at the same weight and then admitted at the bottom that a third of
    them rested on nothing quotable — which is the wrong way round, because
    whether an obstacle is quoted is the first thing a reader needs and the last
    thing they were told. The kinds table carries it per category, each obstacle
    is marked in place, and `--uncited` shows only those. `tracker risks confirm`
    is the command that reads their articles and settles them.
    """
    if ctx.invoked_subcommand is not None:
        return
    if category and category not in RISK_CATEGORIES:
        _fail(f"--category must be one of: {', '.join(RISK_CATEGORIES)}")
    if severity and severity not in RISK_SEVERITIES:
        _fail(f"--severity must be one of: {', '.join(RISK_SEVERITIES)}")

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        stmt = select(Risk, Project).join(Project, Risk.project_id == Project.id)
        if not all_statuses:
            stmt = stmt.where(Risk.status == OPEN_RISK_STATUS)
        if category:
            stmt = stmt.where(Risk.category == category)
        if severity:
            stmt = stmt.where(Risk.severity == severity)
        if state:
            stmt = stmt.where(Project.state == state.upper())
        if uncited:
            stmt = stmt.where(Risk.unconfirmed.is_not(None))
        rows = _dedupe_risks(session.execute(stmt).all())

        if not rows:
            scope = "" if all_statuses else "open "
            console.print(f"[green]no {scope}risks match[/green]")
            return

        by_category: dict[str, list] = {}
        for risk_row, project in rows:
            by_category.setdefault(risk_row.category, []).append((risk_row, project))
        order = sorted(by_category, key=lambda c: (-len(by_category[c]), c))

        if json_mode():
            emit(
                {
                    "risks": [
                        {
                            "id": r.id,
                            "project_id": p.id,
                            "project": f"{p.company} — {p.name}",
                            "category": r.category,
                            "severity": r.severity,
                            "status": r.status,
                            "mw_planned": p.mw_planned,
                            "summary": r.summary,
                            "quote": r.quote,
                            "unconfirmed": r.unconfirmed,
                        }
                        for r, p in rows
                    ]
                }
            )
            return

        _print_risk_kinds(by_category, order)
        if detail:
            _print_risk_detail(by_category, order, limit=limit)
        _print_risk_footer(rows)


def _dedupe_risks(rows: list) -> list:
    """One obstacle per (project, category, sentence).

    The unique constraint on `risk` includes `first_seen`, so two crawls of two
    articles reporting the same concern on the same day-but-one store it twice.
    Measured on the live database that was 20 rows, and each appeared as its own
    line here and as its own question in `tracker logic resolve`.
    """
    seen: set[tuple[int, str, str]] = set()
    out = []
    for risk_row, project in rows:
        key = (project.id, risk_row.category, " ".join((risk_row.summary or "").lower().split()))
        if key in seen:
            continue
        seen.add(key)
        out.append((risk_row, project))
    return out


def _print_risk_kinds(by_category: dict[str, list], order: list[str]) -> None:
    """The table that answers "what is stopping these projects", in one screen."""
    table = Table(
        title="obstacles by kind",
        header_style="bold",
        box=TABLE_BOX,
        title_justify="left",
    )
    table.add_column("kind")
    table.add_column("projects", justify="right")
    table.add_column("capacity", justify="right")
    table.add_column("blocking", justify="right")
    table.add_column("material", justify="right")
    table.add_column("watch", justify="right")
    table.add_column("quoted", justify="right")
    for cat in order:
        entries = by_category[cat]
        mw = sum(p.mw_planned or 0.0 for _, p in entries)
        counts = {s: sum(1 for r, _ in entries if r.severity == s) for s in RISK_SEVERITIES}
        quoted = sum(1 for r, _ in entries if not r.unconfirmed)
        quoted_cell = f"{quoted}/{len(entries)}"
        table.add_row(
            f"[cyan]{cat}[/cyan]",
            str(len(entries)),
            _fmt_mw(mw) if mw else "—",
            f"[bold red]{counts['blocking']}[/bold red]" if counts["blocking"] else "—",
            f"[bright_red]{counts['material']}[/bright_red]" if counts["material"] else "—",
            f"[yellow]{counts['watch']}[/yellow]" if counts["watch"] else "—",
            quoted_cell if quoted == len(entries) else f"[yellow]{quoted_cell}[/yellow]",
        )
    console.print(table)
    console.print()


def _print_risk_detail(
    by_category: dict[str, list], order: list[str], *, limit: int | None
) -> None:
    """Each obstacle under its kind, with its evidence directly beneath it."""
    shown = 0
    for cat in order:
        entries = sorted(
            by_category[cat],
            key=lambda pair: (
                -severity_rank(pair[0].severity),
                -(pair[1].mw_planned or 0.0),
                pair[1].id,
            ),
        )
        mw = sum(p.mw_planned or 0.0 for _, p in entries)
        unknown_mw = sum(1 for _, p in entries if p.mw_planned is None)
        heading = f"[bold cyan]{cat}[/bold cyan]  {len(entries)} project(s)"
        if mw:
            heading += f", {_fmt_mw(mw)} MW"
        if unknown_mw:
            heading += f" [dim](+{unknown_mw} with no cited capacity)[/dim]"
        console.print(heading)

        for risk_row, project in entries:
            if limit is not None and shown >= limit:
                break
            style = _severity_style(risk_row.severity)
            capacity = f"{_fmt_mw(project.mw_planned)} MW" if project.mw_planned else "no capacity"
            console.print(
                f"  [{style}]{risk_row.severity:<8}[/{style}] [bold]#{project.id}[/bold] "
                f"{escape(project.company)} — {escape(project.name)} "
                f"[dim]({escape(_location(project))} · {capacity})[/dim]"
            )
            console.print(f"      {escape(risk_row.summary)}")
            if risk_row.quote:
                console.print(f'      [dim]"{escape(risk_row.quote)}"[/dim]')
            else:
                console.print(f"      [yellow]待确认[/yellow] [dim]{_why_uncited(risk_row)}[/dim]")
            shown += 1
        console.print()
        if limit is not None and shown >= limit:
            console.print(f"[dim]stopped at --limit {limit}[/dim]")
            break


def _print_risk_footer(rows: list) -> None:
    console.print(
        "[dim]MW sums cover only projects whose capacity is cited; they are a "
        "floor, not a total.[/dim]"
    )
    # Counted, and said so. An unconfirmed obstacle is still an obstacle a
    # source reported, and leaving it out of the sums would understate exposure
    # in the one direction that matters — but a total nobody can see the
    # composition of is the thing this database exists not to produce.
    vague = sum(1 for risk_row, _ in rows if risk_row.unconfirmed)
    if not vague:
        return
    reasons: dict[str, int] = {}
    for risk_row, _ in rows:
        if risk_row.unconfirmed:
            reasons[risk_row.unconfirmed] = reasons.get(risk_row.unconfirmed, 0) + 1
    console.print(
        f"[yellow]{vague} of {len(rows)}[/yellow] [dim]are 待确认 — reported by a source with "
        "no quote that stands up, and counted above anyway.[/dim]"
    )
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        console.print(f"  [dim]{count:>4}  {escape(_UNCITED_BECAUSE.get(reason, reason))}[/dim]")
    console.print("\n[dim]settle them by reading the articles again:[/dim] tracker risks confirm")


@risks_app.command("confirm")
def risks_confirm(
    project_id: Annotated[
        int | None, typer.Option("--project", help="Only this project.", show_default=False)
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", help="Only this kind.", show_default=False)
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Obstacles to read. One call each.")] = 20,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Judge at full cost and write nothing.")
    ] = False,
) -> None:
    """Read the article behind each unquoted obstacle and settle it.

    `tracker risks` reports that a third of the open obstacles are 待确认 —
    a source named them and the evidence gate could not find a sentence that says
    so — and counts them anyway, because a reported obstacle is still an obstacle.
    Nobody has ever gone back to check which reading is right. This does.

    **One model call per obstacle, with the whole article.** Not the excerpt: the
    excerpt is the fragment the extraction that already failed chose, so re-reading
    it would mostly reproduce the first answer. The article comes from the crawl
    cache, and the project, the obstacle, and every other obstacle on the row go
    with it — half these rows are a real sentence filed under the wrong category,
    and that is invisible without seeing what the other categories already claim.

    **The model's quote is checked before it is believed**, by the same matcher and
    the same category test the extraction path's evidence gate uses. A paraphrase
    is refused. A real sentence about the wrong thing is refused. The obstacle then
    stays exactly as it was, which is the state it is already in — so the worst
    case of running this is that it cost a call and changed nothing.

    Three outcomes. **confirmed** attaches the quote and clears the 待确认 mark.
    **refuted** marks the obstacle `superseded`, dropping it out of the open counts
    without deleting the record of having believed it. **unclear** writes nothing,
    and is the honest majority answer.
    """
    from tracker import riskcheck

    path = _db_path()
    if not path.is_file():
        _fail(f"database not found: {path}\nRun `tracker init` first.")
    if category and category not in RISK_CATEGORIES:
        _fail(f"--category must be one of: {', '.join(RISK_CATEGORIES)}")

    from tracker.llm import MissingApiKey, reasoning_extractor

    try:
        extractor = reasoning_extractor(get_settings())
    except MissingApiKey as exc:
        _fail(str(exc))
        raise

    engine = _read_engine() if dry_run else _writable("risks confirm")

    cache_dir = install_root() / ".cache" / "articles"

    def announce(outcome) -> None:
        if json_mode():
            return
        style = {
            "confirmed": "green",
            "refuted": "red",
            "unclear": "dim",
            "no_article": "yellow",
            "error": "yellow",
        }.get(outcome.result, "white")
        console.print(
            f"  [{style}]{outcome.result:<10}[/{style}] #{outcome.project_id} "
            f"{escape(outcome.category)} [dim]{escape(outcome.summary[:64])}[/dim]"
        )
        reason = outcome.judgement.reason
        refused = ""
        # The refusal is appended to the reason, and the reason is the long part —
        # clipping the line threw away the only sentence that says *why* an
        # obstacle the model was sure about stayed unevidenced.
        if " — refused: " in reason:
            reason, refused = reason.split(" — refused: ", 1)
        if reason:
            console.print(f"      [dim]{escape(reason[:170])}[/dim]")
        if refused:
            console.print(f"      [yellow]quote refused[/yellow] [dim]— {escape(refused)}[/dim]")
        if outcome.result == "confirmed":
            console.print(f'      [green]"{escape(outcome.judgement.quote[:170])}"[/green]')

    with _explain_db_locks(), session_scope(engine, commit=not dry_run) as session:
        risks_to_read = riskcheck.unconfirmed_risks(
            session, project_id=project_id, category=category, limit=limit
        )
        total = len(riskcheck.unconfirmed_risks(session, project_id=project_id, category=category))
        if not risks_to_read:
            console.print("[green]every open obstacle already has a quote that stands up[/green]")
            return
        if not json_mode():
            console.print(
                f"[bold]{len(risks_to_read)}[/bold] of {total} unquoted obstacle(s), "
                "worst first — one model call each, reading the whole article.\n"
            )
        outcomes = riskcheck.confirm(
            session,
            risks_to_read,
            extractor=extractor,
            cache_dir=cache_dir,
            apply=not dry_run,
            on_each=announce,
        )

    if json_mode():
        emit({"outcomes": [o.as_json() for o in outcomes], "total_unconfirmed": total})
        return

    tally: dict[str, int] = {}
    for outcome in outcomes:
        tally[outcome.result] = tally.get(outcome.result, 0) + 1
    _print_report_rows(
        [
            ("read", len(outcomes)),
            ("confirmed", tally.get("confirmed", 0)),
            ("refuted", tally.get("refuted", 0)),
            ("left unclear", tally.get("unclear", 0)),
            ("no article cached", tally.get("no_article", 0)),
            ("call failed", tally.get("error", 0)),
        ],
        title="risks confirm (dry run)" if dry_run else "risks confirm",
        warn={"call failed"},
    )
    refused = [o for o in outcomes if o.judgement.rejected_quote]
    if refused:
        console.print(
            f"\n[yellow]{len(refused)} quote(s) were offered and refused[/yellow] "
            "[dim]— not verbatim in the article, or not stating that kind of obstacle. "
            "Those obstacles are unchanged.[/dim]"
        )
    if dry_run:
        console.print("\n[yellow]dry run[/yellow] — nothing was written")
    else:
        console.print(
            f"\n[dim]{total - len(outcomes)} unquoted obstacle(s) still unread; "
            "raise --limit to continue.[/dim]"
        )


#: Weights for `exposure --weighted`. **A judgement, not a cited fact**, which is
#: why the weighted column is opt-in and the weights are printed whenever it is
#: used. The unweighted view splits the same capacity across severity columns and
#: lets the reader apply their own.
SEVERITY_WEIGHTS: dict[str, float] = {"blocking": 1.0, "material": 0.5, "watch": 0.25}

#: What `exposure --by` can group on. `category` comes off the risk; the rest are
#: `Project` attributes read by name.
_EXPOSURE_KEYS: tuple[str, ...] = ("category", "state", "company", "customer")


@app.command()
def exposure(
    by: Annotated[
        str, typer.Option("--by", help=f"One of: {', '.join(_EXPOSURE_KEYS)}")
    ] = "category",
    weighted: Annotated[
        bool,
        typer.Option("--weighted", help="Add a single weighted MW column. Weights are printed."),
    ] = False,
) -> None:
    """Planned capacity sitting behind an open obstacle, rolled up.

    The read-through the PRD asks for: capacity blocked on `transmission` or
    `grid_capacity` is a power and utility signal, capacity blocked on `offtake` or
    `chip_supply` is a cloud and semiconductor one, and capacity slipping anywhere
    is deferred revenue for whoever was going to fill it.

    Severity is reported as three columns rather than collapsed into one number.
    Collapsing needs a weighting, a weighting is a judgement rather than anything a
    source said, and this tool's whole discipline is not presenting judgements as
    facts. `--weighted` adds the single number for whoever wants it, and prints the
    weights it used alongside.
    """
    if by not in _EXPOSURE_KEYS:
        _fail(f"--by must be one of: {', '.join(_EXPOSURE_KEYS)}")

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        rows = session.execute(
            select(Risk, Project)
            .join(Project, Risk.project_id == Project.id)
            .where(Risk.status == OPEN_RISK_STATUS)
        ).all()

        if not rows:
            console.print("[green]no open risks[/green] — nothing is recorded as obstructed")
            return

        # (group, project_id) -> worst open severity for that project in that group.
        # Deduplicating per project is what stops a project with three obstacles
        # being counted three times in a company or state rollup.
        worst: dict[tuple[str, int], str] = {}
        projects: dict[int, Project] = {}
        for risk_row, project in rows:
            key = risk_row.category if by == "category" else (getattr(project, by) or NA)
            projects[project.id] = project
            current = worst.get((key, project.id))
            if current is None or severity_rank(risk_row.severity) > severity_rank(current):
                worst[(key, project.id)] = risk_row.severity

        groups: dict[str, dict[str, float | int]] = {}
        for (key, project_id), severity in worst.items():
            bucket = groups.setdefault(
                key,
                {"projects": 0, "blocking": 0.0, "material": 0.0, "watch": 0.0, "uncosted": 0},
            )
            bucket["projects"] += 1
            mw = projects[project_id].mw_planned
            if mw is None:
                bucket["uncosted"] += 1
            else:
                bucket[severity] += mw

        def total(bucket) -> float:
            return sum(bucket[s] for s in RISK_SEVERITIES)

        def weight(bucket) -> float:
            return sum(bucket[s] * SEVERITY_WEIGHTS[s] for s in RISK_SEVERITIES)

        table = Table(
            title=f"open-risk exposure by {by}",
            header_style="bold",
            title_justify="left",
            box=TABLE_BOX,
        )
        table.add_column(by)
        table.add_column("projects", justify="right")
        table.add_column("blocking MW", justify="right")
        table.add_column("material MW", justify="right")
        table.add_column("watch MW", justify="right")
        table.add_column("total MW", justify="right")
        if weighted:
            table.add_column("weighted MW", justify="right")
        table.add_column("no MW", justify="right")

        for key in sorted(groups, key=lambda k: (-weight(groups[k]), -total(groups[k]), k)):
            bucket = groups[key]
            cells = [
                str(key),
                str(bucket["projects"]),
                _fmt_mw(bucket["blocking"]),
                _fmt_mw(bucket["material"]),
                _fmt_mw(bucket["watch"]),
                _fmt_mw(total(bucket)),
            ]
            if weighted:
                cells.append(_fmt_mw(weight(bucket)))
            cells.append(str(bucket["uncosted"]))
            table.add_row(*cells)
        console.print(table)

        if by == "category":
            console.print(
                "[dim]A project appears under every category obstructing it, so the "
                "column does not add up to a fleet total.[/dim]"
            )
        else:
            console.print(
                "[dim]Each project is counted once, in its most severe open category.[/dim]"
            )
        console.print(
            '[dim]"no MW" counts projects with an open risk but no cited capacity. '
            "They are excluded from the MW columns rather than treated as zero.[/dim]"
        )
        if weighted:
            printed = ", ".join(f"{s}={SEVERITY_WEIGHTS[s]}" for s in reversed(RISK_SEVERITIES))
            console.print(
                f"[dim]weighted MW uses {printed} — a judgement of this tool, not "
                "anything a source stated.[/dim]"
            )


@app.command()
def capex(
    rows: Annotated[int, typer.Option("--rows", help="Buyers to show.")] = 20,
    by_quarter: Annotated[
        bool,
        typer.Option("--by-quarter", help="Bucket the pipeline by quarter instead of by year."),
    ] = False,
    include_terminal: Annotated[
        bool,
        typer.Option("--include-terminal", help="Count paused and cancelled projects too."),
    ] = False,
) -> None:
    """Capacity and spend by the company actually buying it.

    The database is keyed on the site; this is the other axis — how much each end
    customer has in flight, when it lands, and how exposed it is. Much
    hyperscaler capacity is built by wholesale developers and leased, so the
    operator on the building is often not the buyer.

    Attribution is a named tenant where a source gives one, the operator itself
    where the operator is an end user building for its own use, and an explicit
    unattributed row otherwise. Every figure is a floor: a project whose capacity
    nobody has cited contributes zero.
    """
    from tracker import capex as capex_mod

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        positions = capex_mod.rollup(session, include_terminal=include_terminal)
        cover = capex_mod.coverage(session)
        precision = capex_mod.date_precision(session)
        blockers = {p.key: capex_mod.blocking_risk(session, p.key) for p in positions if p.key}
        suspects = capex_mod.suspect_attributions(session)
        dupes = capex_mod.suspected_duplicates(session)

    if json_mode():
        from tracker.compute import h200_equivalent

        today = capex_mod.as_of()
        emit(
            {
                "coverage": cover,
                "year_columns": capex_mod.year_columns(positions, start=today.year),
                "quarter_columns": capex_mod.quarter_columns(
                    positions, start=f"{today.year}Q{(today.month - 1) // 3 + 1}"
                ),
                "positions": [
                    {
                        "customer": p.name,
                        "key": p.key,
                        "projects": p.projects,
                        "self_built": p.self_built,
                        "mw_planned": p.mw_planned,
                        "mw_built": p.mw_built,
                        # Derived from MW, not a Position field — the same unit
                        # conversion the project column uses.
                        "h200_equivalent": h200_equivalent(p.mw_planned),
                        "mw_unbuilt": p.mw_unbuilt,
                        "investment_usd": p.investment_usd,
                        "investment_excluded_usd": p.investment_excluded_usd,
                        "investment_unquoted_usd": p.investment_unquoted_usd,
                        "duplicate_rows_skipped": p.duplicate_rows_skipped,
                        "mw_duplicate_skipped": p.mw_duplicate_skipped,
                        "investment_duplicate_skipped_usd": p.investment_duplicate_skipped_usd,
                        "mw_by_year": p.mw_by_year,
                        "mw_by_quarter": p.mw_by_quarter,
                        "projects_at_risk": p.at_risk_projects,
                        "mw_at_risk": p.mw_at_risk,
                        "projects_at_risk_unconfirmed": p.at_risk_unconfirmed,
                        "slipped": p.slipped,
                        "worst_open_risk": blockers.get(p.key),
                        "phases": p.phases,
                    }
                    for p in positions
                ],
            }
        )
        return

    if not positions:
        console.print("[yellow]no projects to attribute[/yellow]")
        return

    table = Table(
        title="capacity by end customer",
        header_style="bold",
        title_justify="left",
        box=TABLE_BOX,
    )
    table.add_column("customer")
    table.add_column("proj", justify="right")
    table.add_column("MW planned", justify="right")
    table.add_column("MW built", justify="right")
    table.add_column("investment", justify="right")
    # Column windows come from capex, not here: years are a continuous range so a
    # gap year shows as an empty column, quarters stay data-only. See
    # `capex.year_columns` for both arguments.
    today = capex_mod.as_of()
    if by_quarter:
        now = f"{today.year}Q{(today.month - 1) // 3 + 1}"
        buckets = capex_mod.quarter_columns(positions, start=now)
        of = lambda p, b: p.mw_by_quarter.get(b)  # noqa: E731
    else:
        buckets = [str(y) for y in capex_mod.year_columns(positions, start=today.year)]
        of = lambda p, b: p.mw_by_year.get(int(b))  # noqa: E731
    for bucket in buckets:
        table.add_column(f"MW {bucket}", justify="right")
    table.add_column("at risk", justify="right")
    table.add_column("slipped", justify="right")
    table.add_column("worst risk")

    for position in positions[:rows]:
        name = position.name[:30]
        if position.key and position.self_built == position.projects:
            name += " *"
        cells = [
            name,
            str(position.projects),
            _fmt_mw(position.mw_planned) if position.mw_planned else NA,
            _fmt_mw(position.mw_built) if position.mw_built else NA,
            _fmt_usd(position.investment_usd) if position.investment_usd else NA,
        ]
        cells += [_fmt_mw(of(position, b)) if of(position, b) else NA for b in buckets]
        cells += [
            _fmt_mw(position.mw_at_risk) if position.mw_at_risk else NA,
            str(position.slipped) if position.slipped else NA,
            escape(blockers.get(position.key) or NA),
        ]
        table.add_row(*cells)
    console.print(table)

    console.print(
        f"[dim]* every project attributed by ownership rather than a named tenant.[/dim]\n"
        f"[dim]attributed: [bold]{cover['attributed_pct']:.0f}%[/bold] of "
        f"{int(cover['projects'])} projects "
        f"({cover['named_tenant_pct']:.0f}% by a named tenant, "
        f"{cover['self_built_pct']:.0f}% self-built). "
        f"{cover['with_capacity_pct']:.0f}% cite a capacity; "
        f"{cover['in_timeline_pct']:.0f}% cite both capacity and a date, "
        f"so only those reach the {'quarter' if by_quarter else 'year'} columns.[/dim]"
    )
    if by_quarter:
        console.print(
            f"[yellow]quarters are a shape, not a schedule.[/yellow] [dim]"
            f"{precision['year_only_pct']:.0f}% of the dated projects land on 1 January, which is "
            "where a source that said only a year normalises to — those are a year of vagueness "
            "sitting in Q1.[/dim]"
        )
    console.print(
        "[dim]sums cover only cited figures — every number is a floor, not a total.[/dim]"
    )
    excluded_usd = sum(p.investment_excluded_usd for p in positions)
    if excluded_usd:
        console.print(
            f"[dim]investment excludes [bold]{_fmt_usd(excluded_usd)}[/bold] whose figure no "
            "source confirms — usually a programme-wide total quoted in an article about one "
            "site, demoted at ingest. Shown here, never summed.[/dim]"
        )
    unquoted_usd = sum(p.investment_unquoted_usd for p in positions)
    if unquoted_usd:
        console.print(
            f"[dim]investment [bold]includes[/bold] {_fmt_usd(unquoted_usd)} that no quote "
            "backs — asserted by a source and never contradicted, unlike the figures above. "
            "Counted, because dropping a figure that is probably right understates the "
            "column.[/dim]"
        )
    vague_risk = sum(p.at_risk_unconfirmed for p in positions)
    if vague_risk:
        at_risk = sum(p.at_risk_projects for p in positions)
        console.print(
            f"[dim]the obstructed column counts [bold]{vague_risk}[/bold] of {at_risk} "
            "project(s) whose only obstacles are 待确认 — a source reported them and no "
            "quote stood up. Counted, because understating exposure is the worse error."
            "[/dim]"
        )
    if dupes:
        skip_mw = sum(p.mw_duplicate_skipped for p in positions)
        skip_usd = sum(p.investment_duplicate_skipped_usd for p in positions)
        set_aside = f"[bold]{_fmt_mw(skip_mw)} MW[/bold]"
        if skip_usd:
            set_aside += f" and [bold]{_fmt_usd(skip_usd)}[/bold]"
        console.print(
            f"\n[yellow]{len(dupes)} pair(s)[/yellow] of rows look like one campus stored twice. "
            f"The table counts one row per suspected group and sets aside {set_aside} held by "
            "the others — skipped, not merged. One site often has a builder, a landlord and an "
            "occupier, and each name makes its own row; confirm with `tracker duplicates` and "
            "fold with `tracker merge`:"
        )
        for pair in dupes[:5]:
            console.print(
                f"  [dim]{pair.locality}, {pair.state}: "
                f"#{pair.a_id} {pair.a_company} vs #{pair.b_id} {pair.b_company}[/dim]"
            )
    if suspects:
        console.print(
            f"\n[yellow]{len(suspects)} project(s)[/yellow] name a tracked operator as their "
            "customer, which is usually an extraction error rather than a lease:"
        )
        for project_id, operator, customer in suspects[:5]:
            console.print(f"  [dim]#{project_id} {operator} -> customer {customer!r}[/dim]")


#: How each duplicate signal reads on screen, and the colour of its confidence.
@app.command()
def blocks(
    project_ids: Annotated[
        list[int] | None,
        typer.Argument(help="Only these projects. Omit for the whole database."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Groups to show.")] = 40,
    only: Annotated[
        str | None,
        typer.Option("--only", help="One verdict: mergeable, collides or ambiguous."),
    ] = None,
) -> None:
    """Blocks on one project that look like one tranche named more than once.

    Free, read-only, no LLM. A campus read by 25 sources acquires a name per
    source: Fairwater held `Building 2`, `Facility 2`, `Second facility` and
    `Area II` for one building, because `blocks.block_key` folds ordinals hard and
    cannot fold *which noun a source chose*.

    Three verdicts, and only the first is a proposal:

    \b
      mergeable  one tranche under several names; nothing disagrees
      collides   two sources confirm DIFFERENT capacities for it — not one
                 figure told twice, and a merge must refuse rather than pick
      ambiguous  a bare ordinal that fits two families, e.g. "Facility 1"
                 against both `Building 1` and `Phase 1`
    """
    from tracker import blockcheck

    engine, _ = init_db(_db_path())
    with session_scope(engine, commit=False) as session:
        groups = blockcheck.scan(session, list(project_ids or []) or None)

    if only:
        wanted = only.strip().lower()
        if wanted not in ("mergeable", "collides", "ambiguous"):
            _fail("--only takes mergeable, collides or ambiguous")
            return
        groups = [g for g in groups if g.verdict == wanted]

    if not groups:
        console.print("[green]no block looks like a duplicate of another[/green]")
        return

    from collections import Counter

    counts = Counter(g.verdict for g in groups)
    saved = sum(len(g.members) for g in groups if g.verdict == "mergeable") - counts["mergeable"]
    console.print(
        f"[bold]{len(groups)}[/bold] group(s) across "
        f"{len({g.project_id for g in groups})} project(s): "
        + ", ".join(f"{counts[v]} {v}" for v in ("mergeable", "collides", "ambiguous") if counts[v])
    )
    if saved > 0:
        console.print(f"[dim]folding the mergeable ones would retire {saved} block row(s)[/dim]")

    style = {"mergeable": "green", "collides": "red", "ambiguous": "yellow"}
    for group in groups[:limit]:
        console.print()
        console.print(
            f"  [{style[group.verdict]}]{group.verdict:9}[/{style[group.verdict]}] "
            f"[bold]#{group.project_id}[/bold] {group.family}  [dim]({group.evidence})[/dim]"
        )
        for member in group.members:
            mw = f"{member.mw:,.0f} MW" if member.mw is not None else NA
            if member.mw is not None and not member.mw_confirmed:
                mw += " 待确认"
            console.print(
                f"    {member.label[:34]:34} {mw:>14}  {member.status:18} "
                f"[dim]src {member.source_id}[/dim]"
            )
        if group.ambiguous_with:
            console.print(
                f"    [yellow]could equally be[/yellow]: {', '.join(group.ambiguous_with)}"
            )
        for conflict in group.conflicts:
            values = ", ".join(str(v) for _, v in conflict.values)
            if conflict.confirmed_both_ways:
                console.print(
                    f"    [red]{conflict.field}[/red]: {values} "
                    "[red]— both confirmed, so this is two figures, not one[/red]"
                )
            else:
                console.print(f"    [dim]{conflict.field}: {values}[/dim]")

    if len(groups) > limit:
        console.print(f"\n[dim]{len(groups) - limit} more; raise --limit to see them[/dim]")
    console.print(
        "\n[dim]Nothing here is written. A block is identity, and folding two of them "
        "is a judgement — the same reason `duplicates` proposes and never merges.[/dim]"
    )


#: The words matter more than they look: "both hold tranche horizon-1" is a fact a
#: reader can check in one command, and "same locality" is not evidence of
#: anything, which is why no pair is ever raised on it alone.
_EVIDENCE_STYLE: dict[str, tuple[str, str]] = {
    "tranche": ("green", "same tranche"),
    "party": ("cyan", "shared operator"),
    "name": ("yellow", "name overlap"),
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
        colour, label = _EVIDENCE_STYLE.get(strongest.kinds[0], ("white", "?"))
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


@app.command()
def stats() -> None:
    """Summary counts by phase, confidence and state."""
    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        total = session.scalar(select(func.count()).select_from(Project)) or 0
        if not total:
            # Under --json, emit the empty payload rather than prose. A consumer
            # that pipes stdout into a parser should get "zero projects", not
            # something unparseable that reads as a crash.
            if json_mode():
                emit(
                    {
                        "projects": 0,
                        "citations": 0,
                        "mw_planned_cited_sum": 0.0,
                        "mw_planned_cited_projects": 0,
                        "investment_usd_cited_sum": 0,
                        "by": {"phase": {}, "confidence": {}, "state": {}},
                    }
                )
                return
            console.print("[yellow]database is empty[/yellow] — run `tracker ingest ...` first")
            return

        sources = session.scalar(select(func.count()).select_from(Source)) or 0
        mw = session.scalar(select(func.sum(Project.mw_planned))) or 0.0
        investment = session.scalar(select(func.sum(Project.investment_usd))) or 0
        with_mw = (
            session.scalar(
                select(func.count()).select_from(Project).where(Project.mw_planned.is_not(None))
            )
            or 0
        )

        if json_mode():
            grouped = {
                label: {
                    str(value): count
                    for value, count in session.execute(
                        select(column, func.count()).group_by(column).order_by(column)
                    ).all()
                }
                for label, column in (
                    ("phase", Project.phase),
                    ("confidence", Project.confidence),
                    ("state", Project.state),
                )
            }
            emit(
                {
                    "projects": total,
                    "citations": sources,
                    # A floor, not a total: only projects that cite a figure
                    # contribute. Named so a consumer cannot mistake it for the
                    # industry's capacity.
                    "mw_planned_cited_sum": mw,
                    "mw_planned_cited_projects": with_mw,
                    "investment_usd_cited_sum": investment,
                    "by": grouped,
                }
            )
            return

        console.print(f"[bold]{total}[/bold] projects, [bold]{sources}[/bold] citations")
        console.print(
            f"planned capacity: [bold]{_fmt_mw(mw)} MW[/bold] across {with_mw} project(s)"
        )
        console.print(f"announced investment: [bold]{_fmt_usd(investment)}[/bold]")
        console.print(
            "[dim]sums cover only projects where the figure is cited; "
            "they are a floor, not a total.[/dim]"
        )

        for label, column in (
            ("phase", Project.phase),
            ("confidence", Project.confidence),
            ("state", Project.state),
        ):
            table = Table(
                title=f"by {label}", header_style="bold", title_justify="left", box=TABLE_BOX
            )
            table.add_column(label)
            table.add_column("projects", justify="right")
            rows = session.execute(
                select(column, func.count()).group_by(column).order_by(func.count().desc())
            ).all()
            for value, count in rows:
                table.add_row(str(value), str(count))
            console.print(table)

        risk_rows = session.execute(
            select(
                Risk.category,
                func.count(func.distinct(Risk.project_id)),
                func.sum(Project.mw_planned),
            )
            .join(Project, Risk.project_id == Project.id)
            .where(Risk.status == OPEN_RISK_STATUS)
            .group_by(Risk.category)
            .order_by(func.count(func.distinct(Risk.project_id)).desc(), Risk.category.asc())
        ).all()
        if risk_rows:
            table = Table(
                title="by open risk", header_style="bold", title_justify="left", box=TABLE_BOX
            )
            table.add_column("category")
            table.add_column("projects", justify="right")
            table.add_column("MW at risk", justify="right")
            for value, count, at_risk in risk_rows:
                table.add_row(str(value), str(count), _fmt_mw(at_risk))
            console.print(table)
            console.print(
                "[dim]MW at risk counts only projects whose capacity is cited, and a "
                "project appears under every category obstructing it.[/dim]"
            )


@ingest_app.command("edgar")
def ingest_edgar(
    per_company: Annotated[
        int,
        typer.Option("--per-company", help="Filings per company per phrase. The cost dial."),
    ] = 2,
    company: Annotated[
        str | None,
        typer.Option("--company", help="Restrict to one company by name.", show_default=False),
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option(
            "--kind",
            help="Restrict to one class: hyperscaler, neocloud, landlord, utility, contractor.",
            show_default=False,
        ),
    ] = None,
    companies_file: Annotated[
        Path | None,
        typer.Option("--companies", help="Company list TOML.", show_default=False),
    ] = None,
    since_days: Annotated[
        int,
        typer.Option("--since-days", help="Ignore filings older than this. 0 for no limit."),
    ] = 730,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Prepare the filings but extract nothing.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Re-extract filings already read.")
    ] = False,
) -> None:
    """Read SEC filings — the one publisher that cannot lock us out.

    Filings state the three things we cover worst: investment in the cash-flow
    statement, in-service dates in MD&A, and the end customer in the lease
    footnotes. Publication is a legal obligation, so unlike a newsroom there is no
    bot filter to be refused by.

    The list covers five classes of filer and `--kind` reads one of them.
    `--kind utility` is the interesting one: the power company is the counterparty
    that cannot be bypassed, its filings say what has actually signed an
    interconnection agreement, and `power` is the track this database is worst at
    and refuses to infer. `--kind contractor` reads backlog, which leads
    energisation. Each class is searched with its own phrases, because a utility
    does not write "build-to-suit".

    Each filing costs one LLM call, like any article, and `--per-company` is the
    dial. A filing is far larger than the model budget, so the relevant section is
    selected first rather than the document truncated — see `tracker/ingest/edgar.py`.

    Needs `TRACKER_USER_AGENT` set to a real contact; SEC blocks the placeholder.
    """
    from tracker.ingest import edgar
    from tracker.ingest.crawl import run as run_crawl

    settings = get_settings()
    cache_dir = install_root() / ".cache" / "articles"
    engine, _ = init_db(_db_path())

    try:
        report, urls = edgar.prepare(
            companies_path=companies_file,
            cache_dir=cache_dir,
            settings=settings,
            per_company=per_company,
            only=company,
            kind=kind,
            since_days=since_days or None,
        )
    except edgar.EdgarError as exc:
        _fail(str(exc))
        raise

    _print_report_rows(report.as_rows(), title="edgar")

    if not urls:
        console.print("[yellow]nothing to extract[/yellow]")
        return
    if dry_run:
        console.print(
            f"[yellow]dry run[/yellow] — {len(urls)} filing(s) prepared and cached, "
            "none extracted. Re-run without --dry-run to read them."
        )
        return

    if not settings.has_api_key():
        _fail(
            f"{len(urls)} filing(s) are prepared, but extraction needs TRACKER_DEEPSEEK_API_KEY.\n"
            "The prepared text is cached, so setting the key and re-running costs no fetches."
        )

    try:
        release_lock = acquire_write_lock(_db_path(), command="ingest edgar")
    except AlreadyRunning as exc:
        _fail(str(exc))
        raise
    atexit.register(release_lock)

    console.rule("[bold]extract[/bold]", align="left")
    with _explain_db_locks(), session_scope(engine) as session:
        crawl_report = run_crawl(session, urls, cache_dir=cache_dir, force=force)
    _print_report(crawl_report, title="ingest edgar")


@ingest_app.command("geo")
def ingest_geo(
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Where the Census files live.", show_default=False),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report without writing.")] = False,
) -> None:
    """Derive county and coordinates from US Census reference data.

    No API key, no LLM, no network once the two reference files are downloaded.
    `county` is left unset for a city that spans several counties, because
    choosing one would be inventing the answer, and every coordinate is recorded
    as the centre of the place rather than the project site.
    """
    from tracker.ingest.geo import GeoDataMissing
    from tracker.ingest.geo import run as run_geo

    root = data_dir or (_db_path().parent / "raw" / "census")
    engine, _ = init_db(_db_path())

    try:
        release_lock = acquire_write_lock(_db_path())
    except AlreadyRunning as exc:
        _fail(str(exc))
        raise
    atexit.register(release_lock)

    try:
        with _explain_db_locks(), session_scope(engine, commit=not dry_run) as session:
            report = run_geo(session, data_dir=root, dry_run=dry_run)
    except GeoDataMissing as exc:
        _fail(str(exc))
        raise

    if dry_run:
        console.print("[yellow]dry run[/yellow] — nothing written")
    console.print(
        f"considered [bold]{report.considered}[/bold] project(s); "
        f"filled county on [bold]{report.county_filled}[/bold], "
        f"coordinates on [bold]{report.coords_filled}[/bold]"
    )
    if report.already_complete:
        console.print(f"[dim]{report.already_complete} already had both[/dim]")
    if report.no_city:
        console.print(f"[dim]{report.no_city} have no city to look up[/dim]")
    if report.spans_counties:
        console.print(
            f"[yellow]{report.spans_counties}[/yellow] city/cities span several counties, "
            "so county stays unset:"
        )
        for name in report.multi_county_places:
            console.print(f"  [dim]{name}[/dim]")
    if report.unmatched:
        console.print(
            f"[yellow]{report.unmatched}[/yellow] city/cities are not in the Census file:"
        )
        for name in report.unmatched_places:
            console.print(f"  [dim]{name}[/dim]")


@app.command()
def enrich(
    project_ids: Annotated[
        list[int] | None,
        typer.Argument(help="Project ids to complete. Omit and use --select to choose for you."),
    ] = None,
    select: Annotated[
        int,
        typer.Option("--select", help="Choose this many projects automatically, best first."),
    ] = 0,
    enrich_all: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Every project still below --target. --budget is the actual bound.",
        ),
    ] = False,
    target: Annotated[
        int,
        typer.Option(
            "--target",
            help=(
                "Stop a project once it holds this many of the 12 fields. Default: no "
                "target when you name project ids (exhaust them), 9 with --select/--all "
                "(spread the budget). 0 also means no target."
            ),
        ),
    ] = -1,
    budget: Annotated[
        int,
        typer.Option("--budget", help="Total articles for the whole run, shared across projects."),
    ] = 200,
    max_rounds: Annotated[
        int, typer.Option("--max-rounds", help="Stop after this many harvest+extract passes.")
    ] = 6,
    max_articles: Annotated[int, typer.Option("--max-articles", help="Articles per round.")] = 25,
    skip_search: Annotated[
        bool, typer.Option("--skip-search", help="Do not use the web-search backend.")
    ] = False,
    skip_archive: Annotated[
        bool, typer.Option("--skip-archive", help="Do not sweep the sitemap archives.")
    ] = False,
    browser: Annotated[
        bool,
        typer.Option(
            "--browser", help="Escalate blocked pages to Crawl4AI. Needs the 'crawl' extra."
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Harvest and report without writing or extracting.")
    ] = False,
) -> None:
    """Throw every retrieval method at one or more projects until rounds stop paying.

    `tracker sync` spreads a budget across the database and, because the queue is
    mostly new-project candidates, it grows sideways. This does the opposite: it
    derives what can be derived, drains the queue and the failed URLs, sweeps the
    sitemap archives, runs gap-targeted web searches if a key is configured,
    re-reads the project's own citations, then extracts and repeats.

        tracker enrich 90 93            two projects by id
        tracker enrich --select 30      the 30 worth finishing, chosen for you
        tracker enrich --all            everything below --target, best first

    `--select` orders by how close a project already is to `--target`, because
    taking one project from 8 fields to 9 costs a single article while taking
    another from 4 to 9 may not get there at all. `--budget` is shared across the
    whole run, and a project stops at `--target` so the rest goes to the next one.
    The archives are swept ONCE for the batch.

    `--all` is `--select` with no cap: every project still below the target, in
    the same closest-first order. It is not unbounded spend — `--budget` is the
    real ceiling, and the ordering decides who gets the articles before it runs
    dry, so raise `--budget` when the point is genuinely the whole database.
    """
    from tracker.ingest import enrich as enrich_mod
    from tracker.ingest.fetch import (
        Crawl4AIFetcher,
        MissingDependency,
        escalation_ladder,
    )
    from tracker.vocab import TRACKED_FIELDS

    chosen_ways = [
        name
        for name, given in (
            ("project ids", bool(project_ids)),
            ("--select", bool(select)),
            ("--all", enrich_all),
        )
        if given
    ]
    if len(chosen_ways) > 1:
        _fail(f"pass only one of project ids, --select or --all (got {' and '.join(chosen_ways)})")
        return
    if not chosen_ways:
        _fail("give at least one project id, use --select N, or --all for every project")
        return

    # The field target is a budget-sharing rule, not a definition of done: its own
    # docstring says it leaves "the rest of a shared budget for the next project".
    # When you name a project there IS no next project, and applying 9 there turned
    # `enrich 10` into a no-op on any row already holding 10 of 12 fields — it
    # printed "reached the 9-field target" without harvesting, searching or reading
    # anything, having first fetched all 22 archive sitemaps. Naming a row is an
    # instruction to work on it.
    if target < 0:
        target = 0 if project_ids else enrich_mod.DEFAULT_TARGET_FIELDS
    target_fields = target or None

    if browser:
        # Checked here rather than caught around the constructor: the import
        # lives in `__aenter__`, so building the fetcher never raises and the
        # friendly message below was unreachable.
        try:
            Crawl4AIFetcher.ensure_available()
        except MissingDependency as exc:
            _fail(str(exc))
            return
    # Cheapest rung first; curl_cffi needs no flag. See fetch.escalation_ladder.
    escalate = escalation_ladder(get_settings(), browser=browser)

    engine, _ = init_db(_db_path())
    try:
        release_lock = acquire_write_lock(_db_path())
    except AlreadyRunning as exc:
        _fail(str(exc))
        raise
    atexit.register(release_lock)

    census = _db_path().parent / "raw" / "census"
    try:
        with _explain_db_locks(), session_scope(engine, commit=not dry_run) as session:
            wanted = list(project_ids or [])
            if select or enrich_all:
                # --all is --select with no cap: same query, same ordering, and
                # `select_projects` already excludes projects at or past the
                # target, so "all" never wastes budget on finished rows.
                chosen = enrich_mod.select_projects(
                    session,
                    None if enrich_all else select,
                    # `--target 0` means "no target", which for *selection* means
                    # every project short of all twelve rather than none of them.
                    target=target_fields or len(TRACKED_FIELDS),
                )
                wanted += [p for p in chosen if p not in wanted]
                if not wanted:
                    console.print(
                        f"[green]nothing to do[/green] — every project already holds "
                        f"{target_fields or len(TRACKED_FIELDS)} of the 12 tracked fields"
                    )
                    return
                console.print(f"selected {len(chosen)} project(s), closest to target first")

            batch = enrich_mod.run_many(
                session,
                wanted,
                target_fields=target_fields,
                max_articles=budget,
                max_articles_per_round=max_articles,
                max_rounds=max_rounds,
                census_dir=census if census.exists() else None,
                escalate=escalate,
                skip_search=skip_search,
                skip_archive=skip_archive,
                dry_run=dry_run,
            )
    except LookupError as exc:
        _fail(str(exc))
        raise

    if len(batch.reports) == 1:
        _render_enrich(batch.reports[0], dry_run=dry_run)
        return
    # With no target enforced, still measure against the PRD's nine: it is the bar
    # a reader cares about even when the run was told not to stop there.
    _render_batch(batch, target=target_fields or enrich_mod.DEFAULT_TARGET_FIELDS, dry_run=dry_run)


def _render_batch(batch, *, target: int, dry_run: bool) -> None:
    """One row per project, plus what the batch as a whole achieved."""
    if dry_run:
        console.print("[yellow]dry run[/yellow] — nothing written")
    if batch.sweep_note:
        console.print(f"[dim]archive: {batch.sweep_note} (swept once for the batch)[/dim]")

    table = Table(header_style="bold", box=TABLE_BOX, title_justify="left")
    table.add_column("id", justify="right")
    table.add_column("project")
    table.add_column("fields", justify="right")
    table.add_column("read", justify="right")
    table.add_column("gained")
    for report in batch.reports:
        before, _ = report.score_before()
        after, attemptable = report.tracked_score()
        moved = f"{before} -> {after} of {attemptable}"
        style = "green" if after >= target else "yellow" if after > before else "dim"
        table.add_row(
            str(report.project_id),
            report.label[:46],
            f"[{style}]{moved}[/{style}]",
            str(report.articles_read),
            ", ".join(report.gained)[:44] or NA,
        )
    console.print(table)

    hit_before = batch.reached_before(target)
    hit_after = batch.reached(target)
    console.print(
        f"projects at >={target} of 12: [bold]{hit_before} -> {hit_after}[/bold] "
        f"of {len(batch.reports)}   ({batch.articles_read} article(s) read)"
    )
    if batch.budget_exhausted:
        console.print(
            "[yellow]article budget spent[/yellow] before every project was reached; "
            "raise --budget to continue"
        )


def _render_enrich(report, *, dry_run: bool) -> None:
    """Print an enrichment run so every claim in it can be audited."""
    from tracker.gaps import FILLED, NOT_APPLICABLE

    console.print(f"[bold]#{report.project_id}[/bold] {report.label}")
    if dry_run:
        console.print("[yellow]dry run[/yellow] — nothing written")

    for name, why in report.skipped:
        # A skip reason can be several lines (the search help names every backend
        # and its variable). Lead with the headline, dim the instructions.
        head, _, rest = why.partition("\n")
        console.print(f"[yellow]{name} unavailable[/yellow]: {head}")
        if rest.strip():
            console.print(f"[dim]{rest.strip()}[/dim]")

    if report.rounds:
        table = Table(header_style="bold", box=TABLE_BOX, title_justify="left")
        table.add_column("round", justify="right")
        for column in ("queue", "retry", "archive", "search", "refresh"):
            table.add_column(column, justify="right")
        table.add_column("read", justify="right")
        table.add_column("filled")
        for rnd in report.rounds:
            found = {h.name: len(h.urls) for h in rnd.harvests}
            table.add_row(
                str(rnd.number),
                *[
                    str(found[c]) if c in found else NA
                    for c in ("queue", "retry", "archive", "search", "refresh")
                ],
                str(rnd.articles_read),
                ", ".join(rnd.fields_filled) or NA,
            )
        console.print(table)
        console.print(
            f"[dim]{NA} in a harvester column means it did not run that round. The archive "
            "and the project's own citations are swept once — re-sweeping a fixed corpus "
            "returns the same URLs for thousands of fetches.[/dim]"
        )
        # What each harvester says about itself, once per harvester rather than
        # once per round. Until this printed, `note` was assembled and thrown
        # away — which is how `enrich` searched Bocha's Chinese-web index for US
        # trade press for weeks while reporting only that it found nothing. The
        # engine that answered belongs on screen, not in a debug log.
        for harvest_name in ("queue", "retry", "archive", "search", "refresh"):
            note = next(
                (
                    h.note
                    for rnd in report.rounds
                    for h in rnd.harvests
                    if h.name == harvest_name and h.note
                ),
                None,
            )
            if note:
                console.print(f"[dim]{harvest_name}: {note}[/dim]")

    fields = Table(header_style="bold", box=TABLE_BOX, title_justify="left")
    fields.add_column("field")
    fields.add_column("before")
    fields.add_column("after")
    before = {s.field: s for s in report.before}
    for state in report.after:
        was = before.get(state.field)
        if state.status == FILLED:
            shown = "[green]filled[/green]"
        elif state.status == NOT_APPLICABLE:
            shown = "[dim]n/a[/dim]"
        else:
            shown = "[red]missing[/red]"
        fields.add_row(
            state.field,
            "filled" if was and was.status == FILLED else NA,
            shown
            + (f" [dim]{state.reason}[/dim]" if state.reason and state.status != FILLED else ""),
        )
    console.print(fields)

    filled, attemptable = report.tracked_score()
    console.print(
        f"tracked fields: [bold]{filled} of {attemptable}[/bold] attemptable "
        f"(of the 12; fields a null is correct for are excluded)"
    )
    if report.derived:
        console.print(f"derived from Census: {', '.join(report.derived)}")
    if report.gained:
        console.print(f"[green]gained[/green]: {', '.join(report.gained)}")
    console.print(
        f"citations {report.sources_before} -> {report.sources_after}, "
        f"confidence {report.confidence_before} -> {report.confidence_after}, "
        f"{report.articles_read} article(s) read"
    )
    console.print(f"[dim]stopped: {report.stopped_because}[/dim]")


@logic_app.command("check")
def logic_check(
    read: Annotated[
        int,
        typer.Option(
            "--read",
            help="Also have a model read this many projects. 0 runs only the free checks.",
        ),
    ] = 0,
    audit: Annotated[
        int,
        typer.Option(
            "--audit",
            help=(
                "Also audit the evidence behind this many rows' values, costliest "
                "first. One LLM call per row; 0 skips it."
            ),
        ),
    ] = 0,
    project_id: Annotated[
        int | None,
        typer.Option("--id", help="Check one project.", show_default=False),
    ] = None,
    severity: Annotated[
        str | None,
        typer.Option("--severity", help="Show only `error` or only `warning`.", show_default=False),
    ] = None,
    code: Annotated[
        str | None,
        typer.Option("--code", help="Show only one kind of finding.", show_default=False),
    ] = None,
    collisions: Annotated[
        bool, typer.Option("--collisions/--no-collisions", help="Show source disagreements.")
    ] = True,
    limit: Annotated[int, typer.Option("--limit", help="Findings to print.")] = 40,
) -> None:
    """Find values that contradict each other, and say which source wins.

    Every other check asks whether a value is supported. This one asks whether the
    supported values agree: a row can be perfectly cited and still be impossible.
    A campus marked `operational` whose construction track has reached nothing is
    either the wrong phase or a missing milestone, and both citations behind it
    can be sound.

    Three layers, and only the last one costs anything.

    **Rules** run always and are free. They state their reasoning, so you can
    disagree with one without reading any code: energised before operational,
    built above planned, online before announced, a milestone dated next year
    counted as already reached.

    **Collisions** are two sources claiming different values for one field. The
    winner is not decided here — it is read back from the same per-field policy
    the write path used, and the reason is printed with it. That reason is *not*
    always "the better source won": built capacity takes the largest figure,
    `first_announced` the earliest, and the identity fields are never overwritten
    at all. Only the rest are settled by credibility and then recency.

    **Judgement** is `--read N`, costs one LLM call per project, and is off by
    default. It catches what a rule cannot phrase — a blocker describing a problem
    the milestones say is solved. Every finding it returns must name two fields
    and quote its evidence, or it is dropped; it is never allowed to pick a
    collision winner, and nothing it says is written to the database.

    **The evidence audit** is `--audit N`, the same cost, and asks the prior
    question: does each value's own recorded sentence actually state it — or is
    it a programme-wide total quoted as one campus's money, a figure about a
    different building, an aspiration recorded as a schedule? Rows are read
    costliest first. A finding a person confirms is answered by demoting the
    value in `tracker review`; an unconfirmed investment figure already stays
    out of the capex sums, so the repair path exists before the audit runs.
    """
    from tracker import logic as logic_mod

    engine = _read_engine()
    extractor = None
    if read > 0 or audit > 0:
        from tracker.llm import MissingApiKey, reasoning_extractor

        try:
            extractor = reasoning_extractor(get_settings())
        except MissingApiKey as exc:
            _fail(str(exc))

    def announce(project) -> None:
        console.print(f"[dim]reading #{project.id} {project.company} — {project.name}[/dim]")

    def announce_audit(project) -> None:
        console.print(
            f"[dim]auditing evidence on #{project.id} {project.company} — {project.name}[/dim]"
        )

    # Progress lines share stdout with the payload, so in --json mode they would
    # corrupt it — the very first announce makes `json.load` fail at char 0.
    narrate = not json_mode()
    with session_scope(engine, commit=False) as session:
        report = logic_mod.review(
            session,
            extractor=extractor,
            read_limit=read or None,
            audit_limit=audit or None,
            only=project_id,
            on_examine=announce if read and narrate else None,
            on_audit=announce_audit if audit and narrate else None,
        )

    findings = report.findings
    if severity:
        findings = [f for f in findings if f.severity == severity.lower()]
    if code:
        findings = [f for f in findings if f.code == code]

    if json_mode():
        emit(
            {
                "projects": report.projects,
                "examined": report.examined,
                "audited": report.audited,
                "unreadable": report.unreadable,
                "findings": [f.as_json() for f in findings],
                "collisions": [c.as_json() for c in report.collisions],
            }
        )
        return

    _print_report_rows(report.as_rows(), title="logic")
    console.print()

    # Said out loud, not left in a table row. A truncated reply was paid for and
    # produced nothing, and the one reading it must not take that for "clean".
    if report.unreadable.get("truncated"):
        console.print(
            f"[yellow]{report.unreadable['truncated']} row(s) ran out of room while the "
            f"model was reasoning[/yellow] [dim]— those calls were paid for and returned "
            f"nothing. That is not the same as finding no contradictions; raise "
            f"MAX_TOKENS in tracker/logic.py (currently {logic_mod.MAX_TOKENS}).[/dim]"
        )
    if report.unreadable.get("unusable") or report.unreadable.get("error"):
        console.print(
            f"[yellow]{report.unreadable.get('unusable', 0) + report.unreadable.get('error', 0)} "
            "row(s) could not be read[/yellow] [dim]— run with -v for the reason[/dim]"
        )

    if not findings:
        console.print("[green]no contradictions[/green]")
    else:
        by_project: dict[int, list] = {}
        for finding in findings[:limit]:
            by_project.setdefault(finding.project_id, []).append(finding)
        for pid, group in by_project.items():
            console.print(f"[bold]#{pid}[/bold]")
            for finding in group:
                style = "red" if finding.severity == logic_mod.ERROR else "yellow"
                tag = "model" if finding.inferred else finding.code
                console.print(f"  [{style}]{tag}[/{style}] {escape(finding.summary)}")
                if finding.remedy:
                    console.print(f"    [dim]{escape(finding.remedy)}[/dim]")
        if len(findings) > limit:
            console.print(f"\n[dim]{len(findings) - limit} more; raise --limit[/dim]")

    if collisions and report.collisions:
        console.print(
            f"\n[bold]{len(report.collisions)} field(s) where sources disagree[/bold] "
            "[dim]— the kept value and why[/dim]"
        )
        for collision in report.collisions[:limit]:
            console.print(
                f"  #{collision.project_id} [bold]{collision.field}[/bold]: "
                f"{escape(str(collision.winner)[:44])} "
                f"[dim]over[/dim] {escape(str(collision.loser)[:44])}"
            )
            console.print(f"    [dim]{escape(collision.why)}[/dim]")
            if collision.stored_disagrees:
                console.print(
                    f"    [red]the row holds {escape(str(collision.stored)[:44])}[/red] "
                    "[dim]— run `tracker init` to recompute it from its sources[/dim]"
                )

    console.print(
        "\n[dim]nothing here is written. A contradiction is a question for a person: "
        "confirm the row in `tracker review`, or fold duplicates with `tracker merge`.[/dim]"
    )


@logic_app.command("resolve")
def logic_resolve(
    auto_only: Annotated[
        bool,
        typer.Option("--auto", help="Only the repairs needing no decision. No prompts."),
    ] = False,
    apply: Annotated[
        bool, typer.Option("--apply", help="With --auto: write. Otherwise implied.")
    ] = False,
    llm: Annotated[
        bool,
        typer.Option("--llm", help="Let a model choose, one call each. It skips when unsure."),
    ] = False,
    code: Annotated[
        str | None,
        typer.Option("--code", help="Work through one kind of finding only.", show_default=False),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Findings to offer.")] = 30,
) -> None:
    """Settle the collisions that can be settled, by re-running the merge policy.

    **Where the winner comes from.** Two sources disagreeing on one field is
    decided by how good each source is and how recently it was read — the more
    credible wins, and recency breaks the tie. That is already applied at write
    time, which is why a healthy database shows nothing to do here: the value on
    the row *is* the winner. This command exists for when it is not, which happens
    after a hand edit or when a source is attached by a path that did not
    re-derive. Re-running the policy is arithmetic, not a judgement.

    **Five fields do not use credibility, and that is deliberate.** Built capacity
    takes the largest cited figure, because energised megawatts only go up and a
    better source describing an earlier state should not walk it back.
    `first_announced` takes the earliest, because that is what "first" means.
    `phase` takes the furthest along unless a source says it stopped. Name,
    company and the location fields are never overwritten once set, because churn
    in an identity field is worse than staleness. `tracker logic check` prints
    which rule settled each collision, so the reason is always on screen.

    **Everything else needs a person and is left alone.** Whether 100 MW built
    against 32 MW planned means the plan was revised or the two figures describe
    different phases of one campus is not in the row; a tool that picked one would
    be inventing a fact. Measured on the live database, 0 of 149 findings were
    mechanically resolvable — which is the honest reason there is no button that
    fixes everything.

    **Then it hands you the rest, one at a time.** With the evidence on screen and
    the answers as single keys, because that is the difference between a report
    and a tool: a person looking at `100 MW built against 32 MW planned` with both
    quotes in front of them settles it in two seconds, and what they lacked was
    somewhere to put the answer. Every edit is written into the row's notes as
    plain prose, which is the one kind of note re-ingesting never erases.

    `--auto` does the no-decision repairs and stops, for scripts and for the
    console, which has no keyboard.

    **`--llm` lets a model choose instead of you**, one call per finding, and it
    skips whenever the evidence does not clearly favour one option. This is
    allowed where `tracker infer` bars a model from 关键数字, and the difference is
    worth understanding: `infer` asks a model to produce a number from general
    knowledge, whereas here every option was written by a person and operates only
    on figures already in the row with citations behind them. The model's whole
    output is one character from a closed set — it cannot type a value.

    Two things it may never do. It cannot mark a row verified, because that means
    "an operator says this is right" and feeds the confidence score. And its edits
    are recorded as `model resolved`, never `operator resolved`, so a reader six
    months later can tell that nobody looked.
    """
    import sys as _sys

    from tracker import logic as logic_mod

    path = _db_path()
    if not path.is_file():
        _fail(f"database not found: {path}\nRun `tracker init` first.")

    extractor = None
    if llm:
        from tracker.llm import MissingApiKey, reasoning_extractor

        try:
            extractor = reasoning_extractor(get_settings())
        except MissingApiKey as exc:
            _fail(str(exc))

    interactive = not auto_only and not llm and _sys.stdin.isatty() and not json_mode()
    writing = interactive or apply or llm

    if writing:
        engine, _ = init_db(path)
        try:
            release_lock = acquire_write_lock(path, command="logic resolve")
        except AlreadyRunning as exc:
            _fail(str(exc))
            raise
        atexit.register(release_lock)
    else:
        engine = _read_engine()

    with _explain_db_locks(), session_scope(engine, commit=writing) as session:
        repairs = logic_mod.resolve_drift(session, apply=writing)
        report = logic_mod.review(session)
        findings = [f for f in report.findings if not code or f.code == code]
        # Findings something can be done about, first. The list was ordered by
        # project id, so `--limit 30` handed a person — or a model — thirty
        # questions whose only answers were "verify" and "skip", and the ones with
        # a real edit behind them sat at position 190.
        findings.sort(key=lambda f: (not logic_mod.resolvable(f), f.project_id))

        if json_mode():
            emit(
                {
                    "repaired": [
                        {
                            "project_id": r.project_id,
                            "changes": {f: {"was": w, "now": n} for f, (w, n) in r.changes.items()},
                        }
                        for r in repairs
                    ],
                    "left_for_a_person": [f.as_json() for f in findings],
                }
            )
            return

        for repair in repairs:
            console.print(
                f"[green]repaired[/green] #{repair.project_id} {escape(repair.label[:60])}"
            )
            for name, (was, now) in repair.changes.items():
                console.print(f"  {name}: {escape(str(was)[:36])} -> {escape(str(now)[:36])}")
        if repairs:
            console.print()

        if not findings:
            console.print("[green]nothing left to decide[/green]")
            return

        if llm:
            # The whole list, not `[:limit]`: the limit is a budget for *calls*,
            # and slicing before the unanswerable ones are filtered out spent it
            # on findings the model can only decline. Ordered by project id, the
            # first thirty were almost all of that kind.
            _triage_by_model(session, findings, logic_mod, extractor, limit=limit)
            return

        if not interactive:
            console.print(
                f"[bold]{len(findings)}[/bold] contradiction(s) need a person.\n"
                "[dim]Run `tracker logic resolve` in a terminal to work through them; "
                "each one offers the edits that answer it.[/dim]"
            )
            return

        _triage(session, findings[:limit], logic_mod)


def _triage_by_model(session, findings: list, logic_mod, extractor, *, limit: int = 30) -> None:
    """Let a model answer each finding. One call per finding; it skips when unsure.

    Prints every decision *and* every skip with the reason, because a run that
    silently resolved 12 of 30 and said nothing about the other 18 would read as
    "the rest were fine". They were not — nobody has looked at them.

    **Findings no edit can answer never reach the model.** Eleven of the sixteen
    rules offer no action — a phase enum arguing with a campus that is half
    energised is a contradiction in the schema, not in the data — and `decide`
    can only ever answer "nothing to choose between" for them. On the live
    database that was 174 of 283 findings, and with the queue ordered by project
    id the first `--limit 30` was almost entirely made of them: twelve lines of
    "left alone" before a single decision. They are counted here and reported in
    one line, which is what they are worth.
    """
    from tracker.models import Project

    unanswerable = [f for f in findings if not logic_mod.resolvable(f)]
    findings = [f for f in findings if logic_mod.resolvable(f)]
    if unanswerable:
        by_code: dict[str, int] = {}
        for finding in unanswerable:
            by_code[finding.code] = by_code.get(finding.code, 0) + 1
        listed = ", ".join(
            f"{code} x{n}" for code, n in sorted(by_code.items(), key=lambda kv: -kv[1])
        )
        console.print(
            f"[dim]{len(unanswerable)} finding(s) have no edit that answers them and were "
            f"not sent to the model — {escape(listed)}.\n"
            "They are still worth reading: `tracker logic check`.[/dim]\n"
        )
    if not findings:
        console.print("[yellow]nothing here a model could act on[/yellow]")
        return

    over_budget = max(0, len(findings) - limit)
    findings = findings[:limit]

    applied = 0
    declined = 0
    rejected: dict[str, int] = {}
    for index, finding in enumerate(findings, start=1):
        project = session.get(Project, finding.project_id)
        if project is None:
            continue

        decision = logic_mod.decide(project, finding, extractor=extractor)
        head = f"[dim]{index}/{len(findings)}[/dim] #{project.id} {escape(project.name[:34])}"

        if not decision.acted:
            # A decline is the model doing what it was told; a rejection is its
            # answer being unusable. Counting them together would hide a broken
            # prompt inside a pile of sensible caution.
            if decision.outcome == "declined":
                declined += 1
                # On its own line and not truncated to 90 characters. A decline is
                # now a substantive reading of the obstacle and the milestone —
                # "the noise complaint was first seen after the permit was
                # approved" is the answer, and clipping it mid-word threw away the
                # most useful thing the run produced.
                console.print(f"{head}  [dim]left alone[/dim]")
                console.print(f"      [dim]{escape(decision.note[:220])}[/dim]")
            else:
                rejected[decision.note.split(":")[0]] = (
                    rejected.get(decision.note.split(":")[0], 0) + 1
                )
                console.print(
                    f"{head}  [yellow]unusable answer[/yellow] "
                    f"[dim]— {escape(decision.note[:70])}[/dim]"
                )
            continue

        action = next(a for a in logic_mod.ACTIONS[finding.code] if a.key == decision.key)
        changed = action.apply(session, project, finding)
        logic_mod.record_decision(
            project,
            finding.code,
            changed,
            by=f"model ({decision.confidence:.2f})",
            detail=decision.reason,
        )
        session.commit()
        applied += 1
        console.print(f"{head}  [green]{escape(changed)}[/green]")
        console.print(f"      [dim]{escape(decision.reason[:110])}[/dim]")

    from tracker.upsert import recompute_confidence

    recompute_confidence(session)
    session.commit()

    console.print(
        f"\n[bold]{applied}[/bold] resolved, [bold]{declined}[/bold] the model declined"
        + (f", [yellow]{sum(rejected.values())}[/yellow] unusable" if rejected else "")
        + (f", {over_budget} beyond --limit" if over_budget else "")
    )
    for reason, count in sorted(rejected.items(), key=lambda kv: -kv[1]):
        console.print(f"  [yellow]{count:>3}[/yellow]  [dim]{escape(reason)}[/dim]")
    console.print(
        "\n[dim]Each edit is recorded on the project as `model resolved`, never as "
        "`operator resolved` — nobody has read the sources for these. Run "
        "`tracker logic resolve` with no flags to go through the rest yourself.[/dim]"
    )


def _triage(session, findings: list, logic_mod) -> None:
    """Walk an operator through findings, applying the edit they choose.

    Committed after each answer rather than at the end. Somebody triaging forty
    rows will stop partway, and losing the first thirty decisions to a Ctrl-C is
    the kind of thing that stops a tool being used twice.
    """
    from tracker.models import Project

    done = skipped = 0
    total = len(findings)
    for index, finding in enumerate(findings, start=1):
        project = session.get(Project, finding.project_id)
        if project is None:
            continue

        console.rule(f"[dim]{index} of {total}[/dim]", align="left")
        console.print(
            f"[bold]#{project.id}[/bold] {escape(project.company)} — {escape(project.name)}"
            f"  [dim]{escape(_location(project))}[/dim]"
        )
        style = "red" if finding.severity == logic_mod.ERROR else "yellow"
        console.print(f"[{style}]{finding.code}[/{style}] {escape(finding.summary)}")
        if finding.remedy:
            console.print(f"[dim]{escape(finding.remedy)}[/dim]")

        # The values in play, so the decision does not need another command.
        for name in finding.fields or ():
            value = getattr(project, name, None)
            quote = _one_quote(project, name)
            console.print(f"  [bold]{name}[/bold] = {escape(str(value))}")
            if quote:
                console.print(f'    [dim]"{escape(quote[:150])}"[/dim]')

        actions = logic_mod.ACTIONS.get(finding.code, ())
        for action in actions:
            console.print(f"  [bold cyan]{action.key}[/bold cyan]  {action.label}")
        console.print("  [bold cyan]v[/bold cyan]  it is fine as it is — mark the row verified")
        console.print("  [bold cyan]s[/bold cyan]  skip    [bold cyan]q[/bold cyan]  stop here")

        valid = {a.key for a in actions} | {"v", "s", "q"}
        choice = ""
        while choice not in valid:
            choice = typer.prompt("  >", default="s", show_default=False).strip().lower()

        if choice == "q":
            break
        if choice == "s":
            skipped += 1
            continue
        if choice == "v":
            from tracker.models import utcnow

            project.last_verified_at = utcnow()
            logic_mod.record_decision(project, finding.code, "confirmed correct as it stands")
        else:
            action = next(a for a in actions if a.key == choice)
            changed = action.apply(session, project, finding)
            logic_mod.record_decision(project, finding.code, changed)
            console.print(f"  [green]{escape(changed)}[/green]")
        session.commit()
        done += 1

    from tracker.upsert import recompute_confidence

    rescored = recompute_confidence(session)
    session.commit()
    console.print(
        f"\n[bold]{done}[/bold] decided, [bold]{skipped}[/bold] skipped"
        + (f", {rescored} row(s) rescored" if rescored else "")
        + "\n[dim]every decision is written into the project's notes. "
        "Re-run `tracker logic check` to see what is left.[/dim]"
    )


def _one_quote(project, field: str) -> str | None:
    """The sentence behind a value, when there is one, for the triage screen."""
    from tracker.gaps import provenance

    try:
        prov = provenance(project, field)
    except Exception:
        return None
    return (prov.quote or "").strip() if prov else None


@app.command()
def point(
    name: Annotated[str, typer.Argument(help="The data center to go and get.")],
    url: Annotated[
        list[str] | None,
        typer.Option(
            "--url",
            help="Read this link instead of searching. Repeatable.",
            show_default=False,
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Say what it would do and stop. Costs one call.")
    ] = False,
    max_articles: Annotated[
        int, typer.Option("--max-articles", help="Articles to read when building a new profile.")
    ] = 5,
) -> None:
    """Go and get one named data center: match it to a row, or build its profile.

    Everything else here works in batches. This is the other direction — you heard
    a name and want that one campus now.

    It first asks whether we already have it. The same building routinely appears
    under three names, because whoever writes about it picks the developer, the
    landowner or the tenant: "Stargate Abilene", "Crusoe Abilene Data Center" and
    "Oracle Stargate — Abilene" are one site, and adding a fourth row makes the
    capex table wrong in exactly the way `tracker duplicates` exists to catch.

    **Matched** — `enrich` runs on that project, throwing every retrieval method
    at it until a round stops paying.

    **Not matched** — it searches for that name specifically, reads what it finds,
    and the ordinary write path creates the row with its citations.

    `--url` replaces the finding step in both branches: you already have the link,
    so read that instead of searching for one. This is the path for a press release
    or a filing that search will not surface — and it works whether or not the
    campus is already tracked, because the write path merges by dedup key either
    way. Repeatable.

    The model chooses from a shortlist and can only answer with an id from it or
    "none"; anything hedged below the confidence floor is treated as "none". That
    asymmetry is deliberate. A wrong "no" makes a duplicate, which is detected and
    fixable; a wrong "yes" folds a real campus into another project's history, and
    nothing detects that.

    Costs one call to identify, then whatever the branch it takes costs.
    """
    from tracker import point as point_mod
    from tracker.llm import MissingApiKey, reasoning_extractor

    settings = get_settings()
    try:
        extractor = reasoning_extractor(settings)
    except MissingApiKey as exc:
        _fail(str(exc))
        return

    engine, _ = init_db(_db_path())
    with session_scope(engine, commit=False) as session:
        candidates = point_mod.shortlist(session, name)

    console.print(f"[bold]{escape(name)}[/bold]")
    if candidates:
        console.print(f"[dim]{len(candidates)} row(s) share a distinctive word:[/dim]")
        for candidate in candidates[:5]:
            console.print(f"  [dim]#{candidate.project_id:<5} {escape(candidate.label[:66])}[/dim]")
    else:
        console.print("[dim]nothing in the database shares a distinctive word with it[/dim]")

    match = point_mod.identify(name, candidates, extractor=extractor)
    if match.rejected:
        console.print(f"[yellow]could not identify it:[/yellow] {escape(match.rejected)}")
        console.print("[dim]treating it as new, which is the recoverable mistake[/dim]")
    elif match.matched:
        console.print(
            f"[green]already tracked[/green] as [bold]#{match.project_id}[/bold] "
            f"[dim](confidence {match.confidence:.2f})[/dim]"
        )
    else:
        console.print(
            f"[yellow]not in the database[/yellow] [dim](confidence {match.confidence:.2f})[/dim]"
        )
    if match.reason:
        console.print(f"[dim]{escape(match.reason)}[/dim]")

    links = [u.strip() for u in (url or []) if u and u.strip()]
    if dry_run:
        if links:
            plan = f"read the {len(links)} link(s) given"
            plan += f" and merge into #{match.project_id}" if match.matched else " as a new row"
        elif match.matched:
            plan = f"enrich #{match.project_id}"
        else:
            plan = f"search and read up to {max_articles} article(s)"
        console.print(f"\n[yellow]dry run[/yellow] — would {plan}")
        return

    console.print()
    if links:
        _point_read(name, point_mod, links, match)
    elif match.matched:
        _point_enrich(match.project_id)
    else:
        _point_build(name, point_mod, settings, max_articles)


def _h200_cell(project) -> str:
    """The accelerator count, labelled with where it came from.

    Almost always a restatement of the capacity, so it says so — printing it bare
    beside quoted figures would read as something a source reported. When a source
    *did* report a chip count it goes through the evidence gate like any value and
    is shown plainly.
    """
    from tracker.compute import h200_equivalent, kw_per_h200

    count = project.h200_equivalent
    if count is None:
        return NA
    basis = project.mw_built if project.mw_built else project.mw_planned
    if count == h200_equivalent(basis):
        return f"{count:,} [dim](derived from {_fmt_mw(basis)} at {kw_per_h200()} kW each)[/dim]"
    return f"{count:,}"


def _point_read(name: str, point_mod, urls: list[str], match) -> None:
    """`--url`: read exactly these links, whoever they turn out to be about.

    No special write path. The links go through `crawl.run` like any other article,
    so the evidence gate, the dedup key and the merge policies all apply unchanged —
    which is what makes this safe to point at a page for a campus we already track.

    The identification above is therefore advisory here rather than load-bearing:
    it tells the operator which row to expect the article to land on, and if the
    article turns out to be about something else, the dedup key sends it there
    instead. Overriding that with the matched id would be worse — it would let a
    mistyped name file a filing under the wrong campus, which is the one error
    nothing downstream detects.
    """
    from tracker.ingest import crawl as crawl_mod

    engine, _ = init_db(_db_path())
    try:
        release_lock = acquire_write_lock(_db_path(), command="point")
    except AlreadyRunning as exc:
        _fail(str(exc))
        raise
    atexit.register(release_lock)

    console.rule("[bold]read[/bold]", align="left")
    for link in urls:
        console.print(f"  [dim]{escape(link)}[/dim]")
    if match.matched:
        console.print(
            f"[dim]expected to land on #{match.project_id}; the dedup key decides, not this.[/dim]"
        )

    cache_dir = install_root() / ".cache" / "articles"
    with _explain_db_locks(), session_scope(engine) as session:
        report = crawl_mod.run(session, urls, cache_dir=cache_dir)
    _print_report(report, title="read")

    with session_scope(engine, commit=False) as session:
        fresh = point_mod.shortlist(session, name, limit=3)
    if fresh:
        console.print("\n[green]in the database:[/green]")
        for candidate in fresh:
            console.print(f"  #{candidate.project_id:<5} {escape(candidate.label[:70])}")
    else:
        console.print(
            "\n[yellow]nothing matching that name came out of it[/yellow] "
            "[dim]— the page may be about a different site, or the gate found no "
            "quotable value. `tracker queue` shows what was fetched.[/dim]"
        )


def _point_enrich(project_id: int) -> None:
    """The matched branch: hand it to the enrichment loop already built for this."""
    from tracker.ingest import enrich as enrich_mod

    engine, _ = init_db(_db_path())
    try:
        release_lock = acquire_write_lock(_db_path(), command="point")
    except AlreadyRunning as exc:
        _fail(str(exc))
        raise
    atexit.register(release_lock)

    console.rule("[bold]enrich[/bold]", align="left")
    with _explain_db_locks(), session_scope(engine) as session:
        report = enrich_mod.run(session, project_id)
    _print_report(report, title=f"enrich #{project_id}")


def _point_build(name: str, point_mod, settings, max_articles: int) -> None:
    """The unmatched branch: search for this name only, read the hits, let upsert build the row.

    The queries are hand-built rather than model-written. `search --from-llm` asks
    a model for queries and steers them *away* from projects already tracked,
    which is the right instinct for prospecting and exactly wrong here — the name
    is already the specific thing wanted.
    """
    from tracker.ingest import crawl as crawl_mod
    from tracker.ingest import search as search_mod

    if not settings.has_search_keys():
        console.print(f"[yellow]search is not configured[/yellow]\n{search_mod.SEARCH_KEY_HELP}")
        raise typer.Exit(2)
    try:
        provider = search_mod.build_provider(settings)
    except search_mod.SearchError as exc:
        _fail(str(exc))
        return

    queries = point_mod.queries_for(name)
    console.rule("[bold]search[/bold]", align="left")
    for query in queries:
        console.print(f"  [dim]{escape(query)}[/dim]")

    engine, _ = init_db(_db_path())
    try:
        release_lock = acquire_write_lock(_db_path(), command="point")
    except AlreadyRunning as exc:
        _fail(str(exc))
        raise
    atexit.register(release_lock)

    with _explain_db_locks(), session_scope(engine) as session:
        report, candidates = search_mod.run(session, queries, provider=provider, settings=settings)
    _print_report_rows(report.as_rows(), title="search")

    urls = [c.url for c in candidates][:max_articles]
    if not urls:
        console.print(
            "\n[yellow]nothing readable found[/yellow] [dim]— either the name is wrong, or "
            "nobody has written about it under that name. Try the operator's name with the "
            "town.[/dim]"
        )
        return

    console.rule("[bold]read[/bold]", align="left")
    cache_dir = install_root() / ".cache" / "articles"
    with _explain_db_locks(), session_scope(engine) as session:
        crawl_report = crawl_mod.run(session, urls, cache_dir=cache_dir)
    _print_report(crawl_report, title="read")

    with session_scope(engine, commit=False) as session:
        fresh = point_mod.shortlist(session, name, limit=3)
    if fresh:
        console.print("\n[green]now in the database:[/green]")
        for candidate in fresh:
            console.print(f"  #{candidate.project_id:<5} {escape(candidate.label[:70])}")
        console.print(
            "[dim]run `tracker show <id>` for the citations, or `tracker point` again "
            "to deepen it.[/dim]"
        )
    else:
        console.print(
            "\n[yellow]the articles read did not yield a project row[/yellow] "
            "[dim]— the evidence gate drops anything it cannot tie to a quote. "
            "`tracker queue` shows what was fetched.[/dim]"
        )


@audit_app.callback(invoke_without_command=True)
def audit(ctx: typer.Context) -> None:
    """Find figures that are physically or economically implausible.

    Bare `tracker audit` checks every project and is what this group runs with no
    subcommand. `check` is the same listing with project ids — `tracker audit
    check 72 25` — and `resolve` is the ladder that settles what either one finds.

    The ids moved onto `check` when `resolve` arrived, because a command group
    cannot take a variable number of positional arguments *and* dispatch a
    subcommand: `tracker audit resolve` would parse "resolve" as a project id.

    `logic check` asks whether a row contradicts itself. This asks whether a
    number could be true at all — and the difference matters, because the errors
    it hunts leave a row perfectly self-consistent around a figure that is wrong
    by a thousandfold. Project 72 sat as the largest number in the database:
    11,250 MW for a colocation *expansion*, unquoted, implying $187k per MW.
    Nothing contradicted it, so nothing flagged it.

    Free — no LLM, no network, read-only. Run it after every sync or backfill;
    an empty result is the point.
    """
    if ctx.invoked_subcommand is not None:
        return
    audit_check(project_ids=None)


@audit_app.command("check")
def audit_check(
    project_ids: Annotated[
        list[int] | None,
        typer.Argument(
            help="Only these projects. Default: the whole database.", show_default=False
        ),
    ] = None,
) -> None:
    """The implausibility listing, optionally scoped to project ids.

    Identical to bare `tracker audit`, which runs it over everything. The only
    reason to type the longer form is to name the rows you care about:
    `tracker audit check 72 25`. Free, read-only, no LLM; `tracker audit resolve`
    is what settles anything it reports.
    """
    from tracker import audit as audit_mod

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        findings = audit_mod.run(session, project_ids=project_ids or None)

    if json_mode():
        emit(
            {
                "findings": [
                    {
                        "project_id": f.project_id,
                        "name": f.name,
                        "code": f.code,
                        "summary": f.summary,
                        "remedy": f.remedy,
                    }
                    for f in findings
                ]
            }
        )
        return

    if not findings:
        scope = f"{len(project_ids)} project(s)" if project_ids else "every project"
        console.print(f"[green]nothing implausible[/green] [dim]— checked {scope}.[/dim]")
        return

    table = Table(
        title="implausible figures", header_style="bold", box=TABLE_BOX, title_justify="left"
    )
    table.add_column("project")
    table.add_column("what is implausible", max_width=58)
    table.add_column("do this", max_width=44, style="dim")
    for f in findings:
        table.add_row(
            f"#{f.project_id} {escape(f.name[:24])}\n[dim]{f.code}[/dim]",
            escape(f.summary),
            escape(f.remedy),
        )
    console.print(table)
    console.print(
        f"\n[yellow]{len(findings)} finding(s)[/yellow] on "
        f"{len({f.project_id for f in findings})} project(s). "
        "[dim]Nothing was changed here.[/dim]\n"
        "[dim]answer them with:[/dim] tracker audit resolve"
    )


@audit_app.command("resolve")
def audit_resolve(
    project_ids: Annotated[
        list[int] | None,
        typer.Argument(help="Only these projects.", show_default=False),
    ] = None,
    ask: Annotated[
        bool,
        typer.Option(
            "--ask/--no-ask",
            help="Put each one to you first. Off, or with no terminal, goes straight to the model.",
        ),
    ] = True,
    llm: Annotated[
        bool, typer.Option("--llm/--no-llm", help="Let a reasoning model decide what you did not.")
    ] = True,
    search: Annotated[
        bool,
        typer.Option(
            "--search/--no-search",
            help="When the model says the row lacks the answer, go and look for sources.",
        ),
    ] = True,
    code: Annotated[
        str | None, typer.Option("--code", help="Only this kind of finding.", show_default=False)
    ] = None,
    again: Annotated[
        bool, typer.Option("--again", help="Re-ask findings a previous run already settled.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="Findings to work through.")] = 20,
) -> None:
    """Settle the implausible figures, escalating only as far as it has to.

    `tracker audit` has always been able to say that 11,250 MW on a colocation
    expansion cannot be true. It could not do anything about it, so the same
    finding came back on every run with the same sentence telling somebody to go
    and read an article. This is that somebody.

    **Five rungs, and each one only runs because the one above declined.**

    1. **Arithmetic**, free. `h200_equivalent` is a fixed ratio applied to
       capacity, so re-deriving it is not a judgement. Nor is preferring the one
       of two claims that carries a quote when the other carries none.
    2. **You**, free, with every claim and its quote on screen and one-key
       answers. This is the best rung and it is second because it is the only one
       that needs a person present. `--no-ask`, or no terminal, skips it.
    3. **A reasoning model** on what the row holds, one call. Its whole output is
       one key from a list a person wrote, a confidence and a sentence — it cannot
       type a capacity.
    4. **The open web**, when the model answers "the row does not contain the
       answer". A search, up to four pages fetched, and the sentences that mention
       a capacity or a sum. Nothing fetched here is stored as a citation: it
       informs one decision and the decision records what was read.
    5. **The model again**, with those passages in front of it.

    Every edit is written into the row's notes naming who made it — `rule`,
    `operator`, `model` or `model after search` — and a finding settled once is not
    asked again unless you pass `--again`. A model may not mark a row verified;
    that is a claim only a person may make.
    """
    import sys as _sys

    from tracker import audit as audit_mod

    path = _db_path()
    if not path.is_file():
        _fail(f"database not found: {path}\nRun `tracker init` first.")

    settings = get_settings()
    extractor = None
    if llm:
        from tracker.llm import MissingApiKey, reasoning_extractor

        try:
            extractor = reasoning_extractor(settings)
        except MissingApiKey as exc:
            _fail(str(exc))

    interactive = ask and _sys.stdin.isatty() and not json_mode()

    engine = _writable("audit resolve")

    resolutions: list = []
    with _explain_db_locks(), session_scope(engine) as session:
        findings = audit_mod.run(session, project_ids=list(project_ids) if project_ids else None)
        if code:
            findings = [f for f in findings if f.code == code]

        pending = []
        settled_before = 0
        for finding in findings:
            project = session.get(Project, finding.project_id)
            if project is None:
                continue
            if not again and finding.code in audit_mod.settled_codes(project):
                settled_before += 1
                continue
            pending.append((project, finding))

        if not pending:
            if json_mode():
                emit({"resolved": [], "settled_before": settled_before})
                return
            if settled_before:
                console.print(
                    f"[green]nothing left to settle[/green] [dim]— {settled_before} finding(s) "
                    "were answered on an earlier run; --again re-asks them.[/dim]"
                )
            else:
                console.print("[green]nothing implausible[/green]")
            return

        if not json_mode():
            console.print(
                f"[bold]{len(pending)}[/bold] finding(s) to settle"
                + (f" [dim]({settled_before} already answered)[/dim]" if settled_before else "")
                + "\n"
            )

        asker = _audit_ask if interactive else None
        for index, (project, finding) in enumerate(pending[:limit], start=1):
            if not json_mode():
                console.rule(
                    f"[dim]{index} of {min(len(pending), limit)}[/dim]  "
                    f"#{project.id} {escape(project.name[:40])}",
                    align="left",
                )
            got = audit_mod.resolve_one(
                session,
                project,
                finding,
                extractor=extractor,
                ask=asker,
                allow_search=search,
                settings=settings,
            )
            resolutions.append(got)
            session.commit()
            if not json_mode():
                _print_resolution(got)

        from tracker.upsert import recompute_confidence

        rescored = recompute_confidence(session)
        session.commit()

    if json_mode():
        emit(
            {
                "resolved": [r.as_json() for r in resolutions],
                "settled_before": settled_before,
                "rescored": rescored,
            }
        )
        return

    _print_resolution_summary(resolutions, rescored)


def _audit_ask(project, finding, options):
    """Stage 2: put one implausible figure to the person at the keyboard.

    Returns a key to apply, `"s"` to skip it entirely, or None to hand it down to
    the model. That third answer is the one that makes the ladder worth having —
    "I don't know" is the commonest honest response to a figure nobody has a
    source for, and it should cost the operator one keystroke, not a decision.
    """
    from tracker import audit as audit_mod

    console.print(f"[yellow]{finding.code}[/yellow] {escape(finding.summary)}")
    console.print(f"[dim]{escape(finding.remedy)}[/dim]")
    console.print(escape(audit_mod.evidence_block(project, finding)))
    for action in options:
        console.print(f"  [bold cyan]{action.key}[/bold cyan]  {action.label}")
    console.print(
        "  [bold cyan]?[/bold cyan]  I do not know — hand it to the model\n"
        "  [bold cyan]s[/bold cyan]  skip this one entirely"
    )
    valid = {a.key for a in options} | {"?", "s"}
    choice = ""
    while choice not in valid:
        choice = typer.prompt("  >", default="?", show_default=False).strip().lower()
    return None if choice == "?" else choice


#: Colour per rung, so a run reads at a glance: green was free and certain, cyan
#: was a person, magenta cost a call, yellow cost a call and a fetch.
_STAGE_STYLE: dict[str, str] = {
    "arithmetic": "green",
    "operator": "cyan",
    "model": "magenta",
    "model-after-search": "yellow",
}


def _print_resolution(got) -> None:
    if got.acted:
        style = _STAGE_STYLE.get(got.stage, "white")
        confidence = f" [dim]({got.confidence:.2f})[/dim]" if got.confidence < 1.0 else ""
        console.print(f"  [{style}]{got.stage}[/{style}]{confidence} {escape(got.changed)}")
        if got.reason:
            console.print(f"    [dim]{escape(got.reason[:150])}[/dim]")
        if got.searched and got.searched.urls:
            for url in got.searched.urls[:3]:
                console.print(f"    [dim]read {escape(url[:96])}[/dim]")
        return
    console.print(f"  [dim]left alone — {escape(got.note[:120])}[/dim]")
    if got.searched and got.searched.queries and not got.searched.passages:
        console.print(f"    [dim]searched: {escape(got.searched.queries[0][:90])}[/dim]")


def _print_resolution_summary(resolutions: list, rescored: int) -> None:
    if not resolutions:
        return
    acted = [r for r in resolutions if r.acted]
    by_stage: dict[str, int] = {}
    for got in acted:
        by_stage[got.stage] = by_stage.get(got.stage, 0) + 1
    console.print(
        f"\n[bold]{len(acted)}[/bold] of {len(resolutions)} settled"
        + (f", {rescored} row(s) rescored" if rescored else "")
    )
    for stage, count in sorted(by_stage.items(), key=lambda kv: -kv[1]):
        style = _STAGE_STYLE.get(stage, "white")
        console.print(f"  [{style}]{count:>3}[/{style}]  [dim]{stage}[/dim]")
    left = len(resolutions) - len(acted)
    if left:
        console.print(f"  [dim]{left:>3}  nobody could decide — they stay in `tracker audit`[/dim]")
    console.print(
        "\n[dim]every edit is in the row's notes with who made it. A model's answer is "
        "recorded as `model resolved`, never as `operator resolved`.[/dim]"
    )


@app.command("backfill")
def backfill(
    what: Annotated[str, typer.Argument(help="Only `blocks` for now.")] = "blocks",
    limit: Annotated[
        int, typer.Option("--limit", help="Articles to read. 0 reads every one selected.")
    ] = 25,
    project_id: Annotated[
        int | None,
        typer.Option("--project", help="Only this project's sources.", show_default=False),
    ] = None,
    refetch: Annotated[
        bool, typer.Option("--refetch", help="Fetch the articles that are no longer cached.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Re-read articles whose blocks are already stored.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Read and report, write nothing. Still costs calls.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip the confirmation for a large run.")
    ] = False,
) -> None:
    """Re-read stored articles to fill in capacity blocks.

    Migration 0009 created the block table and wrote no rows, because turning an
    article into blocks needs the article text rather than the schema. Every project
    ingested before it therefore has no blocks until this runs.

    Deliberately not `ingest crawl --force`: that re-extracts every field with a
    model that behaves differently than it did at ingest time, churning rows and
    timestamps inside what is meant to be a backfill. This writes one column and
    lets the ordinary rollup do the rest.

    Costs one LLM call per article. Reads from the article cache, so most cost
    nothing to fetch; `--refetch` covers the rest. Resumable — an article whose
    blocks are already stored is skipped — so a sensible way to run it is in
    tranches: `--limit 25`, look at what came back, then more.
    """
    from tracker import backfill as backfill_mod
    from tracker.llm import MissingApiKey, default_extractor

    if what != "blocks":
        _fail(f"nothing to backfill called {what!r}. Only `blocks` exists.")

    settings = get_settings()
    cache_dir = install_root() / ".cache" / "articles"
    engine, _ = init_db(_db_path())

    with session_scope(engine, commit=False) as session:
        picks = backfill_mod.candidates(
            session, cache_dir=cache_dir, project_id=project_id, force=force
        )

    if not picks:
        console.print("[green]nothing to do[/green] — every crawled article already has blocks.")
        return

    ready = [p for p in picks if p.cached]
    chosen = picks if refetch else ready
    if limit:
        chosen = chosen[:limit]

    # Preflight before anything is spent. Two of these numbers decide whether the
    # run is worth making at all, and both were wrong in the original estimate.
    _print_report_rows(
        [
            ("articles without blocks", len(picks)),
            ("already cached", len(ready)),
            ("need fetching", len(picks) - len(ready)),
            ("will read now", len(chosen)),
            ("LLM calls", len(chosen)),
        ],
        title="backfill blocks",
    )
    if not chosen:
        console.print(
            "[yellow]nothing cached to read[/yellow] "
            "[dim]— pass --refetch to fetch the articles again.[/dim]"
        )
        return

    if len(chosen) > 40 and not yes and not json_mode():
        console.print(
            f"\n[yellow]{len(chosen)} LLM calls.[/yellow] "
            "[dim]Re-run with --yes, or use --limit to go in tranches.[/dim]"
        )
        raise typer.Exit(1)

    try:
        extractor = default_extractor(settings)
    except MissingApiKey as exc:
        _fail(str(exc))
        return

    try:
        release_lock = acquire_write_lock(_db_path(), command="backfill")
    except AlreadyRunning as exc:
        _fail(str(exc))
        raise
    atexit.register(release_lock)

    console.rule("[bold]read[/bold]", align="left")
    with _explain_db_locks(), session_scope(engine, commit=not dry_run) as session:
        report = backfill_mod.run(
            session,
            chosen,
            extractor=extractor,
            cache_dir=cache_dir,
            settings=settings,
            refetch=refetch,
            dry_run=dry_run,
        )

    if json_mode():
        emit({"rows": dict(report.as_rows()), "notes": report.notes})
        return

    _print_report(report, title="blocks" + (" (dry run)" if dry_run else ""))
    for note in report.notes[:20]:
        console.print(f"  [dim]{escape(note)}[/dim]")
    remaining = len(picks) - len(chosen)
    if remaining > 0:
        console.print(
            f"\n[dim]{remaining} article(s) still to read. Run it again to continue.[/dim]"
        )


@app.command()
def infer(
    project_ids: Annotated[list[int], typer.Argument(help="Project ids to analyse.")],
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override the reasoning model.", show_default=False),
    ] = None,
) -> None:
    """Ask a reasoning model what is obstructing a project and what to watch for.

    The PRD asks for two things no article contains — an analysis of the
    difficulties a project may hit, and the signal that would show it is still
    advancing. Those are judgements drawn from the facts, not facts to be found.

    **It cannot write a fact.** Only obstacles and next-signals are accepted;
    anything quantitative the model volunteers is dropped and reported. Investment,
    capacity and dates are 关键数字 and must come from a document, so inference is
    barred from them in code rather than by asking the model nicely.

    Nothing here is stored yet — the analysis is printed for review.
    """
    from tracker.infer import analyse
    from tracker.llm import MissingApiKey, reasoning_extractor

    settings = get_settings()
    try:
        extractor = reasoning_extractor(settings)
        if model:
            from tracker.llm import DeepSeekExtractor

            extractor = DeepSeekExtractor(settings, model=model)
    except MissingApiKey as exc:
        _fail(str(exc))
        raise

    engine = _read_engine()
    payloads: list[dict] = []
    with session_scope(engine, commit=False) as session:
        for project_id in project_ids:
            project = session.get(Project, project_id)
            if project is None:
                err.print(f"[yellow]no project with id {project_id}[/yellow]")
                continue

            if not json_mode():
                console.rule(
                    f"[bold]#{project.id}[/bold] {escape(project.company)} — "
                    f"{escape(project.name)}  [dim]{escape(_location(project))}[/dim]",
                    align="left",
                )
            analysis = analyse(project, extractor=extractor)
            if json_mode():
                payloads.append(_infer_json(project, analysis))
                continue
            _print_analysis(project, analysis)

    if json_mode():
        emit({"analyses": payloads})


def _infer_json(project, analysis) -> dict:
    return {
        "project_id": project.id,
        "project": f"{project.company} — {project.name}",
        "model": analysis.model,
        "rejected": list(analysis.rejected),
        "obstacles": [
            {
                "category": r.category,
                "severity": r.severity,
                "confidence": round(r.confidence, 2),
                "reasoning": r.reasoning,
            }
            for r in analysis.obstacles
        ],
        "signals": [
            {
                "signal": s.signal,
                "confidence": round(s.confidence, 2),
                "reasoning": s.reasoning,
            }
            for s in analysis.signals
        ],
    }


def _confidence_bar(value: float) -> str:
    """Five cells of confidence, coloured by how much of it there is.

    A number between 0 and 1 beside four other numbers between 0 and 1 is
    something a reader has to decode every time. The bar is read at a glance and
    the figure is still printed beside it, so nothing is lost.
    """
    filled = max(1, min(5, round(value * 5)))
    colour = "green" if value >= 0.7 else "yellow" if value >= 0.5 else "red"
    return f"[{colour}]{'█' * filled}[/{colour}][dim]{'·' * (5 - filled)}[/dim]"


def _print_analysis(project, analysis) -> None:
    """One project's inference, laid out as two answers to two questions.

    **The old layout put the caveat first and the answer in a table column.** The
    PRD asks two things — what could go wrong, and what would show it is still
    moving — and they were rendered as a four-column table whose widest column was
    free prose, plus a run of unaligned "watch for" lines underneath. Reasoning
    wrapped to two characters a line on a narrow terminal, the two questions were
    indistinguishable, and the disclaimer that none of it is a fact sat at the
    bottom in grey.

    Now: a heading per question, the judgement on its own line with a confidence
    bar, its reasoning indented under it, and the provenance line last but stated
    plainly. Nothing here is stored, and the layout should not look like the
    tables that hold things that are.
    """
    if analysis.rejected:
        console.print(
            f"[yellow]refused to accept[/yellow] {', '.join(analysis.rejected)} "
            "[dim]— a model may not assert a fact[/dim]"
        )
    if analysis.empty:
        console.print("[dim]no conclusion the facts support[/dim]\n")
        return

    if analysis.obstacles:
        console.print("\n[bold]What could obstruct this[/bold] [dim]— 可能遇到的困难[/dim]")
        for risk in analysis.obstacles:
            style = _severity_style(risk.severity)
            console.print(
                f"  {_confidence_bar(risk.confidence)} [dim]{risk.confidence:.2f}[/dim]  "
                f"[magenta]{risk.category}[/magenta] [{style}]{risk.severity}[/{style}]"
            )
            for line in _wrap(risk.reasoning, 96):
                console.print(f"        [dim]{escape(line)}[/dim]")

    if analysis.signals:
        console.print("\n[bold]What would show it is still moving[/bold] [dim]— 推进的信号[/dim]")
        for signal in analysis.signals:
            # A signal is a sentence, not a label — "a TVA interconnection study
            # notice for a third substation" runs past any terminal. Wrapped with
            # a hanging indent so the continuation lines up under the text and not
            # under the confidence bar.
            head, *rest = _wrap(signal.signal, 88)
            console.print(
                f"  {_confidence_bar(signal.confidence)} [dim]{signal.confidence:.2f}[/dim]  "
                f"{escape(head)}"
            )
            for line in rest:
                console.print(f"        {escape(line)}")
            for line in _wrap(signal.reasoning, 96):
                console.print(f"        [dim]{escape(line)}[/dim]")

    open_risks = _open_risk_count(project)
    console.print(
        f"\n[dim]Inferred by {analysis.model} from this row's {open_risks} recorded "
        "obstacle(s), its milestones and its gaps. Not stored, not evidence — a "
        "judgement drawn from the facts, printed beside them.[/dim]\n"
    )


def _wrap(text: str, width: int) -> list[str]:
    """Wrap prose ourselves so the indent is ours, not Rich's cell padding."""
    import textwrap

    return textwrap.wrap(" ".join((text or "").split()), width=width) or [""]


@app.command()
def gaps() -> None:
    """Per-field coverage, measured against the rows where the field applies.

    A NULL is not always a gap. `mw_built` on an announced project is correct,
    and most projects genuinely have no `blocker` — so those are reported against
    a narrower denominator, or as unmeasurable, rather than as a low score that
    sends you looking for facts that do not exist.
    """
    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        total = session.scalar(select(func.count()).select_from(Project)) or 0
        if not total:
            # As in `stats`: --json must still be parseable on an empty database.
            if json_mode():
                emit({"projects": 0, "fields": [], "worst": []})
                return
            console.print("[yellow]database is empty[/yellow] — run `tracker sync` first")
            return

        rows = measure_gaps(session)

        if json_mode():
            emit(
                {
                    "projects": total,
                    "fields": [
                        {
                            "field": g.field,
                            "filled": g.filled,
                            "applicable": g.applicable,
                            "missing": g.missing,
                            "pct": g.pct,
                            "measurable": g.measurable,
                            "note": g.note,
                        }
                        for g in rows
                    ],
                    "worst": [g.field for g in worst_gaps(rows)],
                }
            )
            return

        table = Table(header_style="bold", box=TABLE_BOX, title_justify="left")
        table.add_column("field")
        table.add_column("filled", justify="right")
        table.add_column("of", justify="right")
        table.add_column("", justify="right")
        table.add_column("denominator / why", style="dim")

        for gap in rows:
            pct = gap.pct
            if pct is None:
                shown, style = "n/a", "dim"
            else:
                shown = f"{pct}%"
                style = "green" if pct >= 90 else "yellow" if pct >= 50 else "red"
            table.add_row(
                gap.field,
                str(gap.filled),
                str(gap.applicable),
                f"[{style}]{shown}[/{style}]",
                gap.note or "all projects",
            )
        console.print(table)

        headroom = worst_gaps(rows)
        if headroom:
            console.print("\n[bold]most missing rows[/bold] (measurable fields only)")
            for gap in headroom:
                console.print(f"  {gap.field:16} {gap.missing} of {gap.applicable} to fill")


@app.command()
def sync(
    since_days: Annotated[
        int, typer.Option("--since-days", help="Discover articles no older than this.")
    ] = 45,
    limit: Annotated[
        int, typer.Option("--limit", help="Max NEW candidates to extract this run.")
    ] = 15,
    refresh_days: Annotated[
        int,
        typer.Option("--refresh-days", help="Re-read a project's sources older than this."),
    ] = 30,
    refresh_limit: Annotated[
        int, typer.Option("--refresh-limit", help="Max existing sources to re-read.")
    ] = 15,
    browser: Annotated[
        bool,
        typer.Option(
            "--browser", help="Escalate blocked pages to Crawl4AI. Needs the 'crawl' extra."
        ),
    ] = False,
    breadth_first: Annotated[
        bool,
        typer.Option(
            "--breadth-first",
            help="Drain the queue oldest-first instead of prioritising depth on known projects.",
        ),
    ] = False,
    deep: Annotated[
        bool,
        typer.Option(
            "--deep",
            help="Also walk site archives (sitemaps) for older projects. No API key needed.",
        ),
    ] = False,
    search: Annotated[
        int,
        typer.Option(
            "--search",
            help=(
                "LLM-proposed web searches to run. Default: automatic when a search "
                "key is configured (Serper/Google/Brave/Bocha), 0 otherwise. "
                "Pass 0 to skip searching."
            ),
        ),
    ] = -1,
    skip_discover: Annotated[
        bool, typer.Option("--skip-discover", help="Do not poll feeds; work the existing queue.")
    ] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Also re-attempt URLs a previous run could not read."),
    ] = False,
    skip_refresh: Annotated[
        bool, typer.Option("--skip-refresh", help="Do not re-read existing projects' sources.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Do everything except write to the database.")
    ] = False,
    show_rows: Annotated[int, typer.Option("--rows", help="Projects to list at the end.")] = 30,
) -> None:
    """Everything in one command: find new projects, refresh known ones, then list.

    Four phases:

    \b
      1. discover   poll the feeds and queue new candidate articles
      2. extract    crawl the queue -> new projects in the database
      3. refresh    re-read existing projects' sources -> updated fields
      4. list       show the result

    Both crawl phases are capped (--limit, --refresh-limit) because each article
    costs an LLM call. Use --dry-run to see what a run would do before paying for
    it, and raise the caps once you are happy with what it finds.
    """
    from tracker.ingest import crawl
    from tracker.ingest import discover as disc
    from tracker.llm import DeepSeekExtractor, MissingApiKey
    from tracker.upsert import recompute_confidence

    settings = get_settings()

    # Checked before any network call: every later phase needs it, and failing
    # here costs nothing rather than after a round of feed polling.
    try:
        extractor = DeepSeekExtractor(settings)
    except MissingApiKey as exc:
        _fail(str(exc))
        return

    from tracker.ingest.fetch import Crawl4AIFetcher, MissingDependency, escalation_ladder

    if browser:
        # Fail on the flag, not twenty pages in. `__aenter__` holds the import,
        # so nothing before this point would have noticed the extra was absent.
        try:
            Crawl4AIFetcher.ensure_available()
        except MissingDependency as exc:
            _fail(str(exc))
            return
    # A ladder, cheapest rung first. curl_cffi needs no flag: it costs one
    # ordinary request and clears the WAFs that fingerprint TLS rather than
    # reading the User-Agent. Chromium stays behind --browser.
    escalate = escalation_ladder(settings, browser=browser)

    engine, _ = init_db(_db_path())
    cache_dir = install_root() / ".cache" / "articles"
    totals = {"queued": 0, "new": 0, "refreshed": 0, "failed": 0}

    # Held for the whole run. SQLite takes one writer, and two overlapping syncs
    # fail partway through -- after the second has already paid for LLM calls.
    try:
        release_lock = acquire_write_lock(_db_path(), command="sync")
    except AlreadyRunning as exc:
        _fail(str(exc))
        return
    # atexit rather than a `with` block: this command has several early returns,
    # and registering the release covers all of them including typer.Exit.
    atexit.register(release_lock)

    # --- 1. discover --------------------------------------------------------
    if skip_discover:
        console.print("[dim]1/4 discover — skipped[/dim]")
    else:
        console.rule("[bold]1/4 discover[/bold]", align="left")
        try:
            with session_scope(engine) as session:
                report, _ = disc.run(
                    session,
                    since_days=since_days or None,
                    dry_run=dry_run,
                    # Feeds that syndicate the whole article write it into the same
                    # cache the extract phase reads, so phase 2 below never requests
                    # a page that would answer 403.
                    cache_dir=cache_dir,
                )
        except disc.DiscoverError as exc:
            _fail(str(exc))
            return
        totals["queued"] = report.queued
        console.print(
            f"polled {report.feeds_polled} feed(s), saw {report.entries_seen} entr(ies), "
            f"queued [bold]{report.queued}[/bold] new candidate(s)"
        )
        for name, reason in report.failures:
            err.print(f"[yellow]feed {name}[/yellow]: {reason}")

    # --- 1a. archives -------------------------------------------------------
    if deep and not skip_discover:
        from tracker.ingest import discover as disc2

        specs = disc2.load_sitemaps()
        if not specs:
            console.print("[dim]--deep: no [[sitemap]] entries configured[/dim]")
        else:
            import asyncio

            _, filter_spec = disc2.load_config()
            fetcher = disc2._RawFetcher(settings)
            found, problems = asyncio.run(disc2.sweep_sitemaps(specs, fetcher, filter_spec))
            shim = disc2.DiscoverReport()
            with session_scope(engine, commit=not dry_run) as session:
                # Queued even on a dry run, so the report says what *would* have
                # happened; session_scope then rolls it back.
                disc2.queue_candidates(session, found, run_id="deep", report=shim)
            totals["queued"] += shim.queued
            console.print(
                f"archives: {len(found)} matching URL(s) across {len(specs)} sitemap(s), "
                f"queued [bold]{shim.queued}[/bold] new ({shim.already_known} already known)"
            )
            for problem in problems[:5]:
                err.print(f"[yellow]{problem}[/yellow]")

    # --- 1b. search ---------------------------------------------------------
    # Runs inside phase 1 because it is the same job as polling a feed: turn the
    # outside world into queued candidates. Feeds only see what was published
    # recently; search reaches back for anything already announced.
    #
    # -1 means "not given": a configured key turns searching on by default,
    # because a sync that quietly skips the one phase reaching beyond the
    # configured feeds is how enrich ends up with nothing new to read. An
    # explicit --search 0 still disables it, and no key means no searching —
    # silently, since a keyless setup is a configuration, not an error.
    if search < 0:
        search = settings.search_max_queries if settings.has_search_keys() else 0
        if search:
            console.print(
                f"[dim]search: on by default via "
                f"[bold]{settings.resolve_search_provider()}[/bold] "
                f"({search} queries; --search 0 to skip)[/dim]"
            )
    if search:
        from tracker.ingest import search as srch

        if not settings.has_search_keys():
            err.print("[yellow]--search needs a search backend[/yellow]")
            err.print(srch.SEARCH_KEY_HELP)
        else:
            with session_scope(engine, commit=False) as session:
                known = srch.known_projects(session)
            try:
                queries = srch.generate_queries(extractor, count=search, known=known)
                with session_scope(engine) as session:
                    s_report, _ = srch.run(
                        session,
                        queries,
                        provider=srch.build_provider(settings),
                        settings=settings,
                        dry_run=dry_run,
                    )
            except srch.SearchError as exc:
                err.print(f"[yellow]search skipped[/yellow]: {str(exc).splitlines()[0]}")
            else:
                totals["queued"] += s_report.queued
                console.print(
                    f"searched {s_report.queries_run} quer(ies), {s_report.hits} hit(s), "
                    f"queued [bold]{s_report.queued}[/bold] more"
                )
                if s_report.wiki_mined:
                    console.print(
                        f"[dim]  {s_report.wiki_mined} of those came from Wikipedia's own "
                        "references[/dim]"
                    )
                if s_report.quota_exhausted:
                    err.print("[yellow]daily search quota exhausted[/yellow]")

    # --- 2. extract new -----------------------------------------------------
    console.rule("[bold]2/4 extract new[/bold]", align="left")
    with session_scope(engine, commit=False) as session:
        # known_first spends each LLM call on depth: a queued article covering a
        # project we already track becomes a SECOND source, which fills fields one
        # article cannot and lifts confidence from 2 to 3. Draining oldest-first
        # instead just grows the database sideways with more single-source rows.
        #
        # The filter spec refines that further: among the articles covering a
        # tracked project, the ones reporting an obstacle go first. A press release
        # never names its own blocker, so those are the only calls that can record
        # one at all.
        try:
            _, queue_spec = disc.load_config()
        except disc.DiscoverError:
            queue_spec = None  # ordering is an optimization; a bad config is phase 1's problem
        pending_urls = [
            row.url
            for row in disc.pending(
                session, limit=limit, known_first=not breadth_first, spec=queue_spec
            )
        ]
        backlog = len(disc.pending(session))
        deepening, _fresh = disc.pending_split(session)
        risky = disc.pending_risk_count(session, queue_spec) if queue_spec else 0
        # Counted whether or not we are retrying, so the summary can never claim
        # "0 failed" while a dozen articles sit unread.
        unread = disc.failed(session)
        unread_hosts = disc.failure_summary(session)
        if retry_failed:
            room = max(0, limit - len(pending_urls))
            pending_urls += [row.url for row in unread[:room]]

    if pending_urls and not breadth_first and deepening:
        detail = f", {risky} of them reporting an obstacle" if risky else ""
        console.print(
            f"[dim]{deepening} of {backlog} queued candidate(s) cover a project already "
            f"tracked{detail}; those go first[/dim]"
        )

    if not pending_urls:
        if dry_run and totals["queued"]:
            console.print(
                f"queue is empty here because --dry-run rolled back the "
                f"{totals['queued']} candidate(s) phase 1 found. A real run would "
                f"extract up to {limit} of them."
            )
        else:
            console.print("queue is empty — nothing new to extract")
    else:
        console.print(f"extracting {len(pending_urls)} of {backlog} queued candidate(s)")
        with _explain_db_locks(), session_scope(engine) as session:
            new_report = crawl.run(
                session,
                pending_urls,
                extractor=extractor,
                escalate=escalate,
                settings=settings,
                dry_run=dry_run,
                force=True,
                cache_dir=cache_dir,
            )
        totals["new"] = new_report.inserted
        totals["failed"] += new_report.fetch_error + new_report.parse_error
        _print_report(new_report, title="new projects")
        if backlog > len(pending_urls):
            console.print(
                f"[dim]{backlog - len(pending_urls)} candidate(s) still queued; "
                f"raise --limit or run again[/dim]"
            )

    # --- 3. refresh existing ------------------------------------------------
    if skip_refresh:
        console.print("[dim]3/4 refresh — skipped[/dim]")
    else:
        console.rule("[bold]3/4 refresh existing[/bold]", align="left")
        with session_scope(engine, commit=False) as session:
            stale = crawl.stale_sources(session, older_than_days=refresh_days, limit=refresh_limit)
        if not stale:
            console.print(f"no source read more than {refresh_days} day(s) ago — all current")
        else:
            console.print(f"re-reading {len(stale)} source(s) not seen in {refresh_days} day(s)")
            with _explain_db_locks(), session_scope(engine) as session:
                # cache_dir=None on purpose: the point of refreshing is to find out
                # whether the article changed, and serving it from the local cache
                # would guarantee the answer is "no".
                ref_report = crawl.run(
                    session,
                    stale,
                    extractor=extractor,
                    escalate=escalate,
                    settings=settings,
                    dry_run=dry_run,
                    force=True,
                    cache_dir=None,
                )
            totals["refreshed"] = ref_report.updated
            totals["failed"] += ref_report.fetch_error + ref_report.parse_error
            _print_report(ref_report, title="refreshed projects")

    # Confidence is a cache of a pure function, so recompute after any write.
    if not dry_run:
        with session_scope(engine) as session:
            rescored = recompute_confidence(session)
        if rescored:
            console.print(f"[dim]recomputed confidence on {rescored} project(s)[/dim]")

    # --- 4. list ------------------------------------------------------------
    console.rule("[bold]4/4 projects[/bold]", align="left")
    if dry_run:
        console.print("[yellow]dry run — nothing was written[/yellow]")
    list_projects(
        company=None, state=None, phase=None, min_confidence=None, sort="mw", limit=show_rows
    )

    console.print(
        f"\n[bold]sync complete[/bold]  queued {totals['queued']}  "
        f"new {totals['new']}  refreshed {totals['refreshed']}  failed {totals['failed']}"
    )
    # Always reported, never only when *this* run failed. Unread URLs are invisible
    # to both `discover` (which never re-queues a known URL) and the pending queue,
    # so without this a run says "queue is empty, 0 failed" while articles pile up.
    if unread:
        console.print(
            f"[yellow]{len(unread)} URL(s) previously failed and were never read[/yellow]"
        )
        for host, count in unread_hosts[:5]:
            console.print(f"  {count:3}  {host}")
        console.print(
            "[dim]list them with `tracker queue --failed`; re-attempt with "
            "`tracker sync --retry-failed`[/dim]"
        )
    if (totals["failed"] or unread) and not browser:
        console.print(BROWSER_HINT)

    release_lock()


@app.command("search")
def search_cmd(
    query: Annotated[
        list[str] | None,
        typer.Argument(
            help="Search queries. Omit and use --from-llm to have the model propose them."
        ),
    ] = None,
    from_llm: Annotated[
        int,
        typer.Option("--from-llm", help="Ask the model for this many project search queries."),
    ] = 0,
    print_only: Annotated[
        bool,
        typer.Option("--print-only", help="Show the queries and stop. No search, no writes."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Search but do not queue anything.")
    ] = False,
) -> None:
    """Find candidate articles with Google search instead of waiting for a feed.

    Feeds only surface what was published recently, so a project announced two
    years ago never appears in them. Search goes looking for it.

    With --from-llm, the model proposes which projects to search for. Those are
    leads, never facts: nothing the model names is stored, and a project only
    becomes a row once a real article has been fetched and its values backed by
    verbatim quotes. If the model invents a project, the search finds nothing.
    """
    from tracker.ingest import search as srch
    from tracker.llm import DeepSeekExtractor, MissingApiKey

    settings = get_settings()
    queries = list(query or [])

    if from_llm:
        try:
            extractor = DeepSeekExtractor(settings)
        except MissingApiKey as exc:
            _fail(str(exc))
            return
        engine_ro = _read_engine()
        with session_scope(engine_ro, commit=False) as session:
            known = srch.known_projects(session)
        try:
            queries += srch.generate_queries(extractor, count=from_llm, known=known)
        except srch.SearchError as exc:
            _fail(str(exc))
            return

    if not queries:
        _fail("give at least one query, or use --from-llm N")
        return

    if print_only or not settings.has_search_keys():
        if not print_only:
            err.print(f"[yellow]search is not configured[/yellow]\n{srch.SEARCH_KEY_HELP}")
        console.print(f"\n[bold]{len(queries)} quer(ies)[/bold]")
        for q in queries:
            console.print(f"  {q}")
        if not print_only:
            raise typer.Exit(2)
        return

    try:
        provider = srch.build_provider(settings)
    except srch.SearchError as exc:
        _fail(str(exc))
        return
    engine, _ = init_db(_db_path())
    with session_scope(engine) as session:
        report, candidates = srch.run(
            session, queries, provider=provider, settings=settings, dry_run=dry_run
        )

    _print_report_rows(
        report.as_rows(),
        title=f"search{' (dry run)' if dry_run else ''}",
        warn={"filtered out"},
    )
    for q, reason in report.errors:
        err.print(f"[yellow]{q[:60]}[/yellow]: {reason.splitlines()[0]}")
    if report.quota_exhausted:
        err.print("[yellow]daily search quota exhausted; resets at midnight Pacific[/yellow]")

    if candidates:
        table = Table(title="queued", header_style="bold", title_justify="left", box=TABLE_BOX)
        table.add_column("headline")
        table.add_column("url")
        for c in candidates[:40]:
            table.add_row(escape(c.title[:78]), escape(c.url[:64]))
        console.print(table)
        console.print("\n[dim]next:[/dim] tracker sync --skip-discover")


@app.command()
def discover(
    feeds: Annotated[
        Path | None, typer.Option("--feeds", help="Feed config TOML. Defaults to seed/feeds.toml.")
    ] = None,
    since_days: Annotated[
        int, typer.Option("--since-days", help="Ignore articles older than this. 0 for no limit.")
    ] = 60,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be queued without writing.")
    ] = False,
    show: Annotated[
        bool, typer.Option("--show/--no-show", help="List the candidates found.")
    ] = True,
) -> None:
    """Poll news feeds for candidate articles and queue them for crawling.

    Nothing is fetched or sent to an LLM here — matches land in the queue with
    their headline so you can triage them with `tracker queue` first.
    """
    from tracker.ingest import discover as disc

    engine, _ = init_db(_db_path())
    try:
        with session_scope(engine) as session:
            report, candidates = disc.run(
                session,
                feeds_path=feeds,
                since_days=since_days or None,
                dry_run=dry_run,
                cache_dir=install_root() / ".cache" / "articles",
            )
    except disc.DiscoverError as exc:
        _fail(str(exc))
        return

    _print_report_rows(
        report.as_rows(),
        title=f"discover{' (dry run)' if dry_run else ''}",
        warn={"feeds failed"},
    )

    for name, reason in report.failures:
        err.print(f"[yellow]feed {name}[/yellow]: {reason}")

    if show and candidates:
        table = Table(title="candidates", header_style="bold", title_justify="left", box=TABLE_BOX)
        table.add_column("published")
        table.add_column("feed")
        table.add_column("headline")
        for candidate in sorted(
            candidates, key=lambda c: (c.published_at or utcnow_placeholder(), c.url)
        ):
            table.add_row(
                str(candidate.published_at or NA)[:10],
                candidate.feed,
                (candidate.title or candidate.url)[:96],
            )
        console.print(table)

    if report.queued and not dry_run:
        console.print(
            "\n[dim]next:[/dim] tracker queue        [dim]# review what was found[/dim]\n"
            "[dim]then:[/dim] tracker ingest crawl --from-queue"
        )


def utcnow_placeholder():
    """Sort key for a candidate with no publication date: treat it as newest."""
    from tracker.models import utcnow

    return utcnow()


queue_app = typer.Typer(name="queue", invoke_without_command=True)
app.add_typer(queue_app)


@queue_app.callback(invoke_without_command=True)
def queue(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 40,
    failed: Annotated[
        bool,
        typer.Option(
            "--failed", help="Show URLs a previous run could not read, instead of pending ones."
        ),
    ] = False,
    feed: Annotated[
        str | None,
        typer.Option("--feed", help="Only candidates from this feed.", show_default=False),
    ] = None,
    newest: Annotated[
        bool,
        typer.Option(
            "--newest/--oldest",
            help="Newest first. The crawl drains oldest-first; you want to read newest-first.",
        ),
    ] = True,
    drop: Annotated[
        bool, typer.Option("--drop", help="Delete the listed candidates instead of showing them.")
    ] = False,
    url: Annotated[
        list[str] | None,
        typer.Option("--url", help="Restrict --drop to these URLs. Repeatable."),
    ] = None,
    row_id: Annotated[
        list[int] | None,
        typer.Option("--id", help="Restrict --drop to these queue ids. Repeatable."),
    ] = None,
) -> None:
    """Show articles discovery has queued but nothing has crawled yet.

    Bare `tracker queue` is that listing. The two subcommands keep the promise it
    makes — that everything in it is worth an LLM call: `check` asks every queued
    URL whether it is still there, and `prune` re-applies the filter in
    `seed/feeds.toml` to rows queued under an older version of it. Both report
    first and only delete with `--drop`.

    **The URL column is the whole URL.** It used to be `url[:60]`, which looked
    tidy and was the single most damaging thing in this output: the string on
    screen was a *prefix* of a real link, so opening it gave "404 not found" and
    pasting it into `--drop --url` matched nothing. A queue whose links all 404 is
    a queue nobody trusts. Every row now also carries its id, which is a short
    handle for `--drop --id` and cannot be mistaken for a link.

    Newest first, because a queue is read by a person deciding what is worth a
    crawl and the crawl itself drains oldest-first. `--oldest` restores the
    crawl's own order.
    """
    if ctx.invoked_subcommand is not None:
        return

    from tracker.ingest import discover as disc

    if drop:
        if not (url or row_id or feed):
            _fail(
                "--drop needs something to drop: --url, --id or --feed.\n"
                "Dropping the entire queue is `tracker queue prune --drop` or a "
                "deliberate `--feed` per feed."
            )
        engine, _ = init_db(_db_path())
        with session_scope(engine) as session:
            removed = disc.drop_pending(
                session,
                list(url) if url else None,
                ids=list(row_id) if row_id else None,
                feeds=[feed] if feed else None,
            )
        console.print(f"dropped {removed} queued candidate(s)")
        return

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        rows = disc.failed(session) if failed else disc.pending(session)
        if feed:
            rows = [r for r in rows if (r.feed or "") == feed]
        total = len(rows)
        if newest:
            rows = sorted(rows, key=lambda r: (str(r.published_at or ""), r.id), reverse=True)
        rows = rows[:limit] if limit else rows

        if not rows:
            if failed:
                console.print("[green]no failed URLs[/green]")
            elif feed:
                console.print(f"[green]nothing queued from {escape(feed)}[/green]")
            else:
                console.print(
                    "[green]queue is empty[/green] — run `tracker discover` to look for articles"
                )
            return

        if json_mode():
            emit(
                {
                    "total": total,
                    "rows": [
                        {
                            "id": r.id,
                            "url": r.url,
                            "title": r.title,
                            "feed": r.feed,
                            "published_at": str(r.published_at or ""),
                            "status": r.status,
                        }
                        for r in rows
                    ],
                }
            )
            return

        table = Table(
            title=f"{total} {'unread' if failed else 'queued'} candidate(s)"
            + (f" from {feed}" if feed else ""),
            header_style="bold",
            title_justify="left",
            box=TABLE_BOX,
        )
        table.add_column("id", justify="right")
        table.add_column("published")
        table.add_column("feed")
        table.add_column("headline", max_width=52)
        table.add_column("url", overflow="fold")
        for row in rows:
            table.add_row(
                str(row.id),
                str(row.published_at or NA)[:10],
                escape(row.feed or NA),
                escape((row.title or NA)[:70]),
                escape(row.url),
            )
        console.print(table)
        if total > len(rows):
            console.print(f"[dim]showing {len(rows)} of {total}; --limit to see more[/dim]")
        console.print(
            "\n[dim]crawl them:  [/dim] tracker ingest crawl --from-queue\n"
            "[dim]drop one:    [/dim] tracker queue --drop --id <ID>\n"
            "[dim]drop a feed: [/dim] tracker queue --drop --feed <FEED>\n"
            "[dim]dead links:  [/dim] tracker queue check\n"
            "[dim]off-topic:   [/dim] tracker queue prune"
        )


@queue_app.command("check")
def queue_check(
    limit: Annotated[
        int, typer.Option("--limit", help="URLs to probe. 0 checks the whole queue.")
    ] = 200,
    feed: Annotated[
        str | None, typer.Option("--feed", help="Only this feed.", show_default=False)
    ] = None,
    drop: Annotated[
        bool, typer.Option("--drop", help="Delete the ones that are gone. Nothing else.")
    ] = False,
) -> None:
    """Ask every queued URL whether it is still there, and optionally drop the dead.

    A queued URL is never re-checked between the sitemap that produced it and the
    crawl that spends an LLM call on it, and a sitemap is a snapshot: articles get
    unpublished. This is the check, and it is deliberately conservative about what
    "dead" means.

    **404 and 410 are dead. 403 and 429 are not.** A newsroom answering 403 to a
    non-browser is exactly the case `tracker ingest crawl --browser` exists for,
    and on the live queue that was 55 URLs across seven publishers — the
    best-defended sources, which is often to say the good ones. Dropping those
    would have been the most expensive tidy-up available.
    """
    from tracker.ingest import discover as disc

    engine = _read_engine() if not drop else _writable("queue check")
    settings = get_settings()

    with session_scope(engine, commit=drop) as session:
        rows = disc.pending(session)
        if feed:
            rows = [r for r in rows if (r.feed or "") == feed]
        if limit:
            rows = rows[:limit]
        if not rows:
            console.print("[green]nothing queued to check[/green]")
            return

        if not json_mode():
            console.print(f"[dim]asking {len(rows)} URL(s)…[/dim]")
        verdicts = disc.verify_urls(rows, settings=settings)
        dead = [v for v in verdicts if v.verdict == "dead"]
        blocked = [v for v in verdicts if v.verdict == "blocked"]
        errored = [v for v in verdicts if v.verdict == "error"]
        alive = [v for v in verdicts if v.verdict == "ok"]

        removed = 0
        if drop and dead:
            removed = disc.drop_ids(session, [v.row_id for v in dead])

    if json_mode():
        emit(
            {
                "checked": len(verdicts),
                "ok": len(alive),
                "dead": [{"id": v.row_id, "url": v.url, "status": v.status} for v in dead],
                "blocked": len(blocked),
                "errors": len(errored),
                "dropped": removed,
            }
        )
        return

    _print_report_rows(
        [
            ("checked", len(verdicts)),
            ("reachable", len(alive)),
            ("gone (404/410)", len(dead)),
            ("blocked (403/429)", len(blocked)),
            ("could not tell", len(errored)),
        ],
        title="queue check",
        warn={"gone (404/410)"},
    )
    for verdict in dead[:20]:
        console.print(
            f"  [red]{verdict.status or 'no answer'}[/red] #{verdict.row_id} {escape(verdict.url)}"
        )
    if len(dead) > 20:
        console.print(f"  [dim]…and {len(dead) - 20} more[/dim]")
    if blocked:
        hosts: dict[str, int] = {}
        for verdict in blocked:
            host = urlsplit(verdict.url).netloc.removeprefix("www.")
            hosts[host] = hosts.get(host, 0) + 1
        listed = ", ".join(f"{h} x{n}" for h, n in sorted(hosts.items(), key=lambda kv: -kv[1])[:6])
        console.print(
            f"\n[yellow]{len(blocked)} blocked[/yellow] [dim]— {escape(listed)}. "
            "Kept: these are the pages `tracker ingest crawl --browser` is for.[/dim]"
        )
    if drop:
        console.print(f"\n[green]dropped {removed} dead URL(s)[/green]")
    elif dead:
        console.print("\n[dim]remove them with:[/dim] tracker queue check --drop")


@queue_app.command("prune")
def queue_prune(
    drop: Annotated[
        bool, typer.Option("--drop", help="Delete them. Without this it only reports.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="Examples to print.")] = 25,
) -> None:
    """Re-apply the discovery filter to everything already queued.

    The filter in `seed/feeds.toml` is data, and data gets edited — a term added, an
    exclusion tightened, a newsroom marked `topic_implied`. Nothing ever re-applied
    it to rows that were queued under an earlier version, so the queue accumulated
    everything that passed any *past* filter. Measured on the live database, 417 of
    1,241 queued candidates no longer qualified: NTT marketing articles, DataBank
    compliance blogs, sponsored posts and Meta's announcement of the winners of an
    AR effects contest, each of which would have cost an extraction call to
    discover it says nothing about a data center project.

    Free and read-only until `--drop`. Rows from a feed that is no longer in the
    config are left alone: commenting out a feed should not delete a queue.
    """
    from tracker.ingest import discover as disc

    engine = _writable("queue prune") if drop else _read_engine()
    with session_scope(engine, commit=drop) as session:
        try:
            stale, examined = disc.refilter_pending(session)
        except disc.DiscoverError as exc:
            _fail(str(exc))
            raise
        removed = disc.drop_ids(session, [c.row_id for c in stale]) if drop and stale else 0

    if json_mode():
        emit(
            {
                "examined": examined,
                "no_longer_matching": [
                    {"id": c.row_id, "url": c.url, "feed": c.feed, "reason": c.reason}
                    for c in stale
                ],
                "dropped": removed,
            }
        )
        return

    if not stale:
        console.print(
            f"[green]the whole queue still matches the filter[/green] "
            f"[dim]— {examined} candidate(s) examined.[/dim]"
        )
        return

    by_feed: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for candidate in stale:
        by_feed[candidate.feed or "?"] = by_feed.get(candidate.feed or "?", 0) + 1
        by_reason[candidate.reason] = by_reason.get(candidate.reason, 0) + 1

    console.print(
        f"[yellow]{len(stale)}[/yellow] of {examined} queued candidate(s) would not be "
        "queued by today's filter\n"
    )
    table = Table(header_style="bold", box=TABLE_BOX, title_justify="left", title="by feed")
    table.add_column("feed")
    table.add_column("would go", justify="right")
    for name, count in sorted(by_feed.items(), key=lambda kv: -kv[1])[:15]:
        table.add_row(escape(name), str(count))
    console.print(table)
    console.print("\n[bold]why[/bold]")
    for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1])[:6]:
        console.print(f"  {count:>4}  [dim]{escape(reason)}[/dim]")
    console.print("\n[bold]examples[/bold]")
    for candidate in stale[:limit]:
        console.print(f"  #{candidate.row_id} [dim]{escape((candidate.title or '')[:64])}[/dim]")
        console.print(f"        {escape(candidate.url)}")
    if len(stale) > limit:
        console.print(f"  [dim]…and {len(stale) - limit} more[/dim]")

    if drop:
        console.print(f"\n[green]dropped {removed} candidate(s)[/green]")
    else:
        console.print("\n[dim]remove them with:[/dim] tracker queue prune --drop")


@app.command("export")
def export_cmd(
    fmt: Annotated[str, typer.Argument(help="md | csv | json | html")],
    out: Annotated[Path | None, typer.Option("--out", help="Write here instead of stdout.")] = None,
    company: Annotated[str | None, typer.Option("--company")] = None,
    state: Annotated[str | None, typer.Option("--state")] = None,
    phase: Annotated[str | None, typer.Option("--phase")] = None,
    min_confidence: Annotated[int | None, typer.Option("--min-confidence", min=0, max=3)] = None,
    stamp: Annotated[
        bool,
        typer.Option(
            "--stamp/--no-stamp",
            help="Include a generated-at timestamp. Off by default so output is reproducible.",
        ),
    ] = False,
) -> None:
    """Write the database out as Markdown, CSV or JSON.

    Output is deterministic: the same data exports byte-identically every time,
    unless you ask for --stamp.
    """
    from tracker.export import FORMATS, ExportFilter, fetch_projects, render, write_export
    from tracker.models import utcnow

    if fmt not in FORMATS:
        _fail(f"format must be one of: {', '.join(FORMATS)}")
    if phase and phase not in PHASES:
        _fail(f"--phase must be one of: {', '.join(PHASES)}")

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        projects = fetch_projects(session, ExportFilter(company, state, phase, min_confidence))
        text = render(fmt, projects, generated_at=utcnow().isoformat(sep=" ") if stamp else None)

    if out is None:
        # Written directly rather than through Rich: an export is data, and Rich
        # would wrap long lines and interpret square brackets as markup.
        sys.stdout.write(text)
    else:
        write_export(text, out)
        err.print(f"wrote {len(projects)} project(s) to [bold]{out}[/bold]")


@app.command()
def review(
    limit: Annotated[int, typer.Option("--limit", help="Rows to review at most.")] = 20,
    max_confidence: Annotated[
        int, typer.Option("--max-confidence", min=0, max=3, help="Review at or below this.")
    ] = 1,
    verify: Annotated[
        int | None,
        typer.Option("--verify", help="Mark this project id as operator-verified and exit."),
    ] = None,
    unverify: Annotated[
        int | None, typer.Option("--unverify", help="Clear the verification on this id.")
    ] = None,
) -> None:
    """Show low-confidence projects, or record that you have verified one.

    Per the PRD, confidence below 2 always needs a human. Verifying a row sets
    `last_verified_at`, which is deliberately separate from `updated_at`:
    `updated_at` means "a field changed", this means "an operator says it is right".
    """
    from tracker.confidence import compute_for_project, needs_review
    from tracker.models import utcnow
    from tracker.upsert import recompute_confidence

    if verify is not None and unverify is not None:
        _fail("pass only one of --verify and --unverify")

    if verify is not None or unverify is not None:
        target = verify if verify is not None else unverify
        engine, _ = init_db(_db_path())
        with session_scope(engine) as session:
            project = session.get(Project, target)
            if project is None:
                _fail(f"no project with id {target}", code=1)
                return
            project.last_verified_at = utcnow() if verify is not None else None
            session.flush()
            recompute_confidence(session)
            session.refresh(project)
            state = "verified" if verify is not None else "no longer verified"
            console.print(
                f"project {project.id} ({project.company} / {project.name}) is {state}; "
                f"confidence is now {project.confidence}"
            )
        return

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        projects = session.scalars(
            select(Project)
            .where(Project.confidence <= max_confidence)
            .order_by(Project.confidence.asc(), Project.company.asc(), Project.id.asc())
            .limit(limit)
        ).all()

        if not projects:
            console.print(
                f"[green]nothing to review[/green] — no project at or below confidence "
                f"{max_confidence}"
            )
            return

        console.print(
            f"[bold]{len(projects)} project(s) need review[/bold] "
            f"(confidence <= {max_confidence})\n"
        )
        for p in projects:
            score = compute_for_project(p, p.sources)
            console.print(
                f"[bold]#{p.id}[/bold] {p.company} — {p.name} ({_location(p)})  "
                f"confidence {_confidence_cell(p.confidence)}"
            )
            console.print(f"  why: {'; '.join(score.reasons)}")
            for r in _ordered_risks(p.risks):
                if r.status == OPEN_RISK_STATUS and r.source_id is None:
                    console.print(
                        f"  [yellow]uncited {r.severity} risk[/yellow] "
                        f"([cyan]{r.category}[/cyan]): {escape(r.summary)}"
                    )
            for s in sorted(p.sources, key=lambda x: x.url):
                console.print(f"  [cyan]{s.source_type}[/cyan] {s.url}")
            for line in (p.notes or "").splitlines():
                if line.strip():
                    console.print(f"  [dim]{line}[/dim]")
            console.print(f"  [dim]verify with:[/dim] tracker review --verify {p.id}\n")
        console.print(
            f"[dim]{sum(1 for p in projects if needs_review(p.confidence))} of these are "
            "below the auto-approval threshold of 2.[/dim]"
        )


@app.command("verify")
def verify_coverage(
    required: Annotated[
        Path | None,
        typer.Option("--required", help="File of required project names, one per line."),
    ] = None,
    target: Annotated[int, typer.Option("--target", help="Project count the PRD asks for.")] = 30,
) -> None:
    """Report progress toward the required project list.

    The PRD's definition of done names 30 specific projects, but that list is not
    in the PRD text. This turns an unmeasurable requirement into a measurable one:
    paste the names into `seed/required-projects.txt` and this reports which are
    present. Until then it reports the count against `--target`.
    """
    from tracker import required as required_list

    required = required or required_list.default_path()

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        projects = session.scalars(select(Project)).all()
        total = len(projects)
        confident = sum(1 for p in projects if p.confidence >= 2)

        console.print(f"projects in database: [bold]{total}[/bold] (target {target})")
        console.print(f"at confidence >= 2:   [bold]{confident}[/bold]")
        if total < target:
            console.print(f"[yellow]{target - total} short of the target count[/yellow]")

        if not required.is_file():
            console.print(
                f"\n[dim]no required-project list at {required}.\n"
                "Create it with one project per line (`Company | Project name` or just a "
                "name) to get a present/missing breakdown.[/dim]"
            )
            return

        wanted = required_list.load(required)
        if not wanted:
            console.print(f"\n[dim]{required.name} is empty.[/dim]")
            return

        matches = required_list.match(projects, wanted)
        present = sum(1 for m in matches if m.met)
        console.print(f"\nrequired list: [bold]{present}/{len(wanted)}[/bold] present")
        for m in matches:
            if m.met:
                console.print(f"  [green]ok[/green]      #{m.project_id}  {m.entry}")
        for m in matches:
            if not m.met:
                console.print(f"  [red]missing[/red] {m.entry}")


def _print_report_rows(
    rows: list[tuple[str, int]], *, title: str, warn: set[str] | None = None
) -> None:
    bad = {"rejected", "fetch errors", "parse errors"}
    caution = {"duplicates flagged", "field conflicts"} | (warn or set())
    table = Table(title=title, header_style="bold", title_justify="left", box=TABLE_BOX)
    table.add_column("outcome")
    table.add_column("count", justify="right")
    for label, count in rows:
        style = None
        if count and label in bad:
            style = "red"
        elif count and label in caution:
            style = "yellow"
        table.add_row(label, str(count), style=style)
    console.print(table)


def _print_report(report, *, title: str) -> None:
    _print_report_rows(report.as_rows(), title=title)

    if report.rejected:
        err.print(
            f"[yellow]{report.rejected} record(s) rejected[/yellow] — see the log lines above."
        )


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point.

    Runs in typer's standalone mode, so typer itself renders usage errors and
    raises `SystemExit` with the right code — which propagates out through
    `sys.exit(main())` unchanged. The alternative (`standalone_mode=False`)
    requires catching click's exception hierarchy, and typer 0.27 vendors click
    under the private `typer._click`, so there is nothing stable to catch.
    """
    app(args=argv)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
