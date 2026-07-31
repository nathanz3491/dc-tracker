"""What the console is allowed to run, and with which flags.

This is the security boundary. A request names a command and a flag dict; nothing
reaches a subprocess that is not in this catalog, and no value is interpolated into
a string. `argv` is assembled as a list and handed to `subprocess.Popen` with no
shell, so quoting, `;`, backticks and `&&` have no meaning at any point.

The catalog is read out of Typer rather than hand-written, so it cannot fall behind
the CLI: a flag added to a command appears here on the next start, with its real
type, default and help text. Only the cost tag is authored by hand, because Typer
has no way to know that `sync` spends money and `gaps` does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Commands that spend real LLM tokens. The console refuses to start one of these
#: without a matching confirmation string, so a stray click cannot cost money.
#:
#: Listed explicitly rather than inferred. A command silently gaining a cost is a
#: worse failure than one that has to be added here by hand when it does.
LLM_COMMANDS: frozenset[str] = frozenset({"sync", "enrich", "infer", "search", "ingest crawl"})

#: Commands the console will not run at any cost, with the reason shown in the UI.
#: They remain visible in the palette with their argv, so an operator can copy the
#: line into a terminal — hiding them would just be confusing.
BLOCKED: dict[str, str] = {
    "serve": "already running",
    "version": "shown in the header",
}

#: Flags that make no sense from a browser, or that would hand the request control
#: over where output goes. `--out` writes a file at a path the caller chooses; that
#: is a filesystem write with an attacker-supplied path, so the console exports
#: through its own download route instead.
BLOCKED_FLAGS: frozenset[str] = frozenset({"--out", "--rejects-out", "--db", "--data-dir"})


def _enum_hints() -> dict[str, tuple[str, ...]]:
    """Closed vocabularies for flags Typer only knows as free text.

    Several commands take a `str` and validate it themselves against a tuple in
    `vocab.py` — `--phase`, `--risk`, `--severity` — so the allowed values live in
    the help string and nowhere a form can read. Without this the console renders a
    text box, the operator types "constructing", and the run dies at argument
    parsing having taught them nothing.

    Sourced from the same tuples the CLI checks against, so the dropdown cannot
    offer a value the command would reject.
    """
    from tracker.export import FORMATS
    from tracker.ingest.iso_maps import ISO_MAPS
    from tracker.vocab import PHASES, RISK_CATEGORIES, RISK_SEVERITIES

    return {
        "--phase": tuple(PHASES),
        "--risk": tuple(RISK_CATEGORIES),
        "--severity": tuple(RISK_SEVERITIES),
        "--category": tuple(RISK_CATEGORIES),
        "--iso": tuple(sorted(ISO_MAPS)),
        "--sort": ("mw", "investment", "date", "confidence", "name"),
        "--by": ("category", "company", "state", "severity"),
        "fmt": tuple(FORMATS),
    }


#: Rendered as a section heading, in the order an operator meets them.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("The loop", ("sync", "discover", "enrich", "search")),
    ("Load data", ("ingest manual", "ingest pjm", "ingest crawl", "ingest geo")),
    ("Inspect", ("list", "show", "risks", "exposure", "stats", "gaps", "queue")),
    ("Judge", ("review", "verify", "infer")),
    ("Maintain", ("init", "export")),
)


@dataclass(frozen=True)
class Flag:
    name: str  # "--limit" or a positional's name
    kind: str  # bool | int | text | float | choice | path
    positional: bool = False
    required: bool = False
    default: Any = None
    help: str = ""
    choices: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "f": self.name,
            "t": self.kind,
            "positional": self.positional,
            "req": self.required,
            "d": self.default,
            "h": self.help,
            "o": list(self.choices),
        }


@dataclass(frozen=True)
class Command:
    name: str  # "sync" or "ingest crawl"
    help: str
    cost: str  # free | llm
    flags: tuple[Flag, ...]
    blocked: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "cmd": self.name,
            "desc": self.help,
            "cost": self.cost,
            "blocked": self.blocked,
            "flags": [f.as_json() for f in self.flags],
        }


def _kind_of(param) -> tuple[str, tuple[str, ...]]:
    """Map a click parameter type onto something a form can render.

    Duck-typed on purpose. Typer 0.27 vendors click privately as `typer._click`, so
    there is no importable `click` to isinstance against and reaching into the
    private module would break on the next typer release.
    """
    ptype = getattr(param, "type", None)
    name = (getattr(ptype, "name", "") or "").lower()
    choices = tuple(str(c) for c in (getattr(ptype, "choices", None) or ()))
    if choices:
        return "choice", choices
    if getattr(param, "is_flag", False) or name == "boolean":
        return "bool", ()
    if name in {"integer", "int"}:
        return "int", ()
    if name in {"float", "number"}:
        return "float", ()
    if name in {"path", "file", "directory"}:
        return "path", ()
    return "text", ()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _flags_for(command) -> tuple[Flag, ...]:
    hints = _enum_hints()
    out: list[Flag] = []
    for param in command.params:
        if getattr(param, "hidden", False):
            continue
        opts = list(getattr(param, "opts", ()) or ())
        positional = not any(o.startswith("-") for o in opts)
        # The long form is the stable one; `-v` can be re-lettered without notice.
        name = (
            param.name
            if positional
            else next((o for o in opts if o.startswith("--")), opts[0] if opts else param.name)
        )
        if name in {"--help", "help"} or name in BLOCKED_FLAGS:
            continue
        kind, choices = _kind_of(param)
        if not choices and name in hints:
            kind, choices = "choice", hints[name]
        out.append(
            Flag(
                name=name,
                kind=kind,
                positional=positional,
                required=bool(getattr(param, "required", False)),
                default=_jsonable(getattr(param, "default", None)),
                help=getattr(param, "help", "") or "",
                choices=choices,
            )
        )
    return tuple(out)


def _walk(group, prefix: str = "") -> list[Command]:
    out: list[Command] = []
    for name, command in sorted(getattr(group, "commands", {}).items()):
        # `_print_standing` was registered by a stray decorator once; anything whose
        # name is not a real word is a bug in the CLI, not a command to offer.
        if name.startswith(("_", "-")):
            continue
        full = f"{prefix}{name}"
        if hasattr(command, "commands"):
            out.extend(_walk(command, prefix=f"{full} "))
            continue
        out.append(
            Command(
                name=full,
                help=(command.help or "").strip().split("\n\n")[0].replace("\n", " "),
                cost="llm" if full in LLM_COMMANDS else "free",
                flags=_flags_for(command),
                blocked=BLOCKED.get(full),
            )
        )
    return out


def load() -> list[Command]:
    """Every runnable command, read out of the live Typer app."""
    import typer.main

    from tracker.cli import app

    return _walk(typer.main.get_command(app))


def by_name() -> dict[str, Command]:
    return {c.name: c for c in load()}


def grouped_json() -> list[dict[str, Any]]:
    """The catalog as the console renders it: named sections, then the rest."""
    commands = by_name()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for label, names in GROUPS:
        items = [commands[n].as_json() for n in names if n in commands]
        seen.update(n for n in names if n in commands)
        if items:
            out.append({"group": label, "items": items})
    rest = [c.as_json() for name, c in sorted(commands.items()) if name not in seen]
    if rest:
        out.append({"group": "Other", "items": rest})
    return out


class InvalidRequest(ValueError):
    """The posted command or flags are not something the catalog permits."""


def build_argv(cmd: str, flags: dict[str, Any], *, db_path: Any = None) -> list[str]:
    """Validate a request and return the argv to execute.

    Every element is produced here from the catalog, never from concatenating the
    caller's text into a command line. An unknown command or an unknown flag is an
    error rather than something to pass through — passing through is how a
    ``--db`` or an ``--out`` would arrive.

    ``db_path`` is injected by the runner, not accepted from the request: `--db`
    is in :data:`BLOCKED_FLAGS` precisely so a caller cannot point a run at
    another database, and the console must still tell the child which one it is
    serving. Without it a run resolves the default path from its own working
    directory and quietly operates on a different file than the page displays.
    """
    import sys

    commands = by_name()
    command = commands.get(cmd)
    if command is None:
        raise InvalidRequest(f"unknown command {cmd!r}")
    if command.blocked:
        raise InvalidRequest(f"{cmd} cannot be run from the console: {command.blocked}")

    known = {f.name: f for f in command.flags}
    # `--db` is a callback option, so it belongs before the subcommand.
    prefix = ["--db", str(db_path)] if db_path is not None else []
    argv: list[str] = [sys.executable, "-m", "tracker", *prefix, *cmd.split()]
    positionals: list[str] = []

    for raw_name, raw_value in (flags or {}).items():
        flag = known.get(raw_name)
        if flag is None:
            raise InvalidRequest(f"{cmd} has no flag {raw_name!r}")
        if raw_value is None or raw_value == "":
            continue
        if flag.kind == "bool":
            if bool(raw_value):
                argv.append(flag.name)
            continue
        value = str(raw_value)
        if flag.choices and value not in flag.choices:
            raise InvalidRequest(
                f"{raw_name} must be one of: {', '.join(flag.choices)} (got {value!r})"
            )
        if flag.kind in {"int", "float"}:
            try:
                float(value)
            except ValueError:
                raise InvalidRequest(f"{raw_name} must be a number (got {value!r})") from None
        if flag.positional:
            positionals.append(value)
        else:
            argv += [flag.name, value]

    missing = [
        f.name for f in command.flags if f.required and f.name not in (flags or {}) and f.positional
    ]
    if missing:
        raise InvalidRequest(f"{cmd} needs {', '.join(missing)}")

    return argv + positionals


__all__ = [
    "BLOCKED",
    "BLOCKED_FLAGS",
    "GROUPS",
    "LLM_COMMANDS",
    "Command",
    "Flag",
    "InvalidRequest",
    "build_argv",
    "by_name",
    "grouped_json",
    "load",
]
