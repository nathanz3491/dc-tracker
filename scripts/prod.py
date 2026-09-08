"""Run one command on the production host, from wherever you happen to be.

**One documented form, correct on every machine.** Every doc and workflow page
used to spell a writing command `ssh $PROD 'tracker enrich 42'`, which is right
from a laptop and wrong the moment you are already standing on the host — and
agents now are, in worktrees on the machine that serves the console. Two forms of
every instruction is how a page goes stale: the reader on the other machine
either translates it silently or follows it and reaches the wrong database.

So the machine difference moves out of the prose and into one variable.
`TRACKER_PROD_HOST` is an ssh alias, and **empty on the production host itself**,
meaning "here". This script reads it and either execs the command locally or
sends it over ssh:

    python scripts/prod.py tracker enrich 42
    python scripts/prod.py --restart-console
    python scripts/prod.py --where        # answer without running anything

**Why not just tell people to omit the ssh.** Because the repo is public and
`CLAUDE.md` §6 forbids naming the production host in any tracked file — no
hostname, no ssh alias, no launchd label. A committed page cannot say `ssh mm`,
and `ssh $PROD` only works if every reader has exported `$PROD`. Reading the
alias from configuration is what lets a committed instruction talk about the host
without naming it, and lets the same line be run by an agent on the host and by a
person on a laptop.

**It runs the command; it does not vet it.** The guard against a workspace
writing the production database is `~/.local/bin/tracker` on the host, outside
this repo, because a guard a bad commit could replace is not a guard. What this
script guarantees is only *where* a command runs.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path


def settings():
    """The project's own configuration, which is where the alias lives."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tracker.config import get_settings

    return get_settings()


def prod_host() -> str:
    """The ssh alias, or "" when this machine IS production."""
    return (settings().prod_host or "").strip()


def serve_label() -> str:
    label = (settings().serve_label or "").strip()
    if not label:
        raise SystemExit(
            "TRACKER_SERVE_LABEL is not set, so the console service has no name here.\n"
            "  It is per-machine and lives in .env, never in the repo (CLAUDE.md sec 6).\n"
            "  Set it on the machine that serves the console."
        )
    return label


def console_url() -> str:
    """The public URL of the console, assembled from configuration.

    Here for the same reason as everything else in this file: a committed doc has
    to be able to say "check that the console answers" without writing the
    domain down, because the domain is one of the things `CLAUDE.md` §6 keeps out
    of a public repo.
    """
    hostname = (settings().tunnel_hostname or "").strip()
    if not hostname:
        raise SystemExit(
            "TRACKER_TUNNEL_HOSTNAME is not set, so the console has no public URL here.\n"
            "  Set it on the machine that publishes, alongside TRACKER_TUNNEL_NAME."
        )
    return f"https://{hostname}/"


def describe() -> str:
    host = prod_host()
    return f"over ssh, on `{host}`" if host else "locally - this machine is production"


def prod_checkout() -> str:
    checkout = (settings().prod_checkout or "").strip()
    if not checkout:
        raise SystemExit(
            "TRACKER_PROD_CHECKOUT is not set, so there is nowhere to run this.\n"
            "  It is the path, on the production host, of the checkout that serves\n"
            "  the console - the one carrying the `.production` marker. Per-machine,\n"
            "  so it lives in .env and never in the repo (CLAUDE.md sec 6)."
        )
    return checkout


def run_there(command: str) -> int:
    """Run `command` in the production checkout, wherever that is from here.

    The command crosses a shell either way — a local `sh -c` or a remote one — so
    it is one string rather than a list, and what is inside it is the caller's to
    quote.

    **The `cd` is load-bearing, not tidiness.** An ssh hop starts in the home
    directory, and the host's `tracker` wrapper refuses to write the production
    database from anywhere outside the production checkout — so without this,
    `prod.py tracker enrich 42`, the form every doc now uses, would be refused
    every single time, and the only way anyone found to write data would be to
    reach past both safeguards. Running inside the checkout is also simply where
    a production command belongs: it is the directory whose `.env`, database and
    caches the command is meant to be using.
    """
    host = prod_host()
    line = f"cd {_cd_target()} && {command}"
    argv = ["ssh", host, line] if host else ["sh", "-c", line]
    return subprocess.run(argv).returncode


def _cd_target() -> str:
    """The checkout path, quoted for the shell that will read it.

    `~` cannot simply be quoted along with the rest: the shell expands a tilde
    only when it is unquoted, so `cd '~/dev/tracker/repo'` looks for a directory
    literally named `~`. Quoting the remainder and letting `$HOME` do the work
    keeps a path with spaces safe without breaking the common `~/…` spelling
    that belongs in a config file, since the home directory differs by machine.
    """
    checkout = prod_checkout()
    if checkout == "~":
        return '"$HOME"'
    if checkout.startswith("~/"):
        return '"$HOME"/' + shlex.quote(checkout[2:])
    return shlex.quote(checkout)


def restart_console() -> int:
    """Kick the console service, which only launchd can do and only there.

    `launchctl kickstart -k` stops and restarts in one step. The `gui/<uid>`
    domain is deliberate: the service runs in the user's GUI session, because the
    tunnel it starts holds credentials in that user's home directory.
    """
    label = serve_label()
    return run_there(f'launchctl kickstart -k "gui/$(id -u)/{label}"')


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="With no options, every remaining argument is the command to run.",
    )
    parser.add_argument(
        "--where",
        action="store_true",
        help="say where a command would run, and run nothing",
    )
    parser.add_argument(
        "--restart-console",
        action="store_true",
        help="restart the console service on production",
    )
    parser.add_argument(
        "--console-url",
        action="store_true",
        help="print the console's public URL, and run nothing",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the command to run")
    args = parser.parse_args()

    if args.where:
        print(f"commands run {describe()}")
        return 0

    if args.console_url:
        print(console_url())
        return 0

    if args.restart_console:
        return restart_console()

    if not args.command:
        parser.error("nothing to run: give a command, --restart-console, or --where")

    # A single quoted argument is already a shell line ("tracker sync --full");
    # several bare words are one too. Joining rather than re-quoting keeps both
    # `prod.py tracker enrich 42` and `prod.py 'tracker enrich 42'` working, which
    # matters because the docs use both shapes.
    command = args.command[0] if len(args.command) == 1 else shlex.join(args.command)
    return run_there(command)


if __name__ == "__main__":
    raise SystemExit(main())
