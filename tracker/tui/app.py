"""The app shell: panes, keys, and one read of the database shared between them.

The database is read once per refresh, on a worker thread, and handed to every
pane. Not per pane, and never on the event loop: `dataset.build` walks 300 projects
with their sources, events, risks and blocks, and doing that inside the frame that
switched tabs is how a terminal interface earns its reputation for freezing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, TabbedContent, TabPane

from tracker.tui.commands import CommandsPane
from tracker.tui.data import Snapshot
from tracker.tui.views import CapexPane, CoveragePane, OverviewPane, ProjectsPane, QueuePane


class TrackerApp(App):
    """`tracker tui`."""

    TITLE = "dc-tracker"

    #: Pane ids, in tab order. `run` walks these headlessly for `--check`, so the
    #: list is the definition of "every pane" in one place rather than two.
    PANES: ClassVar[tuple[str, ...]] = (
        "overview",
        "projects",
        "coverage",
        "capex",
        "queue",
        "run",
    )

    CSS = """
    Screen { layers: base; }
    TabbedContent { height: 1fr; }
    #projects-table { width: 3fr; }
    #detail-wrap { width: 2fr; border-left: solid $panel; padding: 0 1; }
    #coverage-table { width: 3fr; }
    #coverage-side-wrap { width: 2fr; border-left: solid $panel; padding: 0 1; }
    #coverage-head, #capex-head, #queue-head, #command-status { padding: 0 1; height: auto; }
    #queue-table { width: 3fr; }
    #failed-table { width: 2fr; border-left: solid $panel; }
    #commands-top { height: 1fr; }
    #commands-left { width: 2fr; }
    #command-detail-wrap { width: 3fr; border-left: solid $panel; padding: 0 1; }
    #command-log { height: 40%; border-top: solid $panel; }
    #command-line { border: solid $accent; }
    #command-confirm { border: solid $error; }
    Input { height: 3; }
    """

    BINDINGS: ClassVar = [
        Binding("q", "quit", "quit"),
        Binding("r", "reload", "reload data"),
        Binding("1", "pane('overview')", "overview"),
        Binding("2", "pane('projects')", "projects"),
        Binding("3", "pane('coverage')", "coverage"),
        Binding("4", "pane('capex')", "capex"),
        Binding("5", "pane('queue')", "queue"),
        Binding("6", "pane('run')", "run"),
        Binding("slash", "focus_filter", "filter"),
        # The two per-project actions worth a key: both prefill the run pane rather
        # than starting anything, because `enrich` spends money and the ritual for
        # that lives in one place.
        Binding("e", "enrich_highlighted", "enrich this row"),
        Binding("s", "show_highlighted", "show this row"),
        Binding("p", "prospect_highlighted", "prospect this operator"),
    ]

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self.db_path = Path(db_path)
        self.snapshot = Snapshot()
        #: Anything that stopped a pane from filling. `tracker tui --check` exits
        #: non-zero on these, which is what makes the headless boot a real test.
        self.startup_problems: list[str] = []

    # --- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="overview", id="tabs"):
            with TabPane("Overview", id="overview"):
                yield OverviewPane(id="overview-pane")
            with TabPane("Projects", id="projects"):
                yield ProjectsPane(id="projects-pane")
            with TabPane("Coverage", id="coverage"):
                yield CoveragePane(id="coverage-pane")
            with TabPane("Capex", id="capex"):
                yield CapexPane(id="capex-pane")
            with TabPane("Queue", id="queue"):
                yield QueuePane(id="queue-pane")
            with TabPane("Run", id="run"):
                yield CommandsPane(self.db_path, id="commands-pane")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = str(self.db_path)
        self.reload_data()

    # --- data -------------------------------------------------------------

    @work(thread=True, exclusive=True)
    def reload_data(self) -> None:
        """Read the database off the event loop, then fill the panes on it."""
        try:
            snapshot = Snapshot.load(self.db_path)
        except Exception as exc:  # surfaced in the UI, not printed to a dead stdout
            self.call_from_thread(self._load_failed, exc)
            return
        self.call_from_thread(self._loaded, snapshot)

    def _loaded(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.startup_problems = list(snapshot.problems)
        for pane_id, pane_type in (
            ("#overview-pane", OverviewPane),
            ("#projects-pane", ProjectsPane),
            ("#coverage-pane", CoveragePane),
            ("#capex-pane", CapexPane),
            ("#queue-pane", QueuePane),
        ):
            try:
                self.query_one(pane_id, pane_type).load(snapshot)
            except Exception as exc:  # one broken pane must not blank the others
                self.startup_problems.append(f"{pane_id.lstrip('#')}: {exc}")
        totals = snapshot.totals
        self.sub_title = (
            f"{self.db_path}  ·  {totals.get('projects', 0)} projects  ·  "
            f"schema {snapshot.schema_version}"
        )
        for problem in snapshot.problems:
            self.notify(problem, severity="warning", timeout=8)

    def _load_failed(self, exc: Exception) -> None:
        self.startup_problems.append(f"could not read {self.db_path}: {exc}")
        self.notify(str(exc), title="database", severity="error", timeout=12)

    # --- actions ----------------------------------------------------------

    def show_pane(self, pane: str) -> None:
        """Switch tabs. Used by the key bindings and by the headless check."""
        self.query_one("#tabs", TabbedContent).active = pane

    def action_pane(self, pane: str) -> None:
        self.show_pane(pane)

    def action_reload(self) -> None:
        self.notify("reading the database…", timeout=2)
        self.reload_data()

    def action_focus_filter(self) -> None:
        self.show_pane("projects")
        self.query_one("#filter").focus()

    def action_enrich_highlighted(self) -> None:
        self._prefill_for_project("enrich {id}")

    def action_show_highlighted(self) -> None:
        self._prefill_for_project("show {id}")

    def action_prospect_highlighted(self) -> None:
        """From the coverage pane, chase the operator under the cursor."""
        name = self.query_one("#coverage-pane", CoveragePane).highlighted_name
        if name is None:
            self.notify("no operator highlighted", severity="warning", timeout=4)
            return
        self._prefill(f'prospect "{name}"')

    def _prefill_for_project(self, template: str) -> None:
        project_id = self.query_one("#projects-pane", ProjectsPane).highlighted_id
        if project_id is None:
            self.notify("no project highlighted", severity="warning", timeout=4)
            return
        self._prefill(template.format(id=project_id))

    def _prefill(self, line: str) -> None:
        self.show_pane("run")
        self.query_one("#commands-pane", CommandsPane).prefill(line)

    # --- messages ---------------------------------------------------------

    def on_commands_pane_data_changed(self, _message: Any) -> None:
        """A run wrote rows, so re-read rather than leaving stale panes on screen."""
        self.notify("a run changed rows — re-reading", timeout=3)
        self.reload_data()

    def render_startup_report(self) -> Text:
        """For `--check`: what, if anything, failed to fill."""
        if not self.startup_problems:
            return Text("every pane filled", style="green")
        return Text("\n".join(self.startup_problems), style="red")


__all__ = ["TrackerApp"]
