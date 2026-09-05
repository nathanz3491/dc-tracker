"""`python -m tracker <subcommand>` / `tracker <subcommand>`.

Typer + Rich: the PRD asks for formatted tables on stdout, and both are already
available. Read commands open the database **read-only**, which turns the PRD's
"never modify the DB except for ingest and review" from a convention into a
guarantee enforced by SQLite itself.

**This package is a directory of command families, and this file is only its
entrance.** `_shared.py` holds the Typer app, its sub-groups, the two consoles and
the dozen helpers every command uses; `_render.py` holds the printers more than one
family needs. Each family is a module — `logic`, `duplicates`, `ingest`, `sync`,
`enrich`, `projects`, `capacity`, `quality`, `people`, `serve` — imported below for
the side effect of registering its commands on those groups.

A new command goes in the family module that owns its subject, or starts a new one.
It does not go here. The arrow points one way: families import `_shared` and
`_render`, a family may import another family for a helper it owns, and nothing
inside the package imports `tracker.cli` itself.

`main` stays because `tracker/__main__.py`, the `tracker` console script,
`tracker/webui/catalog.py` and the TUI all import it from `tracker.cli`.
"""

from __future__ import annotations

import sys

# `tracker.cli` is the package's public face: `__main__`, the console script, the
# web console's catalog and the TUI all reach for these here, and moving a command
# into a family module must not move the name they import.
from tracker.cli._shared import BROWSER_HINT, app  # noqa: F401

# The command families, imported for the side effect of registering their commands
# on the groups `_shared` defines. The ORDER IS THE `--help` LISTING, and it is the
# order of the work rather than the alphabet: set the database up, put data in it,
# read it, settle what is wrong with it, then publish it and say who may read it.
# Typer lists commands in registration order, so moving a line here moves the help,
# which is why the block is fenced off from the import sorter.
# isort: off
from tracker.cli import quality as _quality  # noqa: F401
from tracker.cli import ingest as _ingest  # noqa: F401
from tracker.cli import sync as _sync  # noqa: F401
from tracker.cli import enrich as _enrich  # noqa: F401
from tracker.cli import projects as _projects  # noqa: F401
from tracker.cli import capacity as _capacity  # noqa: F401
from tracker.cli import logic as _logic  # noqa: F401
from tracker.cli import duplicates as _duplicates  # noqa: F401
from tracker.cli import serve as _serve  # noqa: F401
from tracker.cli import people as _people  # noqa: F401
# isort: on


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point.

    Runs in typer's standalone mode, so typer itself renders usage errors and
    raises `SystemExit` with the right code — which propagates out through
    `sys.exit(main())` unchanged. The alternative (`standalone_mode=False`)
    requires catching click's exception hierarchy, and typer 0.27 vendors click
    under the private `typer._click`, so there is nothing stable to catch.
    """
    app(args=argv)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
