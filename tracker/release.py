"""The version the console shows, and where its digits come from.

**The number is a fact about the repository, not a decision.** It is the commit
count with dots put before the last two digits: 190 commits is `1.9.0`, 7 is
`0.0.7`, 1,234 is `12.3.4`. Nobody has to remember it, and it cannot be typed
wrong — which matters because it is the one number on the console a reader sees
before anything else, and a stale one quietly says the deploy did not land.

**It is stamped into the source and travels through GitHub like code.** That is
the whole reason this module writes files rather than a database row. Per
`CLAUDE.md` §1 the host's checkout is reset to the pushed commit on every poll, so
a version written on the host is reverted within two minutes; and a version stored
in the database would be a label that can disagree with the code actually running,
which is the confusion `webui.server.deployed_commit` exists to prevent. Stamped
into `tracker/__init__.py`, the number on the header is necessarily the number of
the commit serving it.

**It is one behind, always, and that is not worth fixing.** Stamping is itself a
commit, so a stamp made at 190 commits writes `1.9.0` and then becomes commit 191.
Closing that gap would mean writing the file from a commit hook or rewriting a
commit after making it, and both are worse than a label that trails its own repo
by one. The next deploy stamps again.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

#: Where the number is written. Both, always: `pyproject.toml` is what an install
#: reports and `tracker/__init__.py` is what the console reads, and a reader who
#: finds them disagreeing cannot tell which one is the version.
_INIT: Final = "tracker/__init__.py"
_PYPROJECT: Final = "pyproject.toml"

_INIT_LINE: Final = re.compile(r'^(__version__\s*=\s*)"[^"]*"', re.MULTILINE)
#: Anchored to the `[project]` table's own `version`, because `requires-python`
#: and a dependency pin are not the version and a looser pattern would find them.
#:
#: **Multiline only, never DOTALL.** The first version of this carried `(?ms)`, and
#: with `.` matching newlines the `(?:(?!^\[).*\n)*?` walk became catastrophic
#: backtracking — `tracker version --stamp` hung rather than failing, which is the
#: worst way for a regex to be wrong. `[^\n]*` says the same thing and cannot.
_PYPROJECT_LINE: Final = re.compile(
    r'^(\[project\][^\n]*\n(?:(?!\[)[^\n]*\n)*?version\s*=\s*)"[^"]*"', re.MULTILINE
)

VERSION: Final = re.compile(r"^\d+\.\d+\.\d+$")


def from_commit_count(count: int) -> str:
    """`190 -> "1.9.0"`. The digits, with dots before the last two.

    Deliberately not `count // 100` arithmetic spelled out three times: the rule a
    reader has to check is "put dots in the number", and writing it as string
    surgery is the version of it they can check at a glance.
    """
    if count < 0:
        raise ValueError(f"a commit count cannot be negative: {count}")
    digits = f"{count:03d}"
    return f"{int(digits[:-2])}.{digits[-2]}.{digits[-1]}"


def commit_count(root: Path | None = None) -> int | None:
    """How many commits are behind `HEAD`, or None outside a checkout.

    None rather than an exception: a tarball install has no `.git`, and the CLI
    has to be able to say so plainly rather than traceback. Counts `HEAD` and not
    `origin/main`, because the thing being stamped is what is about to be pushed.
    """
    root = root or repo_root()
    try:
        done = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        return int(done.stdout.strip())
    except ValueError:
        return None


def repo_root() -> Path:
    """The checkout this package lives in — the directory holding `pyproject.toml`."""
    return Path(__file__).resolve().parent.parent


def stamp(version: str, root: Path | None = None) -> list[Path]:
    """Write `version` into both files. Returns the ones that actually changed.

    Returning only the changed files is what lets the deploy step say "nothing to
    commit" rather than making an empty commit every time somebody runs it.
    """
    if not VERSION.match(version):
        raise ValueError(f"not a three-part version: {version!r}")

    root = root or repo_root()

    # Both rewrites are computed before either is written. The first version wrote
    # as it went and left `tracker/__init__.py` stamped and `pyproject.toml` not
    # when the second pattern failed to match — two files that disagree about the
    # version, which is the one state this function exists to prevent.
    pending: list[tuple[Path, str, str]] = []
    for name, pattern in ((_INIT, _INIT_LINE), (_PYPROJECT, _PYPROJECT_LINE)):
        path = root / name
        before = path.read_text(encoding="utf-8")
        after, hits = pattern.subn(rf'\g<1>"{version}"', before, count=1)
        if not hits:
            raise ValueError(f"no version line found in {name}")
        pending.append((path, before, after))

    changed: list[Path] = []
    for path, before, after in pending:
        if after != before:
            path.write_text(after, encoding="utf-8")
            changed.append(path)
    return changed


def stamped(root: Path | None = None) -> str | None:
    """The version currently written in the source, read without importing it."""
    root = root or repo_root()
    found = _INIT_LINE.search((root / _INIT).read_text(encoding="utf-8"))
    if found is None:
        return None
    quoted = re.search(r'"([^"]*)"', found.group(0))
    return quoted.group(1) if quoted else None


__all__ = [
    "VERSION",
    "commit_count",
    "from_commit_count",
    "repo_root",
    "stamp",
    "stamped",
]
