"""A full-screen terminal interface over the same commands and the same data.

**Why this exists beside the CLI and the console.** The CLI prints a snapshot and
forgets it: comparing two projects means running two commands and scrolling back,
and a filter you want to keep is a shell history entry. The console keeps state
and draws properly, but it needs a browser, a password and a tunnel — and the
machine that owns the database is reached by ssh. This is the interface for the
place the data actually lives.

**It offers every command the CLI does, and that is structural rather than
maintained.** The command surface is read out of the live Typer app through
`webui.catalog`, the same introspection the console's palette uses, so a command
added to `cli.py` appears here on the next start with its real flags, types,
defaults and help. Nothing here holds a hand-written list of commands that could
fall behind — see `tracker/tui/commands.py`.

**Runs go through `webui.runner`, not a fresh subprocess.** One definition of
"run a command": argv built from a validated flag dict with no shell at any point,
the confirmation ritual for anything that spends tokens or destroys rows, one
writer at a time because SQLite takes one, and the run recorded in the same
`data/runs/` history the console lists. A TUI that re-implemented that would be a
second set of gates to keep in step with the first.

Textual is imported lazily by `run` below, and that is deliberate: the deployer
refuses a commit that does not import, so a host that has not yet had its
dependencies installed must still be able to take this code and serve the console.
Without the extra, `tracker tui` says what to install and exits 2 — everything
else keeps working.
"""

from __future__ import annotations

from pathlib import Path

#: Shown when Textual is absent. The dependency is declared in `pyproject.toml`,
#: so this is what an environment installed before it existed looks like.
MISSING_TEXTUAL = (
    "the TUI needs Textual, which is not installed in this environment.\n"
    "Install it with:\n"
    '  python -m pip install -e ".[dev]"   # or: python -m pip install "textual>=1.0"\n'
    "Everything else — the CLI and `tracker serve` — works without it."
)


class MissingDependency(RuntimeError):
    """Textual is not installed. Message is operator-facing."""


def ensure_available() -> None:
    """Raise before anything else happens, rather than twenty lines in."""
    try:
        import textual  # noqa: F401
    except ImportError as exc:
        raise MissingDependency(MISSING_TEXTUAL) from exc


def run(
    db_path: Path,
    *,
    check: bool = False,
    screenshot: Path | None = None,
    pane: str | None = None,
    size: tuple[int, int] = (160, 48),
) -> int:
    """Start the interface. Returns a process exit code.

    `check` and `screenshot` are the headless paths, and they exist because the
    thing this deploys onto is reached by ssh: "does the TUI work on the host" has
    to be answerable without a human sitting at a terminal there. Both boot the
    real app against the real database, mount every pane, and render an actual
    frame — a smoke test that would fail on a missing column or a bad query, not a
    version string. `pane` decides which one the screenshot ends on; every pane is
    filled either way.
    """
    ensure_available()

    from tracker.tui.app import TrackerApp

    if not (check or screenshot):
        TrackerApp(db_path).run()
        return 0

    import asyncio

    async def _headless() -> int:
        app = TrackerApp(db_path)
        async with app.run_test(size=size) as pilot:
            # Every pane, not just the one that opens: a broken query in the capex
            # view is exactly the kind of thing this is meant to catch. The loop
            # variable is not `pane` — shadowing the argument left every screenshot
            # showing whichever pane happened to be walked last.
            for each in TrackerApp.PANES:
                app.show_pane(each)
                await pilot.pause()
            app.show_pane(pane or TrackerApp.PANES[0])
            await pilot.pause()
            if screenshot:
                app.save_screenshot(str(screenshot))
            problems = app.startup_problems
        for problem in problems:
            print(f"error {problem}")
        return 1 if problems else 0

    return asyncio.run(_headless())


__all__ = ["MISSING_TEXTUAL", "MissingDependency", "ensure_available", "run"]
