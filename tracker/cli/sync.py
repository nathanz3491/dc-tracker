"""`tracker sync` and the commands that feed it — discover, prospect, search, feeds,
and the queue those three fill.

`sync` is a pipeline wearing a single name; its phases, caps and the gate between
the queue and a new row are drawn in `docs/workflows/sync.md`. Touching a function
named in that page's source map means updating the page in the same commit
(`CLAUDE.md` §7).
"""

from __future__ import annotations

import atexit
import contextlib
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from rich.markup import escape
from rich.table import Table

from tracker.cli._shared import (
    BROWSER_HINT,
    NA,
    TABLE_BOX,
    _db_path,
    _explain_db_locks,
    _fail,
    _print_report,
    _print_report_rows,
    _read_engine,
    _use_llm,
    _writable,
    app,
    console,
    emit,
    err,
    json_mode,
    queue_app,
)
from tracker.cli.enrich import _gapfill_batch, _render_batch
from tracker.cli.ingest import _identity_arbiter, _report_arbiter
from tracker.cli.projects import list_projects
from tracker.config import cache_dir as article_cache
from tracker.config import get_settings
from tracker.db import AlreadyRunning, acquire_write_lock, init_db, session_scope


def _print_feed_verdicts(verdicts: list, report, funnel_mod) -> None:
    """The retire half of `tracker feeds`.

    Prints the three not-a-verdict classes as well as the proposals, because the
    whole point of the split is that they are indistinguishable from volume alone
    and an operator who only sees "retire" will not know that.
    """
    if json_mode():
        emit(
            {
                "verdicts": [v.as_json() for v in verdicts],
                "waste_from_feeds": report.no_project - funnel_mod.no_feed_share(report)[0],
                "waste_total": report.no_project,
            }
        )
        return

    retire = [v for v in verdicts if v.verdict == "retire"]
    thin = [v for v in verdicts if v.verdict == "low yield"]
    blocked = [v for v in verdicts if v.verdict == "cannot read"]
    unjudged = [v for v in verdicts if v.verdict in {"too few to judge", "not read yet"}]

    if retire:
        console.rule("[bold]worth retiring[/bold]", align="left")
        for v in retire:
            console.print(f"  [yellow]{v.feed}[/yellow] — {v.why}")
            console.print(f"    [dim]tracker queue --drop --feed {v.feed}[/dim]")
        console.print(
            "\n[dim]Then comment the entry out of seed/feeds.toml, with a line saying "
            "why. Nothing here edits that file.[/dim]"
        )
    else:
        console.print(
            "[green]nothing worth retiring[/green] — no feed has been read "
            "enough times to judge and cited nothing"
        )

    if thin:
        console.print(
            "\n[bold]earning their keep thinly[/bold] [dim](reported, not proposed)[/dim]"
        )
        for v in thin:
            console.print(f"  {v.feed} — {v.why}")

    if blocked:
        console.print("\n[bold]cannot be read, which is not the same as worthless[/bold]")
        for v in blocked:
            console.print(f"  {v.feed} — [dim]{v.why}[/dim]")

    if unjudged:
        console.print(
            f"\n[dim]{len(unjudged)} feed(s) have not been read enough to judge. "
            f"They are not evidence of anything yet.[/dim]"
        )

    # The number that says how much any of this can achieve.
    no_feed, total = funnel_mod.no_feed_share(report)
    if total:
        console.print(
            f"\n[dim]Scope: {no_feed} of {total} wasted call(s) came from URLs no feed "
            f"found — search and archive sweeps. Retiring feeds addresses the other "
            f"{100 * (total - no_feed) / total:.0f}%.[/dim]"
        )


@app.command("feeds")
def feeds_cmd(
    hosts: Annotated[
        list[str] | None,
        typer.Argument(help="Probe these hosts instead of the ones the record suggests."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Publishers to probe.")] = 15,
    min_decisive: Annotated[
        int, typer.Option("--min-decisive", help="Values a host must already decide to qualify.")
    ] = 2,
    no_probe: Annotated[
        bool,
        typer.Option("--no-probe", help="Skip the network half. Only report what to retire."),
    ] = False,
    min_read: Annotated[
        int,
        typer.Option("--min-read", help="Calls a feed must have cost before it can be judged."),
    ] = 0,
) -> None:
    """Feeds worth adding, and feeds worth retiring. Free, no LLM, writes nothing.

    Both halves of the rolling loop in one place: widen where the record says a
    publisher is worth reading, converge where it says one is not.

    **Retiring** reads the database only. A feed is proposed for retirement when it
    has been read enough times to judge and has never once backed a stored value.
    It is deliberately *not* proposed on volume: a feed we cannot read looks
    identical to one that says nothing, and `datacenterdynamics` — kept on purpose,
    with ten lines in `seed/feeds.toml` explaining why — would head the kill list
    under a queued-versus-cited ratio.

    **Adding** costs requests. Candidates come from the record — hosts whose claims
    decide stored values, that `feeds.toml` does not list — rather than from a
    model, because the database already knows which outlets are worth reading.
    Three rungs per host: `robots.txt` `Sitemap:` lines, then well-known paths like
    `/feed` and `/rss.xml`, then the homepage's `<link rel="alternate">`. Every hit
    is parsed and run through the real filter, so the report says how many entries
    would have been *queued* rather than that a URL responded.

    Prints what to paste and what to comment out, and edits nothing.
    `seed/feeds.toml` is mostly hand-written justification — including the comment
    that stops someone deleting a blocked-but-valuable feed — and a command that
    rewrote it would strip exactly the reasoning that prevents the mistake.
    """
    from tracker import funnel as funnel_mod
    from tracker.ingest import probe as probe_mod

    engine = _read_engine()

    # The free half first, and only when the caller did not name hosts explicitly —
    # `tracker feeds some.host` is a question about that host, not a review.
    if not hosts:
        with session_scope(engine, commit=False) as session:
            report = funnel_mod.survey(session)
        verdicts = funnel_mod.verdicts(report, min_read=min_read or funnel_mod.MIN_READ_TO_JUDGE)
        _print_feed_verdicts(verdicts, report, funnel_mod)
        if no_probe:
            return
    with session_scope(engine, commit=False) as session:
        if not hosts:
            picks = probe_mod.candidates(session, limit=limit, min_decisive=min_decisive)
            if not picks:
                console.print(
                    "[green]nothing to propose[/green] — every publisher deciding "
                    f"{min_decisive}+ values is already in seed/feeds.toml."
                )
                return
            console.print(
                f"[dim]probing {len(picks)} publisher(s) that decide values but are not "
                f"configured[/dim]"
            )
        results = probe_mod.run(
            session, hosts=list(hosts) if hosts else None, limit=limit, min_decisive=min_decisive
        )

    found = [r for r in results if r.hits]
    if json_mode():
        emit(
            {
                "probed": len(results),
                "found": len(found),
                "hosts": [
                    {
                        "host": r.host,
                        "cited": r.cited,
                        "decisive": r.decisive,
                        "requests": r.tried,
                        "feeds": [
                            {
                                "url": h.url,
                                "entries": h.entries,
                                "would_queue": h.would_queue,
                                "found_via": h.found_via,
                            }
                            for h in r.hits
                        ],
                    }
                    for r in results
                ],
            }
        )
        return

    table = Table(header_style="bold", box=TABLE_BOX)
    table.add_column("publisher")
    table.add_column("decides", justify="right")
    table.add_column("feed found")
    table.add_column("entries", justify="right")
    table.add_column("would queue", justify="right")
    for result in results:
        hit = result.best
        if hit is None:
            table.add_row(
                result.host, str(result.decisive), f"[dim]{result.note or '—'}[/dim]", "", ""
            )
            continue
        table.add_row(
            result.host,
            str(result.decisive),
            hit.url[:58],
            str(hit.entries),
            f"{hit.would_queue} ({hit.hit_rate:.0%})",
        )
    console.print(table)

    worth = [r for r in found if r.best and r.best.would_queue]
    if not worth:
        console.print(
            "\n[yellow]Nothing worth adding.[/yellow] [dim]A feed that parses but "
            "matches no entry is not a find — the filter is what decides.[/dim]"
        )
        return

    console.rule("[bold]paste into seed/feeds.toml[/bold]", align="left")
    for result in worth:
        console.print(escape(result.as_toml()))
    console.print(
        "[dim]Nothing was written. Check the hit rate first: a low one means the feed "
        "carries mostly other coverage, and every queued entry costs an LLM call.[/dim]"
    )


@app.command()
def sync(
    verify_identity: Annotated[
        bool,
        typer.Option(
            "--verify-identity/--no-verify-identity",
            help=(
                "Default. Before a phase creates a row that has a near-match, a model "
                "reads the article and says whether it is the same site — preventing "
                "the duplicate rather than reporting it afterwards. Fires only on "
                "ambiguous inserts and creates the row whenever it is unsure."
            ),
        ),
    ] = True,
    agent: Annotated[
        bool,
        typer.Option(
            "--agent/--no-agent",
            help=(
                "Default. In the enrich phase, after the harvest rounds, a model goes "
                "after the fields the query templates could not reach and cites what "
                "it finds. Only reached when `--enrich` asked for that phase at all."
            ),
        ),
    ] = True,
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
    prospect_limit: Annotated[
        int,
        typer.Option(
            "--prospect",
            help="Also chase this many operators the roster says we have no rows for.",
        ),
    ] = 0,
    enrich_limit: Annotated[
        int,
        typer.Option(
            "--enrich", help="Also complete this many of the thinnest projects we already hold."
        ),
    ] = 0,
    enrich_budget: Annotated[
        int, typer.Option("--enrich-budget", help="Articles the enrich phase may read in total.")
    ] = 60,
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help="Every phase: prospect 5, enrich 10, --deep and --retry-failed. Spends the most.",
        ),
    ] = False,
    skip_derive: Annotated[
        bool, typer.Option("--skip-derive", help="Do not re-derive what the citations imply.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Do everything except write to the database. Not free: it still "
                "polls, searches and reads articles."
            ),
        ),
    ] = False,
    show_rows: Annotated[int, typer.Option("--rows", help="Projects to list at the end.")] = 30,
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Everything in one command: find what is missing, read it, settle it, list it.

    \b
      1. discover   poll the feeds, sweep archives, search -> queued candidates
      2. prospect   chase operators the roster says we have no rows for  (--prospect)
      3. extract    crawl the queue -> new projects in the database
      4. refresh    re-read existing projects' sources -> updated fields
      5. enrich     throw every method at the thinnest rows we hold      (--enrich)
      6. settle     re-derive what the citations imply, rescore confidence
      7. list       show the result

    Phases 2 and 5 are the expensive ones and are off unless asked for, which is
    what keeps a bare `tracker sync` the same cheap keep-current run it has always
    been. `--full` turns everything on and is the "bring the database up to date"
    button:

    \b
        tracker sync                 keep current: discover, extract, refresh
        tracker sync --full          the whole loop, including the operators we lack
        tracker sync --prospect 3    keep current, and go after three missing operators
        tracker sync --dry-run       write nothing (still polls, searches and reads)

    Every phase that costs LLM calls is capped separately — `--limit`,
    `--refresh-limit`, `--prospect`, `--enrich`/`--enrich-budget` — because they buy
    different things. `--limit` buys breadth (new rows), `--refresh-limit` buys
    currency, `--prospect` buys coverage of operators we are blind to, and
    `--enrich` buys depth on rows that are already here. A single budget spread
    across all four would silently favour whichever phase ran first.
    """
    _use_llm(llm_provider)
    from tracker import policy as policy_mod
    from tracker.ingest import crawl
    from tracker.ingest import discover as disc
    from tracker.llm import LLMUnavailable, build_extractor
    from tracker.upsert import recompute_confidence

    settings = get_settings()

    # `--full` sets the phases that are otherwise off, and does NOT override a
    # number given explicitly: `--full --prospect 1` means one operator, because a
    # flag that silently discarded the value beside it would be a trap.
    if full:
        prospect_limit = prospect_limit or 5
        enrich_limit = enrich_limit or 10
        deep = True
        retry_failed = True

    # Checked before any network call: every later phase needs it, and failing
    # here costs nothing rather than after a round of feed polling.
    try:
        extractor = build_extractor(settings)
    except LLMUnavailable as exc:
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
    cache_dir = article_cache("articles")
    totals = {"queued": 0, "new": 0, "refreshed": 0, "failed": 0, "enriched": 0, "chased": 0}
    #: URLs the prospect phase wants read first, handed to the extract phase below.
    prospect_urls: list[str] = []

    # The phases this run will actually perform, decided before the first one
    # prints. A phase that was not asked for is absent from the count rather than
    # shown as skipped: "2/7 prospect — skipped" on every ordinary run would train
    # a reader to ignore the labels, and the point of numbering a long run is that
    # somebody watching it knows how much is left.
    plan = ["discover"]
    if prospect_limit:
        plan.append("prospect")
    plan += ["extract new", "refresh existing"]
    if enrich_limit:
        plan.append("enrich")
    if not skip_derive:
        plan.append("settle")
    plan.append("projects")

    def step(name: str, *, skipped: str = "") -> None:
        """Announce a phase, numbered within the plan this run chose."""
        position = f"{plan.index(name) + 1}/{len(plan)}"
        if skipped:
            console.print(f"[dim]{position} {name} — {skipped}[/dim]")
        else:
            console.rule(f"[bold]{position} {name}[/bold]", align="left")

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

    # --- discover -----------------------------------------------------------
    if skip_discover:
        step("discover", skipped="skipped")
    else:
        step("discover")
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

    # --- prospect -----------------------------------------------------------
    # A separate phase from discover even though both end in queued candidates,
    # because they answer different questions. Discover and search ask "what is
    # being published"; this asks "who are we blind to", which is a question only
    # the roster can pose. Nebius was absent from 300 projects and no amount of
    # feed polling was ever going to say so.
    #
    # It runs BEFORE extract so the candidates it queues are eligible for this
    # run's crawl rather than waiting for the next one.
    if prospect_limit:
        step("prospect")
        from tracker import prospect as prospect_mod
        from tracker import roster as roster_mod
        from tracker.ingest import enrich as enrich_mod
        from tracker.ingest import search as srch2

        try:
            operators = roster_mod.load()
        except roster_mod.RosterError as exc:
            # Not fatal: the roster is one phase's input, and a run that has
            # already polled the feeds should still extract what it found.
            err.print(f"[yellow]prospect skipped[/yellow]: {str(exc).splitlines()[0]}")
        else:
            with session_scope(engine, commit=False) as session:
                coverage_before = roster_mod.measure(session, operators)
            targets = roster_mod.hunt_order(coverage_before)[:prospect_limit]
            if not targets:
                console.print("[dim]every rostered operator already has rows[/dim]")
            else:
                console.print(
                    f"chasing {len(targets)} operator(s) with no or thin coverage: "
                    + ", ".join(f"{t.name} ({t.status})" for t in targets)
                )
                provider = None
                if settings.has_search_keys():
                    with contextlib.suppress(srch2.SearchError):
                        provider = srch2.build_provider(settings)
                # After the emptiness check, not before: the sweep is ~30 requests
                # across the configured sitemaps, and paying for them to chase
                # nobody is the mistake `enrich.run_many` already learned.
                sweep = enrich_mod.sweep_archives(settings)
                with session_scope(engine, commit=False) as session:
                    p_report = prospect_mod.run(
                        session,
                        targets,
                        provider=provider,
                        extractor=extractor,
                        settings=settings,
                        sweep=sweep,
                        dry_run=dry_run,
                    )
                totals["queued"] += p_report.queued
                totals["chased"] = len(p_report.outcomes)
                # Respect the source policy here rather than in the extract phase's
                # partition, which has already run by then on a different list.
                prospect_urls = policy_mod.load().partition(p_report.queued_urls)[0]
                console.print(
                    f"{p_report.queries_run} search(es), queued "
                    f"[bold]{p_report.queued}[/bold] candidate(s) naming them"
                )
                for outcome in p_report.outcomes:
                    detail = f"{len(outcome.queued)} queued"
                    if outcome.archive_hits:
                        detail += f" ({outcome.archive_hits} from the archives)"
                    if outcome.from_queue:
                        detail += f", {len(outcome.from_queue)} already queued and unread"
                    console.print(f"  [dim]{outcome.name:<24} {detail}[/dim]")
                for name, reason in p_report.errors[:3]:
                    err.print(f"[yellow]{escape(name[:40])}[/yellow]: {reason.splitlines()[0]}")

    # --- extract new --------------------------------------------------------
    step("extract new")
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
        # Ordered and filtered BEFORE the limit bites, which is the whole point:
        # truncating first and prioritising after would reorder a batch that was
        # already chosen. `pending` is asked for everything and cut here, which
        # also removes the second full query this used to run for `backlog`.
        ordered = disc.pending(session, known_first=not breadth_first, spec=queue_spec)
        backlog = len(ordered)
        source_policy = policy_mod.load()
        kept, ignored_urls = source_policy.partition([row.url for row in ordered])
        # The prospect phase's finds go to the front, and this is the whole reason
        # that phase is worth running inside sync. `known_first` sorts by "covers a
        # project we already track", which an article about an operator we have NO
        # rows for can never satisfy — so left to the ordinary ordering it sits
        # behind a permanent supply of better candidates and is never read. That is
        # how a queue ends up holding a Nebius URL while the database holds no
        # Nebius row.
        if prospect_urls:
            kept = prospect_urls + [url for url in kept if url not in set(prospect_urls)]
        pending_urls = kept[:limit]
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
    if ignored_urls:
        # Named, not merely subtracted: the queue still holds these rows and
        # `tracker queue` still lists them, so the number has to be attributable.
        console.print(
            f"[dim]{len(ignored_urls)} candidate(s) skipped — seed/sources.toml ignores "
            f"their publisher[/dim]"
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
        first = sum(1 for url in pending_urls if url in set(prospect_urls))
        console.print(
            f"extracting {len(pending_urls)} of {backlog} queued candidate(s)"
            + (f", {first} of them named by the prospect phase" if first else "")
        )
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
                # The phase that creates rows, so the phase that creates
                # duplicates. Checked here rather than reported later.
                arbiter=_identity_arbiter(verify_identity, label="extract"),
            )
        _report_arbiter("extract")
        totals["new"] = new_report.inserted
        totals["failed"] += new_report.fetch_error + new_report.parse_error
        _print_report(new_report, title="new projects")
        if backlog > len(pending_urls):
            console.print(
                f"[dim]{backlog - len(pending_urls)} candidate(s) still queued; "
                f"raise --limit or run again[/dim]"
            )

    # --- refresh existing ---------------------------------------------------
    if skip_refresh:
        step("refresh existing", skipped="skipped")
    else:
        step("refresh existing")
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
                    # A refresh re-reads urls already attached, so these match by
                    # key and the arbiter almost never fires. It is passed anyway
                    # because `force=True` means this path *can* still create a
                    # row, and a duplicate born in the refresh phase would be no
                    # less expensive than one born in extract.
                    arbiter=_identity_arbiter(verify_identity, label="refresh"),
                )
            _report_arbiter("refresh")
            totals["refreshed"] = ref_report.updated
            totals["failed"] += ref_report.fetch_error + ref_report.parse_error
            _print_report(ref_report, title="refreshed projects")

    # --- enrich -------------------------------------------------------------
    # The opposite spend from `extract new`: every article here goes to a row that
    # already exists. Both are worth having and they compete for the same money,
    # which is why they are separate caps rather than one budget — see the
    # docstring. `enrich` picks the projects closest to the field target, because
    # taking one row from 8 fields to 9 costs a single article and taking another
    # from 4 to 9 may never get there.
    if enrich_limit:
        step("enrich")
        from tracker.ingest import enrich as enrich_mod2

        census = _db_path().parent / "raw" / "census"
        with _explain_db_locks(), session_scope(engine, commit=not dry_run) as session:
            chosen = enrich_mod2.select_projects(session, enrich_limit)
            if not chosen:
                console.print(
                    "[green]nothing thin enough[/green] [dim]— every project already holds "
                    f"{enrich_mod2.DEFAULT_TARGET_FIELDS} of the 12 tracked fields[/dim]"
                )
            else:
                console.print(
                    f"completing {len(chosen)} project(s), closest to target first, "
                    f"within {enrich_budget} article(s)"
                )
                batch = enrich_mod2.run_many(
                    session,
                    chosen,
                    settings=settings,
                    max_articles=enrich_budget,
                    cache_dir=cache_dir,
                    census_dir=census if census.exists() else None,
                    escalate=escalate,
                    extractor=extractor,
                    dry_run=dry_run,
                )
                totals["enriched"] = len(batch.reports)
                _render_batch(batch, target=enrich_mod2.DEFAULT_TARGET_FIELDS, dry_run=dry_run)
                # Same helper the `enrich` command uses, so the two cannot drift
                # into different rules about what the agent is pointed at. It runs
                # after the harvest for the same reason there: the templates are
                # cheap and this is not, so it should only see what they missed.
                if agent and not dry_run:
                    _gapfill_batch(session, list(chosen))

    # --- settle -------------------------------------------------------------
    # Two recomputations, both pure functions of what the rows now cite, and both
    # wrong to skip after a phase that added sources. `derive` reapplies every
    # derived value (county, coordinates, capacity rollups) because those are only
    # recomputed when something writes to the row; confidence is a cache of a
    # function of the citations, so it is stale the moment a citation lands.
    if not skip_derive:
        step("settle")
        if dry_run:
            console.print("[dim]dry run — nothing to settle[/dim]")
        else:
            from tracker import derive as derive_mod

            with _explain_db_locks(), session_scope(engine) as session:
                derived = derive_mod.run(session)
            console.print(
                f"re-derived {derived.changed} project(s) from what their citations imply"
                if derived.changed
                else "[dim]every row already matches what its citations imply[/dim]"
            )
            with session_scope(engine) as session:
                rescored = recompute_confidence(session)
            if rescored:
                console.print(f"[dim]recomputed confidence on {rescored} project(s)[/dim]")
    elif not dry_run:
        # Confidence is a cache of a pure function, so recompute after any write —
        # even when the derive half was declined.
        with session_scope(engine) as session:
            rescored = recompute_confidence(session)
        if rescored:
            console.print(f"[dim]recomputed confidence on {rescored} project(s)[/dim]")

    # --- list ---------------------------------------------------------------
    step("projects")
    if dry_run:
        console.print("[yellow]dry run — nothing was written[/yellow]")
    list_projects(
        company=None, state=None, phase=None, min_confidence=None, sort="mw", limit=show_rows
    )

    summary = (
        f"\n[bold]sync complete[/bold]  queued {totals['queued']}  "
        f"new {totals['new']}  refreshed {totals['refreshed']}"
    )
    if totals["chased"]:
        summary += f"  operators chased {totals['chased']}"
    if totals["enriched"]:
        summary += f"  enriched {totals['enriched']}"
    console.print(f"{summary}  failed {totals['failed']}")
    if not prospect_limit:
        # Printed on every ordinary run, because the gap this closes is the one
        # nothing else here can see: a `sync` that reports "0 failed, queue empty"
        # is silent about the operators that were never in the database at all.
        console.print(
            "[dim]coverage of the operators we should hold is a separate question: "
            "`tracker coverage`, then `tracker sync --full`[/dim]"
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
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Find candidate articles with Google search instead of waiting for a feed.

    Feeds only surface what was published recently, so a project announced two
    years ago never appears in them. Search goes looking for it.

    With --from-llm, the model proposes which projects to search for. Those are
    leads, never facts: nothing the model names is stored, and a project only
    becomes a row once a real article has been fetched and its values backed by
    verbatim quotes. If the model invents a project, the search finds nothing.
    """
    _use_llm(llm_provider)
    from tracker.ingest import search as srch
    from tracker.llm import LLMUnavailable, build_extractor

    settings = get_settings()
    queries = list(query or [])

    if from_llm:
        try:
            extractor = build_extractor(settings)
        except LLMUnavailable as exc:
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
def prospect(
    operator: Annotated[
        list[str] | None,
        typer.Argument(help="Operators to chase by name. Omit to take them from `coverage`."),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", help="Uncovered operators to chase when none are named.")
    ] = 5,
    absent_only: Annotated[
        bool,
        typer.Option("--absent-only", help="Skip the thin operators; chase only the missing ones."),
    ] = False,
    queries: Annotated[int, typer.Option("--queries", help="Search queries per operator.")] = 8,
    extract: Annotated[
        int,
        typer.Option(
            "--extract", help="Articles to read now, out of what this run queues. 0 queues only."
        ),
    ] = 12,
    skip_campuses: Annotated[
        bool,
        typer.Option("--skip-campuses", help="Do not ask the model to name each operator's sites."),
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
        bool,
        typer.Option(
            "--dry-run",
            help="Queue nothing. The searches and the campus-naming call still happen.",
        ),
    ] = False,
    roster: Annotated[
        Path | None,
        typer.Option("--roster", help="A different operator roster.", show_default=False),
    ] = None,
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Go and get the operators the database has no rows for.

    `discover` waits for a feed to mention a project and `search` asks a model to
    brainstorm projects. Both are aimed at projects, and both share a blind spot:
    an operator missing from last week's headlines is missing from the database and
    nothing notices. Nebius sat at zero rows through both of them while running a
    Kansas City campus.

    This runs the other way round. `tracker coverage` says who is absent, and each
    absent name is turned into leads three ways:

    \b
      queue     URLs already discovered that name the operator and were never
                read. Free and already ours — and they need looking for, because
                the extract phase spends its calls depth-first on projects it
                already has, so an article about an operator with no rows waits
                behind a permanent supply of better candidates.
      archive   the configured sitemaps, filtered to URLs naming the operator.
                Free, needs no key, and reaches back years — which matters,
                because an operator we never had is usually one whose
                announcements are old.
      search    four templated queries per operator, plus one for each US campus
                a model proposes for it.

    **The model's campus names are leads, never facts.** They are only ever used to
    build a search query. If it invents a site, the search finds nothing and the run
    moves on; a row appears only where an article was fetched and the evidence gate
    found a verbatim quote for each value, exactly as on every other path.

    Then it reads up to `--extract` of what it queued and re-measures coverage, so
    the run ends by saying whether each operator actually gained a row rather than
    how many URLs it collected.

    \b
        tracker prospect                    the five worst gaps
        tracker prospect Nebius CoreWeave   these two, now
        tracker prospect --dry-run          the queries and the archive hits, queueing none

    Costs one LLM call per operator for the campus names, one search per query, and
    one call per article read. `--skip-campuses` removes the first; `--extract 0`
    removes the last and leaves the queue for `tracker sync`.
    """
    _use_llm(llm_provider)
    from tracker import prospect as prospect_mod
    from tracker import roster as roster_mod
    from tracker.ingest import enrich as enrich_mod
    from tracker.ingest import search as srch
    from tracker.ingest.fetch import Crawl4AIFetcher, MissingDependency, escalation_ladder
    from tracker.llm import LLMUnavailable, build_extractor

    settings = get_settings()
    if browser:
        try:
            Crawl4AIFetcher.ensure_available()
        except MissingDependency as exc:
            _fail(str(exc))
            return

    try:
        operators = roster_mod.load(roster)
    except roster_mod.RosterError as exc:
        _fail(str(exc))
        return

    engine, _ = init_db(_db_path())
    with session_scope(engine, commit=False) as session:
        report_before = roster_mod.measure(session, operators)

    named = [name.strip() for name in (operator or []) if name.strip()]
    if named:
        targets = []
        for name in named:
            found = next(
                (row for row in report_before.rows if row.operator.matches(name) is not None), None
            )
            if found is None:
                # Refused rather than prospected anyway: an operator absent from the
                # roster has no aliases and no kind, so the run would be
                # unrepeatable and its result unattributable.
                _fail(
                    f"{name!r} is not in the roster. Add it to seed/operators.toml — "
                    f"`tracker coverage` lists the companies already in the database "
                    f"that no entry claims."
                )
                return
            if found not in targets:
                targets.append(found)
    else:
        targets = roster_mod.hunt_order(report_before, include_thin=not absent_only)[:limit]

    if not targets:
        console.print(
            "[green]nothing to chase[/green] — every rostered operator has rows. "
            "`tracker coverage --covered` shows them."
        )
        return

    # Both halves are optional and each says so once, here, rather than per
    # operator: a keyless install can still prospect the archives, and that is a
    # configuration rather than a failure.
    extractor = None
    if not skip_campuses:
        try:
            extractor = build_extractor(settings)
        except LLMUnavailable:
            console.print(
                "[yellow]no LLM available[/yellow] [dim]— templated queries only, "
                "no campus names[/dim]"
            )
    provider = None
    if settings.has_search_keys():
        try:
            provider = srch.build_provider(settings)
        except srch.SearchError as exc:
            err.print(f"[yellow]search unavailable[/yellow]: {exc}")
    else:
        console.print("[yellow]no search backend[/yellow] [dim]— archives only[/dim]")

    sweep = None
    if not skip_archive:
        console.print("[dim]sweeping the configured archives once for the whole run…[/dim]")
        sweep = enrich_mod.sweep_archives(settings)
        if sweep.skipped:
            console.print(f"[dim]archives: {sweep.skipped}[/dim]")
    if provider is None and (sweep is None or sweep.skipped):
        _fail(
            "nothing to prospect with: no search backend configured and no archives to "
            f"sweep.\n{srch.SEARCH_KEY_HELP}"
        )
        return

    console.print(
        f"chasing [bold]{len(targets)}[/bold] operator(s): "
        + ", ".join(f"{t.name} ({t.status})" for t in targets)
    )

    if not dry_run:
        try:
            release_lock = acquire_write_lock(_db_path(), command="prospect")
        except AlreadyRunning as exc:
            _fail(str(exc))
            return
        atexit.register(release_lock)

    with _explain_db_locks(), session_scope(engine, commit=False) as session:
        report = prospect_mod.run(
            session,
            targets,
            provider=provider,
            extractor=extractor,
            settings=settings,
            sweep=sweep,
            per_operator=queries,
            dry_run=dry_run,
        )

    table = Table(header_style="bold", box=TABLE_BOX, title_justify="left", title="leads")
    table.add_column("operator")
    table.add_column("rows", justify="right")
    table.add_column("queue", justify="right")
    table.add_column("archive", justify="right")
    table.add_column("hits", justify="right")
    table.add_column("queued", justify="right")
    table.add_column("campuses proposed", style="dim", no_wrap=True, max_width=44)
    for outcome in report.outcomes:
        table.add_row(
            escape(outcome.name),
            str(outcome.projects_before),
            str(len(outcome.from_queue)) if outcome.from_queue else "-",
            str(outcome.archive_hits) if outcome.archive_hits else "-",
            str(outcome.hits) if outcome.hits else "-",
            f"[bold]{len(outcome.queued)}[/bold]" if outcome.queued else "-",
            escape(", ".join(c.label for c in outcome.campuses)) or outcome.note,
        )
    console.print(table)
    console.print(
        f"[dim]{report.queries_run} search(es) run, {report.queued} candidate(s) queued, "
        f"{report.from_queue} already in the queue and never read[/dim]"
    )
    for name, reason in report.errors[:5]:
        err.print(f"[yellow]{escape(name[:50])}[/yellow]: {reason.splitlines()[0]}")
    if report.quota_exhausted:
        err.print("[yellow]daily search quota exhausted; resets at midnight Pacific[/yellow]")

    if dry_run:
        console.print("\n[yellow]dry run — nothing was written[/yellow]")
        for outcome in report.outcomes:
            for lead in outcome.queries:
                console.print(f"  [dim]{outcome.name:<22} {escape(lead.query[:70])}[/dim]")
        return

    urls = report.queued_urls[:extract] if extract else []
    if not urls:
        waiting = report.queued + report.from_queue
        if waiting:
            console.print(
                f"[dim]{waiting} candidate(s) waiting and not read. `tracker sync` will "
                f"crawl them, though an article about an operator with no rows sorts last "
                f"there — which is what --extract is for.[/dim]"
            )
        return

    try:
        crawl_extractor = build_extractor(settings)
    except LLMUnavailable as exc:
        console.print(f"[yellow]queued but not read[/yellow] [dim]— {exc}[/dim]")
        return

    from tracker.ingest import crawl
    from tracker.upsert import recompute_confidence

    console.rule("[bold]reading[/bold]", align="left")
    console.print(f"reading {len(urls)} of {report.queued + report.from_queue} candidate(s)")
    with _explain_db_locks(), session_scope(engine) as session:
        crawl_report = crawl.run(
            session,
            urls,
            extractor=crawl_extractor,
            escalate=escalation_ladder(settings, browser=browser),
            settings=settings,
            force=True,
            cache_dir=article_cache("articles"),
        )
    _print_report(crawl_report, title="prospect")

    with session_scope(engine) as session:
        recompute_confidence(session)

    # The only honest measure of this command: coverage, re-run. "Queued 40 URLs"
    # says what we spent; "Nebius 0 -> 2" says what we got, and the gap between
    # those two numbers is the whole reason the report ends here.
    with session_scope(engine, commit=False) as session:
        report_after = roster_mod.measure(session, operators)
    by_name = {row.name: row for row in report_after.rows}
    console.rule("[bold]coverage[/bold]", align="left")
    gained = 0
    for outcome in report.outcomes:
        after = by_name.get(outcome.name)
        if after is None:
            continue
        moved = after.projects - outcome.projects_before
        gained += 1 if moved > 0 else 0
        style = "green" if moved > 0 else "dim"
        console.print(
            f"  [{style}]{escape(outcome.name):<24} {outcome.projects_before} -> "
            f"{after.projects} row(s)[/{style}]"
        )
    console.print(
        f"\n[bold]prospect complete[/bold]  {gained} of {len(report.outcomes)} operator(s) "
        f"gained a row"
    )
    if gained < len(report.outcomes):
        console.print(
            "[dim]an operator that gained nothing is not necessarily a failure: the "
            "articles may exist and say nothing quotable, which is what the evidence "
            "gate is for. `tracker queue --failed` shows what could not be read.[/dim]"
        )


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
                cache_dir=article_cache("articles"),
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


@queue_app.command("stats")
def queue_stats(
    limit: Annotated[int, typer.Option("--limit", help="Feeds to show.")] = 20,
) -> None:
    """What each feed costs and what it returns. Free, read-only, no LLM.

    The number this exists for: **49% of URLs that reach an LLM call produce no
    project at all.** The discovery filter is two tiers of keywords over a headline
    and a URL path, so it cannot tell an article *about* a project from one that
    mentions the industry, and half the extraction budget goes on the difference.

    Read per feed it becomes actionable rather than depressing. A `topic_implied`
    newsroom that covers a wider beat than the flag assumes shows up as a column of
    `none`, and a feed at 100% waste over a real number of calls is one to
    reconsider or mark `discovery-only`.

    Derived from `ingest_url` on every run, so there is no counter to drift.
    """
    from tracker import funnel as funnel_mod

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        report = funnel_mod.survey(session)
        failures = funnel_mod.fetch_failures(session)

    if json_mode():
        emit({**report.as_json(), "fetch_failures": dict(failures)})
        return
    if not report.total_urls:
        console.print("[yellow]nothing discovered yet[/yellow] — run `tracker discover` first")
        return

    tone = "red" if report.waste >= 0.4 else "yellow" if report.waste >= 0.2 else "green"
    console.print(
        f"[bold]{report.total_urls}[/bold] URL(s) discovered; "
        f"[bold]{report.reached_model}[/bold] reached an LLM call, of which "
        f"[{tone}]{report.no_project}[/{tone}] ({report.waste:.0%}) produced no project"
    )
    console.print(
        f"[dim]{report.dated} of {report.total_urls} carry a publication date — "
        f"what the merge tiebreak ranks on. `tracker backfill dates` fills more.[/dim]"
    )

    table = Table(header_style="bold", box=TABLE_BOX)
    table.add_column("feed")
    table.add_column("queued", justify="right")
    table.add_column("read", justify="right")
    table.add_column("none", justify="right")
    table.add_column("waste", justify="right")
    table.add_column("cited", justify="right")
    table.add_column("dated", justify="right")
    for stat in report.ranked()[:limit]:
        if not stat.read:
            continue
        colour = "red" if stat.waste >= 0.8 else "yellow" if stat.waste >= 0.5 else ""
        waste = f"{stat.waste:.0%}"
        table.add_row(
            stat.feed,
            str(stat.queued),
            str(stat.read),
            str(stat.no_project),
            f"[{colour}]{waste}[/{colour}]" if colour else waste,
            str(stat.cited),
            str(stat.dated),
        )
    console.print(table)
    console.print(
        "[dim]`none` is an article the model read and found no project in — the cost "
        "of a keyword filter. `cited` is URLs that back a stored value today.[/dim]"
    )

    if failures:
        # The silent-timeout audit. A timeout means raise the timeout, a 403 means
        # escalate the fetcher, a 404 means the URL is gone — three different
        # remedies that a single `fetch_error` count cannot tell apart.
        console.print("\n[bold]why fetches failed[/bold]")
        for error, count in failures:
            console.print(f"  {count:>5}  [dim]{escape(error)}[/dim]")


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
