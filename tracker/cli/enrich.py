"""`tracker enrich` and `tracker point` — going after what a row does not have yet.

Both start from a gap rather than from an article. `enrich` works the thinnest rows
we hold; `point` goes and gets one named campus on demand. `enrich` is a staged
pipeline and is drawn in `docs/workflows/enrich.md` (`CLAUDE.md` §7).
"""

from __future__ import annotations

import atexit
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from tracker import attempts as attempts_mod
from tracker.cli._shared import (
    NA,
    TABLE_BOX,
    _db_path,
    _explain_db_locks,
    _fail,
    _print_report,
    _print_report_rows,
    _use_llm,
    app,
    console,
    err,
)
from tracker.config import cache_dir as article_cache
from tracker.config import get_settings
from tracker.db import AlreadyRunning, acquire_write_lock, init_db, session_scope
from tracker.models import Project


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
    agent: Annotated[
        bool,
        typer.Option(
            "--agent/--no-agent",
            help=(
                "Default. After the harvest rounds, a model goes after whatever is "
                "STILL missing: it picks its own searches, reads the pages, and cites "
                "what it finds. It is the expensive rung — ~77,000 tokens a row — so "
                "it runs last, only on the residue. `--no-agent` for the cheap pass "
                "alone."
            ),
        ),
    ] = True,
    skip_archive: Annotated[
        bool, typer.Option("--skip-archive", help="Do not sweep the sitemap archives.")
    ] = False,
    skip_settle: Annotated[
        bool,
        typer.Option(
            "--skip-settle",
            help="Do not put the disagreements this run created to a model.",
        ),
    ] = False,
    basics: Annotated[
        bool,
        typer.Option(
            "--basics",
            help=(
                "Go after the eight fields that say what a project IS — who, where, "
                "what stage, how big, when live. Works the whole database when no "
                "rows are named. Cheap: it is the narrowest question the agent can "
                "be asked."
            ),
        ),
    ] = False,
    fields: Annotated[
        str | None,
        typer.Option(
            "--fields",
            help=(
                "Comma-separated fields the agent pass may go after, instead of "
                "every empty one. Narrowing is the cheapest saving there is."
            ),
            show_default=False,
        ),
    ] = None,
    token_budget: Annotated[
        int,
        typer.Option(
            "--token-budget",
            help=(
                "Stop the agent pass once this many tokens are spent. Checked "
                "BETWEEN rows, never mid-row. 0 means no limit."
            ),
        ),
    ] = 0,
    max_attempts: Annotated[
        int,
        typer.Option(
            "--max-attempts",
            help=(
                "Stop asking about a field after this many fruitless attempts, "
                "until the row gains a citation. 0 asks every time."
            ),
        ),
    ] = attempts_mod.DEFAULT_MAX_ATTEMPTS,
    browser: Annotated[
        bool,
        typer.Option(
            "--browser", help="Escalate blocked pages to Crawl4AI. Needs the 'crawl' extra."
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Harvest and report without writing or extracting.")
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
    """Throw every retrieval method at one or more projects until rounds stop paying.

    `tracker sync` spreads a budget across the database and, because the queue is
    mostly new-project candidates, it grows sideways. This does the opposite: it
    derives what can be derived, drains the queue and the failed URLs, sweeps the
    sitemap archives, runs gap-targeted web searches if a key is configured,
    re-reads the project's own citations, then extracts and repeats.

        tracker enrich 90 93            two projects by id
        tracker enrich --select 30      the 30 worth finishing, chosen for you
        tracker enrich --all            everything below --target, best first
        tracker enrich --basics         every row short of a field that defines it

    `--basics` is a modifier rather than a fourth way of choosing rows, so it
    composes with all three above. Alone it also picks the rows: every one missing
    part of who / where / what stage / how big / when live, fewest gaps first. It is
    the cheap mode — the agent is asked about four fields instead of eight, and
    `tracker clean` reports the same condition for free, so you can see the size of
    the job before paying for any of it.

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
    _use_llm(llm_provider)
    from tracker import clean as clean_mod
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
    # `--basics` is a modifier, not a fourth selector: it changes which FIELDS are
    # chased, not how rows are picked, so it composes with all three. On its own it
    # also implies the rows — every row whose defining fields are short — because
    # "check the whole database" is the reason the mode exists.
    if not chosen_ways and not basics:
        _fail(
            "give at least one project id, use --select N, --all for every project, "
            "or --basics for every row missing a field that defines it"
        )
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
    # `--basics` names rows that are short of a *defining* field, which is a
    # different question from "9 of the 12 tracked". Enforcing both would stop a row
    # at nine tracked fields while the phase nobody sourced was the thing that put
    # it on the list, so the field target steps aside and `--budget` does the
    # bounding — as it does when ids are named, and for the same reason.
    if basics:
        target_fields = None

    # What the agent pass may go after. Narrowing it is the cheapest saving
    # available: the receiving end of `gapfill.fill(gaps=...)` has existed all
    # along and nothing ever passed it.
    agent_fields: tuple[str, ...] | None = None
    if fields and basics:
        _fail("pass --fields or --basics, not both — they are two ways to say the same thing")
        return
    if fields:
        from tracker.gapfill import FILLABLE_FIELDS

        agent_fields = tuple(name.strip() for name in fields.split(",") if name.strip())
        unknown = [name for name in agent_fields if name not in FILLABLE_FIELDS]
        if unknown:
            _fail(
                f"the agent cannot fill {', '.join(unknown)} — "
                f"choose from {', '.join(sorted(FILLABLE_FIELDS))}"
            )
            return
    elif basics:
        agent_fields = clean_mod.basic_fillable()

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
            if basics and not project_ids:
                # Rows whose defining fields are short, fewest gaps first. Not the
                # full `clean` sweep: that also runs logic, audit, quality and the
                # duplicate detector for ten conditions this question never asks.
                wanted = clean_mod.basics_worklist(session, limit=select or None)
                if not wanted:
                    console.print(
                        "[green]nothing to do[/green] — every row holds all "
                        f"{len(clean_mod.BASIC_FIELDS)} fields that define it"
                    )
                    return
                console.print(
                    f"selected {len(wanted)} row(s) short of a defining field, fewest gaps first"
                )
            elif select or enrich_all:
                # --all is --select with no cap: same query, same ordering, and
                # `select_projects` already excludes projects at or past the
                # target, so "all" never wastes budget on finished rows.
                chosen = enrich_mod.select_projects(
                    session,
                    None if enrich_all else select,
                    # `--target 0` means "no target", which for *selection* means
                    # every project short of all twelve rather than none of them.
                    target=target_fields or len(TRACKED_FIELDS),
                    # The same cap the agent pass honours, so a row it would ask
                    # nothing of is not handed to the harvest either.
                    max_attempts=max_attempts,
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
                # Without this, every article `enrich` reads is thrown away. It was
                # missing here and passed at all ten sibling call sites, so a
                # 36-hour `--all` run read ~3,000 articles and cached none of them,
                # and the newest file in the cache predated the run by two days.
                # Three later steps read that cache and not the network:
                # `ingest crawl --stale-prompt` (re-extract with a better prompt),
                # `backfill blocks`, and `riskcheck.article_for` — which reports
                # `no_article` and settles nothing without it.
                cache_dir=article_cache("articles"),
                census_dir=census if census.exists() else None,
                escalate=escalate,
                skip_search=skip_search,
                skip_archive=skip_archive,
                skip_settle=skip_settle,
                dry_run=dry_run,
                # The same narrowing the agent pass gets, one rung earlier: a
                # query is an API call and every hit it returns is an article to
                # read, so a field nobody has published costs money every round
                # until it stops being asked about.
                want_fields=tuple(clean_mod.BASIC_FIELDS) if basics else None,
                max_attempts=max_attempts,
            )
    except LookupError as exc:
        _fail(str(exc))
        raise

    if agent and not dry_run:
        # After the cheap rung, not instead of it. `_FIELD_QUERIES` finds the
        # fields somebody wrote a query template for, and finds them for a few
        # hundred tokens; the agent costs ~77,000 a row. So it is pointed only at
        # what the templates could not reach — "where it is needed" literally.
        #
        # **Only the rows the harvest actually reached.** `run_many` stops when the
        # article budget runs out and leaves the rest of `wanted` untouched, so
        # handing it `wanted` pointed the most expensive rung in the tool at rows
        # that had received no cheap rung at all. Measured on the shape that
        # prompted this: `--all` over 403 rows with `--budget 200` gives each row
        # one article a round, exhausts the budget after roughly fifty of them, and
        # then billed the agent for all 403 — about 85% of ~31M tokens spent on
        # rows the harvest never opened.
        with _explain_db_locks(), session_scope(engine) as session:
            _gapfill_batch(
                session,
                [report.project_id for report in batch.reports],
                only_fields=agent_fields,
                token_budget=token_budget,
                max_attempts=max_attempts,
            )

    if len(batch.reports) == 1:
        _render_enrich(batch.reports[0], dry_run=dry_run)
        return
    # With no target enforced, still measure against the PRD's nine: it is the bar
    # a reader cares about even when the run was told not to stop there.
    _render_batch(batch, target=target_fields or enrich_mod.DEFAULT_TARGET_FIELDS, dry_run=dry_run)


#: What one row of the agent pass typically costs. Used only to decide whether the
#: NEXT row fits in what is left of `--token-budget`, before any run has measured a
#: real figure. See `README.md` for where the number comes from.
TYPICAL_ROW_TOKENS = 77_000


def _gapfill_batch(
    session,
    project_ids: list[int],
    *,
    only_fields: tuple[str, ...] | None = None,
    token_budget: int = 0,
    max_attempts: int = attempts_mod.DEFAULT_MAX_ATTEMPTS,
) -> None:
    """Let a model find and cite what the harvest rounds left empty.

    Committed per project, so a provider failure on row 20 keeps the first 19.
    Rows with nothing left to fill cost nothing at all — `gapfill.fill` returns
    before making a call.

    Three things keep the bill down, and all three work by *not asking*:

    * `only_fields` narrows the question to the fields the caller cares about.
      `gapfill.fill` has taken a `gaps=` list all along and no caller ever passed
      one, so every run asked about all eight fillable fields whatever it wanted.
    * `max_attempts` drops fields this row has already been asked about fruitlessly
      — see `tracker.attempts`. A row with nothing left to ask costs zero tokens.
    * `token_budget` stops the pass **between rows**. Never mid-row: aborting a run
      in flight would spend the tokens and store nothing, which is the waste this
      is here to prevent rather than a way to prevent it.
    """
    from tracker import gapfill
    from tracker.llm import LLMUnavailable, agent_extractor

    try:
        extractor = agent_extractor(get_settings())
    except LLMUnavailable as exc:
        err.print(f"[yellow]--agent skipped: {exc}[/yellow]")
        return

    console.print("\n[bold]agent pass[/bold] — what the harvest could not find")
    filled = nothing = errored = 0
    settled_already = 0
    spent = cache_hit = cache_miss = called = 0
    unreached: list[int] = []

    for index, project_id in enumerate(project_ids):
        project = session.get(Project, project_id)
        if project is None:
            continue
        head = f"#{project.id} {escape(project.name[:34])}"

        # Which fields this row may be asked about: empty, wanted, and not already
        # asked about to exhaustion.
        empty = {f for f in gapfill.FILLABLE_FIELDS if getattr(project, f, None) is None}
        if only_fields is not None:
            empty &= set(only_fields)
        askable = sorted(empty - attempts_mod.exhausted(project, max_attempts=max_attempts))
        if not askable:
            settled_already += 1
            continue

        # Before the call, never during one. `expected` is what a row has actually
        # cost this run; the constant only stands in until a row has been measured.
        expected = spent // called if called else TYPICAL_ROW_TOKENS
        if token_budget and spent + expected > token_budget:
            unreached = project_ids[index:]
            break

        try:
            out = gapfill.fill(session, project, extractor=extractor, gaps=askable)
        except Exception as exc:
            session.rollback()
            errored += 1
            console.print(f"  {head}  [red]failed[/red] [dim]— {escape(str(exc)[:110])}[/dim]")
            continue

        spent += out.prompt_tokens + out.completion_tokens
        cache_hit += out.cache_hit_tokens
        cache_miss += out.cache_miss_tokens
        called += 1

        # Only a run that reached an answer is evidence about the field. An error
        # or an unusable shape says something about the call, not about whether
        # anybody published the figure, and retiring a field on that would be a
        # silent loss.
        if out.verdict in {"filled", "nothing"} and out.missed:
            attempts_mod.record(project, out.missed)

        if out.verdict == "filled":
            session.commit()
            filled += 1
            for line in out.stored:
                console.print(f"  {head}  [green]{escape(line)}[/green]")
        elif out.verdict == "nothing":
            # Commits the attempt note even though no fact landed: that note is the
            # entire saving, and rolling it back would re-ask this row forever.
            session.commit()
            nothing += 1
            console.print(f"  {head}  [dim]nothing published — {escape(out.note[:90])}[/dim]")
        else:
            session.rollback()
            errored += 1
            console.print(f"  {head}  [yellow]{out.verdict}[/yellow]")
        # Printed even on success: a refusal is the evidence gate working, and a
        # run that quietly dropped four of five facts should look different from
        # one that stored them all.
        for line in out.refused:
            console.print(f"      [dim]refused: {escape(line[:120])}[/dim]")

    console.print(
        f"\n[bold]{filled}[/bold] row(s) gained a cited fact, "
        f"[bold]{nothing}[/bold] had nothing published"
        + (f", [red]{errored}[/red] failed" if errored else "")
        + f"  [dim]~{spent:,} tokens[/dim]"
    )
    if cache_hit or cache_miss:
        rate = cache_hit / (cache_hit + cache_miss)
        console.print(f"  [dim]prompt cache: {rate:.0%} hit[/dim]")
    if settled_already:
        console.print(
            f"  [dim]{settled_already} row(s) asked nothing — every field either "
            f"filled or already looked for {max_attempts}x without success[/dim]"
        )
    if unreached:
        console.print(
            f"[yellow]token budget spent[/yellow] — {len(unreached)} row(s) not "
            f"reached. Raise --token-budget to continue."
            + (
                f"\n[dim]nothing was attempted: a row costs about {TYPICAL_ROW_TOKENS:,} "
                f"tokens and the budget is {token_budget:,}[/dim]"
                if not called
                else ""
            )
        )


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

    # The settle step, and the refusals as loudly as the decisions. A field two
    # publishers genuinely disagree about is a *finding*, and printing only the
    # settlements would read as though the run had nothing to say about it.
    if report.settled or report.refused:
        console.print(
            f"\n[bold]settled[/bold] [dim]— every field whose sources disagreed, read "
            f"together rather than sorted. {report.settle_calls} call(s)[/dim]"
        )
        for line in report.settled:
            console.print(f"  [green]•[/green] {escape(line)}")
        for line in report.refused:
            console.print(f"  [yellow]refused[/yellow] {escape(line)}")

    console.print(f"[dim]stopped: {report.stopped_because}[/dim]")


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
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
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
    _use_llm(llm_provider)
    from tracker import point as point_mod
    from tracker.llm import LLMUnavailable, reasoning_extractor

    settings = get_settings()
    try:
        extractor = reasoning_extractor(settings)
    except LLMUnavailable as exc:
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

    links = [u.strip() for u in (url or []) if u and u.strip()]

    # **With links in hand, this is the wrong question to pay for.** Identifying
    # from the name typed here answers "which row does this string look like",
    # when the articles are about to say the operator and the town outright.
    # That answer used to be printed and then discarded anyway — `--url` let the
    # dedup key decide alone, which is how one campus became two rows. So the
    # question is deferred to `_article_router`, once per article, on evidence.
    match = None
    if links:
        console.print("[dim]each article is identified from what it says, not from this name[/dim]")
    else:
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
                f"[yellow]not in the database[/yellow] "
                f"[dim](confidence {match.confidence:.2f})[/dim]"
            )
        if match.reason:
            console.print(f"[dim]{escape(match.reason)}[/dim]")

    if dry_run:
        if links:
            plan = (
                f"read the {len(links)} link(s) given and file each one against the row its "
                "own operator and locality name, or a new row"
            )
        elif match.matched:
            plan = f"enrich #{match.project_id}"
        else:
            plan = f"search and read up to {max_articles} article(s)"
        console.print(f"\n[yellow]dry run[/yellow] — would {plan}")
        return

    console.print()
    if links:
        _point_read(name, point_mod, links, extractor=extractor)
    elif match.matched:
        _point_enrich(match.project_id)
    else:
        _point_build(name, point_mod, settings, max_articles, extractor=extractor)


def _article_router(session, point_mod, extractor):
    """Build the per-article identification `crawl.run` calls before each write.

    **This is the step `--url` used to skip.** The links went through `crawl.run`
    like any other article and the dedup key decided alone — correct for a batch
    crawl, where nobody has asked about a particular campus, and wrong here. A
    dedup key cannot express "this town is in that county", so an article naming
    Point Pleasant minted a second row beside the one already stored under Mason
    County, and `tracker duplicates` inherited a pair somebody now has to settle.

    Identifying from the *record* rather than from the typed name is what makes
    acting on the answer safe. The old objection to overriding the key — a
    mistyped name would file a filing under the wrong campus — was about a name
    somebody guessed. This asks about the operator and the locality the article
    itself asserts, which is the same evidence a person would use, and it is asked
    per article so a mixed URL list still lands in several places.

    Returning None means "no confident match", which is the ordinary write path
    and a new row. Every failure lands there too.
    """

    def route(record) -> int | None:
        identity = point_mod.Identity.from_record(record)
        if not identity.company or not identity.locality:
            # The write path will refuse this record anyway — it needs a company,
            # a state and a locality — so spending a call to identify it would buy
            # nothing. Saying so is worth a line: two of the four URLs in the run
            # that prompted all this were dropped exactly here, silently enough
            # that the row ended up without the tenant they named.
            console.print(
                f"  [dim]{escape(identity.name or identity.company or 'a record')}: "
                "no locality in the article, so nothing to identify against[/dim]"
            )
            return None

        candidates = point_mod.shortlist(session, identity.as_query())
        if not candidates:
            console.print(
                f"  [dim]{escape(identity.name)}: nothing similar tracked — new row[/dim]"
            )
            return None

        found = point_mod.identify_extracted(identity, candidates, extractor=extractor)
        where = ", ".join(x for x in (identity.locality, identity.state) if x)
        if found.matched:
            console.print(
                f"  [green]{escape(identity.name)}[/green] [dim]({escape(where)})[/dim] → "
                f"[bold]#{found.project_id}[/bold] [dim](confidence {found.confidence:.2f})[/dim]"
            )
        else:
            console.print(
                f"  [yellow]{escape(identity.name)}[/yellow] [dim]({escape(where)})[/dim] → "
                f"new row [dim]({escape(found.rejected or 'no confident match')})[/dim]"
            )
        if found.reason:
            console.print(f"    [dim]{escape(found.reason)}[/dim]")
        return found.project_id

    return route


def _point_read(name: str, point_mod, urls: list[str], *, extractor) -> None:
    """`--url`: read exactly these links, and file each where the article says.

    The evidence gate, the merge policies and the write path all apply unchanged;
    the only addition is that identification now happens *after* extraction, per
    article, and its answer is acted on rather than printed. See
    :func:`_article_router` for why that reversal is the whole point of this path.
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

    cache_dir = article_cache("articles")
    with _explain_db_locks(), session_scope(engine) as session:
        report = crawl_mod.run(
            session,
            urls,
            cache_dir=cache_dir,
            route=_article_router(session, point_mod, extractor),
        )
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


def _point_build(name: str, point_mod, settings, max_articles: int, *, extractor) -> None:
    """The unmatched branch: search for this name only, read the hits, file what comes back.

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
    cache_dir = article_cache("articles")
    with _explain_db_locks(), session_scope(engine) as session:
        # Routed too, for the same reason as `--url`. "Not matched" was decided
        # from the typed name before anything was read; the articles search just
        # found may well name a campus already tracked under another spelling or
        # at county granularity, and the write path cannot see that on its own.
        crawl_report = crawl_mod.run(
            session,
            urls,
            cache_dir=cache_dir,
            route=_article_router(session, point_mod, extractor),
        )
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
