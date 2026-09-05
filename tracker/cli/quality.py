"""Making the stored data true: initialise, recompute, sweep, audit, confirm, rank.

`backfill` and `clean` recompute what is derivable and report what moved; `audit`
and `risks` put implausible figures and unquoted obstacles to a model; `sources`
ranks publishers and records the policy that ranking justifies.
"""

from __future__ import annotations

import atexit
import json
from pathlib import Path
from typing import Annotated

import typer
from rich import box
from rich.markup import escape
from rich.table import Table
from sqlalchemy import func, select

from tracker import __version__
from tracker.cli._render import (
    _dedupe_risks,
    _print_risk_detail,
    _print_risk_footer,
    _print_risk_kinds,
)
from tracker.cli._shared import (
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
    audit_app,
    console,
    emit,
    err,
    json_mode,
    risks_app,
    sources_app,
)
from tracker.config import cache_dir as article_cache
from tracker.config import get_settings
from tracker.db import AlreadyRunning, acquire_write_lock, init_db, schema_version, session_scope
from tracker.models import Project, Risk
from tracker.vocab import OPEN_RISK_STATUS, RISK_CATEGORIES, RISK_SEVERITIES


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
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
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
    _use_llm(llm_provider)
    from tracker import riskcheck

    path = _db_path()
    if not path.is_file():
        _fail(f"database not found: {path}\nRun `tracker init` first.")
    if category and category not in RISK_CATEGORIES:
        _fail(f"--category must be one of: {', '.join(RISK_CATEGORIES)}")

    from tracker.llm import LLMUnavailable, reasoning_extractor

    try:
        extractor = reasoning_extractor(get_settings())
    except LLMUnavailable as exc:
        _fail(str(exc))
        raise

    engine = _read_engine() if dry_run else _writable("risks confirm")

    cache_dir = article_cache("articles")

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


@sources_app.callback(invoke_without_command=True)
def sources_cmd(
    ctx: typer.Context,
    by: Annotated[
        str,
        typer.Option(
            "--by",
            help="Order: decisive (how much we use it), contested (quality), yield (per citation).",
        ),
    ] = "decisive",
    limit: Annotated[int, typer.Option("--limit", help="Publishers to show.")] = 25,
    min_cited: Annotated[
        int | None,
        typer.Option(
            "--min-cited",
            help="Citations required before a per-citation ordering ranks a host.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Which publishers actually decide a stored value. Free, read-only, no LLM.

    Ranks nothing by hand. `SOURCE_WEIGHTS` gives six *types* a weight from 1 to 3
    and nothing can check it; this counts what each publisher's claims did, which
    re-runs to the same answer or the database moved.

    `decisive` is a value this host's claim won. `contested` is the subset where a
    rival asserted something different — the only column that is evidence of
    anything, because an unopposed win just means nobody else spoke. Identity
    fields are excluded: `name` and `company` are FILL_ONLY, so winning one
    records crawl order.

    `tracker sources policy` turns this table into a file the pipeline obeys.
    """
    # A group with a callback: without this, `tracker sources policy` would print
    # the whole ranking first and then run the subcommand.
    if ctx.invoked_subcommand is not None:
        return

    from tracker import sources as sources_mod

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        survey = sources_mod.survey(session)

    if not survey.hosts:
        if json_mode():
            emit(survey.as_json())
            return
        console.print("[yellow]no citations yet[/yellow] — run `tracker sync` first")
        return

    try:
        ranked = survey.ranked(by=by, min_cited=min_cited)
    except ValueError as exc:
        _fail(str(exc))

    if json_mode():
        # The full ranking, not the display slice: a consumer asked for the data.
        emit({**survey.as_json(), "ordered_by": by, "hosts": [h.as_json() for h in ranked]})
        return

    console.print(
        f"[bold]{len(survey.hosts)}[/bold] publisher(s) across "
        f"{survey.sources_read} citation(s) on {survey.projects_read} project(s); "
        f"[bold]{survey.decisions}[/bold] value(s) decided, "
        f"[bold]{survey.contested}[/bold] against a disagreeing rival"
    )
    if survey.skipped:
        console.print(
            f"[dim]{survey.skipped} row(s) excluded as reference data or placeholders — "
            f"they publish nothing to rank.[/dim]"
        )

    table = Table(header_style="bold", box=TABLE_BOX)
    table.add_column("publisher")
    table.add_column("cited", justify="right")
    table.add_column("decided", justify="right")
    table.add_column("contested", justify="right")
    table.add_column("inert", justify="right")
    table.add_column("per cite", justify="right")
    table.add_column("weight", justify="right")
    table.add_column("wins most")
    for host in ranked[:limit]:
        best = host.fields.most_common(1)
        table.add_row(
            host.host,
            str(host.cited),
            str(host.decisive),
            str(host.contested),
            f"[yellow]{host.inert}[/yellow]" if host.inert else "0",
            f"{host.yield_per_citation:.2f}",
            str(host.type_weight),
            f"{best[0][0]} ({best[0][1]})" if best else "—",
        )
    console.print(table)

    if by != "decisive":
        floor = survey.MIN_CITED_FOR_RATIO if min_cited is None else min_cited
        hidden = len(survey.hosts) - len([h for h in survey.hosts if h.cited >= floor])
        if hidden:
            console.print(
                f"[dim]{hidden} host(s) below {floor} citation(s) are not ranked under "
                f"--by {by}: one citation on a single-source project wins every field "
                f"unopposed and outscores any real outlet.[/dim]"
            )

    # The divergence this report exists to surface. A hand-set weight that the
    # observed record contradicts is the finding, not a rounding error.
    proven = [h for h in survey.hosts if h.cited >= survey.MIN_CITED_FOR_RATIO]
    under = [h for h in proven if h.type_weight <= 2 and h.decisive >= 40]
    if under:
        console.print(
            "[yellow]Weight disagrees with the record[/yellow] for "
            + ", ".join(
                f"{h.host} (weight {h.type_weight}, decided {h.decisive})" for h in under[:4]
            )
            + " — heavier than their `source_type` says."
        )
    console.print(
        "[dim]Nothing here changes a weight. `SOURCE_WEIGHTS` is edited by hand in "
        "tracker/confidence.py; this is the evidence for doing so. "
        "`tracker sources policy` turns it into a file the pipeline obeys.[/dim]"
    )


@sources_app.command("policy")
def sources_policy(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write the file. Without it, proposes and writes nothing."),
    ] = False,
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Policy file to read and write.", show_default=False),
    ] = None,
    show_refused: Annotated[
        bool, typer.Option("--refused", help="List every publisher it declined to judge.")
    ] = False,
) -> None:
    """Turn the publisher ranking into a list the pipeline obeys. Free, no LLM.

    `tracker sources` has measured which publishers decide stored values for a
    while and could only ever print it. This writes `seed/sources.toml`, which
    `sync`, `enrich` and `ingest crawl` read: `priority` domains are offered first
    when a run is working to a budget, `ignore` domains are not queued or fetched
    again.

    **It changes what gets read, never what a stored citation is worth.** Weight
    stays per `source_type` and hand-edited. Citations already stored keep their
    values, quotes and weight whatever this writes.

    **Most publishers get no entry, and that is the design.** 560 of 654 are cited
    fewer than five times, where a per-citation ratio means nothing. `priority`
    needs ten citations, a win against a disagreeing rival, and a yield above the
    fleet's own average. `ignore` needs ten citations and a record of never once
    backing a stored value — the publisher-level twin of the `retire` verdict
    `tracker feeds` gives a feed.

    **Four things it refuses to propose**, and they carry more information than the
    proposals: a publisher we mostly cannot *fetch* (blocked is not worthless — the
    mistake `tracker feeds` documents), one still configured as a feed (retire it
    there, or discovery keeps polling and discarding), an operator's own newsroom,
    and anything merely thin. Thin is a prompt to look, never a proposal.

    Proposes by default; `--apply` writes. A domain already in the file keeps its
    rank and its sentence — an operator who has vetoed a proposal should not have to
    veto it again after every run — and nothing is ever deleted.
    """
    from tracker import policy as policy_mod
    from tracker import sources as sources_mod
    from tracker.ingest import discover as disc

    target = path or policy_mod.default_path()
    existing = policy_mod.load(target)

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        survey = sources_mod.survey(session)
        unread = disc.failure_summary(session)

    # Both are advisory context for the refusals; a broken feeds.toml must not stop
    # the policy being written, the same posture `sync` takes with it.
    try:
        feeds, _ = disc.load_config()
        feed_hosts = frozenset(policy_mod.registrable_domain(f.url) for f in feeds)
    except disc.DiscoverError:
        feed_hosts = frozenset()
    # Advisory only, and swallowed for the same reason `crawl.operator_hosts` does:
    # a missing or broken newsroom map should soften a refusal, never stop the run.
    try:
        newsrooms = frozenset(
            policy_mod.registrable_domain(f"https://{h}") for h in disc.newsroom_companies()
        )
    except (OSError, ValueError, KeyError):
        newsrooms = frozenset()

    analysis = policy_mod.analyse(
        survey,
        existing,
        unread_hosts=unread,
        feed_hosts=feed_hosts,
        newsroom_hosts=newsrooms,
    )
    proposed = policy_mod.to_policy(analysis.proposals)

    if json_mode():
        emit(
            {
                "path": str(target),
                "proposals": [
                    {"domain": p.domain, "rank": p.rank, "why": p.why, "was": p.was, "do": p.verb}
                    for p in analysis.proposals
                ],
                "refused": [
                    {"domain": r.domain, "class": r.why_class, "detail": r.detail}
                    for r in analysis.refusals
                ],
                "stale": analysis.stale,
                "applied": apply,
            }
        )
        if apply:
            policy_mod.write(proposed, target)
        return

    changing = [p for p in analysis.proposals if p.verb != "keep"]
    _print_report_rows(
        [
            ("publishers measured", len(survey.hosts)),
            ("priority", sum(1 for p in analysis.proposals if p.rank == policy_mod.PRIORITY)),
            ("ignore", sum(1 for p in analysis.proposals if p.rank == policy_mod.IGNORE)),
            ("would add or change", len(changing)),
            ("declined to judge", len(analysis.refusals)),
        ],
        title="sources policy" + ("" if apply else " (proposal)"),
    )

    for entry in analysis.proposals:
        if entry.verb == "keep":
            continue
        verb = "add   " if entry.verb == "add" else f"{entry.was} ->"
        tone = "green" if entry.rank == policy_mod.PRIORITY else "yellow"
        console.print(
            f"  [{tone}]{entry.rank:<8}[/{tone}] {escape(entry.domain):<28} "
            f"[dim]{verb} {escape(entry.why)}[/dim]"
        )

    by_class = analysis.by_class()
    for name in ("cannot read", "still a feed", "own newsroom", "thin", "too few to judge"):
        found = by_class.get(name)
        if not found:
            continue
        shown = ", ".join(f"{escape(r.domain)} ({escape(r.detail)})" for r in found[:4])
        more = f" and {len(found) - 4} more" if len(found) > 4 else ""
        console.print(f"  [dim]{name:<17}[/dim] {shown}{more}")
        if show_refused and len(found) > 4:
            for r in found[4:]:
                console.print(f"  [dim]{'':<17} {escape(r.domain)} ({escape(r.detail)})[/dim]")

    for line in analysis.stale:
        console.print(
            f"  [yellow]no longer justified[/yellow] {escape(line)} [dim]— left alone[/dim]"
        )

    if apply:
        written = policy_mod.write(proposed, target)
        console.print(f"\n[green]wrote[/green] {written}")
        console.print(
            "[dim]`sync`, `enrich` and `ingest crawl` read it on their next run. "
            "Nothing here touched a stored value.[/dim]"
        )
    else:
        console.print(
            f"\n[yellow]Nothing written.[/yellow] [dim]--apply writes {target}. "
            "It changes what gets read, never what a stored citation is worth.[/dim]"
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
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
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
    _use_llm(llm_provider)
    import sys as _sys

    from tracker import audit as audit_mod

    path = _db_path()
    if not path.is_file():
        _fail(f"database not found: {path}\nRun `tracker init` first.")

    settings = get_settings()
    extractor = None
    if llm:
        from tracker.llm import LLMUnavailable, reasoning_extractor

        try:
            extractor = reasoning_extractor(settings)
        except LLMUnavailable as exc:
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


def _backfill_scope(*, apply: bool, dry_run: bool) -> None:
    """Re-gate every stored `this_site` claim, and say what moved.

    Reports before it writes, like `dates`, because this changes what a label
    *means* on 9,000 claims: `this_site` used to be the value the gate could not
    refuse, so a stored one carries no information about whether it was ever
    checked. Nothing reads the axis to choose a value yet, so a re-gate cannot move
    a published figure today — it makes the axis worth reading tomorrow.
    """
    from tracker.backfill import regate_scope

    engine = _writable("backfill scope") if (apply and not dry_run) else _read_engine()
    with _explain_db_locks(), session_scope(engine, commit=apply and not dry_run) as session:
        report = regate_scope(session, apply=apply and not dry_run)

    if json_mode():
        emit(
            {
                "sources": report.sources,
                "claims": report.claims,
                "changed": report.changed,
                "moved": report.moved,
                "applied": apply and not dry_run,
            }
        )
        return

    table = Table(title="backfill scope", box=box.SIMPLE_HEAD)
    table.add_column("outcome")
    table.add_column("count", justify="right")
    table.add_row("sources with an envelope", f"{report.sources:,}")
    table.add_row("`this_site` claims re-gated", f"{report.claims:,}")
    table.add_row("relabelled", f"{report.changed:,}")
    console.print(table)

    for was, went in sorted(report.moved.items()):
        for now, count in sorted(went.items(), key=lambda kv: -kv[1]):
            console.print(f"  {was} -> [bold]{now}[/bold]  {count:,}")

    if not (apply and not dry_run):
        console.print("\n[dim]Nothing written. `--apply` writes the labels.[/dim]")
    else:
        console.print(
            "\n[dim]Written. `this_site` now means the sentence named this campus, "
            "and `block:*` means it named a tranche instead.[/dim]"
        )


def _backfill_dates(*, limit: int, refetch: bool, apply: bool, yes: bool, everything: bool) -> None:
    """`tracker backfill dates`. No LLM, no API key, one column.

    Kept out of `backfill()` proper because the two halves share almost nothing:
    this one spends requests rather than calls, needs no extractor, and writes
    `ingest_url.published_at` instead of `source.blocks`.
    """
    from tracker import dates as dates_mod
    from tracker.ingest.fetch import escalation_ladder

    settings = get_settings()
    engine, _ = init_db(_db_path())

    if refetch and not yes and not json_mode():
        from tracker.ingest.fetch import date_from_url

        with session_scope(engine, commit=False) as session:
            pending = dates_mod.undated_urls(session, everything=everything)
        # The free rung shrinks the fetch list, so quote what would actually be
        # requested rather than the whole backlog.
        would_fetch = sum(1 for url in pending if date_from_url(url) is None)
        if limit:
            would_fetch = min(would_fetch, limit)
        if would_fetch > 200:
            console.print(
                f"[yellow]{would_fetch} page(s) would be requested[/yellow] across "
                f"third-party hosts. [dim]Re-run with --yes, or --limit to go in "
                f"tranches.[/dim]"
            )
            raise typer.Exit(1)

    if apply:
        try:
            release_lock = acquire_write_lock(_db_path(), command="backfill dates")
        except AlreadyRunning as exc:
            _fail(str(exc))
            raise
        atexit.register(release_lock)

    escalate = escalation_ladder(settings, browser=False) if refetch else None
    with _explain_db_locks(), session_scope(engine, commit=apply) as session:
        report = dates_mod.run(
            session,
            limit=limit or None,
            refetch=refetch,
            apply=apply,
            everything=everything,
            settings=settings,
            escalate=escalate,
        )

    if json_mode():
        emit({"rows": dict(report.as_rows()), "remaining": report.remaining})
        return

    _print_report_rows(report.as_rows(), title="backfill dates" + ("" if apply else " (preview)"))
    for url, when, via in report.examples[:8]:
        console.print(f"  [dim]{when:%Y-%m-%d}  {via:<9} {escape(url[:78])}[/dim]")

    if not apply and report.dated:
        console.print(
            f"\n[yellow]Nothing written.[/yellow] [dim]--apply writes the "
            f"{report.dated} date(s) above.[/dim]"
        )
    if not refetch and report.remaining:
        console.print(
            f"[dim]{report.remaining} URL(s) carry no date in the path. --refetch asks "
            f"the publisher — one request each, no LLM, --limit to go in tranches.[/dim]"
        )
    elif report.remaining:
        console.print(f"[dim]{report.remaining} more beyond --limit. Run it again.[/dim]")
    if report.written:
        console.print(
            "\n[dim]The merge tiebreak still ranks on `fetched_at` until "
            "TRACKER_MERGE_BY_PUBLICATION_DATE=1. Measure first: "
            "`python scripts/measure_extraction.py`.[/dim]"
        )


def _backfill_derive(*, project_id: int | None, dry_run: bool) -> None:
    """`tracker backfill derive`. No LLM, no network, no migration.

    The command to run after any change to how something is derived. Every value
    on a project is a function of its citations, but the function is only applied
    when something writes to the row — so improving the merge policy, the block
    rollup or the evidence gate reaches stored projects only through this.

    Kept out of `backfill()` proper for the same reason `dates` is: it shares
    nothing with re-reading articles. It spends neither calls nor requests, needs
    no extractor, and writes every column rather than one.
    """
    from tracker import derive as derive_mod

    engine, _ = init_db(_db_path())

    if not dry_run:
        try:
            release_lock = acquire_write_lock(_db_path(), command="backfill derive")
        except AlreadyRunning as exc:
            _fail(str(exc))
            raise
        atexit.register(release_lock)

    # `--dry-run` is a transaction that is never committed, deliberately not a
    # second code path: a preview computed differently from the write is a preview
    # of something else.
    with _explain_db_locks(), session_scope(engine, commit=not dry_run) as session:
        report = derive_mod.run(session, project_id=project_id)
        by_field = report.by_field
        lines = [change.render() for change in report.changes]
        conflicts = dict(report.conflicts)
        rows = report.as_rows()

    if json_mode():
        emit({"rows": dict(rows), "by_field": by_field, "changes": lines})
        return

    _print_report_rows(rows, title="backfill derive" + (" (dry run)" if dry_run else ""))
    for name, count in by_field.items():
        console.print(f"  [dim]{name:<20} {count}[/dim]")
    for line in lines[:30]:
        console.print(f"  [dim]{escape(line)}[/dim]")
    if len(lines) > 30:
        console.print(f"  [dim]… and {len(lines) - 30} more[/dim]")
    if conflicts:
        console.print(
            f"\n[dim]{len(conflicts)} project(s) still hold fields their sources disagree "
            f"about. Both values keep their own citation; `tracker logic check` lists "
            f"them.[/dim]"
        )
    if dry_run:
        console.print("\n[yellow]dry run[/yellow] [dim]— nothing written.[/dim]")
    elif not (report.changed or report.blocks_touched):
        console.print(
            "\n[green]nothing moved[/green] [dim]— every row already matches what its "
            "citations imply.[/dim]"
        )


@app.command("backfill")
def backfill(
    what: Annotated[str, typer.Argument(help="`blocks`, `dates`, `derive` or `scope`.")] = "blocks",
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
    apply: Annotated[
        bool,
        typer.Option(
            "--apply", help="`dates` only: write. Without it, reports and writes nothing."
        ),
    ] = False,
    all_urls: Annotated[
        bool,
        typer.Option(
            "--all",
            help="`dates` only: include URLs no citation and no queue entry uses.",
        ),
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
    """Bring stored rows up to date. Three jobs, one family.

    * `derive` — re-derive every project from the citations it already holds. No
      LLM, no network, no migration. **Run this after any change to how something
      is derived**, because a value is a function of its sources and the function
      is only applied when something writes to the row.
    * `dates` — fill in when each article was actually published, so a merge tie
      is broken by publication order rather than by crawl order.
    * `blocks` — re-read stored articles to fill in capacity blocks. The default,
      and the only one that spends anything.

    **`blocks`, in detail.** Migration 0009 created the block table and wrote no
    rows, because turning an article into blocks needs the article text rather
    than the schema; every project ingested before it has no blocks until this
    runs.

    Deliberately not `ingest crawl --force`: that re-extracts every field with a
    model that behaves differently than it did at ingest time, churning rows and
    timestamps inside what is meant to be a backfill. This writes one column and
    lets the ordinary rollup do the rest.

    Costs one LLM call per article. Reads from the article cache, so most cost
    nothing to fetch; `--refetch` covers the rest. Resumable — an article whose
    blocks are already stored is skipped — so a sensible way to run it is in
    tranches: `--limit 25`, look at what came back, then more.
    """
    _use_llm(llm_provider)
    from tracker import backfill as backfill_mod
    from tracker.llm import LLMUnavailable, default_extractor

    if what == "derive":
        # Dispatched first, and before any extractor is constructed: this half
        # costs nothing at all, and asking for an API key to run it would be a lie
        # about what it does.
        for flag, name in (
            (refetch, "--refetch"),
            (force, "--force"),
            (apply, "--apply"),
            (all_urls, "--all"),
        ):
            if flag:
                _fail(f"{name} applies to `backfill blocks` or `dates`, not to `derive`.")
        _backfill_derive(project_id=project_id, dry_run=dry_run)
        return
    if what == "dates":
        # Dispatched before anything blocks-specific runs: this half costs no LLM
        # calls, needs no API key, and writes one column.
        for flag, name in ((project_id is not None, "--project"), (force, "--force")):
            if flag:
                _fail(f"{name} applies to `backfill blocks`, not to `dates`.")
        _backfill_dates(limit=limit, refetch=refetch, apply=apply, yes=yes, everything=all_urls)
        return
    if what == "scope":
        # Free, like `derive`, and for the same reason: `axis_gate` is a pure
        # function of the entry, the stored quote and the record's labels, and all
        # three are already in the database. No article is re-read and no model is
        # called, so no extractor is constructed above this point.
        for flag, name in ((refetch, "--refetch"), (force, "--force"), (all_urls, "--all")):
            if flag:
                _fail(f"{name} applies to `backfill blocks` or `dates`, not to `scope`.")
        _backfill_scope(apply=apply, dry_run=dry_run)
        return
    if what != "blocks":
        _fail(
            f"nothing to backfill called {what!r}. Expected `blocks`, `dates`, `derive` or `scope`."
        )

    settings = get_settings()
    cache_dir = article_cache("articles")
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
    except LLMUnavailable as exc:
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


#: Where `clean --snapshot` accumulates. A file rather than a table: the numbers
#: are a pure function of the rows, so storing them per project would drift the
#: moment a source changed — but a *time series* is the one thing a column cannot
#: be, and "did the campaign work" is only answerable by comparing two runs.
CLEAN_LOG = "clean.jsonl"


def _clean_log_path() -> Path:
    return _db_path().parent / "runs" / CLEAN_LOG


def _clean_census(session) -> dict:
    """The evidence census, in the shape both the payload and a snapshot carry.

    This is the campaign's headline number, so it goes in every `clean` payload and
    not only into a snapshot. `quote_backed` versus `flagged_unconfirmed` is the
    split that matters: the second is the gate *working* — a value it refused to
    confirm and said so — and lumping the two together reported hundreds of
    successes as failures.
    """
    from tracker import quality

    census = quality.evidence_census(session)
    return {
        "total": census.total,
        "buckets": census.buckets,
        "defects": census.defects,
        "defects_by_vintage": census.defects_by_vintage,
        "quote_backed_share": round(census.share(quality.QUOTE_BACKED), 4),
    }


def _write_clean_snapshot(session, sweep, census: dict) -> Path:
    """Append one line: the tier histogram, the census, and what produced them.

    The provenance fields are not decoration. A snapshot whose numbers moved is
    only interpretable if you know whether the *prompt* moved too, so the stamp and
    the schema version travel with the counts.
    """
    from tracker.models import utcnow
    from tracker.prompts import load_prompt

    line = {
        "at": utcnow().isoformat(timespec="seconds"),
        "prompt": load_prompt("extract-v1").stamp,
        "schema_version": schema_version(session.get_bind()),
        **sweep.as_json(),
        "census": census,
    }
    path = _clean_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def _read_clean_snapshots() -> list[dict]:
    path = _clean_log_path()
    if not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                # A half-written line from a killed run. Say so and keep the rest:
                # the file is append-only history, so one bad line must not make
                # the whole series unreadable.
                err.print(f"[yellow]skipping an unreadable line in {path}[/yellow]")
    return out


def _diff_clean_snapshots(back: int) -> dict:
    """This run against the one `back` snapshots ago. Empty when there is no pair."""
    history = _read_clean_snapshots()
    if len(history) <= back:
        return {"available": len(history), "note": f"need {back + 1} snapshots to diff"}
    now, before = history[-1], history[-1 - back]
    moved = {}
    for key in sorted(set(now.get("failures", {})) | set(before.get("failures", {}))):
        was = before.get("failures", {}).get(key, 0)
        is_now = now.get("failures", {}).get(key, 0)
        if was != is_now:
            moved[key] = {"was": was, "now": is_now, "delta": is_now - was}
    return {
        "from": before.get("at"),
        "to": now.get("at"),
        "prompt_changed": before.get("prompt") != now.get("prompt"),
        "at_or_above": {
            tier: {
                "was": before.get("at_or_above", {}).get(tier, 0),
                "now": now.get("at_or_above", {}).get(tier, 0),
            }
            for tier in sorted(now.get("at_or_above", {}))
        },
        "failures": moved,
    }


def _render_clean_card(got) -> None:
    colour = {3: "green", 2: "green", 1: "yellow", 0: "yellow"}.get(got.tier, "red")
    console.print(
        f"\n[bold]#{got.project_id} {got.name}[/bold] — "
        f"[{colour}]T{got.tier} {got.label}[/{colour}]"
    )
    table = Table(header_style="bold", box=TABLE_BOX, title_justify="left")
    table.add_column("")
    table.add_column("condition")
    table.add_column("why / what is left", style="dim")
    for level, label, keys in clean_mod_tiers():
        for key in keys:
            cond = got.by_key.get(key)
            if cond is None:
                continue
            mark = "[green]ok[/green]" if cond.ok else "[red]no[/red]"
            table.add_row(mark, f"T{level} {key}", cond.detail or f"[dim]{label}[/dim]")
    console.print(table)

    if got.blocking:
        console.print(f"\n[bold]to reach T{got.tier + 1}[/bold]")
        for cond in got.blocking:
            remedy = cond.remedy(got.project_id)
            console.print(f"  {cond.key:20} [cyan]{remedy}[/cyan]" if remedy else f"  {cond.key}")
    else:
        console.print("\n[green]nothing outstanding[/green] — this row is the reference shape")


def clean_mod_tiers():
    from tracker.clean import TIERS

    return TIERS


def _render_clean_sweep(sweep, *, total: int, census: dict | None = None) -> None:
    table = Table(header_style="bold", box=TABLE_BOX, title="rows by tier", title_justify="left")
    table.add_column("tier")
    table.add_column("rows", justify="right")
    table.add_column("at or above", justify="right")
    table.add_column("meaning", style="dim")
    unsourced = sweep.histogram.get(-1, 0)
    if unsourced:
        table.add_row("—", str(unsourced), "", "not even sourced")
    for level, label, _keys in clean_mod_tiers():
        table.add_row(
            f"T{level} {label}",
            str(sweep.histogram.get(level, 0)),
            str(sweep.at_or_above(level)),
            _TIER_MEANING.get(level, ""),
        )
    console.print(table)

    if census and census.get("total"):
        from tracker import quality

        buckets = census["buckets"]
        console.print(
            f"\n[bold]what the {census['total']} stored values rest on[/bold] — "
            f"{census['quote_backed_share']:.1%} carry a sentence"
        )
        for bucket in quality.BUCKETS:
            count = buckets.get(bucket, 0)
            if not count:
                continue
            # 待确认 is the gate working, not a defect. Only the third bucket is a
            # value presented as established with nothing behind it.
            tone = (
                "red"
                if bucket == quality.SILENT_DEFECT
                else "green"
                if bucket == quality.QUOTE_BACKED
                else "dim"
            )
            console.print(f"  [{tone}]{bucket:24}[/{tone}] {count:5}")
        if census["defects_by_vintage"]:
            console.print(f"  [dim]defects by prompt: {census['defects_by_vintage']}[/dim]")

    if sweep.failures:
        console.print("\n[bold]what is failing, most rows first[/bold]")
        for key, count in sorted(sweep.failures.items(), key=lambda kv: (-kv[1], kv[0])):
            share = f"{count / total:.0%}" if total else "—"
            console.print(f"  {key:22} {count:5}  [dim]{share} of rows[/dim]")
        console.print(
            "\n[dim]one row's detail:[/dim] tracker clean --project N"
            "   [dim]the worklist:[/dim] tracker clean --plan --tier 1"
        )


_TIER_MEANING = {
    0: "cited, and not self-contradictory",
    1: "nothing in a total is a lie",
    2: "the fields a reader acts on are present and backed",
    3: "every open question answered",
}


def _render_clean_plan(rows, *, tier: int, short: int, total: int) -> None:
    if not rows:
        console.print(f"[green]every row is at T{tier} or above[/green]")
        return
    console.print(
        f"[bold]{len(rows)} row(s)[/bold] short of T{tier}, closest first "
        f"[dim]({short} of {total} already there)[/dim]\n"
    )
    for got in rows:
        console.print(f"[bold]#{got.project_id}[/bold] {got.name} [dim]T{got.tier}[/dim]")
        for cond in got.blocking:
            remedy = cond.remedy(got.project_id)
            detail = f" [dim]— {cond.detail}[/dim]" if cond.detail else ""
            console.print(f"    [cyan]{remedy}[/cyan]{detail}" if remedy else f"    {cond.key}")


def _render_clean_diff(diff: dict) -> None:
    if "note" in diff:
        console.print(f"\n[yellow]{diff['note']}[/yellow]")
        return
    console.print(f"\n[bold]since {diff['from']}[/bold]")
    if diff.get("prompt_changed"):
        console.print("  [yellow]the prompt changed between these runs[/yellow]")
    for tier, pair in diff["at_or_above"].items():
        delta = pair["now"] - pair["was"]
        arrow = "[green]+[/green]" if delta > 0 else "[red]-[/red]" if delta < 0 else " "
        console.print(f"  at or above T{tier}: {pair['was']} -> {pair['now']} {arrow}{abs(delta)}")
    for key, moved in diff["failures"].items():
        colour = "green" if moved["delta"] < 0 else "red"
        console.print(f"  {key:22} {moved['was']} -> [{colour}]{moved['now']}[/{colour}]")


@app.command()
def clean(
    project_id: Annotated[
        int | None,
        typer.Option("--project", help="One row's card, with the command that fixes each failure."),
    ] = None,
    plan: Annotated[
        bool,
        typer.Option("--plan", help="Print the ordered worklist as runnable command lines."),
    ] = False,
    tier: Annotated[
        int, typer.Option("--tier", help="With --plan: the tier to bring rows up to.")
    ] = 1,
    limit: Annotated[int, typer.Option("--limit", help="With --plan: how many rows to list.")] = 40,
    snapshot: Annotated[
        bool,
        typer.Option("--snapshot", help="Append this run's numbers to data/runs/clean.jsonl."),
    ] = False,
    since: Annotated[
        int | None,
        typer.Option("--since", help="Diff against the snapshot N runs back (1 = the last one)."),
    ] = None,
) -> None:
    """How trustworthy each row is, on four tiers, and what would raise it.

    Composes the detectors that already exist — nothing here is a new opinion about
    the data. Read-only and free: no LLM, no network, no write lock, so it runs
    while an ingest holds the database.

        T0 SOURCED    something real cites it, and it does not contradict itself
        T1 SOUND      nothing in a total is a lie          <- the bar worth chasing
        T2 COMPLETE   the fields a reader acts on are there, and each is backed
        T3 SETTLED    every open question has been answered

    T1 first, because the numbers this tool publishes are sums: an incomplete row
    makes a total smaller, but an implausible figure or an unanswered duplicate
    makes it wrong.
    """
    from tracker import clean as clean_mod

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        if project_id is not None:
            project = session.get(Project, project_id)
            if project is None:
                _fail(f"project #{project_id} does not exist")
                return
            got = clean_mod.card(session, project)
            if json_mode():
                emit(got.as_json())
                return
            _render_clean_card(got)
            return

        total = session.scalar(select(func.count()).select_from(Project)) or 0
        if not total:
            if json_mode():
                emit({"projects": 0, "histogram": {}, "failures": {}})
                return
            console.print("[yellow]database is empty[/yellow] — run `tracker sync` first")
            return

        sweep = clean_mod.scan(session)

        if plan:
            rows = clean_mod.worklist(sweep, tier=tier, limit=limit)
            if json_mode():
                emit(
                    {
                        "tier": tier,
                        "rows": [
                            {**c.as_json(), "next": [b.key for b in c.blocking]} for c in rows
                        ],
                    }
                )
                return
            _render_clean_plan(rows, tier=tier, short=sweep.at_or_above(tier), total=total)
            return

        census = _clean_census(session)
        payload = {**sweep.as_json(), "census": census}
        if snapshot:
            payload["snapshot"] = str(_write_clean_snapshot(session, sweep, census))
        if since is not None:
            payload["since"] = _diff_clean_snapshots(since)

        if json_mode():
            emit(payload)
            return
        _render_clean_sweep(sweep, total=total, census=census)
        if snapshot:
            console.print(f"\n[dim]recorded in {payload['snapshot']}[/dim]")
        if since is not None:
            _render_clean_diff(payload["since"])


def _home_reason() -> str:
    """Which of `home()`'s four rules answered, so a surprising answer explains itself."""
    import os

    from tracker.config import _ROOT_MARKER, find_project_root, home

    if (os.environ.get("TRACKER_HOME") or "").strip():
        return "TRACKER_HOME is set"
    beside = Path(__file__).resolve().parents[2]
    if (beside / _ROOT_MARKER).is_file() and beside == home():
        return "the checkout this package is installed from (editable install)"
    if find_project_root() == home():
        return "the nearest checkout above the current directory"
    return "this platform's user-data directory (no checkout involved)"


@app.command()
def paths() -> None:
    """Where this installation keeps its code and its data.

    The first command to run when `tracker` behaves as though it belongs to a
    different database than the one you meant. Two anchors decide everything: the
    **package**, holding the code and the files that ship with it, and **home**,
    holding the database, the caches and this installation's own `.env`. They used to
    be one function returning one directory, which is why an installed CLI put its
    database wherever it happened to be standing and could not find its own
    migrations at all.
    """
    from tracker.config import cache_dir, home, package_root, seed_path

    root = package_root()
    migrations = root / "migrations"
    table = Table(box=TABLE_BOX, show_header=False)
    table.add_column("", style="bold")
    table.add_column("")
    for name, value in (
        ("package", str(root)),
        ("migrations", f"{migrations}  ({len(list(migrations.glob('*.sql')))} files)"),
        ("home", str(home())),
        ("home decided by", _home_reason()),
        ("database", str(_db_path())),
        ("article cache", str(cache_dir("articles"))),
    ):
        table.add_row(name, value)
    console.print(table)

    seeds = Table(box=TABLE_BOX, title="seed files")
    seeds.add_column("file")
    seeds.add_column("source")
    seeds.add_column("path")
    for name in sorted(q.name for q in (root / "seed").glob("*")):
        path = seed_path(name)
        packaged = path.parent == root / "seed"
        seeds.add_row(name, "packaged" if packaged else "your override", str(path))
    console.print(seeds)
    console.print(
        "\n[dim]TRACKER_HOME moves everything but the package. A seed file copied to "
        "<home>/seed/ overrides the packaged default.[/dim]"
    )
