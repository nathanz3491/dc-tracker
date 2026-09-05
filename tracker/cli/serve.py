"""`tracker serve`, `tracker cloudflare`, `tracker tui` — the three ways to look at
the dataset without typing a query.

Publishing is here too: the console is public and account-gated, and the tunnel is
what makes it reachable. Per `CLAUDE.md` §4 the tool refuses to publish with no
accounts, because a tunnel bypasses the loopback-only check by design.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from tracker.cli._shared import (
    _db_path,
    _fail,
    _read_engine,
    app,
    console,
)
from tracker.config import get_settings
from tracker.db import MigrationError, open_db, session_scope


@app.command()
def tui(
    check: Annotated[
        bool,
        typer.Option("--check", help="Boot headless, fill every pane, exit. For a host or CI."),
    ] = False,
    screenshot: Annotated[
        Path | None,
        typer.Option(
            "--screenshot",
            help="Render one frame to this SVG and exit. Implies --check.",
            show_default=False,
        ),
    ] = None,
    pane: Annotated[
        str | None,
        typer.Option("--pane", help="Pane to leave open for --screenshot.", show_default=False),
    ] = None,
    width: Annotated[int, typer.Option("--width", help="Headless terminal width.")] = 160,
    height: Annotated[int, typer.Option("--height", help="Headless terminal height.")] = 48,
) -> None:
    """A full-screen interface over the same data and the same commands.

    Six panes, and the last one is every command this CLI has:

    \b
      1 overview   the headline numbers, field coverage drawn, open obstacles
      2 projects   the table, filtered live, one project opened beside it
      3 coverage   rostered operators against the rows we hold
      4 capex      who is buying the capacity, and which year it lands
      5 queue      what is waiting to be read, and what could not be
      6 run        every CLI command, its whole flag surface, and its output

    The command list is read out of this CLI rather than written down, so anything
    added to it appears there with its real flags — including the confirmation a
    command that spends tokens or deletes rows requires, which is the console's
    ritual unchanged rather than a second, laxer gate.

    `r` re-reads the database, `/` jumps to the projects filter, and `e`, `s` and
    `p` prefill `enrich`, `show` and `prospect` for whatever the cursor is on.

    `--check` boots it headless against the real database, fills every pane and
    exits non-zero if any of them failed — which is how "does the TUI work on the
    host" gets answered over ssh, with nobody sitting at a terminal there.
    `--screenshot` writes what it rendered to an SVG.
    """
    from tracker import tui as tui_mod
    from tracker.tui.app import TrackerApp

    if pane is not None and pane not in TrackerApp.PANES:
        _fail(f"--pane must be one of {', '.join(TrackerApp.PANES)}")
        return
    # The database is opened here purely as a precondition, and read-only, so a
    # missing file says "run `tracker init`" rather than opening a full-screen
    # interface over nothing. The app opens it again for itself.
    _read_engine()
    try:
        code = tui_mod.run(
            _db_path(),
            check=check or screenshot is not None,
            screenshot=screenshot,
            pane=pane,
            size=(width, height),
        )
    except tui_mod.MissingDependency as exc:
        _fail(str(exc))
        return
    if check or screenshot is not None:
        if code == 0:
            console.print("[green]every pane filled[/green]")
            if screenshot is not None:
                console.print(f"[dim]wrote {screenshot}[/dim]")
        raise typer.Exit(code)


#: `--run/--no-run` used to decide whether the page could execute commands. It
#: cannot any more — the runner is gone — so the flag does nothing.
#:
#: **It is still accepted, and that is a deploy-safety decision rather than
#: politeness.** The host's `serve.sh` lives outside the repo (`deploy/` is
#: gitignored, per CLAUDE.md §5), so the poller does not update it: it will keep
#: passing `--no-run` after this commit lands. A flag that errored would turn the
#: next launchd restart into an argument-parsing failure with nothing serving the
#: console — an outage caused by a *removal*, which is the worst kind to debug.
#:
#: So it warns and carries on, and is removed once `serve.sh` has been edited on
#: the host. `hidden=True` keeps it out of `--help`, where advertising a no-op
#: would only invite somebody to pass it.
_DEPRECATED_RUN = typer.Option(
    "--run/--no-run",
    hidden=True,
    help="Does nothing. The console can no longer run commands; use `tracker tui`.",
)


def _warn_deprecated_run(run: bool) -> None:
    """Say the flag was noticed and ignored. Only when it was actually passed."""
    if not run:
        console.print(
            "[yellow]--no-run does nothing now[/yellow][dim] — this console cannot run "
            "commands at all. Drop the flag; `tracker tui` has the palette.[/dim]"
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
    run: Annotated[bool, _DEPRECATED_RUN] = True,
    ai: Annotated[
        bool,
        typer.Option(
            "--ai/--no-ai",
            help="Allow the LLM panels (briefing, infer, capex overview).",
        ),
    ] = True,
    watch_edits: Annotated[
        bool,
        typer.Option(
            "--watch-edits/--no-watch-edits",
            help="Allow the landing page to edit the signed-in account's watchlist.",
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
            help="Publish through a Cloudflare quick tunnel. Requires an account.",
        ),
    ] = False,
) -> None:
    """Open the console: the database as a live page, read-only.

    Different from `tracker export html`, and both are worth having. The export is
    one self-contained file you can email, frozen at the moment it was written.
    This re-reads the database on every request.

    **It cannot change the data.** Nothing here writes a project, a citation or a
    figure — that is the CLI's job, and `tracker tui` is the version of this with
    the commands in it. The single exception is each reader's own watchlist, which
    is a preference rather than a fact.

    `tracker users add` puts a sign-in in front of it and gives each person their
    own watchlist. With no accounts it opens straight in, which is fine on loopback
    — reaching localhost already means having the machine — and is why `--tunnel`
    refuses until at least one account exists.

    `--tunnel` uses the tunnel configured in `TRACKER_TUNNEL_NAME` /
    `TRACKER_TUNNEL_HOSTNAME` if there is one, and an anonymous quick tunnel
    otherwise. `tracker cloudflare` is the same thing with a readiness check and
    flags to override either.
    """
    settings = get_settings()
    _warn_deprecated_run(run)
    _run_console(
        port=port,
        host=host,
        open_browser=open_browser,
        ai=ai,
        watch_edits=watch_edits,
        allow_remote=allow_remote,
        publish=(("named" if settings.tunnel_name else "quick") if tunnel else None),
        tunnel_name=settings.tunnel_name if tunnel else None,
        hostname=settings.tunnel_hostname if tunnel else None,
    )


def _console_accounts() -> int:
    """How many accounts exist, i.e. whether the console has a way to gate itself.

    This replaced `TRACKER_CONSOLE_PASSWORD`. The question a publish check has to
    ask is the same one it always asked — "is there anything in front of this?" —
    but the answer now lives in the database rather than the environment, which is
    also why it is a *count* rather than a boolean: the messages want to say how
    many, and "0" is what refuses a tunnel.

    A database that is missing or unmigrated returns 0 rather than raising, and
    `open_db` is called directly rather than through `_read_engine` for exactly
    that reason: `_read_engine` turns both into `_fail`, which exits. The caller
    wants to reach `_console_preflight`, which says the same thing about a missing
    file and also checks the front-end files — so a broken install reports
    everything wrong with it rather than the first thing.
    """
    from sqlalchemy.exc import OperationalError

    from tracker import accounts

    try:
        with session_scope(open_db(_db_path()), commit=False) as session:
            return accounts.count(session)
    except (FileNotFoundError, MigrationError, OperationalError):
        return 0


def _console_preflight() -> Path:
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
    ai: bool = True,
    watch_edits: bool = True,
    allow_remote: bool = False,
    publish: str | None = None,
    tunnel_name: str | None = None,
    hostname: str | None = None,
    proxy: str | None = None,
    use_proxy: bool = True,
) -> None:
    """Start the console, optionally behind cloudflared. Shared by `serve` and `cloudflare`.

    One implementation rather than two, because the interesting part is the
    refusals — no account behind a public URL, a non-loopback bind without
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

    accounts_held = _console_accounts()

    if host not in {"127.0.0.1", "localhost", "::1"} and not allow_remote:
        _fail(
            f"--host {host} would expose this console to the network.\n"
            "It cannot run commands, but anyone who can reach that address could "
            "read the whole dataset and — with --ai — spend LLM tokens a panel at "
            "a time. Pass --allow-remote if that is genuinely what you want."
        )

    # A tunnel is a public URL handed to anyone who learns it. Refusing rather than
    # warning is the point: a warning scrolls past, and there is no safe reading of
    # "published and open". The console can no longer start a run, so what is behind
    # the URL is the dataset and the token spend — still not things to publish
    # anonymously.
    if publish and not accounts_held:
        _fail(
            "publishing this console puts it on a public URL, and it has no accounts, "
            "so there is nothing in front of it.\n\n"
            "Make one first:\n"
            "  tracker users add you@example.com\n\n"
            "Anything that reaches the URL could otherwise read the whole dataset "
            "and spend LLM tokens on the model panels."
        )

    path = _console_preflight()

    console.print(f"database: [bold]{path}[/bold]")
    console.print(f"local:    [bold]http://{host}:{port}/[/bold]")
    console.print(
        f"[green]{accounts_held} account(s)[/green] — everyone signs in"
        if accounts_held
        else "[yellow]no accounts[/yellow] — open, and no watchlists. "
        "Fine on loopback; `tracker users add` to change either."
    )
    console.print("[dim]read-only: nothing here can change a project[/dim]")

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
            allow_ai=ai,
            allow_watch=watch_edits,
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
    run: Annotated[bool, _DEPRECATED_RUN] = True,
    ai: Annotated[
        bool,
        typer.Option(
            "--ai/--no-ai",
            help="Allow the LLM panels (briefing, infer, capex overview).",
        ),
    ] = True,
    watch_edits: Annotated[
        bool,
        typer.Option(
            "--watch-edits/--no-watch-edits",
            help="Allow the landing page to edit the signed-in account's watchlist.",
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

    **At least one account is required either way and this refuses to start
    without one** (`tracker users add`). The URL is public, and what is behind it is
    the whole dataset plus, with `--ai`, a model panel that spends real tokens per
    click; a random hostname is obscurity, not access control.

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

    _warn_deprecated_run(run)
    _run_console(
        port=port,
        host="127.0.0.1",
        open_browser=False,
        ai=ai,
        watch_edits=watch_edits,
        publish="named" if name else "quick",
        tunnel_name=name,
        hostname=hostname,
        proxy=proxy,
        use_proxy=use_proxy,
    )


def _cloudflare_check(name: str | None, hostname: str | None = None) -> None:
    """Print a publish-readiness report and exit non-zero if it would fail."""
    from tracker.webui import assets
    from tracker.webui.tunnel import (
        CloudflaredMissing,
        detect_proxy,
        find_cloudflared,
        named_tunnels,
        version,
    )

    rows: list[tuple[bool, str, str]] = []

    held = _console_accounts()
    if held:
        rows.append((True, "accounts", f"{held}, so every visitor signs in"))
    else:
        rows.append(
            (
                False,
                "accounts",
                "none — required to publish. `tracker users add you@example.com`",
            )
        )

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
