"""Capacity and money: `capex`, `blocks`, `exposure`.

The three views that add numbers up rather than listing them, which is why the
duplicate report matters to them: `capex.rollup` holds one row of every suspected
group out of the buyer table, so a false pair takes a real campus's capacity out of
a published figure. See `docs/duplicate-shapes.md`.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from tracker.cli._shared import (
    NA,
    TABLE_BOX,
    _db_path,
    _fail,
    _fmt_mw,
    _fmt_usd,
    _read_engine,
    app,
    console,
    emit,
    json_mode,
)
from tracker.db import init_db, session_scope
from tracker.models import Project, Risk
from tracker.vocab import OPEN_RISK_STATUS, RISK_SEVERITIES, severity_rank

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
