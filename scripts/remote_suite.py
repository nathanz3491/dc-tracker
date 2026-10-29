"""Lint here, then run the full test suite on the production host, in a scratch clone.

    python scripts/remote_suite.py                  # lint and format here, every test there
    python scripts/remote_suite.py --check          # lint without changing a file
    python scripts/remote_suite.py -- -k cancelled  # everything after -- goes to pytest
    python scripts/remote_suite.py -- --lf          # only what failed in the last run there
    python scripts/remote_suite.py --attach         # follow the run in progress again
    python scripts/remote_suite.py --stop           # end the run in progress

Run it with the project's interpreter (`.venv/Scripts/python`, or
`../../../.venv/Scripts/python` from a worktree), which has ruff.

**Why there.** The host's CPU runs the suite several times faster than this
machine, which is in use for other work while it runs.

**Why lint stays here.** `ruff check --fix` and `ruff format` rewrite files. Run
there, the fixes would have to be copied back; run here, they are already in the
working tree, and the tree that is sent is the fixed one.

**What is tested is what you would commit**: the working tree as `git add -A` sees
it — staged or not, untracked files included, ignored ones (`.env`, `data/`) left
out. It travels as a commit object built on a throwaway index, so the real index,
HEAD, the branch and the stash are untouched, and `git push` sends only the
objects the host does not already have.

**The run outlives the connection.** It is started detached on the host and its
log followed from here; when the link drops, this reattaches at the byte where it
stopped. Ctrl-C here stops following, not the run.

**It cannot touch production.** The host half, `scripts/remote_suite.sh`, works in
`~/dev/tracker/tracker-dev` and runs the tests inside a macOS sandbox that denies
production's checkout, deployer and backups, every port already listening there,
and the internet, and it proves each of those before the first test runs.
`docs/testing-on-the-host.md` has the details.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

#: Relative to the host's home. The host half refuses any directory not named
#: tracker-dev, and any inside production's checkout, deployer or backups.
DEFAULT_DIR = "dev/tracker/tracker-dev"

#: The host has ten performance cores; the console and the nightly loop keep the
#: efficiency cores, and the tests run at low priority besides.
DEFAULT_WORKERS = 10

#: How many times a dropped link is reattached before giving up on following. The
#: run goes on regardless; `--attach` picks it up again.
REATTACH = 30

HOST_SCRIPT = Path(__file__).with_name("remote_suite.sh")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ConnectTimeout=20"]

_RESULT = re.compile(r"^SUITE-RESULT exit=(\d+) sha=(\w+) seconds=(\d+)$")
_DONE = re.compile(r"^SUITE-DONE exit=(\w+)$")
_SUMMARY = re.compile(r"^=*\s*(\d+ (passed|failed|error|errors|skipped)\b.*|no tests ran.*)$")


def git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{done.stderr.strip()}")
    return done.stdout.strip()


def lint(root: Path, *, fix: bool) -> bool:
    """ruff over the checkout, fixing what it safely can unless `fix` is False.

    True when nothing is left to report. Lint never stops the suite: a failure
    here and a failure there are both worth knowing from one run.
    """
    ruff = [sys.executable, "-m", "ruff"]
    if fix:
        steps = [[*ruff, "check", "--fix", "."], [*ruff, "format", "."]]
    else:
        steps = [[*ruff, "check", "."], [*ruff, "format", "--check", "."]]
    clean = True
    for step in steps:
        clean = subprocess.run(step, cwd=root, check=False).returncode == 0 and clean
    return clean


def snapshot(root: Path) -> tuple[str, int]:
    """The working tree as a commit: (sha, files differing from HEAD).

    Built on a copy of the index, so `git add -A` stages nothing for real. A tree
    identical to HEAD's is tested as HEAD itself.
    """
    index = Path(git("rev-parse", "--git-path", "index", cwd=root))
    if not index.is_absolute():
        index = root / index
    scratch = Path(tempfile.mkdtemp(prefix="remote-suite-"))
    try:
        temp_index = scratch / "index"
        if index.is_file():
            shutil.copyfile(index, temp_index)
        env = {**os.environ, "GIT_INDEX_FILE": str(temp_index)}
        git("add", "-A", cwd=root, env=env)
        tree = git("write-tree", cwd=root, env=env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    head = git("rev-parse", "HEAD", cwd=root)
    if tree == git("rev-parse", "HEAD^{tree}", cwd=root):
        return head, 0
    sha = git("commit-tree", tree, "-p", head, "-m", "remote suite snapshot", cwd=root)
    changed = git("diff", "--name-only", head, sha, cwd=root)
    return sha, len(changed.splitlines())


def remote_command(*args: str) -> str:
    """`bash -s -- ...` with every argument quoted for the far shell.

    ssh joins its arguments into one command line for the login shell there, so an
    unquoted `-k "a and b"` would arrive as three words.
    """
    return "bash -s -- " + " ".join(shlex.quote(a) for a in args)


def host_script() -> bytes:
    # LF whatever the checkout's line endings: bash on the host reads a CR as part
    # of every command. `.gitattributes` keeps *.sh LF already; this is the backstop.
    return HOST_SCRIPT.read_bytes().replace(b"\r\n", b"\n")


def _host_process(host: str, *args: str, stderr=subprocess.STDOUT) -> subprocess.Popen:
    sys.stdout.flush()  # what print() buffered goes out before the raw bytes that follow
    process = subprocess.Popen(
        [*SSH, host, remote_command(*args)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
    )
    assert process.stdin
    process.stdin.write(host_script())
    process.stdin.close()
    return process


def on_host(host: str, *args: str) -> int:
    """One short step of the host half, its output echoed. Returns its exit code."""
    process = _host_process(host, *args)
    assert process.stdout
    for raw in process.stdout:
        sys.stdout.buffer.write(raw)
        sys.stdout.flush()
    return process.wait()


def follow(host: str, directory: str, log=None) -> tuple[str | None, list[str]]:
    """Echo the run's log until it ends, reattaching whenever the link drops.

    Returns what the host recorded as the run's exit (`"0"`, `"1"`, `"130"`, `"lost"`
    for a run that died unrecorded), or None if following gave up first, and the
    log's lines. Only the log is counted toward the offset: the host's own messages
    go to stderr, and the closing SUITE-DONE line is not part of the log.
    """
    offset, lines, drops = 0, [], 0
    while True:
        process = _host_process(host, "follow", directory, str(offset), stderr=None)
        assert process.stdout
        for raw in process.stdout:
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            if match := _DONE.match(text):
                process.wait()
                return match.group(1), lines
            offset += len(raw)
            sys.stdout.buffer.write(raw)
            sys.stdout.flush()
            if log is not None:
                log.write(raw)
                log.flush()
            lines.append(text)
        code = process.wait()
        # 255 is ssh's own failure: the link, not the host half, which exits 2 when
        # it refuses — and a refusal does not get better by asking again.
        if code != 255 or drops >= REATTACH:
            return None, lines
        drops += 1
        print(f"\n[the link to the host dropped; reattaching ({drops})]", flush=True)
        time.sleep(5)


def result_of(lines: list[str]) -> tuple[int | None, str]:
    """(pytest's exit code, its summary line) from the run's log.

    The code is None when the run never reached pytest — an isolation check that
    failed, or an environment that would not build.
    """
    code, summary = None, ""
    for line in lines:
        if match := _RESULT.match(line):
            code = int(match.group(1))
        elif _SUMMARY.match(line.strip()):
            summary = line.strip("= ").strip()
    return code, summary


def _report(lines: list[str], ended: str | None, *, lint_clean: bool | None, log_path, started):
    suite_code, summary = result_of(lines)
    print()
    if lint_clean is not None:
        print(f"lint     {'clean' if lint_clean else 'problems left (above)'}")
    if suite_code is not None:
        print(f"suite    {summary or f'pytest exited {suite_code}'}")
    elif ended is None:
        print("suite    lost the link to the host; the run goes on there (--attach)")
    elif ended == "lost":
        print("suite    the run ended on the host without recording a result")
    else:
        print("suite    did not run: see the host's message above")
    print(f"took     {time.monotonic() - started:.0f}s in all")
    if log_path is not None:
        print(f"log      {log_path}")
    if suite_code is None:
        return 2
    if suite_code:
        return suite_code
    return 0 if lint_clean in (None, True) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="Anything after -- is passed to pytest on the host.",
    )
    parser.add_argument("--host", default=None, help="ssh host alias (default: sync_db's)")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="scratch clone on the host")
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS, help="pytest-xdist workers there"
    )
    parser.add_argument("--check", action="store_true", help="lint without fixing anything")
    parser.add_argument("--no-lint", action="store_true", help="skip lint")
    parser.add_argument("--attach", action="store_true", help="follow the run in progress")
    parser.add_argument("--stop", action="store_true", help="end the run in progress")
    argv = sys.argv[1:] if argv is None else argv
    extra: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, extra = argv[:cut], argv[cut + 1 :]
    args = parser.parse_args(argv)
    if args.host is None:
        # One place names the host's alias; it is imported here, not at the top, so
        # the tests can load this file without scripts/ on the path.
        from sync_db import DEFAULT_HOST

        args.host = DEFAULT_HOST

    started = time.monotonic()
    if args.stop:
        return on_host(args.host, "stop", args.dir)
    if args.attach:
        ended, lines = follow(args.host, args.dir)
        return _report(lines, ended, lint_clean=None, log_path=None, started=started)

    root = Path(git("rev-parse", "--show-toplevel", cwd=Path.cwd()))
    lint_clean = None
    if not args.no_lint:
        print(f"lint here ({'check only' if args.check else 'fixing what ruff can'})")
        lint_clean = lint(root, fix=not args.check)

    sha, changed = snapshot(root)
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    what = f"{branch} as committed" if not changed else f"{branch} + {changed} uncommitted file(s)"
    print(f"snapshot {sha[:10]} = {what}")

    origin = git("remote", "get-url", "origin", cwd=root)
    if on_host(args.host, "prepare", args.dir, origin) != 0:
        print("remote suite: could not prepare the scratch clone on the host")
        return 2
    pushed = subprocess.run(
        ["git", "push", "--quiet", f"{args.host}:{args.dir}", f"{sha}:refs/snapshots/{sha}"],
        cwd=root,
        check=False,
    )
    if pushed.returncode != 0:
        print("remote suite: could not send the snapshot to the host")
        return 2
    if on_host(args.host, "start", args.dir, sha, str(args.workers), *extra) != 0:
        print("remote suite: could not start the run on the host")
        return 2

    log_path = root / "data" / "runs" / "remote-suite.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("wb") as log:
            ended, lines = follow(args.host, args.dir, log)
    except KeyboardInterrupt:
        print("\nstopped following; the run goes on: --attach to follow it, --stop to end it")
        return 130
    return _report(lines, ended, lint_clean=lint_clean, log_path=log_path, started=started)


if __name__ == "__main__":
    raise SystemExit(main())
