"""Where the code lives, and where this installation's data lives.

One function used to answer both. `install_root()` returned "the directory containing
the package", which is the repository root in an editable install and `site-packages/`
in a wheel — so a non-editable install looked for `migrations/` beside
`site-packages/tracker`, found nothing, and every command died in
`discover_migrations` before it could create anything. The database, meanwhile,
resolved against the current directory and so landed wherever the operator happened
to be standing.

They are now two functions with two jobs: :func:`package_root` for what ships, and
:func:`home` for what this installation owns. The tests here pin the precedence,
because rule 2 is what keeps every existing checkout and the production host on
exactly the directory they use today.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tracker.config import Settings, cache_dir, home, package_root, seed_path


@pytest.fixture(autouse=True)
def _fresh_home():
    """`home()` is cached — it cannot change while a process runs, and it is read on
    every settings access. A test that moves the environment has to say so."""
    home.cache_clear()
    yield
    home.cache_clear()


def test_the_package_holds_what_ships():
    """Migrations, seeds, prompts and the dashboard template are inputs the code
    cannot run without, so they live inside the package and travel in the wheel."""
    root = package_root()
    assert root.name == "tracker"
    assert (root / "migrations").is_dir()
    assert list((root / "migrations").glob("*.sql"))
    assert (root / "seed" / "sample-projects.json").is_file()
    assert (root / "prompts").is_dir()


def test_tracker_home_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKER_HOME", str(tmp_path))
    home.cache_clear()
    assert home() == tmp_path.resolve()


def test_a_user_path_is_expanded(monkeypatch):
    monkeypatch.setenv("TRACKER_HOME", "~/tracker-data")
    home.cache_clear()
    assert home() == (Path.home() / "tracker-data").resolve()


def test_an_editable_checkout_is_home(monkeypatch):
    """Rule 2, and the reason nothing moves for anybody already running this.

    `pip install -e .` leaves the package inside the repository, so the directory
    above it carries `pyproject.toml` — and that is exactly what `install_root()`
    used to return. The production host and every developer keep the same database.
    """
    monkeypatch.delenv("TRACKER_HOME", raising=False)
    home.cache_clear()
    beside = Path(__import__("tracker.config", fromlist=["x"]).__file__).resolve().parents[1]
    assert (beside / "pyproject.toml").is_file(), "this test suite runs from a checkout"
    assert home() == beside


def test_without_a_checkout_it_is_the_platform_data_directory(monkeypatch, tmp_path):
    """Rule 4, which is what makes `pipx install` usable at all.

    Faked by pointing the package somewhere with no `pyproject.toml` above it, since
    the suite itself always runs from a checkout.
    """
    monkeypatch.delenv("TRACKER_HOME", raising=False)
    monkeypatch.setattr("tracker.config.Path.cwd", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("tracker.config.__file__", str(tmp_path / "nowhere" / "config.py"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    home.cache_clear()

    got = home()

    assert got.name == "tracker"
    assert tmp_path in got.parents, got


def test_nothing_is_created_just_by_asking(monkeypatch, tmp_path):
    """`home()` is a question, not a side effect. Only `cache_dir` makes a directory,
    and only when something is about to be written into it."""
    monkeypatch.setenv("TRACKER_HOME", str(tmp_path / "unborn"))
    home.cache_clear()

    assert not home().exists()
    assert cache_dir("articles").is_dir()


def test_the_database_follows_home_not_the_current_directory(monkeypatch, tmp_path):
    """The failure this prevents: an installed `tracker` creating a fresh empty
    database in whatever directory it was run from, or silently adopting an
    unrelated project's `data/` because that project had a `pyproject.toml`."""
    monkeypatch.setenv("TRACKER_HOME", str(tmp_path))
    home.cache_clear()

    assert Settings().resolve_db() == (tmp_path / "data" / "tracker.db").resolve()


def test_an_absolute_database_override_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKER_HOME", str(tmp_path))
    home.cache_clear()
    elsewhere = tmp_path / "other" / "x.db"

    assert Settings().resolve_db(elsewhere) == elsewhere


def test_a_seed_file_falls_back_to_the_packaged_copy(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKER_HOME", str(tmp_path))
    home.cache_clear()

    assert seed_path("feeds.toml") == package_root() / "seed" / "feeds.toml"


def test_an_installation_may_override_a_seed_file(monkeypatch, tmp_path):
    """The seed files are inputs a person edits — which operators to chase, which
    feeds to poll — so an installation can carry its own without touching the
    package, which the next reinstall would discard."""
    monkeypatch.setenv("TRACKER_HOME", str(tmp_path))
    home.cache_clear()
    mine = tmp_path / "seed" / "feeds.toml"
    mine.parent.mkdir(parents=True)
    mine.write_text("# mine\n", encoding="utf-8")

    assert seed_path("feeds.toml") == mine


def test_the_policy_writer_never_writes_into_the_package(monkeypatch, tmp_path):
    """`sources apply` is the one command that writes a seed file. Writing it beside
    the package would edit a file inside site-packages, shared by every database on
    the machine and discarded by the next reinstall."""
    from tracker.policy import Policy, write

    monkeypatch.setenv("TRACKER_HOME", str(tmp_path))
    home.cache_clear()

    written = write(Policy())

    assert written == tmp_path / "seed" / "sources.toml"
    assert package_root() not in written.parents
