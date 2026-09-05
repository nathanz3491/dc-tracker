"""Does a wheel, installed somewhere else, actually work?

Every other test in this suite runs from the checkout, where the package sits inside
the repository and everything it needs is a directory above it. That is precisely the
arrangement that hid the bug this file exists to catch: `migrations/` and `seed/` used
to live at the repository root, outside the package, so a wheel carried neither and
`tracker init` failed on a fresh install with "migrations directory not found" — while
the whole suite stayed green.

So this builds a real wheel, installs it into a throwaway virtual environment, and
runs the CLI from a directory that is not a checkout. It is the only test here that
can tell an editable install from a real one.

**Opt in**, because it costs a wheel build, a venv and a pip install — a minute or so
against the rest of the suite's seconds:

    TRACKER_TEST_INSTALL=1 pytest tests/test_install.py

CI and anyone changing `pyproject.toml`, `tracker/config.py` or `tracker/db.py`'s path
resolution should run it. It is skipped otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.environ.get("TRACKER_TEST_INSTALL") != "1",
    reason="set TRACKER_TEST_INSTALL=1 to build a wheel and install it (about a minute)",
)


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    """A real wheel, built the way a release would be."""
    out = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(out), str(REPO)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    built = sorted(out.glob("*.whl"))
    assert built, f"no wheel produced: {result.stdout[-2000:]}"
    return built[-1]


@pytest.fixture(scope="module")
def installed(wheel, tmp_path_factory) -> Path:
    """The wheel, in a virtual environment of its own. Returns the `tracker` script."""
    root = tmp_path_factory.mktemp("venv")
    venv.create(root, with_pip=True)
    scripts = root / ("Scripts" if sys.platform == "win32" else "bin")
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", str(wheel)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return scripts / ("tracker.exe" if sys.platform == "win32" else "tracker")


def _run(tracker: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "TRACKER_HOME": str(home), "COLUMNS": "200"}
    env.pop("TRACKER_DB", None)
    # Run from somewhere that is emphatically not a checkout.
    return subprocess.run(
        [str(tracker), *args], capture_output=True, text=True, cwd=str(home), env=env
    )


def test_the_wheel_carries_the_data_the_code_reads(wheel):
    """The root cause, checked directly: a wheel contains the package and nothing
    else, so anything the runtime opens has to be inside it."""
    names = zipfile.ZipFile(wheel).namelist()
    for prefix in ("tracker/migrations/", "tracker/seed/", "tracker/prompts/"):
        assert [n for n in names if n.startswith(prefix)], f"the wheel is missing {prefix}"
    assert [n for n in names if n.endswith(".sql")], "no migrations in the wheel"


def test_it_reports_where_it_thinks_it_is(installed, tmp_path):
    home = tmp_path / "away"
    home.mkdir()

    result = _run(installed, home, "paths")

    assert result.returncode == 0, result.stderr
    assert "site-packages" in result.stdout, "the package should be the installed one"
    assert str(home) in result.stdout


def test_init_works_outside_a_checkout(installed, tmp_path):
    """The failure that was invisible from inside the repository."""
    home = tmp_path / "away-init"
    home.mkdir()

    result = _run(installed, home, "init")

    assert result.returncode == 0, result.stderr
    assert (home / "data" / "tracker.db").is_file()


def test_the_quick_start_runs_from_anywhere(installed, tmp_path):
    """README names a seed file by bare name; the seed files ship in the package."""
    home = tmp_path / "away-seed"
    home.mkdir()
    assert _run(installed, home, "init").returncode == 0

    loaded = _run(
        installed,
        home,
        "ingest",
        "manual",
        "--json",
        "sample-projects.json",
        "--allow-placeholders",
    )
    assert loaded.returncode == 0, loaded.stderr

    listed = _run(installed, home, "--json", "list")
    assert listed.returncode == 0, listed.stderr
    assert len(json.loads(listed.stdout)["projects"]) == 3


def test_two_homes_are_two_databases(installed, tmp_path):
    """What `TRACKER_HOME` buys: the same installation serving separate datasets,
    instead of one database that follows the current directory around."""
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()

    assert _run(installed, first, "init").returncode == 0
    assert _run(installed, second, "init").returncode == 0

    assert (first / "data" / "tracker.db").is_file()
    assert (second / "data" / "tracker.db").is_file()
