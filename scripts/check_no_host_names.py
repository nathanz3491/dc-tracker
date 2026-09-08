"""Refuse to let the production host's identity into a tracked file.

This repo is public. `CLAUDE.md` §6 forbids naming the production host anywhere
in it — no hostname, no domain, no ssh alias, no launchd label, no deploy-key
filename — in code, comments, tests, changelog or docs. Where a doc must refer to
the host it says `$PROD`; where code needs the real value it reads `.env`.

**Why a script rather than the one-line grep it replaces.** The rule used to be
enforced by a `git ls-files | xargs grep` pinned in `CLAUDE.md`, excluding
`CLAUDE.md` itself because the line names the patterns it forbids. That worked
until the patterns needed to appear in a second place, and it had already failed
once in a way worth remembering: the pattern list looked for the alias only in
`ssh <alias>` form, so the same alias sat in a tracked file for weeks as a
default value, in the very script whose job is talking to that host, and the
check passed every time. One list, in one file that can exclude itself, is
harder to get wrong than a regex copied into prose.

Run it before pushing:

    python scripts/check_no_host_names.py

Silence and exit 0 means clean. Any hit is printed with its file and line.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: Files allowed to contain these patterns, because their subject IS the rule.
#: Both spell out what must not appear elsewhere, so matching themselves is not a
#: leak — it is the documentation of the leak.
EXEMPT = {
    "CLAUDE.md",
    "scripts/check_no_host_names.py",
}

#: What the production host's identity looks like. Each entry is a regex and the
#: reason it is here, printed on a hit so the fix is obvious rather than a puzzle.
PATTERNS: list[tuple[str, str]] = [
    (r"mastri", "the host's domain and the prefix of its launchd labels"),
    (r"Mac ?mini|Macmini", "the machine's model, which identifies it"),
    (r"(?i)\bthe mini\b", "the nickname this project used for the host for months"),
    (r"\bssh\s+mm\b", "the ssh alias, in the form it is usually typed"),
    (
        r"(?i)host\s*=\s*[\"']?mm\b",
        "the ssh alias as a default value -- the form that leaked once already",
    ),
    (
        r"dctracker\.(poll|serve|notify|overnight)",
        "a launchd label; read TRACKER_SERVE_LABEL from .env instead",
    ),
]


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line and line not in EXEMPT]


def main() -> int:
    compiled = [(re.compile(pattern), why) for pattern, why in PATTERNS]
    hits = 0
    for name in tracked_files():
        path = Path(name)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # a submodule or a path git knows and the filesystem does not
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern, why in compiled:
                if pattern.search(line):
                    hits += 1
                    print(f"{name}:{lineno}: {line.strip()[:100]}")
                    print(f"    {why}")
    if hits:
        print()
        print(f"{hits} leak(s). A tracked file names the production host.")
        print("Redact it ($PROD, <app-id>, <deploy-key>, console.example in tests),")
        print("or read the value from .env the way scripts/prod.py does.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
