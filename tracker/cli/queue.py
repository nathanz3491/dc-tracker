"""`tracker queue` — the pages discovery found, waiting to be read, and the passes over them.

What is queued and why (`queue`), whether its pages answer (`queue check`), where they
came from (`queue stats`), and the passes that take pages out before they are paid for:
expired ones (`queue prune`), a story's second copy (`queue duplicates`), pages no
campus story can be in (`queue aside`), headlines a model rules out (`queue screen`),
and publishers that refuse every read (`queue unreadable`). Moved out of `sync.py`,
which fills the queue, when that module passed the package's size guard; the reading
of the queue is drawn in `docs/workflows/sync.md`.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

import typer
from rich.markup import escape
from rich.table import Table

from tracker.cli._shared import (
    NA,
    TABLE_BOX,
    _db_path,
    _fail,
    _print_report_rows,
    _read_engine,
    _writable,
    console,
    emit,
    json_mode,
    queue_app,
)
from tracker.config import cache_dir as article_cache
from tracker.config import get_settings
from tracker.db import init_db, session_scope


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
        if failed and (stuck := len(disc.given_up(session))):
            console.print(
                f"[dim]{stuck} of these failed the same way {disc.MAX_SAME_FAILURES} times "
                "running and are no longer retried automatically; "
                "`tracker ingest crawl --url <URL>` still reads one[/dim]"
            )
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

    **A name that does not resolve is dead only if other names did.** During a DNS
    outage every URL fails its lookup the same way, and `--drop` used to delete
    everything the check asked about. See `discover.verify_urls`.
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


@queue_app.command("duplicates")
def queue_duplicates(
    drop: Annotated[
        bool,
        typer.Option("--drop", help="Set them aside. Without this it only reports."),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="Examples to print.")] = 10,
) -> None:
    """Queued articles that are another copy of a story already read or queued.

    The same page under another address, the same headline on two sites, a headline
    a search engine cut short. `ingest crawl --from-queue` and `sync` set them aside
    before reading the queue anyway; this does it on demand and shows what goes. Set
    aside is `skipped` with the address of the copy kept, not deleted, and a copy is
    put back when the one kept could not be read.
    """
    from tracker.ingest import discover as disc

    engine = _writable("queue duplicates") if drop else _read_engine()
    with session_scope(engine, commit=drop) as session:
        reopened = disc.reopened_names(session)
        unreadable = disc.unreadable_test(article_cache("articles"), reopened=reopened)
        found, back = disc.drop_same_story(session, unreadable, apply=drop)
        pairs = [(row.url, kept) for row, kept in found]

    if json_mode():
        emit({"copies": [{"url": u, "kept": k} for u, k in pairs], "put_back": back})
        return
    if back:
        console.print(f"{back} copy/copies set aside earlier {'are' if drop else 'would be'} back")
    if not pairs:
        console.print("[green]nothing queued is another copy of a story[/green]")
        return
    console.print(f"[yellow]{len(pairs)}[/yellow] queued article(s) are another copy of a story\n")
    for url, kept in pairs[:limit]:
        console.print(f"  {escape(url)}\n    [dim]same story as[/dim] {escape(kept)}")
    if drop:
        console.print(f"\n[green]set aside {len(pairs)} article(s)[/green]")
    else:
        console.print("\n[dim]set them aside with:[/dim] tracker queue duplicates --drop")


@queue_app.command("aside")
def queue_aside(
    drop: Annotated[
        bool,
        typer.Option("--drop", help="Set them aside. Without this it only reports."),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="Examples to print.")] = 10,
) -> None:
    """Queued pages no campus story can be in, and why each.

    A PDF (the reader takes no text from one), a translated copy, a stock-quote or
    stock-chatter page, a job posting, a directory's ZIP-code page, a site that has
    refused every read; and from a builder's own archive, a post about a place abroad
    or an old post naming nothing. `ingest crawl --from-queue` and `sync` set them
    aside before reading the queue anyway; this does it on demand and shows what goes.
    Set aside is `skipped` with the reason, not deleted, and a page no rule names any
    more is put back on the next run.
    """
    from collections import Counter

    from tracker.ingest import discover as disc
    from tracker.ingest import screen

    engine = _writable("queue aside") if drop else _read_engine()
    with session_scope(engine, commit=drop) as session:
        found, back = disc.set_aside_not_news(session, apply=drop)
        rows = [(row.id, row.url, reason) for row, reason in found]

    if json_mode():
        emit(
            {
                "aside": [{"id": i, "url": u, "reason": r} for i, u, r in rows],
                "put_back": back,
            }
        )
        return
    if back:
        console.print(f"{back} page(s) set aside earlier {'are' if drop else 'would be'} back")
    if not rows:
        console.print("[green]nothing queued is a page no campus story can be in[/green]")
        return
    console.print(f"[yellow]{len(rows)}[/yellow] queued page(s) no campus story can be in\n")
    for label, count in Counter(screen.kind(r) for _, _, r in rows).most_common():
        console.print(f"  {count:>5}  {escape(label)}")
    console.print("\n[bold]examples[/bold]")
    for row_id, url, reason in rows[:limit]:
        console.print(f"  #{row_id} {escape(url)}\n        [dim]{escape(reason)}[/dim]")
    if drop:
        console.print(f"\n[green]set aside {len(rows)} page(s)[/green]")
    else:
        console.print("\n[dim]set them aside with:[/dim] tracker queue aside --drop")


@queue_app.command("screen")
def queue_screen(
    measure: Annotated[
        bool,
        typer.Option(
            "--measure",
            help="Judge pages already read and count the campuses the screen would have "
            "lost. Writes nothing; each fifty headlines is a paid call.",
        ),
    ] = False,
    drop: Annotated[
        bool,
        typer.Option("--drop", help="Set aside what it names. Without this it only reports."),
    ] = False,
    undo: Annotated[
        bool,
        typer.Option(
            "--undo",
            help="Put back every page set aside by its headline, and keep the screen from "
            "setting them aside again under this prompt.",
        ),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="Queued pages to screen, in reading order; with --measure, past reads to "
            "judge, newest first. 0 means all.",
        ),
    ] = 100,
    examples: Annotated[int, typer.Option("--examples", help="Pages to print.")] = 20,
) -> None:
    """Have a model read queued headlines and set aside what cannot be campus news.

    A hire, an award, a webinar, a product launch, market commentary, a place
    abroad: a cheap call reads fifty headlines at once, and a page it names is
    `skipped` with the kind, not deleted. `--measure` puts the headlines of pages
    already read through it first, and lists every read that found a campus it
    would have set aside. `ingest crawl --from-queue --screen` does this before
    reading; the nightly loop passes it only with its own `--screen`.
    """
    from collections import Counter

    from sqlalchemy import select

    from tracker.ingest import discover as disc
    from tracker.ingest import headlines
    from tracker.llm import LLMUnavailable, build_extractor
    from tracker.models import IngestUrl
    from tracker.vocab import PENDING_URL_STATUS

    if sum((measure, drop, undo)) > 1:
        _fail("pass one of --measure, --drop or --undo")
        return

    if undo:
        engine = _writable("queue screen")
        with session_scope(engine) as session:
            rows = list(
                session.scalars(
                    select(IngestUrl).where(
                        IngestUrl.status == "skipped",
                        IngestUrl.error.like(f"{headlines.ASIDE}%"),
                    )
                )
            )
            for row in rows:
                row.status, row.error = PENDING_URL_STATUS, None
                row.screened_by = headlines.STAMP
        console.print(f"[green]{len(rows)} page(s) set aside by their headline are back[/green]")
        return

    try:
        extractor = build_extractor(get_settings())
    except LLMUnavailable as exc:
        _fail(str(exc))
        return

    if measure:
        with session_scope(_read_engine(), commit=False) as session:
            try:
                result = headlines.measure(session, extractor, limit=limit or None)
            except LLMUnavailable as exc:
                _fail(str(exc))
                return
            lost = [(r.id, r.title, r.url, kind, cited) for r, kind, cited in result.lost]
            spared = [kind for _, kind in result.spared]
            spared_examples = [(r.id, r.title, kind) for r, kind in result.spared[:examples]]
        if json_mode():
            emit(
                {
                    "found": result.found,
                    "empty": result.empty,
                    "lost": [
                        {"id": i, "title": t, "url": u, "kind": k, "cited": c}
                        for i, t, u, k, c in lost
                    ],
                    "spared": dict(Counter(spared)),
                }
            )
            return
        console.print(
            f"judged {result.found + result.empty} past read(s) by their headline: "
            f"{result.found} found a campus, {result.empty} found none\n"
        )
        share = len(lost) / result.found if result.found else 0.0
        console.print(
            f"  found a campus   {result.found - len(lost):>6} kept, "
            f"[{'red' if lost else 'green'}]{len(lost)} would have been set aside[/] "
            f"({share:.1%}), {sum(c for *_, c in lost)} of them cited by a campus today"
            + (f": {escape(headlines.summary(k for *_, k, _ in lost))}" if lost else "")
        )
        saved = len(spared) / result.empty if result.empty else 0.0
        console.print(
            f"  found nothing    {len(spared):>6} would have been set aside ({saved:.0%})"
            + (f": {escape(headlines.summary(spared))}" if spared else "")
        )
        acted = sorted(headlines.ACTED_ON)
        acted_lost = sum(1 for *_, k, _ in lost if k in headlines.ACTED_ON)
        acted_spared = sum(1 for k in spared if k in headlines.ACTED_ON)
        console.print(
            f"\n  acting on {escape(', '.join(acted))} only, as the crawl does: "
            f"{acted_spared} of the {result.empty} useless reads set aside, "
            f"{acted_lost} of the {result.found} useful ones lost"
        )
        if lost:
            console.print("\n[bold]reads that found a campus and would have been lost[/bold]")
            for row_id, title, url, kind, cited in lost:
                mark = " [yellow]cited[/yellow]" if cited else ""
                console.print(
                    f"  #{row_id} {escape(f'[{kind}]')}{mark} {escape(title or '')}\n"
                    f"        [dim]{escape(url)}[/dim]"
                )
        if spared_examples:
            console.print("\n[bold]reads that found nothing and would have been set aside[/bold]")
            for row_id, title, kind in spared_examples:
                console.print(f"  #{row_id} {escape(f'[{kind}]')} {escape(title or '')}")
        return

    engine = _writable("queue screen") if drop else _read_engine()
    with session_scope(engine, commit=drop) as session:
        rows = disc.pending(session, new_first=True)
        exempt = {row.id for row in disc.deciding(session)}
        try:
            result = headlines.screen_queue(
                extractor,
                rows,
                want=limit or None,
                exempt=exempt,
                apply=drop,
                places=headlines.places_held(session),
            )
        except LLMUnavailable as exc:
            _fail(str(exc))
            return
        aside = [(row.id, row.title, kind) for row, kind in result.aside]
    if json_mode():
        emit(
            {
                "asked": result.asked,
                "aside": [{"id": i, "title": t, "kind": k} for i, t, k in aside],
            }
        )
        return
    console.print(
        f"asked about {result.asked} headline(s); "
        f"[yellow]{len(aside)}[/yellow] {'set aside' if drop else 'would be set aside'}"
        + (f": {escape(headlines.summary(k for *_, k in aside))}" if aside else "")
    )
    for row_id, title, kind in aside[:examples]:
        console.print(f"  #{row_id} {escape(f'[{kind}]')} {escape(title or '')}")
    if aside and not drop:
        console.print("\n[dim]set them aside with:[/dim] tracker queue screen --drop")


@queue_app.command("unreadable")
def queue_unreadable(
    drop: Annotated[
        bool,
        typer.Option("--drop", help="Take them out of the queue. Without this it only reports."),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="Examples to print.")] = 10,
) -> None:
    """Queued pages on publishers marked closed in `seed/feeds.toml`, with nothing cached.

    No fetch will read them: those publishers refuse every client this project is
    willing to be. `ingest crawl --from-queue` and `sync` discard them before reading
    the queue anyway; this does it on demand and shows what goes. Discarded is
    `skipped` with a reason that marks it, not deleted: when a publisher's weekly
    check answers again, its pages are put back.
    """
    from collections import Counter

    from tracker.confidence import registrable_domain
    from tracker.ingest import discover as disc

    engine = _writable("queue unreadable") if drop else _read_engine()
    with session_scope(engine, commit=drop) as session:
        reopened = disc.reopened_names(session)
        unreadable = disc.unreadable_test(article_cache("articles"), reopened=reopened)
        found = [row.url for row in disc.pending(session) if unreadable(row.url)]
        removed = disc.discard_unreadable(session, unreadable) if drop else 0

    if json_mode():
        emit({"unreadable": found, "discarded": removed})
        return
    if not found:
        console.print("[green]nothing queued is unreadable[/green]")
        return
    console.print(f"[yellow]{len(found)}[/yellow] queued page(s) no fetch will read\n")
    for domain, count in Counter(map(registrable_domain, found)).most_common(10):
        console.print(f"  {count:>5}  {escape(domain or '?')}")
    console.print("\n[bold]examples[/bold]")
    for url in found[:limit]:
        console.print(f"  {escape(url)}")
    if drop:
        console.print(f"\n[green]discarded {removed} page(s)[/green]")
    else:
        console.print("\n[dim]discard them with:[/dim] tracker queue unreadable --drop")
