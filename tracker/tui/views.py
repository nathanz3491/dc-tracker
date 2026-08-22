"""The read panes: what the CLI prints, kept on screen and comparable.

Each pane takes a `Snapshot` and fills itself. None of them queries the database
and none computes a judgement of its own — the numbers arrive decided, for the
reason `docs/architecture.md` gives about the console: a second implementation of
"which years the capex grid shows" is a second opinion free to disagree with the
first, and nothing tells you when it starts to.
"""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Static

from tracker.tui.data import (
    NA,
    PHASE_STYLE,
    SEVERITY_STYLE,
    STATUS_STYLE,
    Snapshot,
    bar,
    confidence_cell,
    fmt_mw,
    fmt_usd,
    value_with_tier,
)


class Pane(Vertical):
    """Common shape: a pane is filled from a snapshot and can be refilled."""

    def load(self, snapshot: Snapshot) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


# --- Overview ---------------------------------------------------------------


class OverviewPane(Pane):
    """The landing answer: how big is this, how solid, and what is in the way."""

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static(id="overview-body"))

    def load(self, snapshot: Snapshot) -> None:
        totals = snapshot.totals
        gaps = snapshot.gaps.get("fields") or []
        projects = snapshot.projects

        headline = Table.grid(padding=(0, 3))
        headline.add_column(justify="right", style="bold")
        headline.add_column()
        headline.add_row(f"{totals.get('projects', 0):,}", "projects tracked")
        headline.add_row(f"{totals.get('mw_planned', 0):,.0f}", "MW planned, where cited")
        headline.add_row(
            fmt_usd(totals.get("investment_usd")), "announced investment, where cited"
        )
        headline.add_row(f"{totals.get('citations', 0):,}", "citations behind those facts")
        headline.add_row(f"{totals.get('states', 0)}", "states")

        # Field coverage, drawn. The CLI prints these as percentages and a reader
        # compares twelve numbers by hand; the bars are the same data in a shape
        # you can scan.
        field_table = Table(
            title="fields, against the rows where the field applies",
            title_justify="left",
            title_style="bold",
            box=None,
            padding=(0, 1),
        )
        field_table.add_column("field")
        field_table.add_column("", justify="right")
        field_table.add_column("filled")
        field_table.add_column("why", style="dim")
        for gap in gaps:
            pct = gap.get("pct")
            applicable = gap.get("applicable") or 0
            filled = gap.get("filled") or 0
            if pct is None:
                shown, drawn = Text("n/a", style="dim"), Text("unmeasurable", style="dim")
            else:
                style = "green" if pct >= 90 else "yellow" if pct >= 50 else "red"
                shown = Text(f"{pct}%", style=style)
                drawn = bar(filled, applicable, style=style)
            field_table.add_row(
                gap.get("field", ""), shown, drawn, gap.get("note") or "all projects"
            )

        # Obstacles by cited capacity. A project appears under every category
        # obstructing it, so these deliberately do not sum — the caveat travels
        # with the figure, because a bar chart invites exactly that misreading.
        risk_table = Table(
            title="open obstacles, by cited capacity",
            title_justify="left",
            title_style="bold",
            box=None,
            padding=(0, 1),
        )
        risk_table.add_column("category")
        risk_table.add_column("MW", justify="right")
        risk_table.add_column("")
        risk_table.add_column("projects", justify="right")
        exposure = snapshot.exposure[:8]
        worst = max((e["mw"] for e in exposure), default=0.0)
        for entry in exposure:
            risk_table.add_row(
                entry["category"],
                fmt_mw(entry["mw"]),
                bar(entry["mw"], worst, style="red"),
                Text(str(entry["projects"])),
            )
        if not exposure:
            risk_table.add_row(Text("nothing open", style="dim"), NA, Text(""), NA)

        summary = Group(
            headline,
            Text(),
            field_table,
            Text(),
            risk_table,
            Text(),
            self._coverage_line(snapshot),
            self._queue_line(snapshot, projects),
        )
        self.query_one("#overview-body", Static).update(
            Panel(summary, title=f"dc-tracker {snapshot.version}", title_align="left")
        )

    @staticmethod
    def _coverage_line(snapshot: Snapshot) -> Text:
        report = snapshot.coverage
        if report is None:
            return Text("coverage unavailable", style="dim")
        absent = len(report.absent)
        thin = len(report.thin)
        line = Text("operators: ", style="bold")
        line.append(f"{absent} with no row at all", style="red" if absent else "green")
        line.append(f", {thin} thin", style="yellow" if thin else "dim")
        line.append(f", {len(report.covered)} covered", style="green")
        if report.unrostered:
            line.append(
                f"  ({len(report.unrostered)} spelling(s) no roster entry claims)", style="dim"
            )
        return line

    @staticmethod
    def _queue_line(snapshot: Snapshot, projects: list[dict[str, Any]]) -> Text:
        line = Text("queue: ", style="bold")
        line.append(f"{len(snapshot.queue)} candidate(s) waiting")
        failed = sum(entry["count"] for entry in snapshot.failed)
        if failed:
            line.append(f", {failed} URL(s) never read", style="yellow")
        thin = sum(1 for p in projects if (p.get("filled") or 0) < 9)
        line.append(f"  ·  {thin} project(s) below 9 of 12 fields", style="dim")
        return line


# --- Projects ---------------------------------------------------------------

#: Ordered by how much a reader needs them, because a narrow terminal truncates
#: from the right and the table scrolls horizontally rather than hiding anything.
#: `investment` sits last: the detail pane beside it always shows the figure.
PROJECT_COLUMNS = ("id", "company", "project", "location", "phase", "MW", "9/12", "conf", "investment")


class ProjectsPane(Pane):
    """The table, filtered live, with one project opened beside it.

    The filter is the reason this beats `tracker list`: it narrows on every
    keystroke across company, name, location, phase and customer at once, and the
    selection survives it — so comparing two campuses is two arrow keys rather than
    two commands and a scroll.
    """

    def compose(self) -> ComposeResult:
        yield Input(placeholder="filter — company, project, city, state, phase, customer", id="filter")
        with Horizontal():
            yield DataTable(id="projects-table", cursor_type="row", zebra_stripes=True)
            yield VerticalScroll(Static(id="project-detail"), id="detail-wrap")

    def on_mount(self) -> None:
        table = self.query_one("#projects-table", DataTable)
        table.add_columns(*PROJECT_COLUMNS)
        self._rows: list[dict[str, Any]] = []
        self._snapshot: Snapshot | None = None

    def load(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot
        self.refill()

    def refill(self) -> None:
        if self._snapshot is None:
            return
        needle = self.query_one("#filter", Input).value.strip().lower()
        rows = [p for p in self._snapshot.projects if _matches(p, needle)]
        rows.sort(key=lambda p: (-(p.get("mw_planned") or 0), p.get("company") or ""))
        self._rows = rows

        table = self.query_one("#projects-table", DataTable)
        table.clear()
        for project in rows:
            table.add_row(*_project_row(project), key=str(project["id"]))
        self.border_title = f"{len(rows)} of {len(self._snapshot.projects)} project(s)"
        if rows:
            self.show_detail(rows[0]["id"])
        else:
            self.query_one("#project-detail", Static).update(
                Text("nothing matches that filter", style="dim")
            )

    def show_detail(self, project_id: int) -> None:
        if self._snapshot is None:
            return
        project = self._snapshot.project(project_id)
        if project is None:
            return
        self.query_one("#project-detail", Static).update(_detail(project))

    @property
    def highlighted_id(self) -> int | None:
        table = self.query_one("#projects-table", DataTable)
        if not self._rows or table.cursor_row is None:
            return None
        try:
            return int(self._rows[table.cursor_row]["id"])
        except (IndexError, KeyError, TypeError):
            return None

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.refill()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            self.show_detail(int(event.row_key.value))


def _matches(project: dict[str, Any], needle: str) -> bool:
    if not needle:
        return True
    haystack = " ".join(
        str(project.get(key) or "")
        for key in ("company", "name", "city", "county", "state", "phase", "customer")
    ).lower()
    # Every word has to appear somewhere, so "meta la" finds Meta in Louisiana
    # rather than everything mentioning either.
    return all(word in haystack for word in needle.split())


def _project_row(project: dict[str, Any]) -> list[Text]:
    phase = project.get("phase") or ""
    location = ", ".join(
        part for part in (project.get("city") or project.get("county"), project.get("state")) if part
    )
    filled = project.get("filled") or 0
    return [
        Text(str(project.get("id"))),
        Text((project.get("company") or "")[:18]),
        Text((project.get("name") or "")[:26]),
        Text(location[:20]),
        Text(phase, style=PHASE_STYLE.get(phase, "")),
        fmt_mw(project.get("mw_planned")),
        Text(f"{filled}/12", style="green" if filled >= 9 else "yellow" if filled >= 6 else "red"),
        confidence_cell(project.get("confidence")),
        fmt_usd(project.get("investment_usd")),
    ]


#: The five tracks, in the order the model puts them. Site control and permits can
#: be finished while power is years out, which is the whole reason progress is not
#: one ladder — so they are drawn side by side rather than summed.
TRACK_ORDER = ("site", "permits", "power", "construction", "commercial")


def _detail(project: dict[str, Any]) -> Group:
    """One project, with what each value rests on and where it stands."""
    head = Text()
    head.append(f"#{project.get('id')}  ", style="dim")
    head.append(f"{project.get('company') or '?'} — {project.get('name') or '?'}\n", style="bold")
    location = ", ".join(
        part for part in (project.get("city") or project.get("county"), project.get("state")) if part
    )
    head.append(location or "location unknown", style="dim")

    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="dim", justify="right")
    facts.add_column()
    for label, key, renderer in (
        ("phase", "phase", lambda v: Text(str(v), style=PHASE_STYLE.get(str(v), ""))),
        ("MW planned", "mw_planned", fmt_mw),
        ("MW built", "mw_built", fmt_mw),
        ("H200 equivalent", "h200_equivalent", lambda v: Text(f"{int(v):,}")),
        ("investment", "investment_usd", fmt_usd),
        ("customer", "customer", lambda v: Text(str(v))),
        ("announced", "first_announced", lambda v: Text(str(v))),
        ("online", "expected_online", lambda v: Text(str(v))),
        ("blocker", "blocker", lambda v: Text(str(v), style="yellow")),
    ):
        value = project.get(key)
        rendered = NA if value in (None, "") else renderer(value)
        facts.add_row(label, value_with_tier(project, key, rendered))
    facts.add_row("confidence", confidence_cell(project.get("confidence")))

    tracks = Table.grid(padding=(0, 2))
    tracks.add_column(style="dim", justify="right")
    tracks.add_column()
    tracks.add_column()
    standing = (project.get("standing") or {}).get("tracks") or []
    by_track = {t.get("track"): t for t in standing}
    for name in TRACK_ORDER:
        state = by_track.get(name)
        if state is None:
            continue
        reached = len(state.get("reached") or [])
        total = max(reached, len(state.get("reached") or []) + (0 if state.get("complete") else 1))
        style = "green" if state.get("complete") else "red" if state.get("blockers") else "yellow"
        label = Text(str(state.get("status") or "?"), style=style)
        if state.get("blockers"):
            label.append(f"  ← {', '.join(state['blockers'][:2])}", style="red")
        tracks.add_row(name, bar(reached, total or 1, width=10, style=style), label)

    risks = Table.grid(padding=(0, 2))
    risks.add_column(style="dim", justify="right")
    risks.add_column()
    open_risks = [r for r in (project.get("risks") or []) if r.get("status") == "open"][:5]
    for risk in open_risks:
        risks.add_row(
            Text(str(risk.get("severity")), style=SEVERITY_STYLE.get(str(risk.get("severity")), "")),
            Text(f"{risk.get('category')}: {(risk.get('summary') or '')[:52]}"),
        )
    if not open_risks:
        risks.add_row("", Text("no open obstacles recorded", style="dim"))

    sources = Table.grid(padding=(0, 2))
    sources.add_column(style="dim", justify="right")
    sources.add_column()
    for source in (project.get("sources") or [])[:6]:
        sources.add_row(
            str(source.get("source_type") or "?"), Text(str(source.get("url") or "")[:66])
        )

    return Group(
        head,
        Text(),
        facts,
        Text("\nfive tracks, not one ladder", style="bold"),
        tracks,
        Text("\nopen obstacles", style="bold"),
        risks,
        Text(f"\n{len(project.get('sources') or [])} citation(s)", style="bold"),
        sources,
    )


# --- Coverage ---------------------------------------------------------------


class CoveragePane(Pane):
    """Rostered operators against the rows we hold. The absent ones first."""

    def compose(self) -> ComposeResult:
        yield Static(id="coverage-head")
        with Horizontal():
            yield DataTable(id="coverage-table", cursor_type="row", zebra_stripes=True)
            yield VerticalScroll(Static(id="coverage-side"), id="coverage-side-wrap")

    def on_mount(self) -> None:
        table = self.query_one("#coverage-table", DataTable)
        table.add_columns("operator", "kind", "status", "rows", "MW", "states", "stored as")
        self._rows: list[Any] = []

    def load(self, snapshot: Snapshot) -> None:
        report = snapshot.coverage
        head = self.query_one("#coverage-head", Static)
        if report is None:
            head.update(Text("coverage unavailable — see the roster file", style="yellow"))
            return

        line = Text()
        line.append(f"{len(report.rows)} rostered operator(s)  ", style="bold")
        line.append(f"{len(report.absent)} with no row at all", style="red")
        line.append(f"  {len(report.thin)} thin", style="yellow")
        line.append(f"  {len(report.covered)} covered", style="green")
        line.append(
            f"\n{report.rostered_projects} of {report.projects_total} project(s) claimed by an entry",
            style="dim",
        )
        head.update(line)

        # Absent, then thin, then covered: the gaps are the reason to open this.
        order = {"absent": 0, "thin": 1, "covered": 2}
        rows = sorted(report.rows, key=lambda r: (order[r.status], -r.projects, r.name))
        self._rows = rows
        table = self.query_one("#coverage-table", DataTable)
        table.clear()
        biggest = max((r.mw_planned for r in rows), default=0.0)
        for row in rows:
            spellings = ", ".join(
                f"{name}{' ~' if how == 'loose' else ''}" for name, _, how in row.matched[:2]
            )
            table.add_row(
                Text(row.name, style=STATUS_STYLE[row.status]),
                Text(row.kind, style="dim"),
                Text(row.status, style=STATUS_STYLE[row.status]),
                Text(str(row.projects)) if row.projects else NA,
                bar(row.mw_planned, biggest or 1, width=10, style="green")
                if row.projects
                else Text("·" * 10, style="dim"),
                Text(str(len(row.states))) if row.states else NA,
                Text(spellings or row.operator.note[:40], style="dim"),
                key=row.name,
            )
        self._side(report)

    def _side(self, report: Any) -> None:
        """The reverse gap, which is how the roster grows."""
        table = Table(
            title="companies no roster entry claims",
            title_justify="left",
            title_style="bold",
            box=None,
            padding=(0, 1),
        )
        table.add_column("rows", justify="right")
        table.add_column("company")
        table.add_column("MW", justify="right")
        for name, count, mw in report.unrostered[:20]:
            table.add_row(Text(str(count)), Text(name[:34]), fmt_mw(mw))
        if not report.unrostered:
            table.add_row(NA, Text("every company is rostered", style="dim"), NA)
        hint = Text(
            "\neach is an operator to add to seed/operators.toml,\nor a spelling to alias onto one "
            "already there",
            style="dim",
        )
        self.query_one("#coverage-side", Static).update(Group(table, hint))

    @property
    def highlighted_name(self) -> str | None:
        table = self.query_one("#coverage-table", DataTable)
        if not self._rows or table.cursor_row is None:
            return None
        try:
            return str(self._rows[table.cursor_row].name)
        except IndexError:
            return None


# --- Capex ------------------------------------------------------------------


class CapexPane(Pane):
    """Who is buying the capacity, and when it lands."""

    def compose(self) -> ComposeResult:
        yield Static(id="capex-head")
        yield DataTable(id="capex-table", cursor_type="row", zebra_stripes=True)

    def load(self, snapshot: Snapshot) -> None:
        capex = snapshot.capex
        positions = capex.get("positions") or []
        years = [str(y) for y in (capex.get("year_columns") or [])]

        table = self.query_one("#capex-table", DataTable)
        table.clear(columns=True)
        table.add_columns("end customer", "projects", "MW planned", "MW built", "investment", *years)
        biggest = max((p.get("mw_planned") or 0 for p in positions), default=0)
        for position in positions:
            cells = [
                Text(str(position.get("customer") or "?")[:26]),
                Text(str(position.get("projects") or 0)),
                Group(fmt_mw(position.get("mw_planned")), bar(position.get("mw_planned") or 0, biggest or 1, width=10)),
                fmt_mw(position.get("mw_built")),
                fmt_usd(position.get("investment_usd")),
            ]
            by_year = position.get("mw_by_year") or {}
            cells += [fmt_mw(by_year.get(year)) for year in years]
            table.add_row(*cells)

        head = Text()
        coverage = capex.get("coverage") or {}
        total = int(coverage.get("projects") or 0)
        attributed = coverage.get("attributed_pct") or 0.0
        head.append("attribution: ", style="bold")
        # Percentages, because that is what `capex.coverage` computes — the honest
        # companion to the table, as its docstring puts it: a rollup that silently
        # speaks for a third of the database looks authoritative and is not.
        head.append(
            f"{attributed:.0f}% of {total} project(s) traced to an end customer",
            style="green" if attributed >= 60 else "yellow" if attributed >= 30 else "red",
        )
        head.append(
            f"  ·  {coverage.get('with_capacity_pct', 0):.0f}% carry a cited capacity"
            f"  ·  {coverage.get('in_timeline_pct', 0):.0f}% are datable",
            style="dim",
        )
        duplicates = (capex.get("duplicates") or {}).get("double_counted_mw") or 0
        if duplicates:
            head.append(
                f"\n{duplicates:,.0f} MW would be counted twice — `duplicates` and `merge` fix it",
                style="yellow",
            )
        self.query_one("#capex-head", Static).update(head)


# --- Queue ------------------------------------------------------------------


class QueuePane(Pane):
    """What is waiting to be read, and what could not be."""

    def compose(self) -> ComposeResult:
        yield Static(id="queue-head")
        with Horizontal():
            yield DataTable(id="queue-table", cursor_type="row", zebra_stripes=True)
            yield DataTable(id="failed-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one("#queue-table", DataTable).add_columns("headline", "depth", "feed")
        self.query_one("#failed-table", DataTable).add_columns("host", "URLs", "HTTP")

    def load(self, snapshot: Snapshot) -> None:
        queue_table = self.query_one("#queue-table", DataTable)
        queue_table.clear()
        for entry in snapshot.queue[:200]:
            deepens = entry.get("depth")
            queue_table.add_row(
                Text(str(entry.get("title") or entry.get("url") or "")[:60]),
                Text(f"#{deepens}", style="green") if deepens else Text("new", style="dim"),
                Text(str(entry.get("feed") or "")[:26], style="dim"),
            )
        failed_table = self.query_one("#failed-table", DataTable)
        failed_table.clear()
        for entry in snapshot.failed[:100]:
            failed_table.add_row(
                Text(str(entry.get("host"))[:30]),
                Text(str(entry.get("count"))),
                Text(str(entry.get("http_status") or "-"), style="yellow"),
            )

        head = Text()
        head.append(f"{len(snapshot.queue)} candidate(s) waiting", style="bold")
        deepening = sum(1 for e in snapshot.queue if e.get("depth"))
        head.append(
            f"  ·  {deepening} cover a project already tracked, so those go first", style="dim"
        )
        failed = sum(e["count"] for e in snapshot.failed)
        if failed:
            head.append(
                f"\n{failed} URL(s) could not be read — `sync --retry-failed`, or `--browser`",
                style="yellow",
            )
        self.query_one("#queue-head", Static).update(head)


__all__ = [
    "CapexPane",
    "CoveragePane",
    "OverviewPane",
    "Pane",
    "ProjectsPane",
    "QueuePane",
]
