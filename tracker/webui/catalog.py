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
#: `logic` is here even though its default run is free, and that is the rule
#: working rather than an over-reaction. The gate is on the command name and never
#: on its flags — `--read 50` spends fifty calls, so the command can spend, so it
#: confirms. A gate that reads arguments is a gate with a bypass in it.
LLM_COMMANDS: frozenset[str] = frozenset(
    {
        "sync",
        "enrich",
        "infer",
        "search",
        "point",
        "logic check",
        "ingest crawl",
        "ingest edgar",
        "backfill",
    }
)

#: Commands that destroy data, and the sentence shown before one is started.
#:
#: A separate axis from :data:`LLM_COMMANDS` rather than another entry in it,
#: because the two guard different losses and a reader should not have to be told
#: `merge` "spends LLM tokens" when it spends none. The *mechanism* is shared —
#: both need the command name typed back — and that is the point: the console has
#: one confirmation ritual, used for both kinds of irreversibility.
#:
#: `merge` folds rows together and deletes the ones folded in. Every field on the
#: survivor is then recomputed from the combined citations, so nothing a source
#: said is lost — but the row numbers are gone and there is no undo.
DESTRUCTIVE: dict[str, str] = {
    "merge": "deletes the rows it folds in. There is no undo.",
    # Not destruction — it writes values derived from citations that stay exactly
    # where they are, and running it twice changes nothing the second time. It is
    # here because it is the only command that rewrites fields across the whole
    # database at once, and a bulk write deserves the same deliberate pause as a
    # deletion. Over-warning is the safe direction; the sentence says what it
    # really does rather than implying loss.
    "logic resolve": "rewrites every row whose stored values its own sources no longer support.",
}

#: Commands the console will not run at any cost, with the reason shown in the UI.
#: They remain visible in the palette with their argv, so an operator can copy the
#: line into a terminal — hiding them would just be confusing.
BLOCKED: dict[str, str] = {
    "serve": "already running",
    # A console that can publish itself is a console that can be told to publish
    # itself. The command blocks forever running a tunnel, so it would also hold
    # the single run slot until the timeout — but the reason it is here is the
    # first one: putting this page on the public internet is a decision for
    # somebody at a terminal, not a button on the page.
    "cloudflare": "run it from a terminal — publishing this page is not a click",
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
#:
#: Hand-written, unlike everything else here, because there is nothing in Typer
#: that knows `exposure` is an inspection and `merge` is a repair. The cost of
#: that is real and worth naming: a command left out of this table still appears,
#: but in an "Other" bucket at the bottom, which is where the four commands added
#: with `capex` and `duplicates` sat until they were listed.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("The loop", ("sync", "discover", "enrich", "search", "point")),
    (
        "Load data",
        ("ingest manual", "ingest pjm", "ingest crawl", "ingest edgar", "ingest geo"),
    ),
    (
        "Inspect",
        ("list", "show", "risks", "exposure", "capex", "stats", "gaps", "queue"),
    ),
    ("Judge", ("review", "verify", "infer", "logic check")),
    ("Repair", ("duplicates", "merge", "logic resolve")),
    ("Maintain", ("init", "backfill", "export")),
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
    #: Typer's `list[str]` options, which the CLI accepts more than once
    #: (`--url A --url B`), and variadic positionals, which take any number of
    #: values in a row (`merge 4 7 9`). A request may send a list for these and
    #: only these.
    repeatable: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "f": self.name,
            "t": self.kind,
            "positional": self.positional,
            "req": self.required,
            "d": self.default,
            "h": self.help,
            "o": list(self.choices),
            "many": self.repeatable,
        }


@dataclass(frozen=True)
class Command:
    name: str  # "sync" or "ingest crawl"
    help: str
    cost: str  # free | llm
    flags: tuple[Flag, ...]
    blocked: str | None = None
    #: What this command destroys, or None. Orthogonal to `cost`.
    destroys: str | None = None

    @property
    def needs_confirmation(self) -> bool:
        """Whether the runner requires the command name typed back."""
        return self.cost == "llm" or self.destroys is not None

    def as_json(self) -> dict[str, Any]:
        return {
            "cmd": self.name,
            "desc": self.help,
            "cost": self.cost,
            "blocked": self.blocked,
            "destroys": self.destroys,
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
                # Click marks a `list[str]` option `multiple` and a variadic
                # positional `nargs=-1`. Read both rather than keeping a
                # hand-written list, so a new repeatable parameter works without
                # anyone remembering this file exists.
                repeatable=(
                    bool(getattr(param, "multiple", False)) or getattr(param, "nargs", 1) == -1
                ),
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
                destroys=DESTRUCTIVE.get(full),
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


def parse_command_line(text: str) -> tuple[str, dict[str, Any]]:
    """Turn a typed line into ``(command, flags)`` this catalog recognises.

    For the console's command box. It looks like a terminal and is deliberately
    not one: **there is no shell here at any point**. This produces the same
    `(cmd, flags)` pair the form produces, `build_argv` turns that into the same
    validated argument list, and everything outside the catalog — `cd`, `rm`, a
    pipe, a semicolon, a backtick — is a word the catalog has never heard of and
    is refused by name. A leading `tracker` is accepted and ignored, because
    people will type it.

    Quoting is handled with `shlex` in POSIX mode purely as a *tokeniser*: it is
    what splits `--name "Stargate Abilene"` into two words. None of the shell
    behaviour `shlex` knows about is applied to the result.

    Boolean flags take no value. Everything else consumes the next token, and a
    flag given twice becomes a list — which `build_argv` accepts only where the
    CLI itself is repeatable, so `--url a --url b` works and `--limit 1 --limit 2`
    is refused there rather than silently taking one.
    """
    import shlex

    try:
        tokens = shlex.split(text.strip(), comments=False, posix=True)
    except ValueError as exc:  # an unbalanced quote
        raise InvalidRequest(f"unbalanced quotes: {exc}") from None
    if not tokens:
        raise InvalidRequest("nothing to run")
    if tokens[0] == "tracker":
        tokens = tokens[1:]
    if not tokens:
        raise InvalidRequest("nothing to run after `tracker`")

    commands = by_name()
    # Longest match first, so `ingest crawl` is not read as `ingest` with a stray
    # positional and `logic check` is not read as `logic`.
    name = None
    for size in (3, 2, 1):
        candidate = " ".join(tokens[:size])
        if candidate in commands:
            name, tokens = candidate, tokens[size:]
            break
    if name is None:
        guess = _closest(tokens[0], commands)
        hint = f" Did you mean `{guess}`?" if guess else ""
        raise InvalidRequest(f"there is no `{tokens[0]}` command.{hint}")

    command = commands[name]
    known = {f.name: f for f in command.flags}
    positionals = [f for f in command.flags if f.positional]
    flags: dict[str, Any] = {}

    def remember(key: str, value: Any) -> None:
        if key in flags:
            existing = flags[key] if isinstance(flags[key], list) else [flags[key]]
            flags[key] = [*existing, value]
        else:
            flags[key] = value

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            key, _, inline = token.partition("=")
            flag = known.get(key)
            if flag is None:
                guess = _closest(key, known)
                hint = f" Did you mean `{guess}`?" if guess else ""
                raise InvalidRequest(f"`{name}` has no `{key}` option.{hint}")
            if flag.kind == "bool":
                if inline:
                    raise InvalidRequest(f"`{key}` is a switch and takes no value")
                flags[key] = True
                index += 1
                continue
            if inline:
                remember(key, inline)
                index += 1
                continue
            if index + 1 >= len(tokens):
                raise InvalidRequest(f"`{key}` needs a value")
            remember(key, tokens[index + 1])
            index += 2
            continue

        if not positionals:
            raise InvalidRequest(f"`{name}` takes no arguments, so `{token}` is unexpected")
        # A variadic positional swallows the rest; otherwise fill them in order.
        target = positionals[0] if positionals[0].repeatable else positionals.pop(0)
        remember(target.name, token)
        index += 1

    return name, flags


def _closest(word: str, options) -> str | None:
    """The nearest known name, for a typo. None when nothing is close enough."""
    import difflib

    matches = difflib.get_close_matches(word, list(options), n=1, cutoff=0.7)
    return matches[0] if matches else None


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

        # A list is only ever allowed where the CLI itself takes the option more
        # than once. Anywhere else it would silently stringify to "['a', 'b']"
        # and be passed as one nonsense argument.
        if isinstance(raw_value, list | tuple):
            if not flag.repeatable:
                raise InvalidRequest(f"{raw_name} takes a single value, not a list")
            values = [str(v) for v in raw_value if str(v).strip()]
        else:
            values = [str(raw_value)]

        for value in values:
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
    "DESTRUCTIVE",
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
