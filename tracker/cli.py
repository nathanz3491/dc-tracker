"""`python -m tracker <subcommand>` / `tracker <subcommand>`.

Typer + Rich: the PRD asks for formatted tables on stdout, and both are already
available. Read commands open the database **read-only**, which turns the PRD's
"never modify the DB except for ingest and review" from a convention into a
guarantee enforced by SQLite itself.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich import box
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from sqlalchemy import func, select

from tracker import __version__
from tracker.config import get_settings, install_root
from tracker.db import MigrationError, init_db, open_db, schema_version, session_scope
from tracker.models import Project, Source
from tracker.vocab import PHASES

app = typer.Typer(
    name="tracker",
    help="Track US data center construction projects, with every fact cited.",
    no_args_is_help=True,
    add_completion=False,
)
ingest_app = typer.Typer(name="ingest", help="Load projects from a source.", no_args_is_help=True)
app.add_typer(ingest_app)

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


_utf8(sys.stdout)
_utf8(sys.stderr)

console = Console(width=_width(), soft_wrap=False)
err = Console(stderr=True, width=_width())

#: Rendered for a NULL field. Deliberately ASCII, for the same reason as TABLE_BOX.
NA = "-"

#: Shown when a fetch was blocked. The backslash escapes the bracket for Rich,
#: which would otherwise read "[crawl]" as a style tag and delete the extra's
#: name from the very message telling the operator what to install.
BROWSER_HINT = (
    "[dim]some fetches failed. Several trade-press sites block plain HTTP; "
    r'retry with --browser after: pip install -e ".\[crawl]" && crawl4ai-setup[/dim]'
)

#: Set by the top-level --db option and consumed by each subcommand.
_state: dict[str, object] = {"db": None}


def _fail(message: str, code: int = 2) -> None:
    """Print an operator-facing error and exit without a traceback."""
    err.print(f"[bold red]error[/bold red] {message}")
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


@app.callback()
def main_callback(
    db: Annotated[
        Path | None,
        typer.Option(
            "--db", help="Database file. Defaults to data/tracker.db.", show_default=False
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show debug logging.")] = False,
) -> None:
    _state["db"] = db
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=err, show_time=False, show_path=verbose, markup=False)],
    )


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"dc-tracker {__version__}")


@app.command()
def init() -> None:
    """Create or upgrade the database, then recompute cached confidence."""
    from tracker.upsert import recompute_confidence

    path = _db_path()
    engine, applied = init_db(path)
    with session_scope(engine) as session:
        rescored = recompute_confidence(session)

    console.print(f"database: [bold]{path}[/bold]")
    if applied:
        console.print(f"applied migrations: {', '.join(f'{v:04d}' for v in applied)}")
    else:
        console.print("schema already current, nothing to apply")
    console.print(f"schema version: {schema_version(engine)}")
    if rescored:
        console.print(f"recomputed confidence on {rescored} project(s)")


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
    from_queue: Annotated[
        bool,
        typer.Option(
            "--from-queue", help="Crawl what `tracker discover` queued instead of a file."
        ),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="With --from-queue, take at most this many candidates."),
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
    """
    from tracker.ingest import crawl
    from tracker.llm import MiniMaxExtractor, MissingApiKey

    settings = get_settings()

    if check:
        try:
            info = MiniMaxExtractor(settings).check()
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

    if from_queue and urls is not None:
        _fail("pass either --urls or --from-queue, not both")
        return
    if not from_queue and urls is None:
        _fail("pass --urls FILE, or --from-queue, or --check to test connectivity")
        return

    engine, _ = init_db(_db_path())
    source_label = "queue"

    if from_queue:
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
        extractor = MiniMaxExtractor(settings)
    except MissingApiKey as exc:
        _fail(str(exc))
        return

    escalate = None
    if browser:
        from tracker.ingest.fetch import Crawl4AIFetcher

        escalate = Crawl4AIFetcher(settings)

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


def _filtered(stmt, company, state, phase, min_confidence):
    if company:
        stmt = stmt.where(func.lower(Project.company).like(f"%{company.lower()}%"))
    if state:
        stmt = stmt.where(Project.state == state.upper())
    if phase:
        stmt = stmt.where(Project.phase == phase)
    if min_confidence is not None:
        stmt = stmt.where(Project.confidence >= min_confidence)
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
        stmt = _filtered(select(Project), company, state, phase, min_confidence)
        total = session.scalar(
            select(func.count()).select_from(
                _filtered(select(Project.id), company, state, phase, min_confidence).subquery()
            )
        )
        stmt = stmt.order_by(*order, Project.id.asc())
        if limit:
            stmt = stmt.limit(limit)
        projects = session.scalars(stmt).all()

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
                p.company,
                p.name,
                _location(p),
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

        facts = Table(show_header=False, box=None)
        facts.add_column(style="dim")
        facts.add_column()
        for label, value in [
            ("name", project.name),
            ("company", project.company),
            ("customer", project.customer or NA),
            ("location", _location(project)),
            ("county", project.county or NA),
            ("coordinates", f"{project.lat}, {project.lon}" if project.lat else NA),
            ("phase", project.phase),
            ("MW planned", _fmt_mw(project.mw_planned)),
            ("MW built", _fmt_mw(project.mw_built)),
            ("investment", _fmt_usd(project.investment_usd)),
            ("first announced", str(project.first_announced or NA)),
            ("expected online", str(project.expected_online or NA)),
            ("blocker", project.blocker or NA),
            ("confidence", _confidence_cell(project.confidence)),
            ("created", str(project.created_at)),
            ("updated", str(project.updated_at)),
            ("last verified", str(project.last_verified_at or "never")),
            ("dedup key", project.dedup_key),
        ]:
            facts.add_row(label, str(value))
        console.print(facts)

        score = compute_for_project(project, project.sources)
        console.print(f"\n[dim]why confidence {score.value}:[/dim] {'; '.join(score.reasons)}")

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

        if project.events:
            console.print(f"\n[bold]events[/bold] ({len(project.events)})")
            for e in sorted(project.events, key=lambda x: x.event_date):
                console.print(f"  {e.event_date}  [cyan]{e.event_type}[/cyan]  {e.description}")


@app.command()
def stats() -> None:
    """Summary counts by phase, confidence and state."""
    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        total = session.scalar(select(func.count()).select_from(Project)) or 0
        if not total:
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
            help="Also run this many LLM-proposed Google searches. Needs the Google keys.",
        ),
    ] = 0,
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
    from tracker.llm import MiniMaxExtractor, MissingApiKey
    from tracker.upsert import recompute_confidence

    settings = get_settings()

    # Checked before any network call: every later phase needs it, and failing
    # here costs nothing rather than after a round of feed polling.
    try:
        extractor = MiniMaxExtractor(settings)
    except MissingApiKey as exc:
        _fail(str(exc))
        return

    escalate = None
    if browser:
        from tracker.ingest.fetch import Crawl4AIFetcher

        escalate = Crawl4AIFetcher(settings)

    engine, _ = init_db(_db_path())
    cache_dir = install_root() / ".cache" / "articles"
    totals = {"queued": 0, "new": 0, "refreshed": 0, "failed": 0}

    # --- 1. discover --------------------------------------------------------
    if skip_discover:
        console.print("[dim]1/4 discover — skipped[/dim]")
    else:
        console.rule("[bold]1/4 discover[/bold]", align="left")
        try:
            with session_scope(engine) as session:
                report, _ = disc.run(session, since_days=since_days or None, dry_run=dry_run)
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
    if search:
        from tracker.ingest import search as srch

        if not settings.has_search_keys():
            err.print("[yellow]--search needs the Google keys[/yellow]")
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
                        provider=srch.GoogleCSEProvider(settings),
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
                if s_report.quota_exhausted:
                    err.print("[yellow]daily search quota exhausted[/yellow]")

    # --- 2. extract new -----------------------------------------------------
    console.rule("[bold]2/4 extract new[/bold]", align="left")
    with session_scope(engine, commit=False) as session:
        # known_first spends each LLM call on depth: a queued article covering a
        # project we already track becomes a SECOND source, which fills fields one
        # article cannot and lifts confidence from 2 to 3. Draining oldest-first
        # instead just grows the database sideways with more single-source rows.
        pending_urls = [
            row.url for row in disc.pending(session, limit=limit, known_first=not breadth_first)
        ]
        backlog = len(disc.pending(session))
        deepening, _fresh = disc.pending_split(session)
        # Counted whether or not we are retrying, so the summary can never claim
        # "0 failed" while a dozen articles sit unread.
        unread = disc.failed(session)
        unread_hosts = disc.failure_summary(session)
        if retry_failed:
            room = max(0, limit - len(pending_urls))
            pending_urls += [row.url for row in unread[:room]]

    if pending_urls and not breadth_first and deepening:
        console.print(
            f"[dim]{deepening} of {backlog} queued candidate(s) cover a project already "
            f"tracked; those go first[/dim]"
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
        with session_scope(engine) as session:
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
            with session_scope(engine) as session:
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
        typer.Option("--from-llm", help="Ask MiniMax for this many project search queries."),
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

    With --from-llm, MiniMax proposes which projects to search for. Those are
    leads, never facts: nothing the model names is stored, and a project only
    becomes a row once a real article has been fetched and its values backed by
    verbatim quotes. If the model invents a project, the search finds nothing.
    """
    from tracker.ingest import search as srch
    from tracker.llm import MiniMaxExtractor, MissingApiKey

    settings = get_settings()
    queries = list(query or [])

    if from_llm:
        try:
            extractor = MiniMaxExtractor(settings)
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

    provider = srch.GoogleCSEProvider(settings)
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
            table.add_row(c.title[:78], c.url[:64])
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


@app.command()
def queue(
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 40,
    failed: Annotated[
        bool,
        typer.Option(
            "--failed", help="Show URLs a previous run could not read, instead of pending ones."
        ),
    ] = False,
    drop: Annotated[
        bool, typer.Option("--drop", help="Delete the listed candidates instead of showing them.")
    ] = False,
    url: Annotated[
        list[str] | None,
        typer.Option("--url", help="Restrict --drop to these URLs. Repeatable."),
    ] = None,
) -> None:
    """Show articles discovery has queued but nothing has crawled yet."""
    from tracker.ingest import discover as disc

    if drop:
        engine, _ = init_db(_db_path())
        with session_scope(engine) as session:
            removed = disc.drop_pending(session, list(url) if url else None)
        console.print(f"dropped {removed} queued candidate(s)")
        return

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        if failed:
            rows = disc.failed(session, limit=limit)
            total = len(disc.failed(session))
        else:
            rows = disc.pending(session, limit=limit)
            total = len(disc.pending(session))
        if not rows:
            if failed:
                console.print("[green]no failed URLs[/green]")
            else:
                console.print(
                    "[green]queue is empty[/green] — run `tracker discover` to look for articles"
                )
            return

        table = Table(
            title=f"{total} {'unread' if failed else 'queued'} candidate(s)",
            header_style="bold",
            title_justify="left",
            box=TABLE_BOX,
        )
        table.add_column("published")
        table.add_column("feed")
        table.add_column("headline")
        table.add_column("url")
        for row in rows:
            table.add_row(
                str(row.published_at or NA)[:10],
                row.feed or NA,
                (row.title or NA)[:70],
                row.url[:60],
            )
        console.print(table)
        if total > len(rows):
            console.print(f"[dim]showing {len(rows)} of {total}; --limit to see more[/dim]")
        console.print(
            "\n[dim]crawl them:[/dim] tracker ingest crawl --from-queue\n"
            "[dim]drop one:  [/dim] tracker queue --drop --url <URL>"
        )


@app.command("export")
def export_cmd(
    fmt: Annotated[str, typer.Argument(help="md | csv | json")],
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
    from tracker.dedup import company_key

    required = required or install_root() / "seed" / "required-projects.txt"

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

        wanted = [
            line.strip()
            for line in required.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not wanted:
            console.print(f"\n[dim]{required.name} is empty.[/dim]")
            return

        haystack = [
            (company_key(p.company), (p.name or "").lower(), p.state or "", p.id) for p in projects
        ]
        missing: list[str] = []
        found: list[tuple[str, int]] = []
        for entry in wanted:
            needle_company, _, needle_name = (
                entry.partition("|") if "|" in entry else ("", "", entry)
            )
            ck = company_key(needle_company.strip()) if needle_company.strip() else ""
            nn = needle_name.strip().lower()
            hit = next(
                (
                    pid
                    for ck_p, name_p, _state, pid in haystack
                    if (not ck or ck == ck_p) and (not nn or nn in name_p or name_p in nn)
                ),
                None,
            )
            if hit is None:
                missing.append(entry)
            else:
                found.append((entry, hit))

        console.print(f"\nrequired list: [bold]{len(found)}/{len(wanted)}[/bold] present")
        for entry, pid in found:
            console.print(f"  [green]ok[/green]      #{pid}  {entry}")
        for entry in missing:
            console.print(f"  [red]missing[/red] {entry}")


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
