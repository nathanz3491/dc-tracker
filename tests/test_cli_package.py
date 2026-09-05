"""The shape of the `tracker.cli` package, pinned.

`cli.py` was one module of 10,292 lines holding 63 commands. It is now a package: a
kernel every family imports, a rendering module for the printers more than one family
needs, and one module per family of commands. Two things about that arrangement are
worth a test rather than a convention, because both fail silently.

**The command tree.** Splitting a Typer app across modules means a command exists only
if its module is imported. Drop a line from the registration block in `__init__` and
nine commands vanish from `tracker --help` with nothing raising — the CLI simply has
fewer commands than it did. The full tree is listed here so that cannot happen
quietly.

**The direction of the imports.** Families import the kernel; a family may import
another family for a helper it owns; nothing inside the package imports
`tracker.cli` itself. That last rule is what keeps the package importable at all —
`__init__` imports every family, so a family importing `tracker.cli` back is a cycle
that resolves differently depending on which module Python reached first.
"""

from __future__ import annotations

import ast
from pathlib import Path

from typer.main import get_command

from tracker.cli import app

#: Every command `tracker --help` offers, sub-commands qualified by their group.
#: Captured from the module before it was split, and unchanged by the split.
COMMANDS = {
    "audit",
    "audit check",
    "audit resolve",
    "backfill",
    "blocks",
    "capex",
    "clean",
    "cloudflare",
    "coverage",
    "digest",
    "discover",
    "duplicates",
    "duplicates park",
    "duplicates parked",
    "duplicates resolve",
    "duplicates unpark",
    "enrich",
    "export",
    "exposure",
    "feeds",
    "gaps",
    "infer",
    "ingest",
    "ingest crawl",
    "ingest edgar",
    "ingest geo",
    "ingest manual",
    "ingest pjm",
    "init",
    "list",
    "logic",
    "logic check",
    "logic conflicts",
    "logic resolve",
    "merge",
    "notify",
    "notify preview",
    "notify send",
    "paths",
    "point",
    "prospect",
    "queue",
    "queue check",
    "queue prune",
    "queue stats",
    "review",
    "risks",
    "risks confirm",
    "search",
    "serve",
    "show",
    "sources",
    "sources policy",
    "stats",
    "sync",
    "tui",
    "users",
    "users add",
    "users invite",
    "users passwd",
    "users rm",
    "verify",
    "version",
    "watch",
    "watch add",
    "watch all",
    "watch rm",
}

PACKAGE = Path(__file__).resolve().parents[1] / "tracker" / "cli"


def _tree(command, prefix: str = "") -> set[str]:
    found: set[str] = set()
    for name in command.list_commands(None):
        sub = command.get_command(None, name)
        found.add(prefix + name)
        if hasattr(sub, "list_commands"):
            found |= _tree(sub, prefix + name + " ")
    return found


def test_every_command_is_still_registered():
    """A family module that stops being imported takes its commands with it."""
    assert _tree(get_command(app)) == COMMANDS


def test_the_package_entrance_still_exports_what_other_modules_import():
    """`__main__`, the console script, `webui.catalog` and the TUI all import these
    from `tracker.cli`, so moving a command must not move the name."""
    import tracker.cli as package

    assert callable(package.main)
    assert package.app is app
    assert isinstance(package.BROWSER_HINT, str)


def test_no_family_imports_the_package_it_is_imported_by():
    """The arrow points one way. `__init__` imports every family, so a family
    importing `tracker.cli` back is a cycle whose outcome depends on which module
    Python happened to reach first."""
    offenders = []
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "tracker.cli":
                offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path.name}:{node.lineno}" for a in node.names if a.name == "tracker.cli"
                ]
    assert not offenders, f"these import the package that imports them: {offenders}"


def test_the_kernel_imports_no_command_family():
    """`_shared` is the bottom of the stack. A family import here would make the
    kernel depend on the modules that depend on it."""
    source = (PACKAGE / "_shared.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tracker.cli"):
            raise AssertionError(f"_shared.py imports {node.module} at line {node.lineno}")


def test_no_family_module_is_larger_than_the_file_it_replaced():
    """A guard against the split quietly undoing itself. The old module was 10,292
    lines; nothing here should approach that."""
    biggest = max(
        (len(p.read_text(encoding="utf-8").splitlines()), p.name) for p in PACKAGE.glob("*.py")
    )
    assert biggest[0] < 2500, f"{biggest[1]} has grown to {biggest[0]} lines"
