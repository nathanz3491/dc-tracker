"""The kernel every `tracker` command module shares.

The Typer app and its sub-groups, the two Rich consoles, and the dozen helpers that
open the database, take the write lock, fail with an operator-facing message and
emit JSON. Nothing in here is a command; every command module imports from here
and registers onto the groups defined here.

**Why this file exists.** `cli.py` was one 10,000-line module holding 63 commands.
A dependency graph of it showed the shape: about thirty names used by every
command group, and otherwise groups that touch nothing outside themselves. Those
thirty are this file, so a family of commands can live in its own module without
importing the package that imports it.

Nothing here may import a command module. The package `__init__` imports the
families; the families import this; the arrow only points one way.
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

import typer
from rich import box
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.table import Table

from tracker.config import get_settings
from tracker.db import AlreadyRunning, MigrationError, acquire_write_lock, init_db, open_db
from tracker.models import Project

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

#: The ranking and the policy it justifies, under one name. `invoke_without_command`
#: so bare `tracker sources` keeps printing today's table unchanged.
sources_app = typer.Typer(name="sources", invoke_without_command=True)
app.add_typer(sources_app)

#: The listing and the confirmation pass over obstacles, under one name.
risks_app = typer.Typer(name="risks", invoke_without_command=True)
app.add_typer(risks_app)

#: The watchlist the updates page is about: bare `tracker watch` lists it, `add`
#: and `rm` edit it. The console's landing page writes the same rows through the
#: same module, so a watch set on the page and a watch set here are one thing.
#:
#: Every row belongs to an account, and this side reads them all: a terminal on the
#: host is looking at the database, not at one person's slice of it. `--user`
#: narrows to one, and is required to write, because writing to the wrong person's
#: list is invisible from here.
watch_app = typer.Typer(name="watch", invoke_without_command=True)
app.add_typer(watch_app)

#: Delivering a digest by email. Its own group rather than a flag on `digest`,
#: because the unit is different: `digest` answers for one watchlist and prints,
#: this one loops over people and sends at most one message each.
notify_app = typer.Typer(
    name="notify",
    help="Send each person one email with everything on their watchlist that moved.",
    no_args_is_help=True,
)
app.add_typer(notify_app)

#: Who may sign in to the console. Accounts are made here and nowhere else — a
#: browser can only create one by redeeming an invite that was minted here.
users_app = typer.Typer(name="users", invoke_without_command=True)
app.add_typer(users_app)

#: The news watch: bare `tracker news` searches the news for every campus that is due;
#: `report` is the morning's account of what every source found.
news_app = typer.Typer(name="news", invoke_without_command=True)
app.add_typer(news_app)

#: The company registry (migration 0043): bare `tracker company` lists every company
#: with its campuses and spellings; `show`, `alias`, `unalias`, `rename` and `parent`
#: read and edit it.
company_app = typer.Typer(name="company", invoke_without_command=True)
app.add_typer(company_app)

#: Who plays which role on one campus, and whose role has ended (migration 0044):
#: `tracker party list ID` lists them; `end` and `resume` are a person's hand. No
#: bare form: a group that took a campus number would read `end` as the campus.
party_app = typer.Typer(
    name="party",
    help="Who plays which role on a campus, and whose role has ended.",
    no_args_is_help=True,
)
app.add_typer(party_app)

#: How each campus's tranches nest: bare `tracker tranches` lists the campuses whose
#: tranches still do not add up; `show` draws one as its tree, `review` puts them to
#: the judgement model, `rule` and `undo` are a person's hand.
tranches_app = typer.Typer(name="tranches", invoke_without_command=True)
app.add_typer(tranches_app)

#: The answer key (migration 0050): bare `tracker answers` scores the database against
#: the verified answers; `load` stores a batch and `show` lists them beside the rows.
answers_app = typer.Typer(name="answers", invoke_without_command=True)
app.add_typer(answers_app)

#: The discovery queue: bare `tracker queue` lists it; `check`, `stats` and `prune`
#: work on it. Last, so the group order in `--help` is unchanged from before the
#: package split, when this was defined beside its commands.
queue_app = typer.Typer(name="queue", invoke_without_command=True)
app.add_typer(queue_app)

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

#: Shown when a fetch failed and no browser rung is installed. The backslash
#: escapes the bracket for Rich, which would otherwise read "[browser]" as a style
#: tag and delete the extra's name from the very message saying what to install.
BROWSER_HINT = (
    "[dim]some fetches failed. Pages that assemble themselves with JavaScript need a "
    r'headless browser: pip install -e ".\[browser]", then PLAYWRIGHT_BROWSERS_PATH='
    ".cache/ms-playwright python -m playwright install --only-shell chromium[/dim]"
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


def _use_llm(provider: str | None) -> None:
    """Apply a per-run `--llm-provider` choice before anything reads settings.

    Through the environment plus a cache clear rather than a parallel state dict,
    because that is the one route every reader already honours: `get_settings` is
    lru_cached over the environment, the factories in `tracker.llm` read it
    lazily, and nothing has to be told twice. Called as the first statement of
    every command that spends LLM calls, so no `get_settings()` in the body can
    run ahead of it.

    Which also makes it the place that arms the peak-hours guard
    (`tracker.llm.check_peak`): it runs in exactly the commands that can spend, and
    in no request the console serves.
    """
    from tracker.llm import arm_peak_guard

    arm_peak_guard()
    if provider is None:
        return
    from tracker.llm import LLM_PROVIDERS

    if provider not in LLM_PROVIDERS:
        _fail(f"--llm-provider must be one of {', '.join(LLM_PROVIDERS)} (got {provider!r})")
        return
    os.environ["TRACKER_LLM_PROVIDER"] = provider
    get_settings.cache_clear()


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


def _daytime_ledger() -> None:
    """Record this process's paid calls in the month's daytime ledger, unless one is set.

    The nightly loop names a ledger per night (`TRACKER_SPEND_LEDGER`) and every
    command it runs writes there. Everything else -- a command run by hand, the
    console's AI panels (`tracker serve`) -- recorded nothing, so the loop's share
    of the OpenCode Go month (`tracker.spend.allowance`) had to guess at them: ¥1 a
    day, held back unmeasured. Now each such process appends to
    `runs/daytime-YYYYMM.spend` beside the database, which `allowance` reads with the
    nights'. Only where that `runs` directory already exists -- the installation's,
    never a test's temporary database -- and never over a ledger already named.
    """
    if os.environ.get("TRACKER_SPEND_LEDGER") or get_settings().spend_ledger is not None:
        return
    runs = _db_path().parent / "runs"
    if not runs.is_dir():
        return
    import datetime as dt

    os.environ["TRACKER_SPEND_LEDGER"] = str(runs / f"daytime-{dt.date.today():%Y%m}.spend")
    get_settings.cache_clear()


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
    _daytime_ledger()
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


# --- the paid passes ---------------------------------------------------------
#
# `duplicates resolve`, `audit resolve`, `risks confirm`, `logic resolve`,
# `tranches review` and `enrich` each put a batch of questions to a model through
# `tracker.judged.run`. What they print about that — what was held back, where the
# money ran out, what the batch came to — is said here once, so no two of them say
# it differently.


def again_flag(help: str):
    """The `--again` option every paid pass carries: ask what was already put to it."""
    return typer.Option("--again", help=help)


def _model(tier: str = "judgement"):
    """The extractor a paid pass asks, or the command ends saying why there is none.

    `judgement` is the per-item pick from a closed menu (`llm.judgement_extractor`,
    `TRACKER_DEEPSEEK_JUDGEMENT_EFFORT`, `TRACKER_JUDGEMENT_PROVIDER`); `agent` is
    the tool-using loop (`llm.agent_extractor`, `TRACKER_DEEPSEEK_AGENT_EFFORT`).
    """
    from tracker.llm import LLMUnavailable, agent_extractor, judgement_extractor

    build = agent_extractor if tier == "agent" else judgement_extractor
    try:
        return build(get_settings())
    except LLMUnavailable as exc:
        _fail(str(exc))
        raise


def _workers(tier: str = "judgement") -> int:
    """How many of a pass's questions may be in flight at once.

    The provider that will actually answer that tier decides it
    (`Settings.llm_workers`): the judgement tier can be sent to a local model on its
    own (`judgement_provider`), and a local model wants one.
    """
    settings = get_settings()
    provider = settings.llm_provider
    if tier != "agent":
        provider = settings.judgement_provider or provider
    return settings.llm_workers(provider)


def _print_held(
    count: int, what: str, why: str = "already put to the model", *, out: Console | None = None
) -> None:
    """`holding back 3 pair(s) ... on the same evidence; --again asks them.`"""
    if count and not json_mode():
        (out or console).print(
            f"[dim]holding back {count} {what} {why} on the same evidence; --again asks them.[/dim]"
        )


def _print_ceiling(run) -> bool:
    """Say that the night's money stopped a pass, in the words the loop looks for.

    One line, on stdout — on stderr under `--json`, which must stay a payload. The
    command then ends normally: stopping at the ceiling is the ceiling working, and
    everything done before it is committed.
    """
    from tracker.spend import CEILING_REACHED

    if not run.ceiling:
        return False
    out = err if json_mode() else console
    out.print(
        f"[yellow]{CEILING_REACHED}[/yellow] [dim]— {escape(run.ceiling)}; "
        f"{len(run.unreached)} item(s) not started, everything before them kept.[/dim]"
    )
    return True


def _print_tally(
    parts: list[tuple[int, str, str]],
    tokens=None,
    *,
    beyond: int = 0,
    out: Console | None = None,
) -> None:
    """A pass's closing line and its prompt-cache rate.

    `parts` is `(count, phrase, style)`: a `bold` part is always shown, any other
    only when it is not zero — `3 ruled, 2 left alone, 1 errored, 4 beyond --limit
    ~45,000 tokens`. `tokens` is a `judged.Tokens`.
    """
    shown = [
        f"[{style}]{count}[/{style}] {phrase}"
        for count, phrase, style in parts
        if count or style == "bold"
    ]
    if beyond:
        shown.append(f"{beyond} beyond --limit")
    line = "\n" + ", ".join(shown)
    if tokens is not None:
        line += f"  [dim]~{tokens.total:,} tokens[/dim]"
    out = out or console
    out.print(line)
    if tokens is not None and (tokens.cache_hit or tokens.cache_miss):
        # The number that decides what a night costs: a cached prompt token is a
        # fiftieth of an uncached one, and an agent loop re-sends its history every
        # turn. A low rate here means the prefix is being disturbed.
        rate = tokens.cache_hit / (tokens.cache_hit + tokens.cache_miss)
        out.print(
            f"  [dim]prompt cache: {rate:.0%} hit ({tokens.cache_hit:,} cached, "
            f"{tokens.cache_miss:,} not)[/dim]"
        )
