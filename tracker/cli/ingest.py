"""`tracker ingest` — the paths that create rows.

Five readers converge on one normalizer and one write path. The identity arbiter
lives here rather than in `sync`, because this is where a row is created and
`sync` is a caller: see `docs/workflows/sync.md`.
"""

from __future__ import annotations

import atexit
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.markup import escape

from tracker.cli._shared import (
    _db_path,
    _explain_db_locks,
    _fail,
    _print_report,
    _print_report_rows,
    _use_llm,
    console,
    err,
    ingest_app,
)
from tracker.config import cache_dir as article_cache
from tracker.config import get_settings
from tracker.db import AlreadyRunning, acquire_write_lock, init_db, session_scope

# --- ingest -----------------------------------------------------------------


@ingest_app.command("manual")
def ingest_manual(
    json_path: Annotated[Path, typer.Option("--json", help="Seed JSON file.")],
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
    """Load hand-curated projects from a JSON file.

    A bare filename that names one of the seed files shipped with the package is
    resolved to the packaged copy. That is what keeps the README's quick start
    (`--json sample-projects.json`) working from any directory: the seed files live
    inside the package now, so an installed CLI run from elsewhere has no `seed/`
    beside it to point at. A path that exists always wins; this only fills in.
    """
    from tracker.config import package_root
    from tracker.ingest import manual

    if not json_path.exists():
        packaged = package_root() / "seed" / json_path.name
        if packaged.is_file():
            console.print(f"[dim]using the seed file shipped with tracker: {packaged}[/dim]")
            json_path = packaged
        else:
            _fail(f"no such file: {json_path}")

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


# --- reading articles: one command per way of choosing them ------------------
#
# `ingest crawl` used to be five jobs behind one name -- a file of links, a link, the
# queue, a re-read under a newer prompt, a re-read under a revised gate -- chosen by a
# flag, with every other job's flags accepted and quietly ignored: `--limit` beside
# `--urls`, `--new-first` beside `--stale-prompt`. Each job is its own command now and
# takes only the flags that mean something to it. What they share -- from building the
# model client to printing the report -- is `_read`, and the flags they share are
# declared once, here.

_Prompt = Annotated[str, typer.Option("--prompt", help="Prompt name or path, e.g. extract-v1.")]
_Browser = Annotated[
    bool,
    typer.Option(
        "--browser",
        help="Allow escalation to Crawl4AI for pages plain HTTP cannot read. Needs the 'crawl' extra.",
    ),
]
_DryRun = Annotated[bool, typer.Option("--dry-run", help="Report without writing.")]
_NoCache = Annotated[bool, typer.Option("--no-cache", help="Ignore the on-disk article cache.")]
_VerifyIdentity = Annotated[
    bool,
    typer.Option(
        "--verify-identity/--no-verify-identity",
        help=(
            "Default. Before creating a row that has a near-match, a model reads "
            "the article and says whether it is the same site — preventing the "
            "duplicate instead of reporting it. Fires only on ambiguous inserts, "
            "and falls back to creating the row whenever it is unsure."
        ),
    ),
]
_LlmProvider = Annotated[
    str | None,
    typer.Option(
        "--llm-provider",
        help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
        show_default=False,
    ),
]
_CachedOnly = Annotated[
    bool,
    typer.Option(
        "--cached-only",
        help="Read only articles already cached; never fetch. Chooses only articles "
        "the cache holds, before --limit is cut.",
    ),
]
_ExistingOnly = Annotated[
    bool,
    typer.Option(
        "--existing-only",
        help="Never create a project. Refusals are counted and logged.",
    ),
]


@dataclass(frozen=True)
class _Reading:
    """How to read, whichever command chose what to read: the flags they share."""

    prompt_name: str
    browser: bool
    dry_run: bool
    no_cache: bool
    verify_identity: bool
    cached_only: bool = False
    existing_only: bool = False


def _read(
    command: str,
    engine,
    url_list: list[str],
    *,
    source_label: str,
    force: bool,
    how: _Reading,
) -> None:
    """Read `url_list` through the crawl and print what came of it.

    The model client is built first, so a missing key fails before a single page is
    fetched; then the fetch ladder, the crawl, the identity arbiter's tally, the run
    ledger and the report. `command` is the subcommand's name, which labels all of it.
    """
    from tracker.ingest import crawl
    from tracker.ingest.fetch import MissingDependency, ensure_browser_available, escalation_ladder
    from tracker.llm import LLMUnavailable, build_extractor

    settings = get_settings()
    label = f"ingest {command}"
    # Resolved before any fetching, so a missing key fails immediately rather
    # than after paying for a page load per URL.
    try:
        extractor = build_extractor(settings)
    except LLMUnavailable as exc:
        _fail(str(exc))
        return

    if how.browser:
        # Fail on the flag, not twenty pages in. `__aenter__` holds the import,
        # so nothing before this point would have noticed the extra was absent.
        try:
            ensure_browser_available()
        except MissingDependency as exc:
            _fail(str(exc))
            return
    # A ladder, cheapest rung first. curl_cffi needs no flag: it costs one
    # ordinary request and clears the WAFs that fingerprint TLS rather than
    # reading the User-Agent. Chromium stays behind --browser.
    escalate = escalation_ladder(settings, browser=how.browser)

    cache_dir = None if how.no_cache else article_cache("articles")
    console.print(f"[dim]crawling {len(url_list)} URL(s) from {source_label}[/dim]")

    from tracker import keys as keys_mod

    started = time.monotonic()
    with session_scope(engine) as session:
        report = crawl.run(
            session,
            url_list,
            prompt_name=how.prompt_name,
            extractor=extractor,
            escalate=escalate,
            settings=settings,
            dry_run=how.dry_run,
            force=force,
            cache_dir=cache_dir,
            cached_only=how.cached_only,
            existing_only=how.existing_only,
            # A re-read of an article rows already cite goes back to the row it names,
            # spelling aside — see `keys.citing_route`.
            route=keys_mod.citing_route(session) if how.existing_only else None,
            arbiter=_identity_arbiter(how.verify_identity, label=label),
        )
    _report_arbiter(label)

    from tracker import runlog

    if not how.dry_run:
        runlog.record_ingest(
            _db_path(),
            command=label,
            report=report,
            seconds=time.monotonic() - started,
            model=getattr(settings, "minimax_model", None),
        )

    _print_report(report, title=f"{label}: {source_label}{' (dry run)' if how.dry_run else ''}")
    if report.llm_calls:
        console.print(
            f"[dim]{report.llm_calls} model call(s), {report.tokens:,} tokens "
            f"({report.prompt_tokens:,} in, {report.completion_tokens:,} out). "
            f"Logged to data/runs/{runlog.LOG_NAME}.[/dim]"
        )


def _cache_flags_agree(how: _Reading) -> None:
    """Refuse `--cached-only` beside `--no-cache` before choosing anything to read."""
    if how.cached_only and how.no_cache:
        _fail("--cached-only and --no-cache ask for opposite things")


#: The jobs `ingest crawl` ran behind a flag until each became its own command.
_MOVED: Final = {
    "--from-queue": "queue",
    "--stale-prompt": "reread",
    "--refused-before-gate": "recheck",
}

#: Flags only those jobs took, and the commands that take them now.
_ELSEWHERE: Final = {
    "--limit": ("queue", "reread", "recheck"),
    "--new-first": ("queue",),
    "--screen": ("queue",),
    "--cached-only": ("reread", "recheck"),
    "--existing-only": ("reread", "recheck"),
}


def _refuse_moved(given: dict[str, bool]) -> None:
    """Fail, naming the command to run instead, on a flag `ingest crawl` no longer takes.

    The old spellings are still parsed (as hidden options) so that they reach this
    rather than Click's bare "no such option", which would leave whoever typed
    `ingest crawl --from-queue` -- or a script that has not been updated -- with no
    idea where the job went.
    """
    for flag, command in _MOVED.items():
        if given.get(flag):
            _fail(
                f"`ingest crawl {flag}` is its own command now: run `tracker ingest "
                f"{command}` (`tracker ingest {command} --help` lists its options)"
            )
    for flag, commands in _ELSEWHERE.items():
        if given.get(flag):
            where = ", ".join(f"`tracker ingest {name}`" for name in commands)
            _fail(f"`ingest crawl` reads the links it is given and takes no {flag}: see {where}")


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
    check: Annotated[
        bool,
        typer.Option("--check", help="Test LLM connectivity and exit. Ingests nothing."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="With --urls, re-process URLs a previous run completed."),
    ] = False,
    prompt_name: _Prompt = "extract-v1",
    browser: _Browser = False,
    dry_run: _DryRun = False,
    no_cache: _NoCache = False,
    verify_identity: _VerifyIdentity = True,
    llm_provider: _LlmProvider = None,
    # The flags of the jobs that moved out, hidden: see `_refuse_moved`.
    from_queue: Annotated[bool, typer.Option("--from-queue", hidden=True)] = False,
    stale_prompt: Annotated[bool, typer.Option("--stale-prompt", hidden=True)] = False,
    refused: Annotated[bool, typer.Option("--refused-before-gate", hidden=True)] = False,
    limit: Annotated[int | None, typer.Option("--limit", hidden=True)] = None,
    new_first: Annotated[bool, typer.Option("--new-first", hidden=True)] = False,
    screen: Annotated[bool | None, typer.Option("--screen/--no-screen", hidden=True)] = None,
    cached_only: Annotated[bool, typer.Option("--cached-only", hidden=True)] = False,
    existing_only: Annotated[bool, typer.Option("--existing-only", hidden=True)] = False,
) -> None:
    """Extract projects from the news articles you name, with an LLM.

    Every non-null value must be backed by a verbatim quote that really appears in
    the fetched article; unsupported values are dropped and the drop is recorded
    in the project's notes.

    `--urls FILE` reads a file of links, `--url` one link (repeatable), and either is
    read even if a previous run already read it: a link named by hand is one somebody
    wants read now. `--check` only tests that the model answers.

    The other ways of choosing what to read are commands of their own: `tracker
    ingest queue` reads what `tracker discover` queued, `tracker ingest reread` what
    an older prompt extracted, and `tracker ingest recheck` what the evidence gate
    refused before its last revision.
    """
    _refuse_moved(
        {
            "--from-queue": from_queue,
            "--stale-prompt": stale_prompt,
            "--refused-before-gate": refused,
            "--limit": limit is not None,
            "--new-first": new_first,
            "--screen": screen is not None,
            "--cached-only": cached_only,
            "--existing-only": existing_only,
        }
    )
    _use_llm(llm_provider)
    from tracker.ingest import crawl
    from tracker.llm import LLMUnavailable, build_extractor

    if check:
        try:
            info = build_extractor(get_settings()).check()
        except LLMUnavailable as exc:
            _fail(str(exc))
            return
        except Exception as exc:
            _fail(f"LLM check failed: {exc}")
            return
        for key, value in info.items():
            console.print(f"{key:18} {value}")
        console.print("[green]ok[/green]")
        return

    if urls and url:
        _fail("pass only one of --urls or --url (got --urls, --url)")
        return
    if not urls and not url:
        _fail(
            "pass --urls FILE or --url URL, or --check to test connectivity. The queue "
            "is `tracker ingest queue`; re-reads are `tracker ingest reread` and "
            "`tracker ingest recheck`"
        )
        return

    engine, _ = init_db(_db_path())
    if url:
        from tracker.normalize import canonical_url

        # Deduped and canonicalised like `read_urls` does, so passing the same link
        # twice — or once with its tracking parameters — costs one call, not two.
        url_list = list(dict.fromkeys(canonical_url(u) for u in url if u.strip()))
        bad = [u for u in url_list if not u.lower().startswith(("http://", "https://"))]
        if bad:
            _fail(f"not an http(s) URL: {bad[0]}")
            return
        source_label = url_list[0] if len(url_list) == 1 else f"{len(url_list)} URLs"
        # A URL named explicitly is one the operator wants read now, and the skip
        # rule would otherwise make a second attempt at an already-seen link do
        # nothing at all.
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

    _read(
        "crawl",
        engine,
        url_list,
        source_label=source_label,
        force=force,
        how=_Reading(prompt_name, browser, dry_run, no_cache, verify_identity),
    )


@ingest_app.command("queue")
def ingest_queue(
    limit: Annotated[
        int | None, typer.Option("--limit", help="Read at most this many queued articles.")
    ] = None,
    new_first: Annotated[
        bool,
        typer.Option(
            "--new-first",
            help="Read the news first, then articles naming no tracked campus, newest first.",
        ),
    ] = False,
    screen_headlines: Annotated[
        bool,
        typer.Option(
            "--screen/--no-screen",
            help="Have a model read the headlines first and set aside what cannot be "
            "campus news (`tracker queue screen`).",
        ),
    ] = False,
    prompt_name: _Prompt = "extract-v1",
    browser: _Browser = False,
    dry_run: _DryRun = False,
    no_cache: _NoCache = False,
    verify_identity: _VerifyIdentity = True,
    llm_provider: _LlmProvider = None,
) -> None:
    """Read what `tracker discover` queued: the nightly loop's read of the news.

    Before choosing anything, the queue is tidied (`discover.prepare_queue`, which
    `tracker sync` shares): pages on a publisher closed to us or refusing every read,
    with nothing cached, are discarded, and put back once their publisher is readable
    again; pages no campus story can be in, and every other copy of a story, are set
    aside; and once a week a refusing publisher has one page read first, to see
    whether it still refuses. Each step is reported. That tidying is committed before
    the queue is read, so a dry run reports the queue as it stood.

    Then the queue is read in order — the news-watch results that decide a flag, the
    weekly checks, then with `--new-first` the recent news and the articles naming no
    tracked campus — up to `--limit`. `--screen` first has a model read the headlines
    and set aside what cannot be campus news; the limit then counts what it kept.
    """
    _use_llm(llm_provider)
    from tracker.ingest import discover as disc

    engine, _ = init_db(_db_path())
    cache = None if no_cache else article_cache("articles")
    with session_scope(engine, commit=not dry_run) as session:
        prep = disc.prepare_queue(session, cache, probe=True, limit=limit)
    _print_prepared(prep, dry_run=dry_run)
    # Its own transaction, after the preparation's has committed: the headline screen
    # waits on a model, and nothing should hold the write lock while it does.
    with session_scope(engine, commit=screen_headlines and not dry_run) as session:
        queued = disc.pending(
            session,
            limit=None if screen_headlines else limit,
            new_first=new_first,
            unreadable=prep.unreadable,
            probes=frozenset(prep.probes),
        )
        if screen_headlines:
            queued = _screen_headlines(session, queued, want=limit, apply=not dry_run)
        url_list = [row.url for row in queued]
    if not url_list:
        console.print("[green]queue is empty[/green] — run `tracker discover` to look for articles")
        return
    # A queued URL is `discovered`, not `ok`, so crawl would process it
    # anyway; forcing makes that explicit and independent of the skip rule.
    _read(
        "queue",
        engine,
        url_list,
        source_label="queue",
        force=True,
        how=_Reading(prompt_name, browser, dry_run, no_cache, verify_identity),
    )


def _print_prepared(prep, *, dry_run: bool) -> None:
    """Say what the queue's preparation set aside, discarded, put back and checked."""
    from tracker.ingest import discover as disc

    would = "would be" if dry_run else "were"
    if prep.not_news:
        from tracker.ingest import screen

        console.print(
            f"[dim]{len(prep.not_news)} queued page(s) {would} "
            "set aside before reading, as no campus story can be in them: "
            f"{escape(screen.summary(prep.not_news))} "
            "(`tracker queue aside` lists them)[/dim]"
        )
    if prep.unset:
        console.print(
            f"[dim]{prep.unset} page(s) set aside earlier are back in the queue: no rule "
            "names them any more[/dim]"
        )
    if prep.back:
        console.print(
            f"[dim]{prep.back} article(s) set aside as another copy are back in the queue: "
            "the copy kept could not be read[/dim]"
        )
    if prep.copies:
        console.print(
            f"[dim]{prep.copies} queued article(s) {would} "
            "set aside as another copy of a story already read or queued[/dim]"
        )
    if prep.requeued:
        console.print(
            f"[dim]{prep.requeued} discarded article(s) are back in the queue: their "
            "publisher answered its weekly check, or had a page read[/dim]"
        )
    if prep.refusing:
        worst = sorted(prep.refusing.values(), key=lambda r: (-r.pages, r.host))[:5]
        console.print(
            f"[dim]{len(prep.refusing)} publisher(s) refused every read lately, the last "
            f"{disc.REFUSING_AFTER} or more of their pages on {disc.REFUSING_DAYS} or "
            "more days: "
            + escape(", ".join(f"{r.host} ({r.pages})" for r in worst))
            + (", …" if len(prep.refusing) > len(worst) else "")
            + " (`tracker queue unreadable` lists them)[/dim]"
        )
    if prep.discarded:
        console.print(
            f"[dim]{prep.discarded} queued article(s) {would} "
            "discarded: on publishers marked closed in seed/feeds.toml or refusing "
            "every read, with nothing cached[/dim]"
        )
    if prep.probes:
        console.print(
            f"[dim]{len(prep.probes)} weekly check(s): one page each of "
            + escape(", ".join(sorted({disc.publisher_of(url) for url in prep.probes})))
            + " is read first, to see whether the publisher still refuses[/dim]"
        )


@ingest_app.command("reread")
def ingest_reread(
    limit: Annotated[
        int | None, typer.Option("--limit", help="Re-read at most this many articles.")
    ] = None,
    cached_only: _CachedOnly = False,
    existing_only: _ExistingOnly = False,
    prompt_name: _Prompt = "extract-v1",
    browser: _Browser = False,
    dry_run: _DryRun = False,
    no_cache: _NoCache = False,
    verify_identity: _VerifyIdentity = True,
    llm_provider: _LlmProvider = None,
) -> None:
    """Re-read sources extracted by an older version of the prompt.

    Articles this database has already read, read again with the prompt as it stands
    today. The gate has been tightened repeatedly and each improvement only ever
    applied to rows written after it landed — so a row extracted before migration
    `0007` has no per-field quote, not because nothing supported it but because there
    was nowhere to record the sentence. Those rows read as established ever since.
    Served from the article cache by default, which makes it a re-read rather than a
    re-fetch: `--no-cache` would confound "the prompt improved" with "the page
    changed".

    **Pair it with `--existing-only`.** An article this database already cites also
    names other things — re-reading Hyperion's coverage yields "Project Everest" and a
    parish's generating units — and a repair pass that quietly adds campuses is an
    ingest with no worklist and no review. The refusals are counted and logged, so a
    genuinely new campus can be added deliberately afterwards. With `--cached-only`
    only what the cache can serve is chosen, so the limit is not spent on the same
    refusals every night.
    """
    _use_llm(llm_provider)
    how = _Reading(
        prompt_name, browser, dry_run, no_cache, verify_identity, cached_only, existing_only
    )
    _cache_flags_agree(how)
    from tracker import policy as policy_mod
    from tracker.ingest import crawl
    from tracker.prompts import load_prompt

    engine, _ = init_db(_db_path())
    stamp = load_prompt(prompt_name).stamp
    with session_scope(engine, commit=False) as session:
        url_list = crawl.stale_by_prompt(
            session,
            stamp=stamp,
            limit=limit,
            readable_in=None if (no_cache or not cached_only) else article_cache("articles"),
            policy=policy_mod.load() if cached_only else None,
        )
    if not url_list:
        console.print(f"[green]every extracted source is on {stamp}[/green] — nothing to re-read")
        return
    # Every one of these is already `ok`, so the skip rule would drop all of them.
    # Re-reading an article we have already read, under a prompt that has since been
    # tightened, is the entire point of this command.
    _read(
        "reread",
        engine,
        url_list,
        source_label=f"prompts superseded by {stamp}",
        force=True,
        how=how,
    )


@ingest_app.command("recheck")
def ingest_recheck(
    limit: Annotated[
        int | None, typer.Option("--limit", help="Re-read at most this many articles.")
    ] = None,
    cached_only: _CachedOnly = False,
    existing_only: _ExistingOnly = False,
    prompt_name: _Prompt = "extract-v1",
    browser: _Browser = False,
    dry_run: _DryRun = False,
    no_cache: _NoCache = False,
    verify_identity: _VerifyIdentity = True,
    llm_provider: _LlmProvider = None,
) -> None:
    """Re-read what the evidence gate refused before its last revision, most first.

    Sources holding a value, milestone or party the gate refused, read again under the
    gate as it stands now. The same re-read as `tracker ingest reread`, for a change to the gate rather than
    to the prompt — which does not move the prompt's stamp and so is invisible to
    `reread`: articles holding a refusal from before `crawl.GATE_REVISED_AT` that no
    crawl has tried since. Each is taken once. Articles behind an announcement,
    expansion, filing or delay the tense check filed as a forecast before
    `crawl.TENSE_CHECK_REVISED_AT` go first.

    Pair it with `--existing-only` and `--cached-only`, for the reasons `tracker ingest
    reread --help` gives.
    """
    _use_llm(llm_provider)
    how = _Reading(
        prompt_name, browser, dry_run, no_cache, verify_identity, cached_only, existing_only
    )
    _cache_flags_agree(how)
    from tracker import policy as policy_mod
    from tracker.ingest import crawl

    engine, _ = init_db(_db_path())
    with session_scope(engine, commit=False) as session:
        url_list = crawl.refused_before_gate(
            session,
            limit=limit,
            readable_in=None if (no_cache or not cached_only) else article_cache("articles"),
            policy=policy_mod.load() if cached_only else None,
        )
    if not url_list:
        console.print(
            "[green]no refusal from before the gate's last revision is left to re-read[/green]"
        )
        return
    _read(
        "recheck",
        engine,
        url_list,
        source_label=f"refusals from before {crawl.GATE_REVISED_AT:%Y-%m-%d %H:%M} UTC",
        force=True,
        how=how,
    )


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
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
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
    _use_llm(llm_provider)
    from tracker.ingest import edgar
    from tracker.ingest.crawl import run as run_crawl

    settings = get_settings()
    cache_dir = article_cache("articles")
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

    try:
        # Constructing IS the readiness check, for whichever provider is answering:
        # key presence on the API, a living server locally. Nothing is spent.
        from tracker.llm import LLMUnavailable, build_extractor

        build_extractor(settings)
    except LLMUnavailable as exc:
        _fail(
            f"{len(urls)} filing(s) are prepared, but extraction cannot start:\n{exc}\n"
            "The prepared text is cached, so fixing this and re-running costs no fetches."
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


def _screen_headlines(session, queued: list, *, want: int | None, apply: bool) -> list:
    """The first `want` queued pages the headline screen keeps, setting aside the rest.

    A provider that cannot answer at all leaves the queue as it was: the crawl reads
    the first `want` unscreened, which is what it did before the screen existed.
    """
    from tracker.ingest import discover as disc
    from tracker.ingest import headlines
    from tracker.llm import LLMUnavailable, build_extractor

    try:
        result = headlines.screen_queue(
            build_extractor(get_settings()),
            queued,
            want=want,
            exempt={row.id for row in disc.deciding(session)},
            apply=apply,
            places=headlines.places_held(session),
        )
    except LLMUnavailable as exc:
        console.print(f"[yellow]headline screen skipped: {escape(str(exc))}[/yellow]")
        return queued[:want] if want is not None else queued
    if result.aside:
        console.print(
            f"[dim]{len(result.aside)} of {result.asked} headline(s) read "
            f"{'would be' if not apply else 'were'} set aside as no campus news: "
            f"{escape(headlines.summary(kind for _, kind in result.aside))} "
            "(`tracker queue screen --undo` puts them back)[/dim]"
        )
    return result.kept


def _identity_arbiter(enabled: bool, *, label: str):
    """The write-time duplicate gate, or None.

    Shared by `ingest crawl` and `sync` so the two cannot drift into different
    rules about when a row may be created. Returns None — the old behaviour —
    whenever it is off or no model is configured, because an ingest that refuses
    to run for want of an optional check is worse than one that makes a duplicate.
    """
    if not enabled:
        return None
    from tracker.gatekeeper import same_site_arbiter
    from tracker.llm import LLMUnavailable, agent_extractor

    try:
        extractor = agent_extractor(get_settings())
    except LLMUnavailable as exc:
        err.print(f"[yellow]--verify-identity skipped: {exc}[/yellow]")
        return None

    seen = {"asked": 0, "routed": 0, "tokens": 0, "warm": 0}

    def note(decision: dict) -> None:
        seen["asked"] += 1
        seen["tokens"] += decision["prompt_tokens"] + decision["completion_tokens"]
        # Counted so a run can be asked which path actually decided. The warm one
        # rules from the article the crawl already read; the cold one goes and
        # fetches it again, which is what this change exists to stop.
        seen["warm"] += 1 if decision.get("via") == "warm" else 0
        if decision["routed"]:
            seen["routed"] += 1
            console.print(
                f"  [green]routed[/green] an arriving record onto "
                f"#{decision['candidate_id']} instead of creating a row "
                f"[dim]— {escape(str(decision.get('note', ''))[:90])}[/dim]"
            )
        else:
            console.print(
                f"  [dim]{label}: kept #{decision['candidate_id']} separate "
                f"({escape(str(decision['outcome'])[:60])})[/dim]"
            )

    _ARBITER_TALLY[label] = seen
    return same_site_arbiter(extractor, on_decision=note)


#: What each run's arbiter did, so the summary can report it. Keyed by label
#: because `sync` runs more than one crawl phase.
_ARBITER_TALLY: dict[str, dict] = {}


def _report_arbiter(label: str) -> None:
    seen = _ARBITER_TALLY.pop(label, None)
    if not seen or not seen["asked"]:
        return
    cold = seen["asked"] - seen.get("warm", 0)
    how = f", {cold} needed a re-read" if cold else ""
    console.print(
        f"[dim]identity: {seen['asked']} ambiguous insert(s) checked, "
        f"{seen['routed']} duplicate(s) prevented{how}, ~{seen['tokens']:,} tokens[/dim]"
    )
