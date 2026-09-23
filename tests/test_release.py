"""The version the console shows, and where its digits come from.

It is the first thing a reader sees in the header, and a stale one quietly says
the deploy did not land — so it is derived from the commit count rather than
typed, and stamped into the source rather than stored anywhere the poller would
overwrite or anywhere it could disagree with the code running.
"""

from __future__ import annotations

import re
import subprocess
import time

import pytest

from tracker import release


@pytest.mark.parametrize(
    ("count", "want"),
    [
        (0, "0.0.0"),
        (7, "0.0.7"),
        (42, "0.4.2"),
        (190, "1.9.0"),
        (191, "1.9.1"),
        (200, "2.0.0"),
        (999, "9.9.9"),
        (1234, "12.3.4"),
    ],
)
def test_the_version_is_the_commit_count_with_dots_in_it(count, want):
    """The whole rule, and it has to stay checkable at a glance."""
    assert release.from_commit_count(count) == want


def test_a_negative_count_is_refused_rather_than_formatted():
    with pytest.raises(ValueError):
        release.from_commit_count(-1)


def test_every_derived_version_is_a_three_part_version():
    """Whatever the count, the result has to be something `--number` would accept."""
    for count in (0, 9, 10, 99, 100, 5000):
        assert release.VERSION.match(release.from_commit_count(count))


# --- the two files, and the state they must never be left in -------------------


def _repo(tmp_path, *, init="0.1.0", pyproject="0.1.0"):
    (tmp_path / "tracker").mkdir()
    (tmp_path / "tracker" / "__init__.py").write_text(
        f'"""Docstring."""\n\n__version__ = "{init}"\n\n__all__ = ["__version__"]\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling"]\n\n'
        "[project]\n"
        'name = "dc-tracker"\n'
        f'version = "{pyproject}"\n'
        'requires-python = ">=3.11"\n\n'
        "[tool.ruff]\n"
        'target-version = "py311"\n',
        encoding="utf-8",
    )
    return tmp_path


def test_stamping_writes_both_files(tmp_path):
    root = _repo(tmp_path)
    changed = release.stamp("1.9.0", root)

    assert {p.name for p in changed} == {"__init__.py", "pyproject.toml"}
    assert '__version__ = "1.9.0"' in (root / "tracker" / "__init__.py").read_text(encoding="utf-8")
    assert 'version = "1.9.0"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_stamping_the_version_already_there_changes_nothing(tmp_path):
    """What lets the deploy step say "nothing to commit" instead of making an
    empty commit every time somebody runs it."""
    root = _repo(tmp_path, init="1.9.0", pyproject="1.9.0")
    assert release.stamp("1.9.0", root) == []


def test_only_the_project_version_is_rewritten(tmp_path):
    """`requires-python` and a tool's `target-version` are not the version, and a
    looser pattern finds them first."""
    root = _repo(tmp_path)
    release.stamp("1.9.0", root)
    text = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.11"' in text
    assert 'target-version = "py311"' in text
    assert text.count('"1.9.0"') == 1


def test_a_file_with_no_version_line_leaves_the_other_alone(tmp_path):
    """The failure that made this function atomic.

    Writing as it went left `__init__.py` stamped and `pyproject.toml` not — two
    files disagreeing about the version, which is the one state this exists to
    prevent. It now computes both rewrites before performing either.
    """
    root = _repo(tmp_path)
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    before = (root / "tracker" / "__init__.py").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="no version line"):
        release.stamp("1.9.0", root)

    assert (root / "tracker" / "__init__.py").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("bad", ["1.9", "v1.9.0", "1.9.0-rc1", "", "one.nine.oh"])
def test_a_version_that_is_not_three_numbers_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        release.stamp(bad, _repo(tmp_path))


def test_the_pyproject_pattern_cannot_backtrack_catastrophically():
    """It did, and it hung rather than failing.

    The first version carried the DOTALL flag, so `.` matched newlines and the
    walk from `[project]` to its `version` exploded on a file of any size. A regex
    that is wrong by hanging is the worst kind: `tracker version --stamp` simply
    never returned, with no error to read.
    """
    hostile = "[project]\n" + "".join(f'key{i} = "value{i}"\n' for i in range(400))
    started = time.monotonic()
    assert release._PYPROJECT_LINE.search(hostile) is None
    assert time.monotonic() - started < 1.0, "the pattern is backtracking again"


def test_stamped_reads_the_version_without_importing_it(tmp_path):
    """Read from the file rather than from `tracker.__version__`, so the CLI can
    report what it is about to overwrite rather than what it imported at start."""
    root = _repo(tmp_path, init="2.3.4")
    assert release.stamped(root) == "2.3.4"


# --- the repository this actually runs in --------------------------------------


def test_the_two_files_in_this_checkout_agree():
    """A reader who finds them disagreeing cannot tell which one is the version."""
    root = release.repo_root()
    from_init = release.stamped(root)
    from_pyproject = re.search(
        r'^version\s*=\s*"([^"]*)"',
        (root / "pyproject.toml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert from_pyproject is not None
    assert from_init == from_pyproject.group(1)


def test_the_counter_reads_this_checkout():
    """Skipped outside a checkout, which is what a tarball install is."""
    inside = subprocess.run(["git", "rev-parse"], cwd=release.repo_root(), capture_output=True)
    if inside.returncode != 0:
        pytest.skip("not a git checkout")
    count = release.commit_count()
    assert count is not None and count > 0


def test_no_checkout_is_reported_rather_than_raised(tmp_path):
    """A tarball install has no `.git`, and the CLI has to say so plainly."""
    assert release.commit_count(tmp_path) is None
