"""What the next word could be, given what has been typed so far.

A pure function over the catalog and the line, so the interesting part — "after
`sync --prospect ` the next word is a number, and after `coverage --kind ` it is one
of four strings" — is testable without a terminal.

**Why completion rather than a form.** The run pane used to answer "what can this
command take" with a table of every flag, which is right for reading and wrong for
typing: on `sync` that is twenty rows of the screen to learn one flag name. The
same knowledge as completions costs no height at all until the moment it is wanted,
which is what makes room for the output.

Four kinds of candidate, decided by where the cursor is:

1. **A command name**, while the first word or two are still being typed. Whole
   names, so `ing` offers `ingest crawl` rather than a group that cannot run.
2. **A flag**, once a command is resolved and the word starts with `-`. Flags
   already present in the line are dropped: `--limit` twice is refused downstream,
   so offering it twice is offering a mistake.
3. **A choice**, when the previous word is a flag with a closed vocabulary. These
   are the values `build_argv` would otherwise reject.
4. **A positional value**, from the caller. Project ids for the commands that take
   one and rostered operator names for `prospect` — the two cases where the next
   word is a fact about this database rather than a word from the CLI, and the
   place a terminal interface can help in a way a shell cannot.

Nothing here writes or runs anything, and a completion is only ever text put in a
box: the line still goes through `catalog.parse_command_line` and
`catalog.build_argv`, so completing wrongly costs a rejection message and never a
bad argument.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from tracker.webui.catalog import Command, Flag

#: How many candidates to keep. The list shows a few and scrolls; a hundred
#: matches for an empty prefix is a wall, not a suggestion.
MAX_CANDIDATES = 40

#: Names a positional carries when it means "a project id". Read from the CLI's own
#: parameter names rather than a list of commands, so `tracker point` and anything
#: added later are covered without an entry here.
_PROJECT_POSITIONALS = ("project_id", "project_ids")

#: Ditto for the operator roster.
_OPERATOR_POSITIONALS = ("operator",)


@dataclass(frozen=True)
class Candidate:
    """One suggestion: what to insert, and what to show beside it."""

    text: str
    #: A short right-hand hint — a type, a default, a company name. Display only.
    hint: str = ""
    #: `command`, `flag`, `choice` or `value`. Drives the colour, and lets a test
    #: assert *why* something was offered.
    kind: str = "command"

    @property
    def label(self) -> str:
        return f"{self.text}  {self.hint}".rstrip()


@dataclass(frozen=True)
class Completions:
    """The candidates, and the word they would replace."""

    prefix: str = ""
    items: tuple[Candidate, ...] = ()
    #: The flag whose value is being typed, when that is what is happening. The
    #: pane shows its help line, which is the compact replacement for the table.
    context: Flag | None = None
    command: Command | None = None

    def __bool__(self) -> bool:
        return bool(self.items)

    def apply(self, line: str, candidate: Candidate) -> str:
        """The line with the word under the cursor replaced by this candidate.

        A trailing space is added, because the next thing a person types is always
        another word — and for a multi-word command name that space is what lets
        the flag completions start immediately.
        """
        head = line[: len(line) - len(self.prefix)] if self.prefix else line
        return f"{head}{candidate.text} "


@dataclass
class _Line:
    """The typed line, split into what is settled and what is being typed."""

    words: list[str] = field(default_factory=list)
    current: str = ""

    @classmethod
    def parse(cls, line: str) -> _Line:
        # No shlex here: this runs on every keystroke over half-typed text, where an
        # unbalanced quote is normal rather than an error. `parse_command_line` does
        # the real tokenising when the line is submitted.
        stripped = line.lstrip()
        parts = stripped.split()
        if not stripped or stripped.endswith((" ", "\t")):
            return cls(words=parts, current="")
        return cls(words=parts[:-1], current=parts[-1])


def _resolve(words: Sequence[str], commands: dict[str, Command]) -> tuple[Command | None, int]:
    """The command the settled words name, and how many words it used.

    Longest match first, so `ingest crawl` is not read as `ingest` with a stray
    positional — the same rule `catalog.parse_command_line` applies.
    """
    for size in (3, 2, 1):
        if len(words) >= size:
            candidate = " ".join(words[:size])
            if candidate in commands:
                return commands[candidate], size
    return None, 0


def default_text(flag: Flag) -> str:
    """``=45`` for a flag with a default, empty for one without.

    The membership test this replaces — ``flag.default in (None, False, "")`` —
    swallowed every default of ``0``, because ``0 == False``. So `--prospect`,
    `--enrich` and `--select`, whose default is 0 and whose whole meaning is "off
    unless you pass a number", rendered as a bare `=`.
    """
    default = flag.default
    if default is None or default is False or default == "":
        return ""
    return f"={default}"


def _flag_hint(flag: Flag) -> str:
    if flag.kind == "bool":
        return "switch"
    if flag.choices:
        return "|".join(flag.choices)
    return f"{flag.kind}{default_text(flag)}"


def complete(
    line: str,
    commands: dict[str, Command],
    *,
    values_for: Callable[[Command, Flag], Sequence[tuple[str, str]]] | None = None,
) -> Completions:
    """Candidates for the word being typed at the end of `line`."""
    typed = _Line.parse(line)
    command, used = _resolve(typed.words, commands)

    if command is None:
        return _command_names(typed, commands)

    given = {word for word in typed.words[used:] if word.startswith("-")}
    previous = typed.words[-1] if typed.words else ""
    by_name = {flag.name: flag for flag in command.flags}

    # A value for the flag just named. Checked before flags so `--kind ` offers
    # `neocloud` rather than every other option.
    awaiting = by_name.get(previous)
    if awaiting is not None and awaiting.kind != "bool" and not typed.current.startswith("-"):
        return _values(typed, command, awaiting)

    if typed.current.startswith("-"):
        items = tuple(
            Candidate(flag.name, _flag_hint(flag), "flag")
            for flag in command.flags
            if not flag.positional
            and flag.name not in given
            and flag.name.startswith(typed.current)
        )
        return Completions(typed.current, items[:MAX_CANDIDATES], None, command)

    # A positional, which is where this can offer facts rather than vocabulary.
    positional = next((f for f in command.flags if f.positional), None)
    if positional is not None and values_for is not None:
        supplied = values_for(command, positional)
        items = tuple(
            Candidate(text, hint, "value")
            for text, hint in supplied
            if text.lower().startswith(typed.current.lower())
        )
        if items:
            return Completions(typed.current, items[:MAX_CANDIDATES], positional, command)

    # Nothing to add: a free-text positional, or a value only the operator knows.
    return Completions(typed.current, (), positional, command)


def _command_names(typed: _Line, commands: dict[str, Command]) -> Completions:
    """Whole command names matching everything typed so far.

    The prefix is the *whole* line rather than the last word, because a command
    name can contain a space: after `ingest cr`, the word being completed is
    "ingest cr" and the candidate is "ingest crawl".
    """
    text = " ".join([*typed.words, typed.current]).strip()
    matches = [name for name in sorted(commands) if name.startswith(text)]
    if not matches and text:
        # Nothing starts with it; offer anything that contains it, so `crawl` still
        # finds `ingest crawl`. Ordered so prefix matches never lose to these.
        matches = [name for name in sorted(commands) if text in name]
    items = tuple(
        Candidate(name, commands[name].help[:58], "command") for name in matches[:MAX_CANDIDATES]
    )
    return Completions(text, items, None, None)


def _values(typed: _Line, command: Command, flag: Flag) -> Completions:
    if flag.choices:
        items = tuple(
            Candidate(choice, "", "choice")
            for choice in flag.choices
            if choice.startswith(typed.current)
        )
        return Completions(typed.current, items[:MAX_CANDIDATES], flag, command)
    return Completions(typed.current, (), flag, command)


def project_values(projects: Sequence[dict]) -> list[tuple[str, str]]:
    """Project ids, labelled with what they are. Biggest first.

    Biggest rather than lowest-id: `enrich ` with 300 rows to choose from is most
    likely to want a campus somebody has heard of, and the label is what makes an
    id selectable at all — nobody remembers that 430 is Project Matador.
    """
    ordered = sorted(projects, key=lambda p: -(p.get("mw_planned") or 0))
    return [
        (
            str(project.get("id")),
            f"{project.get('company') or '?'} — {project.get('name') or '?'}"[:52],
        )
        for project in ordered
        if project.get("id") is not None
    ]


def operator_values(coverage) -> list[tuple[str, str]]:
    """Rostered operator names, the ones with no rows first.

    Same argument as the ordering in `roster.hunt_order`: the absent ones are why
    somebody is typing `prospect` at all.
    """
    if coverage is None:
        return []
    order = {"absent": 0, "thin": 1, "covered": 2}
    rows = sorted(coverage.rows, key=lambda r: (order.get(r.status, 3), r.name))
    return [
        (
            row.name if " " not in row.name else f'"{row.name}"',
            f"{row.status}, {row.projects} row(s)",
        )
        for row in rows
    ]


def value_provider(
    projects: Sequence[dict], coverage
) -> Callable[[Command, Flag], list[tuple[str, str]]]:
    """Positional suggestions, keyed on the CLI's own parameter names.

    Keyed on the parameter rather than on a list of commands, so `tracker point`
    and anything added later are covered without an entry here. A positional this
    does not recognise gets nothing, which is the right answer for a URL or a
    free-text name.
    """

    def provide(_command: Command, flag: Flag) -> list[tuple[str, str]]:
        if flag.name in _PROJECT_POSITIONALS:
            return project_values(projects)
        if flag.name in _OPERATOR_POSITIONALS:
            return operator_values(coverage)
        return []

    return provide


__all__ = [
    "MAX_CANDIDATES",
    "Candidate",
    "Completions",
    "complete",
    "default_text",
    "operator_values",
    "project_values",
    "value_provider",
]
