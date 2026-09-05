"""Reading the dataset: one row, many rows, and what is missing from them.

Every command here is a report — one query, one table, nothing to sequence — and
none of them writes, with the single exception of `infer`, which stores a labelled
opinion beside the facts and never a tracked value.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from sqlalchemy import func, select

from tracker.cli._render import (
    _confidence_cell,
    _filtered,
    _fmt_date,
    _h200_cell,
    _open_risk_count,
    _ordered_risks,
    _print_blocks,
    _print_itemisation,
    _print_standing,
    _qualified,
    _severity_style,
)
from tracker.cli._shared import (
    NA,
    TABLE_BOX,
    _db_path,
    _fail,
    _fmt_mw,
    _fmt_usd,
    _location,
    _read_engine,
    _use_llm,
    app,
    console,
    emit,
    err,
    json_mode,
)
from tracker.cli.quality import _clean_census
from tracker.config import get_settings
from tracker.db import init_db, session_scope
from tracker.gaps import DEFAULTED, DERIVED, INFERRED, UNCONFIRMED, basis
from tracker.gaps import measure as measure_gaps
from tracker.gaps import worst as worst_gaps
from tracker.models import Project, Risk, Source
from tracker.vocab import OPEN_RISK_STATUS, PHASES, RISK_CATEGORIES, RISK_SEVERITIES


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

        # Why *this* obstacle out of the open ones. Twenty-seven of them on
        # Hyperion, one sentence on the row, and until now nothing said the other
        # twenty-six had been considered at all.
        from tracker.upsert import blocker_rationale

        rationale = blocker_rationale(project)
        if rationale:
            tone = "yellow" if rationale["arbitrary"] else "dim"
            console.print(
                f"[dim]why this blocker:[/dim] [{tone}]{escape(rationale['why'])}[/{tone}]"
                f" [dim](risk #{rationale['risk_id']}, {rationale['category']})[/dim]"
            )

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
                    "evidence": _clean_census(session),
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

        # What those figures rest on. Printed here because `stats` is the command
        # in the `report` workflow, and a headline capacity means something
        # different when a third of the values behind it carry no sentence. The
        # census is the campaign's primary metric and had no CLI surface at all —
        # it was reachable only from `scripts/measure_extraction.py`.
        from tracker import quality as quality_mod

        census = quality_mod.evidence_census(session)
        if census.total:
            backed = census.share(quality_mod.QUOTE_BACKED)
            tone = "green" if backed >= 0.75 else "yellow" if backed >= 0.5 else "red"
            console.print(
                f"evidence: [{tone}]{backed:.0%}[/{tone}] of {census.total} stored values "
                f"carry a quote"
                + (f", [red]{census.defects} with none[/red]" if census.defects else "")
            )
            console.print("[dim]per-row detail and what would fix it: tracker clean[/dim]")

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


@app.command()
def infer(
    project_ids: Annotated[list[int], typer.Argument(help="Project ids to analyse.")],
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override the reasoning model.", show_default=False),
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
    _use_llm(llm_provider)
    from tracker.infer import analyse
    from tracker.llm import LLMUnavailable, reasoning_extractor

    settings = get_settings()
    try:
        extractor = reasoning_extractor(settings)
        if model:
            # The override names a model on whichever provider is answering, and
            # deliberately keeps this tier's behaviour otherwise: no thinking is
            # what the bare constructor meant, so no thinking is what it stays.
            from tracker.llm import build_extractor

            extractor = build_extractor(settings, model=model)
    except LLMUnavailable as exc:
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
def coverage(
    kind: Annotated[
        str, typer.Option("--kind", help="One class only: hyperscaler, ai_lab, neocloud, landlord.")
    ] = "",
    status: Annotated[str, typer.Option("--status", help="One of absent, thin, covered.")] = "",
    covered: Annotated[
        bool, typer.Option("--covered", help="List the operators we do have, as well as the gaps.")
    ] = False,
    roster: Annotated[
        Path | None,
        typer.Option("--roster", help="A different operator roster.", show_default=False),
    ] = None,
) -> None:
    """Who we are supposed to know about, against who we actually have.

    `tracker gaps` measures the fields missing from the projects we hold. This
    measures the operators missing from the database entirely, which no amount of
    per-project completeness can reveal — a campus nobody wrote about last month
    looks exactly like a campus that does not exist.

    The roster is `seed/operators.toml`, hand-written and checked in. Matching folds the spellings one operator files under: "Nebius"
    finds a row stored as "Nebius Group N.V.", "Aligned" finds "Aligned
    DataCenters", and the aliases in the file handle the renames no string rule
    catches (RagingWire became NTT). Every match prints the spelling it matched,
    and one made by the loose rule alone is marked `~`, so a wrong fold is visible
    rather than silent.

    Three answers per operator:

    \b
      absent    no rows at all — the gap `tracker prospect` exists to close
      thin      one row, or rows with no capacity figure among them
      covered   two or more rows, at least one of them sized

    It also prints the reverse: companies with projects that no roster entry
    claims. Those are how the roster grows — each is either an operator to add or
    a spelling to alias.

    A read. It spends nothing and runs anywhere.
    """
    from tracker import roster as roster_mod

    if kind and kind not in roster_mod.KINDS:
        _fail(f"--kind must be one of {', '.join(roster_mod.KINDS)}")
        return
    statuses = ("absent", "thin", "covered")
    if status and status not in statuses:
        _fail(f"--status must be one of {', '.join(statuses)}")
        return

    try:
        operators = roster_mod.load(roster)
    except roster_mod.RosterError as exc:
        _fail(str(exc))
        return

    engine = _read_engine()
    with session_scope(engine, commit=False) as session:
        # Always measured against the WHOLE roster, whatever --kind says. The
        # unrostered tail is "companies no entry claims", and computing it from a
        # filtered roster would report every landlord we hold as unrostered the
        # moment somebody asked to see the neoclouds.
        report = roster_mod.measure(session, operators)

    shown = [row for row in report.rows if not kind or row.kind == kind]
    if kind and not shown:
        _fail(f"no rostered operator has kind {kind!r}")
        return
    counts = {
        name: sum(1 for row in shown if row.status == name)
        for name in ("absent", "thin", "covered")
    }

    if json_mode():
        emit(
            {
                "rostered": len(shown),
                "projects": report.projects_total,
                "rostered_projects": report.rostered_projects,
                "operators": [
                    {
                        "name": row.name,
                        "kind": row.kind,
                        "status": row.status,
                        "projects": row.projects,
                        "with_capacity": row.with_capacity,
                        "mw_planned": round(row.mw_planned, 1),
                        "states": list(row.states),
                        "matched": [
                            {"company": name, "projects": count, "how": how}
                            for name, count, how in row.matched
                        ],
                    }
                    for row in shown
                    if not status or row.status == status
                ],
                "unrostered": [
                    {"company": name, "projects": count, "mw_planned": round(mw, 1)}
                    for name, count, mw in report.unrostered
                ],
            }
        )
        return

    console.print(
        f"[bold]{len(shown)}[/bold] rostered operator(s)  "
        f"[red]{counts['absent']}[/red] with no row at all  "
        f"[yellow]{counts['thin']}[/yellow] thin  "
        f"[green]{counts['covered']}[/green] covered"
    )

    wanted = [row for row in shown if not status or row.status == status]
    if not status and not covered:
        wanted = [row for row in wanted if row.status != "covered"]
    if wanted:
        table = Table(header_style="bold", box=TABLE_BOX, title_justify="left")
        table.add_column("operator")
        table.add_column("kind")
        table.add_column("rows", justify="right")
        table.add_column("MW", justify="right")
        table.add_column("states", justify="right")
        # One line per operator, cropped rather than wrapped: this table is read as
        # a checklist of names, and a three-line cell for the roster's note on why
        # an operator is listed buries the twenty names underneath it.
        table.add_column("stored as / why listed", style="dim", no_wrap=True, max_width=44)
        for row in wanted:
            style = {"absent": "red", "thin": "yellow", "covered": "green"}[row.status]
            spellings = ", ".join(
                f"{name}{' ~' if how == 'loose' else ''}" for name, _, how in row.matched[:3]
            )
            if len(row.matched) > 3:
                spellings += f", +{len(row.matched) - 3}"
            table.add_row(
                f"[{style}]{escape(row.name)}[/{style}]",
                row.kind,
                str(row.projects) if row.projects else "-",
                _fmt_mw(row.mw_planned or None),
                str(len(row.states)) if row.states else "-",
                escape(spellings or row.operator.note),
            )
        console.print(table)

    if report.unrostered and not status:
        total = sum(count for _, count, _ in report.unrostered)
        console.print(
            f"\n[bold]{len(report.unrostered)} spelling(s)[/bold] hold {total} project(s) "
            f"that no roster entry claims"
        )
        for name, count, mw in report.unrostered[:15]:
            console.print(f"  {count:>3}  {escape(name[:44]):<44} {_fmt_mw(mw or None)}")
        if len(report.unrostered) > 15:
            console.print(f"  [dim]… and {len(report.unrostered) - 15} more[/dim]")
        console.print(
            "[dim]each is an operator to add to seed/operators.toml, or a spelling to "
            "alias onto one already there[/dim]"
        )

    absent = [row for row in shown if row.status == "absent"]
    if absent:
        names = ", ".join(row.name for row in absent[:4])
        console.print(f"\n[dim]next:[/dim] tracker prospect  [dim]— chases {names}…[/dim]")


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
