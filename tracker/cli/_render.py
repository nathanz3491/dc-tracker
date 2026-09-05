"""Row and obstacle rendering, shared by every family that prints a project.

`show`, `discover`, `review`, `risks` and `infer` all print the same things — the
track standing, the tranche table, an obstacle with the reason its quote was
refused — and they lived beside whichever command happened to be written first.
They are here so a family module can print a row without importing another
family. Nothing here is a command and nothing here writes.
"""

from __future__ import annotations

from rich.markup import escape
from rich.table import Table
from sqlalchemy import func, select

from tracker.cli._shared import (
    NA,
    TABLE_BOX,
    _fmt_mw,
    _location,
    console,
)
from tracker.models import Project, Risk
from tracker.vocab import OPEN_RISK_STATUS, RISK_SEVERITIES, severity_rank

# --- queries ----------------------------------------------------------------


def _filtered(stmt, company, state, phase, min_confidence, risk=None, severity=None):
    if company:
        stmt = stmt.where(func.lower(Project.company).like(f"%{company.lower()}%"))
    if state:
        stmt = stmt.where(Project.state == state.upper())
    if phase:
        stmt = stmt.where(Project.phase == phase)
    if min_confidence is not None:
        stmt = stmt.where(Project.confidence >= min_confidence)
    if risk or severity:
        # EXISTS rather than a join, so a project with three matching risks is one
        # row rather than three. Restricted to open risks: filtering on an obstacle
        # that has been resolved would answer a question nobody asked.
        sub = select(Risk.id).where(Risk.project_id == Project.id, Risk.status == OPEN_RISK_STATUS)
        if risk:
            sub = sub.where(Risk.category == risk)
        if severity:
            sub = sub.where(Risk.severity == severity)
        stmt = stmt.where(sub.exists())
    return stmt


def _confidence_cell(value: int) -> str:
    colour = {0: "red", 1: "yellow", 2: "green", 3: "bold green"}.get(value, "white")
    return f"[{colour}]{value}[/{colour}]"


def _severity_style(severity: str) -> str:
    return {"watch": "yellow", "material": "bright_red", "blocking": "bold red"}.get(
        severity, "white"
    )


#: What an uncited obstacle needs, in the words of the work it implies. Naming
#: the reason is the whole point of storing one: "nobody quoted it" sends you
#: looking for another source, while "the sentence does not say that" sends you
#: to correct the category on a source you already have.
_UNCITED_BECAUSE = {
    "no_quote": "uncited — the source named it but quoted nothing; confirm in `tracker review`",
    "quote_unverified": (
        "uncited — the quote offered for it is not in the article; confirm in `tracker review`"
    ),
    "quote_off_target": (
        "uncited — the quoted sentence does not state this category; "
        "recategorise in `tracker review`"
    ),
}


def _why_uncited(risk_row) -> str:
    """The line under an obstacle with no quote behind it."""
    return _UNCITED_BECAUSE.get(risk_row.unconfirmed or "", "uncited — confirm in `tracker review`")


def _open_risk_count(project: Project) -> int:
    return sum(1 for r in project.risks if r.status == OPEN_RISK_STATUS)


def _ordered_risks(risks) -> list:
    """Most severe first, then open before settled, then by category.

    Stable and content-based rather than by id, so the same data renders the same
    way whatever order the rows happened to be written in.
    """
    return sorted(
        risks,
        key=lambda r: (
            -severity_rank(r.severity),
            r.status != OPEN_RISK_STATUS,
            r.category,
            str(r.first_seen or ""),
        ),
    )


def _print_standing(project) -> None:
    """Per-track position, the binding blocker, and what to watch for.

    This is the PRD's central ask — 判断一个项目究竟走到了哪一步 — which a single
    `phase` enum cannot answer. Nothing here is stored: it is derived from the
    project's own dated, cited events and open risks, so it cannot disagree with
    the evidence.
    """
    from tracker.tracks import TRACK_LABELS, standing

    stand = standing(project.id, project.events, project.risks)
    table = Table(title="where it stands", header_style="bold", box=TABLE_BOX, title_justify="left")
    table.add_column("track")
    table.add_column("reached")
    table.add_column("blocked by")
    for state in stand.tracks:
        if state.complete:
            reached = "[green]complete[/green]"
        elif state.reached:
            reached = f"[yellow]{state.status}[/yellow]"
        else:
            reached = f"[dim]{state.status}[/dim]"
        if state.only_implied:
            # Deduced from a later milestone, not read anywhere. A built site must
            # hold its land and permits; saying so beats printing "unknown", but a
            # deduction is not a citation and must not look like one.
            reached += " [dim](implied)[/dim]"
        blocked = ", ".join(state.blockers)
        if blocked and state.blocker_severity:
            blocked += f" [dim]({state.blocker_severity})[/dim]"
        table.add_row(TRACK_LABELS[state.track], reached, blocked or NA)
    console.print()
    console.print(table)

    binding = stand.binding_blocker
    if binding:
        console.print(
            f"binding constraint: [red]{TRACK_LABELS[binding.track]}[/red] — "
            f"{', '.join(binding.blockers)}"
        )
    if stand.watch_for:
        # The PRD's final question, in `tracker.tracks`: what signal proves the
        # project is still advancing.
        console.print(f"[bold]watch for:[/bold] {stand.watch_for}")


#: Block statuses coloured by how far along they are, so a mixed campus reads at a
#: glance. `serving` and `energized` are the two that mean megawatts are delivering.
_BLOCK_STYLE: dict[str, str] = {
    "serving": "green",
    "energized": "green",
    "shell_complete": "yellow",
    "under_construction": "yellow",
    "permitting": "cyan",
    "planned": "dim",
    "paused": "red",
    "cancelled": "red",
}


def _print_blocks(project: Project) -> None:
    """The campus broken into the tranches that have their own state.

    The reason this table exists: the rows above it can only say one phase, one
    capacity, one customer. This is where a campus gets to say it is 100 MW serving
    one buyer beside 150 MW still going up for another — which is what most of these
    projects actually are.

    A capacity no quote confirmed is marked 待确认 *and* stated to be outside the
    campus total, because those are two different facts and a reader who sees only
    the first will assume the number is in `MW planned`.

    **The utility's plant is listed separately, under the campus.** Every sum here
    already excludes generation — a plant's nameplate output and a data center's IT
    load are different quantities — so listing an Entergy gas unit among the halls
    put six of Hyperion's eighteen rows in the wrong table while the arithmetic
    beneath them said otherwise. Split, not dropped: gas being built *for* this
    campus is one of the most important facts about it.
    """
    all_blocks = list(getattr(project, "blocks", ()) or ())
    if not all_blocks:
        return

    from tracker import blocks as blocks_mod

    serving = [b for b in all_blocks if blocks_mod.is_generation(b.label, b.parent)]
    blocks = [b for b in all_blocks if b not in serving]

    got = blocks_mod.rollup(all_blocks)
    table = Table(
        title=f"capacity blocks ({len(blocks)})",
        header_style="bold",
        box=TABLE_BOX,
        title_justify="left",
    )
    table.add_column("block")
    table.add_column("MW", justify="right")
    table.add_column("status")
    table.add_column("customer")
    table.add_column("online")

    for block in sorted(blocks, key=lambda b: b.block_key):
        style = _BLOCK_STYLE.get(block.status, "")
        status = f"[{style}]{block.status}[/{style}]" if style else block.status
        # The hedge the block's own quote put on this figure. A block carries no
        # `claim_meta`, so there is no stored axis to read — but it does store the
        # verbatim sentence, and "Each exceeds 350 MW" is a floor whether or not
        # anything recorded that at ingest time.
        from tracker.export import _mw_bound

        mw = with_bound(_fmt_mw(block.mw), _mw_bound(block))
        if block.mw is not None and not blocks_mod.mw_is_confirmed(block):
            mw = f"[red]{mw} 待确认[/red]"
        when = block.energized_on or block.expected_online
        label = escape(block.label)
        if block.parent:
            label = f"{escape(block.parent)} / {label}"
        table.add_row(label, mw, status, escape(block.customer or NA), str(when or NA))

    # The residual lines, then the total. Without these the tranches visibly fail to
    # add up to `MW planned` — measured on 70 of 118 itemised projects — and a reader
    # who cannot make the arithmetic work stops trusting the rest of the row too.
    account = blocks_mod.account(project)
    if account.residuals:
        table.add_section()
        for residual in account.residuals:
            table.add_row(
                f"[yellow]{residual.reason}[/yellow]",
                f"[yellow]{_fmt_mw(residual.mw)}[/yellow]",
                f"[dim]{escape(residual.note)}[/dim]",
                "",
                "",
            )
    if account.total is not None:
        table.add_section()
        caveat = (
            " [dim](floor — no source states a campus total)[/dim]"
            if (account.total_is_floor)
            else ""
        )
        table.add_row(
            "[bold]accounted for[/bold]",
            f"[bold]{_fmt_mw(account.total)}[/bold]",
            caveat,
            "",
            "",
        )

    console.print()
    console.print(table)

    if serving:
        power = Table(
            title=f"power serving this campus ({len(serving)})",
            header_style="bold",
            box=TABLE_BOX,
            title_justify="left",
            caption="the utility's, measured as generating output — never added to the campus above",
            caption_justify="left",
        )
        power.add_column("asset")
        power.add_column("MW", justify="right")
        power.add_column("status")
        for block in sorted(serving, key=lambda b: b.block_key):
            label = escape(block.label)
            if block.parent:
                label = f"{escape(block.parent)} / {label}"
            power.add_row(label, _fmt_mw(block.mw), block.status)
        console.print()
        console.print(power)

    for note in blocks_mod.reconcile_notes(got):
        console.print(f"  [dim]{escape(note)}[/dim]")


def _print_itemisation(project: Project) -> None:
    """Why a campus shows no tranches. Printed only when it shows none.

    A bare row and an unread row looked identical, so 88 bare rows beside 118
    detailed ones read as uneven research. Most of those campuses really are one
    undivided thing, and saying so is the difference between a gap and an answer.
    """
    if getattr(project, "blocks", None):
        return

    from tracker import blocks as blocks_mod

    state = blocks_mod.itemisation(project)
    console.print(
        f"\n[bold]capacity blocks[/bold] [dim]— {blocks_mod.ITEMISATION_NOTES[state]}[/dim]"
    )


#: How a hedged quantity reads. The article either qualified the number or it did
#: not, and until migration 0015 there was nowhere to record which — prompt RULE 4
#: said `"500-700 MW" -> 500 (the LOWER bound; say so in "notes")`, so the hedge
#: went into prose nothing could read back.
#:
#: One glyph rather than a column. These are qualifiers on a number, not facts of
#: their own, and a `bound` column would be empty on most rows and would push the
#: figures out of alignment on the rest.
#: `at_least` is a SUFFIX — "350+" rather than "≥350" — because that is how a
#: reader outside this codebase writes "or more", and the floor is the case that
#: matters most: Fairwater's 350 MW rests on "Each exceeds 350 MW". The console
#: uses the same two tables, so the two surfaces cannot drift apart.
_BOUND_PREFIX: dict[str, str] = {"approximate": "~", "at_most": "≤"}
_BOUND_SUFFIX: dict[str, str] = {"at_least": "+"}


def with_bound(rendered: str, bound: str | None) -> str:
    """A rendered quantity carrying the hedge its own source used."""
    if not rendered or rendered == NA or not bound or bound == "exact":
        return rendered
    return f"{_BOUND_PREFIX.get(bound, '')}{rendered}{_BOUND_SUFFIX.get(bound, '')}"


#: A date stated to a year rendered as `2024-01-01` asserts a precision the
#: article never gave. `normalize.parse_date` has always known the difference.
_DATE_FORMAT: dict[str, str] = {"year": "%Y", "half": "%Y", "quarter": "%Y", "month": "%Y-%m"}

#: Suffix naming the bucket, where dropping the digits alone would lose it.
_DATE_SUFFIX: dict[str, str] = {"half": " (H1/H2)", "quarter": " (quarter)"}


def _qualified(project, field: str, rendered: str) -> str:
    """A quantity carrying the hedge its own source used, if any.

    The stored axis wins, and where it says `exact` the quote is read directly.
    That fallback is doing most of the work today for two reasons: the `bound`
    axis reached only 32% of claims, and `exceeds` — the commonest hedge in this
    corpus, and the one under Fairwater's own `mw_built` — was missing from the
    marker list until it moved into `vocab`. Rows extracted before that say
    `exact` and will keep saying it until they are re-read, so a display that
    trusted the axis alone would report "Each exceeds 350 MW" as a point value.
    """
    from tracker.gaps import provenance
    from tracker.vocab import bound_from_quote

    prov = provenance(project, field)
    if prov is None:
        return rendered
    bound = prov.bound
    if bound == "exact" and prov.quote:
        bound = bound_from_quote(prov.quote, getattr(project, field, None))
    return with_bound(rendered, bound)


def _fmt_date(project, field: str) -> str:
    """A date at the precision the source actually offered.

    "Q3 2025" and "2025-07-01" are stored identically and mean very different
    things; the row used to print the second whatever the article said. This is
    the rare display change that makes the output *shorter* and more honest at
    once — a year-precision date renders as `2024`, four characters instead of
    ten, and stops claiming a day nobody published.
    """
    value = getattr(project, field, None)
    if value is None:
        return NA
    precision = getattr(project, f"{field}_precision", None)
    fmt = _DATE_FORMAT.get(precision or "")
    if not fmt:
        return str(value)
    return f"{value.strftime(fmt)}{_DATE_SUFFIX.get(precision, '')}"


def _dedupe_risks(rows: list) -> list:
    """One obstacle per (project, category, sentence).

    The unique constraint on `risk` includes `first_seen`, so two crawls of two
    articles reporting the same concern on the same day-but-one store it twice.
    Measured on the live database that was 20 rows, and each appeared as its own
    line here and as its own question in `tracker logic resolve`.
    """
    seen: set[tuple[int, str, str]] = set()
    out = []
    for risk_row, project in rows:
        key = (project.id, risk_row.category, " ".join((risk_row.summary or "").lower().split()))
        if key in seen:
            continue
        seen.add(key)
        out.append((risk_row, project))
    return out


def _print_risk_kinds(by_category: dict[str, list], order: list[str]) -> None:
    """The table that answers "what is stopping these projects", in one screen."""
    table = Table(
        title="obstacles by kind",
        header_style="bold",
        box=TABLE_BOX,
        title_justify="left",
    )
    table.add_column("kind")
    table.add_column("projects", justify="right")
    table.add_column("capacity", justify="right")
    table.add_column("blocking", justify="right")
    table.add_column("material", justify="right")
    table.add_column("watch", justify="right")
    table.add_column("quoted", justify="right")
    for cat in order:
        entries = by_category[cat]
        mw = sum(p.mw_planned or 0.0 for _, p in entries)
        counts = {s: sum(1 for r, _ in entries if r.severity == s) for s in RISK_SEVERITIES}
        quoted = sum(1 for r, _ in entries if not r.unconfirmed)
        quoted_cell = f"{quoted}/{len(entries)}"
        table.add_row(
            f"[cyan]{cat}[/cyan]",
            str(len(entries)),
            _fmt_mw(mw) if mw else "—",
            f"[bold red]{counts['blocking']}[/bold red]" if counts["blocking"] else "—",
            f"[bright_red]{counts['material']}[/bright_red]" if counts["material"] else "—",
            f"[yellow]{counts['watch']}[/yellow]" if counts["watch"] else "—",
            quoted_cell if quoted == len(entries) else f"[yellow]{quoted_cell}[/yellow]",
        )
    console.print(table)
    console.print()


def _print_risk_detail(
    by_category: dict[str, list], order: list[str], *, limit: int | None
) -> None:
    """Each obstacle under its kind, with its evidence directly beneath it."""
    shown = 0
    for cat in order:
        entries = sorted(
            by_category[cat],
            key=lambda pair: (
                -severity_rank(pair[0].severity),
                -(pair[1].mw_planned or 0.0),
                pair[1].id,
            ),
        )
        mw = sum(p.mw_planned or 0.0 for _, p in entries)
        unknown_mw = sum(1 for _, p in entries if p.mw_planned is None)
        heading = f"[bold cyan]{cat}[/bold cyan]  {len(entries)} project(s)"
        if mw:
            heading += f", {_fmt_mw(mw)} MW"
        if unknown_mw:
            heading += f" [dim](+{unknown_mw} with no cited capacity)[/dim]"
        console.print(heading)

        for risk_row, project in entries:
            if limit is not None and shown >= limit:
                break
            style = _severity_style(risk_row.severity)
            capacity = f"{_fmt_mw(project.mw_planned)} MW" if project.mw_planned else "no capacity"
            console.print(
                f"  [{style}]{risk_row.severity:<8}[/{style}] [bold]#{project.id}[/bold] "
                f"{escape(project.company)} — {escape(project.name)} "
                f"[dim]({escape(_location(project))} · {capacity})[/dim]"
            )
            console.print(f"      {escape(risk_row.summary)}")
            if risk_row.quote:
                console.print(f'      [dim]"{escape(risk_row.quote)}"[/dim]')
            else:
                console.print(f"      [yellow]待确认[/yellow] [dim]{_why_uncited(risk_row)}[/dim]")
            shown += 1
        console.print()
        if limit is not None and shown >= limit:
            console.print(f"[dim]stopped at --limit {limit}[/dim]")
            break


def _print_risk_footer(rows: list) -> None:
    console.print(
        "[dim]MW sums cover only projects whose capacity is cited; they are a "
        "floor, not a total.[/dim]"
    )
    # Counted, and said so. An unconfirmed obstacle is still an obstacle a
    # source reported, and leaving it out of the sums would understate exposure
    # in the one direction that matters — but a total nobody can see the
    # composition of is the thing this database exists not to produce.
    vague = sum(1 for risk_row, _ in rows if risk_row.unconfirmed)
    if not vague:
        return
    reasons: dict[str, int] = {}
    for risk_row, _ in rows:
        if risk_row.unconfirmed:
            reasons[risk_row.unconfirmed] = reasons.get(risk_row.unconfirmed, 0) + 1
    console.print(
        f"[yellow]{vague} of {len(rows)}[/yellow] [dim]are 待确认 — reported by a source with "
        "no quote that stands up, and counted above anyway.[/dim]"
    )
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        console.print(f"  [dim]{count:>4}  {escape(_UNCITED_BECAUSE.get(reason, reason))}[/dim]")
    console.print("\n[dim]settle them by reading the articles again:[/dim] tracker risks confirm")


def _h200_cell(project) -> str:
    """The accelerator count, labelled with where it came from.

    Almost always a restatement of the capacity, so it says so — printing it bare
    beside quoted figures would read as something a source reported. When a source
    *did* report a chip count it goes through the evidence gate like any value and
    is shown plainly.
    """
    from tracker.compute import h200_equivalent, kw_per_h200

    count = project.h200_equivalent
    if count is None:
        return NA
    basis = project.mw_built if project.mw_built else project.mw_planned
    if count == h200_equivalent(basis):
        return f"{count:,} [dim](derived from {_fmt_mw(basis)} at {kw_per_h200()} kW each)[/dim]"
    return f"{count:,}"
