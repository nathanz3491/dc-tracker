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


async def loaded(app_under_test, pilot, *, timeout: float = 20.0):
    """Wait for the threaded database read to land, then settle a frame.

    Every pane fills from a snapshot read on a worker thread, so a single
    `pilot.pause()` is not a guarantee — it was enough on an idle machine and not
    enough inside a full suite, which is the definition of a flaky test. The
    product has the same hazard and `tui.run` waits for the same signal.
    """
    import asyncio

    while timeout > 0 and not app_under_test.snapshot.payload:
        await asyncio.sleep(0.05)
        timeout -= 0.05
    assert app_under_test.snapshot.payload, "the snapshot never loaded"
    await pilot.pause()


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
        await loaded(app_under_test, pilot)
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
        await loaded(app_under_test, pilot)
        assert app_under_test.startup_problems == []
        assert app_under_test.snapshot.projects == []


async def test_the_subtitle_says_which_database_and_how_big(curated: Path):
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test() as pilot:
        await loaded(app_under_test, pilot)
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
        await loaded(app_under_test, pilot)
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
        await loaded(app_under_test, pilot)
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
        await loaded(app_under_test, pilot)
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
        await loaded(app_under_test, pilot)
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
        await loaded(app_under_test, pilot)
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


# --- Typing in the run pane -------------------------------------------------


async def _run_pane(app_under_test, pilot):
    await loaded(app_under_test, pilot)
    app_under_test.show_pane("run")
    await pilot.pause()
    pane = app_under_test.query_one("#commands-pane", CommandsPane)
    app_under_test.query_one("#command-line").focus()
    await pilot.pause()
    return pane


async def test_typing_offers_candidates_and_tab_takes_one(curated: Path):
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(120, 30)) as pilot:
        pane = await _run_pane(app_under_test, pilot)
        for char in "cov":
            await pilot.press(char)
        await pilot.pause()
        assert [c.text for c in pane._completions.items] == ["coverage"]
        await pilot.press("tab")
        await pilot.pause()
        assert app_under_test.query_one("#command-line").value == "coverage "


async def test_the_arrows_move_through_the_candidates(curated: Path):
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(120, 30)) as pilot:
        pane = await _run_pane(app_under_test, pilot)
        for char in ("s", "y", "n", "c", "space", "minus", "minus"):
            await pilot.press(char)
        await pilot.pause()
        offered = [c.text for c in pane._completions.items]
        assert len(offered) > 3
        await pilot.press("down")
        await pilot.press("tab")
        await pilot.pause()
        assert app_under_test.query_one("#command-line").value == f"sync {offered[1]} "


async def test_up_from_the_top_wraps_to_the_end(curated: Path):
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(120, 30)) as pilot:
        pane = await _run_pane(app_under_test, pilot)
        for char in ("s", "y", "n", "c", "space", "minus", "minus"):
            await pilot.press(char)
        await pilot.pause()
        offered = [c.text for c in pane._completions.items]
        await pilot.press("up")
        await pilot.press("tab")
        await pilot.pause()
        assert app_under_test.query_one("#command-line").value == f"sync {offered[-1]} "


async def test_escape_closes_the_list_then_leaves_the_box(curated: Path):
    """Because the box eats `1`..`6`, `q` and `r` while it has focus."""
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(120, 30)) as pilot:
        pane = await _run_pane(app_under_test, pilot)
        await pilot.press("c", "o")
        await pilot.pause()
        assert pane.completions_open
        await pilot.press("escape")
        await pilot.pause()
        assert not pane.completions_open
        assert app_under_test.focused is app_under_test.query_one("#command-line")
        await pilot.press("escape")
        await pilot.pause()
        assert app_under_test.focused is not app_under_test.query_one("#command-line")


async def test_a_project_id_can_be_completed_from_the_database(curated: Path):
    """The completion nothing but a live database could offer."""
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(120, 30)) as pilot:
        pane = await _run_pane(app_under_test, pilot)
        for char in ("e", "n", "r", "i", "c", "h", "space"):
            await pilot.press(char)
        await pilot.pause()
        offered = pane._completions.items
        assert offered, "the ids in this database are what should be on offer"
        assert offered[0].hint.startswith("Meta"), "biggest campus first"
        await pilot.press("tab")
        await pilot.pause()
        line = app_under_test.query_one("#command-line").value
        assert line.split()[1].isdigit()


async def test_the_output_is_the_tallest_thing_in_the_pane(curated: Path):
    """The correction that prompted this layout: reading output is the job.

    Asserted rather than eyeballed, because a stray `height:` in the CSS would
    quietly give the reference material the screen back.
    """
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(120, 30)) as pilot:
        await _run_pane(app_under_test, pilot)
        log = app_under_test.query_one("#command-log")
        top = app_under_test.query_one("#commands-top")
        assert log.size.height > top.size.height


async def test_a_command_with_many_flags_does_not_flood_the_detail(curated: Path):
    """`sync` has seventeen; printing them all was six rows of a thirty-row screen."""
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(120, 30)) as pilot:
        pane = await _run_pane(app_under_test, pilot)
        pane.show_command("sync")
        await pilot.pause()
        # Measured off the rendered widget rather than the string it was given: the
        # thing that matters is rows on screen, and the flag line wraps.
        from textual.geometry import Region

        detail = app_under_test.query_one("#command-detail")
        strips = detail.render_lines(Region(0, 0, detail.size.width, detail.size.height))
        text = " / ".join(strip.text for strip in strips)
        assert "more" in text, "the rest have to be accounted for, not just dropped"
        assert detail.size.height <= 7, f"{detail.size.height} rows: {text}"


# --- The run pane as a terminal ---------------------------------------------


async def test_the_log_does_not_reflow_what_the_child_already_wrapped(curated: Path):
    """The console learned this first, in CSS: there is no width but COLUMNS.

    The child wraps its tables and prose to the width it was told, so wrapping
    again here breaks every long line a second time, mid-sentence, with the
    continuation starting at column zero. A line still too wide scrolls sideways —
    which is what a terminal does with one.
    """
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(120, 30)) as pilot:
        await _run_pane(app_under_test, pilot)
        log = app_under_test.query_one("#command-log")
        assert log.wrap is False


async def test_the_child_is_told_this_panes_width(curated: Path):
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(140, 30)) as pilot:
        pane = await _run_pane(app_under_test, pilot)
        seen: dict[str, object] = {}
        real_start = pane._runner.start

        def spy(cmd, flags, *, confirm=None, columns=None):
            seen["columns"] = columns
            raise runner_stub.Busy("not actually running it")

        import tracker.webui.runner as runner_stub

        pane._runner.start = spy
        pane.submit("gaps")
        await pilot.pause()
        pane._runner.start = real_start
        log = app_under_test.query_one("#command-log")
        # Less the scrollbar, which appears once output overflows — after this is
        # measured. A table sized to the full width lost its last column to it.
        assert seen["columns"] == log.content_size.width - log.scrollbar_size_vertical
        assert seen["columns"] > 0


async def test_scrolling_back_stops_the_output_chasing_the_tail(curated: Path):
    """And shift+end puts you back on it.

    A terminal pins to the bottom while output arrives and stays put once you
    scroll up; otherwise the next line yanks you off whatever you were reading.
    """
    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(120, 30)) as pilot:
        await _run_pane(app_under_test, pilot)
        log = app_under_test.query_one("#command-log")
        for index in range(200):
            log.write(f"line {index}")
        await pilot.pause()
        assert log.auto_scroll is True

        await pilot.press("pageup")
        await pilot.pause()
        assert log.auto_scroll is False, "reading has to survive the next line arriving"

        await pilot.press("shift+end")
        await pilot.pause()
        assert log.auto_scroll is True


async def test_the_status_line_does_not_repeat_the_log(curated: Path):
    """Two identical sentences a row apart read as a stutter."""
    import asyncio

    app_under_test = TrackerApp(curated)
    async with app_under_test.run_test(size=(120, 30)) as pilot:
        pane = await _run_pane(app_under_test, pilot)
        pane.submit("gaps")
        for _ in range(600):
            run = pane._runner.current
            if run is not None and run.status != "running":
                break
            await asyncio.sleep(0.1)
        await pilot.pause()
        assert pane._runner.current.status == "ok"
        from textual.geometry import Region

        status = app_under_test.query_one("#command-status")
        strips = status.render_lines(Region(0, 0, status.size.width, max(1, status.size.height)))
        text = " ".join(strip.text for strip in strips)
        assert "gaps ok" not in text, "the log already says that"
        assert "pageup" in text, "the status line is where the next move is explained"


def test_a_check_that_never_gets_the_data_fails_instead_of_passing(
    curated: Path, monkeypatch
):
    """The guard on the bug this was: reporting success before the read happened.

    Panes fill from a worker thread, so `--check` used to walk them, find no
    exceptions and print "every pane filled" — which was true only because nothing
    had been filled yet. It now waits, and says so when the wait runs out.
    """
    import time

    from tracker import tui as tui_mod
    from tracker.tui import data as data_mod

    monkeypatch.setattr(tui_mod, "LOAD_TIMEOUT_S", 0.3)

    def slow(_cls, _db):
        time.sleep(2)
        return data_mod.Snapshot()

    monkeypatch.setattr(data_mod.Snapshot, "load", classmethod(slow))
    result = invoke(curated, "tui", "--check")
    assert result.exit_code == 1
    assert "still being read" in result.output
