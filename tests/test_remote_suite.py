"""`scripts/remote_suite.py` and its host half, `scripts/remote_suite.sh`.

The flow itself needs the host and its sandbox, so what is tested here is what can
go wrong without either: the snapshot (does it hold the tree you would commit, and
does it leave the real index alone), the quoting that carries pytest's arguments
across ssh, the reading of the host's verdict, and the host half's guards — its
sandbox profile and its refusal of any directory that is production's.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _remote_suite():
    spec = importlib.util.spec_from_file_location("remote_suite", _SCRIPTS / "remote_suite.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    (root / ".gitignore").write_text("secret.env\n")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "start")
    return root


# --- the snapshot -----------------------------------------------------------


def test_the_snapshot_is_the_tree_you_would_commit_and_the_index_is_untouched(repo):
    (repo / "a.txt").write_text("two\n")
    (repo / "b.txt").write_text("new, never added\n")
    (repo / "secret.env").write_text("KEY=not-for-the-host\n")
    head, status = _git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain")

    sha, changed = _remote_suite().snapshot(repo)

    files = set(_git(repo, "ls-tree", "-r", "--name-only", sha).splitlines())
    assert files == {".gitignore", "a.txt", "b.txt"}, "ignored files never travel"
    assert _git(repo, "show", f"{sha}:a.txt") == "two"
    assert changed == 2
    assert _git(repo, "rev-parse", f"{sha}^") == head
    assert _git(repo, "rev-parse", "HEAD") == head, "HEAD does not move"
    assert _git(repo, "status", "--porcelain") == status, "nothing is staged for real"


def test_a_tree_with_nothing_uncommitted_is_tested_as_head_itself(repo):
    sha, changed = _remote_suite().snapshot(repo)
    assert (sha, changed) == (_git(repo, "rev-parse", "HEAD"), 0)


# --- across ssh ---------------------------------------------------------------


def test_pytest_arguments_arrive_as_they_were_given():
    """ssh joins its arguments into one line for the far shell; unquoted, `-k "a and
    b"` would arrive as three words."""
    command = _remote_suite().remote_command(
        "run", "dev/tracker/tracker-dev", "abc", "-k", "a and b"
    )
    assert command == "bash -s -- run dev/tracker/tracker-dev abc -k 'a and b'"


def test_the_host_script_is_sent_with_unix_line_endings():
    assert b"\r\n" not in _remote_suite().host_script()


def test_the_verdict_is_read_from_the_hosts_output():
    lines = [
        "........",
        "2 failed, 5031 passed, 12 deselected, 3 warnings in 95.12s (0:01:35)",
        "SUITE-RESULT exit=1 sha=abc123 seconds=97",
    ]
    assert _remote_suite().result_of(lines) == (
        1,
        "2 failed, 5031 passed, 12 deselected, 3 warnings in 95.12s (0:01:35)",
    )


def test_a_run_that_never_reached_pytest_has_no_exit_code():
    lines = ["isolation check:", "  FAIL the internet cannot be reached"]
    assert _remote_suite().result_of(lines) == (None, "")


class _Played:
    """What one ssh session to the host returned: its output, then its exit code."""

    def __init__(self, chunks: list[bytes], code: int):
        self.stdout, self._code = iter(chunks), code

    def wait(self) -> int:
        return self._code


def _follow(monkeypatch, *sessions: _Played):
    module = _remote_suite()
    asked, queue = [], list(sessions)

    def session(host, *args, stderr=None):
        asked.append(args)
        return queue.pop(0)

    monkeypatch.setattr(module, "_host_process", session)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    return module.follow("host", "dev/tracker/tracker-dev"), asked


def test_a_dropped_link_is_reattached_at_the_byte_where_it_stopped(monkeypatch):
    """The link from this machine drops now and then; the run on the host does not
    notice, and following it resumes with nothing repeated and nothing missed."""
    (ended, lines), asked = _follow(
        monkeypatch,
        _Played([b"one\n", b"two\n"], 255),
        _Played(
            [b"three\n", b"SUITE-RESULT exit=0 sha=abc seconds=9\n", b"SUITE-DONE exit=0\n"], 0
        ),
    )
    assert ended == "0"
    assert lines == ["one", "two", "three", "SUITE-RESULT exit=0 sha=abc seconds=9"]
    assert [a[2] for a in asked] == ["0", "8"], "the second session starts after 8 bytes"


def test_a_refusal_from_the_host_is_not_asked_again(monkeypatch):
    """ssh exits 255 for its own failures; the host half exits 2 when it refuses,
    and asking again would only be refused again."""
    (ended, lines), asked = _follow(monkeypatch, _Played([], 2))
    assert (ended, lines, len(asked)) == (None, [], 1)


# --- the host half's guards ------------------------------------------------------


def _bash() -> str | None:
    """A POSIX bash, or None. Windows' System32 `bash.exe` starts WSL, a different
    machine with a different home, so it does not count."""
    found = shutil.which("bash")
    if not found or "system32" in found.lower():
        return None
    return found


def _host(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    bash = _bash()
    if bash is None:
        pytest.skip("no POSIX bash here")
    script = (_SCRIPTS / "remote_suite.sh").read_bytes().replace(b"\r\n", b"\n")
    # Only HOME is replaced, so that no refusal test can reach the real one.
    env = {**os.environ, "HOME": tmp_path.as_posix()}
    return subprocess.run(
        [bash, "-s", "--", *args], input=script, capture_output=True, env=env, check=False
    )


def test_the_sandbox_denies_production_its_ports_and_the_internet(tmp_path):
    done = _host(tmp_path, "profile", "/Users/someone", "8765", "22")
    profile = done.stdout.decode().splitlines()

    for part in ("repo", "ops", "backups"):
        assert (
            f'(deny file-read* file-write* (subpath "/Users/someone/dev/tracker/{part}"))'
            in profile
        )
    internet = profile.index('(deny network-outbound (remote ip "*:*"))')
    loopback = profile.index('(allow network-outbound (remote ip "localhost:*"))')
    console = profile.index('(deny network-outbound (remote ip "localhost:8765"))')
    # Later rules override earlier ones: loopback is opened after the internet is
    # closed, and every port already listening is closed after that.
    assert internet < loopback < console
    assert '(deny network-outbound (remote ip "localhost:22"))' in profile


@pytest.mark.parametrize(
    ("where", "why"),
    [
        ("dev/tracker/repo", b"must be a directory named tracker-dev"),
        ("dev/tracker/elsewhere", b"must be a directory named tracker-dev"),
        ("dev/tracker/repo/tracker-dev", b"inside production's repo/"),
        ("dev/tracker/ops/tracker-dev", b"inside production's ops/"),
        ("dev/tracker/backups/tracker-dev", b"inside production's backups/"),
    ],
)
def test_the_host_half_refuses_anything_but_a_scratch_clone(tmp_path, where, why):
    """Each for its own reason: a refusal that came from something else failing — a
    missing command, say — would pass a test that only looked for "refusing"."""
    done = _host(tmp_path, "start", where, "abc123", "1")
    assert done.returncode == 2
    assert why in done.stderr, done.stderr
    assert list(tmp_path.iterdir()) == [], "refused before a file was touched"


def test_a_symlink_into_production_is_refused_too(tmp_path):
    production = tmp_path / "dev" / "tracker" / "repo"
    production.mkdir(parents=True)
    scratch = tmp_path / "dev" / "tracker" / "tracker-dev"
    try:
        scratch.symlink_to(production, target_is_directory=True)
    except OSError:
        pytest.skip("this machine cannot make symlinks")
    done = _host(tmp_path, "start", "dev/tracker/tracker-dev", "abc123", "1")
    assert done.returncode == 2
    assert b"leads into production's repo/" in done.stderr
