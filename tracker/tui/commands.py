"""Every command the CLI has, and the output of the one you ran.

**The list is read out of the CLI, never written down here.** `webui.catalog`
introspects the live Typer app, so a command added to `cli.py` — with its real
flags, types, defaults, choices and help — appears in this pane the next time the
TUI starts. That is the same argument the console's palette rests on, and a test
asserts the two offer the same set: a hand-maintained list is one that silently
falls behind, and "the TUI has all the CLI's functions" has to be a property of the
code rather than a promise.

**Runs go through `webui.runner`.** Nothing here builds a command line out of the
text you typed: `catalog.parse_command_line` turns it into a `(command, flags)`
pair, `catalog.build_argv` validates that pair against the catalog and returns an
argv list, and the subprocess is spawned with no shell anywhere in between. So
`;`, backticks and pipes are not dangerous here — they are words no command has,
and they are refused by name.

**The confirmation ritual is the console's, unchanged.** Anything that spends LLM
tokens or destroys rows needs its own name typed back. Making that a keystroke in
this pane because a terminal feels more expert would be a second, laxer gate on
the same losses.
"""

from __future__ import annotations

import queue
from typing import Any, ClassVar

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from tracker.webui import catalog
from tracker.webui import runner as runner_mod

#: Cost markers in the list. `llm` spends money; `destroys` cannot be undone.
COST_MARK = {"llm": ("$", "yellow"), "free": (" ", "dim")}


class CommandsPane(Vertical):
    """Pick a command, see its whole surface, run it, watch it."""

    BINDINGS: ClassVar = [
        ("ctrl+k", "cancel_run", "cancel the run"),
    ]

    class DataChanged(Message):
        """A run changed rows, so every read pane is now stale."""

    def __init__(self, db_path: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._db_path = db_path
        self._runner: runner_mod.Runner | None = None
        self._commands: dict[str, catalog.Command] = {}
        self._order: list[str] = []
        self._pending: tuple[str, dict[str, Any]] | None = None

    # --- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="commands-top"):
            with Vertical(id="commands-left"):
                yield Input(placeholder="search commands", id="command-search")
                yield OptionList(id="command-list")
            yield VerticalScroll(Static(id="command-detail"), id="command-detail-wrap")
        yield Input(
            placeholder="tracker … — e.g. `sync --prospect 3`, `enrich 93`, `coverage --kind neocloud`",
            id="command-line",
        )
        yield Static(id="command-status")
        yield RichLog(id="command-log", highlight=False, markup=False, wrap=False, max_lines=4000)

    def on_mount(self) -> None:
        self._runner = runner_mod.Runner(self._db_path)
        self.reload_catalog()

    # --- the catalog ------------------------------------------------------

    def reload_catalog(self) -> None:
        self._commands = catalog.by_name()
        self.fill_list()

    def fill_list(self, needle: str = "") -> None:
        """Grouped as the console groups them, filtered on name and help."""
        option_list = self.query_one("#command-list", OptionList)
        option_list.clear_options()
        self._order = []
        needle = needle.strip().lower()

        grouped = list(catalog.GROUPS)
        seen = {name for _, names in grouped for name in names}
        rest = tuple(sorted(name for name in self._commands if name not in seen))
        for label, names in [*grouped, ("Other", rest)]:
            matching = [
                name
                for name in names
                if name in self._commands
                and (
                    not needle
                    or needle in name.lower()
                    or needle in self._commands[name].help.lower()
                )
            ]
            if not matching:
                continue
            # `add_option(None)` is how Textual 8 draws a separator; the class
            # that used to do it is gone.
            option_list.add_option(None)
            option_list.add_option(Option(Text(label.upper(), style="bold dim"), disabled=True))
            for name in matching:
                option_list.add_option(Option(self._label(self._commands[name]), id=name))
                self._order.append(name)
        if self._order:
            self.show_command(self._order[0])

    def _label(self, command: catalog.Command) -> Text:
        mark, style = COST_MARK.get(command.cost, (" ", "dim"))
        line = Text(f"{mark} ", style=style)
        line.append(command.name, style="bold" if command.cost == "llm" else "")
        if command.destroys:
            line.append("  deletes", style="red")
        if command.blocked:
            line.append("  terminal only", style="dim")
        return line

    def show_command(self, name: str) -> None:
        command = self._commands.get(name)
        if command is None:
            return
        head = Text()
        head.append(f"tracker {command.name}\n", style="bold")
        head.append(command.help or "", style="")
        if command.cost == "llm":
            head.append("\n\nspends LLM tokens — needs the name typed back", style="yellow")
        if command.destroys:
            head.append(f"\n{command.destroys}", style="red")
        if command.blocked:
            head.append(f"\ncannot run from here: {command.blocked}", style="dim")

        table = Table(box=None, padding=(0, 1), show_header=True, header_style="dim")
        table.add_column("flag")
        table.add_column("type")
        table.add_column("default")
        table.add_column("what it does", style="dim")
        for flag in command.flags:
            default = "" if flag.default in (None, False, "") else str(flag.default)
            kind = flag.kind + ("[]" if flag.repeatable else "")
            if flag.choices:
                kind = "|".join(flag.choices)
            table.add_row(
                Text(flag.name, style="cyan" if not flag.positional else "magenta"),
                Text(kind),
                Text(default),
                Text((flag.help or "")[:70]),
            )
        if not command.flags:
            table.add_row(Text("—", style="dim"), Text(""), Text(""), Text("no options"))

        self.query_one("#command-detail", Static).update(Group(head, Text(), table))
        line = self.query_one("#command-line", Input)
        line.value = f"{command.name} "

    # --- events -----------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "command-search":
            self.fill_list(event.value)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id:
            self.show_command(event.option.id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.show_command(event.option.id)
            self.query_one("#command-line", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "command-line":
            self.submit(event.value)
        elif event.input.id == "command-confirm":
            self.confirm(event.value)

    # --- running ----------------------------------------------------------

    def submit(self, text: str) -> None:
        """Parse, then either run or ask for the confirmation first."""
        try:
            cmd, flags = catalog.parse_command_line(text)
        except catalog.InvalidRequest as exc:
            self._status(Text(str(exc), style="red"))
            return
        command = self._commands.get(cmd)
        if command is None:
            self._status(Text(f"unknown command {cmd!r}", style="red"))
            return
        if command.blocked:
            self._status(Text(f"{cmd} cannot run from here: {command.blocked}", style="yellow"))
            return
        if command.needs_confirmation:
            self._pending = (cmd, flags)
            why = command.destroys or "spends LLM tokens."
            self._status(Text(f"`{cmd}` {why}  Type the command name to run it.", style="yellow"))
            self._ask_confirmation()
            return
        self._start(cmd, flags, confirm=None)

    def _ask_confirmation(self) -> None:
        existing = self.query("#command-confirm")
        if existing:
            existing.first(Input).focus()
            return
        confirm = Input(placeholder="type the command name to confirm", id="command-confirm")
        self.mount(confirm, after=self.query_one("#command-line", Input))
        confirm.focus()

    def confirm(self, typed: str) -> None:
        if self._pending is None:
            return
        cmd, flags = self._pending
        self._start(cmd, flags, confirm=typed.strip())

    def _clear_confirmation(self) -> None:
        self._pending = None
        for widget in self.query("#command-confirm"):
            widget.remove()

    def _start(self, cmd: str, flags: dict[str, Any], *, confirm: str | None) -> None:
        if self._runner is None:
            return
        log = self.query_one("#command-log", RichLog)
        try:
            run = self._runner.start(cmd, flags, confirm=confirm)
        except runner_mod.Busy as exc:
            self._status(Text(str(exc), style="yellow"))
            return
        except catalog.InvalidRequest as exc:
            self._status(Text(str(exc), style="red"))
            return
        self._clear_confirmation()
        log.write(Text(f"$ tracker {cmd}", style="bold cyan"))
        self._status(Text(f"running {cmd} …", style="cyan"))
        self._pump(run.id)

    @work(thread=True, exclusive=True)
    def _pump(self, run_id: str) -> None:
        """Drain the runner's event queue on a thread, never on the event loop.

        `subscribe` hands out a bounded queue that drops rather than blocks, which
        is the property that matters: a TUI busy redrawing must not be able to
        stall a four-minute sync.
        """
        if self._runner is None:
            return
        stream = self._runner.subscribe()
        try:
            while True:
                try:
                    event = stream.get(timeout=0.5)
                except queue.Empty:
                    current = self._runner.current
                    if current is None or current.status != "running":
                        break
                    continue
                if event.get("type") == "line":
                    self.app.call_from_thread(self._append, str(event.get("line") or ""))
                elif event.get("type") == "end":
                    self.app.call_from_thread(self._finished, event.get("run") or {})
                    break
        finally:
            self._runner.unsubscribe(stream)

    def _append(self, line: str) -> None:
        # from_ansi rather than plain text: the child is given FORCE_COLOR so its
        # own signalling survives — red is a rejection, amber is 待确认, dim is a
        # hint — and throwing the escapes away would discard exactly the part a
        # reader is scanning for.
        self.query_one("#command-log", RichLog).write(Text.from_ansi(line))

    def _finished(self, summary: dict[str, Any]) -> None:
        status = str(summary.get("status") or "?")
        style = {"ok": "green", "failed": "red", "cancelled": "yellow"}.get(status, "dim")
        touched = summary.get("projects_touched")
        line = Text(f"{summary.get('cmd', '')} {status}", style=style)
        if summary.get("duration_s") is not None:
            line.append(f"  {summary['duration_s']}s", style="dim")
        if touched:
            line.append(f"  {touched} project(s) changed", style="dim")
        self._status(line)
        self.query_one("#command-log", RichLog).write(line)
        # A run that wrote rows makes every read pane stale, and a stale pane beside
        # a fresh log is how somebody reads last hour's number as this minute's.
        if touched:
            self.post_message(self.DataChanged())

    def action_cancel_run(self) -> None:
        if self._runner is not None and self._runner.cancel():
            self._status(Text("cancelling …", style="yellow"))
        else:
            self._status(Text("nothing is running", style="dim"))

    def prefill(self, text: str) -> None:
        """Put a command line in the box, ready to be edited or run."""
        line = self.query_one("#command-line", Input)
        line.value = text
        line.focus()

    def _status(self, text: Text) -> None:
        self.query_one("#command-status", Static).update(text)


__all__ = ["CommandsPane"]
