"""The terminal interface, driven headlessly.

Textual's `run_test` boots the real app against a real database file, so these are
not widget unit tests: a pane that queries a column that no longer exists fails
here. That is the same guarantee `tracker tui --check` gives on the host, which is
how the TUI gets verified over ssh with nobody sitting at a terminal there.

The load-bearing assertion is
:func:`test_the_run_pane_offers_every_command_the_cli_has`. "The TUI has all the
CLI's functions" is only true because the pane reads the catalog rather than a
hand-written list; if that ever changes, this file should fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("textual", reason="the TUI needs the textual extra")

from typer.testing import CliRunner

from tracker.cli import app
from tracker.tui.app import TrackerApp
from tracker.tui.commands import CommandsPane
from tracker.tui.data import Snapshot, bar, fmt_usd
from tracker.tui.views import CoveragePane, ProjectsPane
from tracker.webui import catalog

runner = CliRunner()
SEED = Path(__file__).resolve().parent.parent / "seed" / "sample-projects.json"


def invoke(db: Path, *args: str):
    return runner.invoke(app, ["--db", str(db), *args])


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    db = tmp_path / "t.db"
    assert invoke(db, "init").exit_code == 0
    result = invoke(db, "ingest", "manual", "--json", str(SEED), "--allow-placeholders")
    assert result.exit_code == 0, result.output
    return db


@pytest.fixture
def curated(tmp_path: Path) -> Path:
    """Two rows with real figures, so the panes have something to draw."""
    db = tmp_path / "curated.db"
    assert invoke(db, "init").exit_code == 0
    payload = {
        "projects": [
            {
                "name": "Hyperion",
                "company": "Meta",
                "city": "Richland Parish",
                "state": "LA",
                "mw_planned": 2000,
                "investment_usd": 10_000_000_000,
                "sources": [{"url": "https://news.example/meta-hyperion/"}],
                "risks": [
                    {
                        "category": "transmission",
                        "severity": "blocking",
                        "summary": "Substation energization slipped.",
                        "quote": "the on-site substation is not yet energized",
                    }
                ],
            },
            {
                "name": "Kansas City",
                "company": "Nebius",
                "city": "Kansas City",
                "state": "MO",
                "mw_planned": 300,
                "sources": [{"url": "https://news.example/nebius-kc/"}],
            },
        ]
    }
    path = tmp_path / "curated.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert invoke(db, "ingest", "manual", "--json", str(path)).exit_code == 0
    return db


# --- Booting ----------------------------------------------------------------


async def test_every_pane_fills_against_a_real_database(curated: Path):
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(160, 48)) as pilot:
        for pane in TrackerApp.PANES:
            app_under_test.show_pane(pane)
            await pilot.pause()
        assert app_under_test.startup_problems == []
        assert app_under_test.snapshot.totals["projects"] == 2


async def test_an_empty_database_is_not_an_error(tmp_path: Path):
    """A fresh `tracker init` and nothing else must still open."""
    db = tmp_path / "empty.db"
    assert invoke(db, "init").exit_code == 0
    app_under_test = TrackerApp(db)
    async with app_under_test.run_test() as pilot:
        await pilot.pause()
        assert app_under_test.startup_problems == []
        assert app_under_test.snapshot.projects == []


async def test_the_subtitle_says_which_database_and_how_big(curated: Path):
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        await pilot.pause()
        assert "2 projects" in app_under_test.sub_title
        assert str(curated) in app_under_test.sub_title


def test_a_missing_textual_is_reported_before_anything_else(monkeypatch):
    """The message has to name the install, not raise ImportError at a user."""
    import sys

    from tracker import tui as tui_mod

    monkeypatch.setitem(sys.modules, "textual", None)
    with pytest.raises(tui_mod.MissingDependency, match="textual"):
        tui_mod.ensure_available()


# --- The command surface ----------------------------------------------------


async def test_the_run_pane_offers_every_command_the_cli_has(curated: Path):
    """The whole point of reading the catalog instead of listing commands here.

    A command added to `cli.py` appears in the TUI on the next start. The console's
    palette makes the same promise for the same reason, and this is the assertion
    that keeps it true for this interface.
    """
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        app_under_test.show_pane("run")
        await pilot.pause()
        pane = app_under_test.query_one("#commands-pane", CommandsPane)
        offered = set(pane._order)
    expected = set(catalog.by_name())
    assert offered == expected, f"missing from the TUI: {sorted(expected - offered)}"
    # And the ones this change added are in there, rather than only the old set.
    assert {"coverage", "prospect", "sync", "tui"} <= offered


async def test_searching_narrows_the_command_list(curated: Path):
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        app_under_test.show_pane("run")
        await pilot.pause()
        pane = app_under_test.query_one("#commands-pane", CommandsPane)
        everything = len(pane._order)
        pane.fill_list("coverage")
        await pilot.pause()
        # Name *or* help text, which is why `gaps` survives this search — its own
        # first line is "Per-field coverage". Finding a command by what it is for
        # is the point of searching the help at all.
        assert "coverage" in pane._order
        assert "gaps" in pane._order
        assert "merge" not in pane._order
        assert len(pane._order) < everything / 4


async def test_a_shell_metacharacter_is_a_word_no_command_has(curated: Path):
    """There is no shell here at any point, so this is refused by name.

    Not sanitized, not escaped — `catalog.parse_command_line` looks the first token
    up in the catalog and `rm` is not in it.
    """
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        app_under_test.show_pane("run")
        await pilot.pause()
        pane = app_under_test.query_one("#commands-pane", CommandsPane)
        pane.submit("gaps; rm -rf /")
        await pilot.pause()
        assert pane._runner is not None
        assert pane._runner.current is None, "nothing may start"


async def test_a_command_that_spends_tokens_asks_for_the_name_first(curated: Path):
    """The console's ritual, unchanged: a terminal is not a reason to be laxer."""
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        app_under_test.show_pane("run")
        await pilot.pause()
        pane = app_under_test.query_one("#commands-pane", CommandsPane)
        pane.submit("sync --limit 1")
        await pilot.pause()
        assert pane._runner is not None
        assert pane._runner.current is None, "an LLM command must not start unconfirmed"
        assert pane.query("#command-confirm"), "it has to ask"

        # The wrong word does not open the gate either.
        pane.confirm("yes")
        await pilot.pause()
        assert pane._runner.current is None


async def test_a_blocked_command_says_so_instead_of_running(curated: Path):
    """`tui` cannot start a TUI inside the TUI, and `serve` is already running."""
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        app_under_test.show_pane("run")
        await pilot.pause()
        pane = app_under_test.query_one("#commands-pane", CommandsPane)
        pane.submit("tui")
        await pilot.pause()
        assert pane._runner is not None and pane._runner.current is None


async def test_a_free_command_runs_and_its_output_arrives(curated: Path):
    """End to end through the real runner: a subprocess, streamed back into the log.

    `gaps` is the cheapest command that reads the database and prints a table, so
    this exercises argv construction, the subprocess, the event queue and the log
    without spending anything.
    """
    import asyncio

    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        app_under_test.show_pane("run")
        await pilot.pause()
        pane = app_under_test.query_one("#commands-pane", CommandsPane)
        pane.submit("gaps")
        assert pane._runner is not None
        for _ in range(600):  # up to ~60s; a cold interpreter start dominates
            if pane._runner.current is not None and pane._runner.current.status != "running":
                break
            await asyncio.sleep(0.1)
        run = pane._runner.current
        assert run is not None
        assert run.status == "ok", "\n".join(run.lines[-10:])
        assert any("mw_planned" in line or "field" in line for line in run.lines), run.lines[:5]


# --- Read panes -------------------------------------------------------------


async def test_the_filter_narrows_the_projects_table(curated: Path):
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        app_under_test.show_pane("projects")
        await pilot.pause()
        pane = app_under_test.query_one("#projects-pane", ProjectsPane)
        assert len(pane._rows) == 2
        pane.query_one("#filter").value = "nebius"
        await pilot.pause()
        assert [row["company"] for row in pane._rows] == ["Nebius"]
        # Two words both have to match, so this is Meta in Louisiana and not
        # everything mentioning either.
        pane.query_one("#filter").value = "meta la"
        await pilot.pause()
        assert [row["company"] for row in pane._rows] == ["Meta"]


async def test_the_filter_matching_nothing_says_so(curated: Path):
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        app_under_test.show_pane("projects")
        await pilot.pause()
        pane = app_under_test.query_one("#projects-pane", ProjectsPane)
        pane.query_one("#filter").value = "no such campus"
        await pilot.pause()
        assert pane._rows == []


async def test_coverage_lists_the_absent_operators_first(curated: Path):
    """Nebius is present here, so it must not be in the absent block."""
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        app_under_test.show_pane("coverage")
        await pilot.pause()
        pane = app_under_test.query_one("#coverage-pane", CoveragePane)
        statuses = [row.status for row in pane._rows]
        assert statuses == sorted(statuses, key=lambda s: {"absent": 0, "thin": 1, "covered": 2}[s])
        by_name = {row.name: row for row in pane._rows}
        assert by_name["Nebius"].status != "absent"
        assert by_name["CoreWeave"].status == "absent"


async def test_pressing_e_prefills_enrich_for_the_highlighted_row(curated: Path):
    """The one place the panes and the runner meet, and it stops at prefilling.

    `enrich` spends money, so the key fills the box and switches tab; the
    confirmation still happens in the one place that owns it.
    """
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        app_under_test.show_pane("projects")
        await pilot.pause()
        app_under_test.action_enrich_highlighted()
        await pilot.pause()
        line = app_under_test.query_one("#command-line").value
        assert line.startswith("enrich ")
        assert line.split()[1].isdigit()
        assert app_under_test.query_one("#tabs").active == "run"


async def test_pressing_p_on_coverage_prefills_prospect_for_that_operator(curated: Path):
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        app_under_test.show_pane("coverage")
        await pilot.pause()
        app_under_test.action_prospect_highlighted()
        await pilot.pause()
        assert app_under_test.query_one("#command-line").value.startswith("prospect ")


# --- The headless entry point ----------------------------------------------


def test_check_exits_zero_and_says_so(curated: Path):
    result = invoke(curated, "tui", "--check")
    assert result.exit_code == 0, result.output
    assert "every pane filled" in result.output


def test_a_screenshot_is_written_where_asked(curated: Path, tmp_path: Path):
    target = tmp_path / "frame.svg"
    result = invoke(curated, "tui", "--screenshot", str(target), "--pane", "coverage")
    assert result.exit_code == 0, result.output
    assert target.is_file()
    # The SVG splits text at every style change, so the assertion is on the frame
    # with its tags stripped rather than on the raw markup.
    import re

    rendered = re.sub(r"<[^>]+>", "", target.read_text(encoding="utf-8"))
    assert "rostered" in rendered, "the coverage pane is what should be on screen"


def test_an_unknown_pane_is_refused_before_booting(curated: Path):
    result = invoke(curated, "tui", "--pane", "nonsense")
    assert result.exit_code == 2
    assert "coverage" in result.output


def test_a_missing_database_is_reported_not_created(tmp_path: Path):
    """And it says to run `init`, rather than opening an interface over nothing."""
    absent = tmp_path / "nope.db"
    result = invoke(absent, "tui", "--check")
    assert result.exit_code == 2
    assert "init" in result.output
    assert not absent.exists(), "a read must never create the file it cannot find"


# --- Formatting -------------------------------------------------------------


def test_money_is_rendered_at_the_scale_it_is_reported_at():
    assert fmt_usd(2_992_100_000_000).plain == "$2.99T"
    assert fmt_usd(10_000_000_000).plain == "$10.0B"
    assert fmt_usd(500_000_000).plain == "$500M"
    assert fmt_usd(None).plain == "-"


def test_a_bar_is_empty_rather_than_absent_when_there_is_nothing():
    assert bar(0, 0, width=4).plain == "····"
    assert bar(2, 4, width=4).plain == "██··"
    assert bar(9, 9, width=4).plain == "████"


def test_a_snapshot_reports_a_bad_roster_rather_than_failing(curated: Path, monkeypatch):
    """One hand-edited file must not be able to take the whole interface down."""
    from tracker import roster as roster_mod

    def boom(*_a, **_k):
        raise roster_mod.RosterError("operators.toml is not valid TOML")

    monkeypatch.setattr(roster_mod, "measure", boom)
    snapshot = Snapshot.load(curated)
    assert snapshot.coverage is None
    assert snapshot.problems and "coverage unavailable" in snapshot.problems[0]
    assert len(snapshot.projects) == 2, "the projects still loaded"
