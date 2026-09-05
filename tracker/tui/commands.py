"""Every command the CLI has, and the output of the one you ran.

**The list is read out of the CLI, never written down here.** `webui.catalog`
introspects the live Typer app, so a command added to the `tracker/cli` package — with its real
flags, types, defaults, choices and help — appears in this pane the next time the
TUI starts. That is the same argument the console's palette rests on, and a test
asserts the two offer the same set: a hand-maintained list is one that silently
falls behind, and "the TUI has all the CLI's functions" has to be a property of the
code rather than a promise.

**The output is the biggest thing in the pane, and that was a correction.** The
first version spent the top half of the screen on a table of every flag a command
takes — twenty rows on `sync` — and left the log a quarter of it. Reading output is
what this pane is *for*; the flag table was reference material sitting in the space
the answer needed. The flags are now one wrapped line, the full help appears for
whichever flag is being typed, and the log takes everything left over.

**Typing is completed rather than remembered.** `completion.complete` offers the
next word: command names, then flags, then a closed vocabulary's values, then
project ids and operator names read out of this database. Tab takes the highlighted
candidate, up and down move through them, escape dismisses. So the flag reference
arrives at the moment it is wanted and costs no height until then.

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
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from tracker.tui import completion as completion_mod
from tracker.webui import catalog
from tracker.webui import runner as runner_mod

#: Cost markers in the list. `llm` spends money; `destroys` cannot be undone.
COST_MARK = {"llm": ("$", "yellow"), "free": (" ", "dim")}

#: Characters of flag names the detail will print before it says "+N more".
#:
#: Two lines of a narrow pane, roughly. `sync` has seventeen flags and printing
#: them all wrapped to six rows — which is the whole top of a 30-row terminal, for
#: reference material that the completion list now gives on demand. The cap is what
#: keeps this block short enough for the output to have room.
FLAG_BUDGET = 110

#: Colour per candidate kind, so the list says what sort of word it is offering.
CANDIDATE_STYLE = {
    "command": "bold",
    "flag": "cyan",
    "choice": "magenta",
    "value": "green",
}


class CommandInput(Input):
    """The command line, with the completion keys bound to it.

    A subclass rather than a handler on the pane: a widget's own bindings resolve
    before the screen's, which is what lets `tab` mean "take this completion" in
    this box while still meaning "next widget" everywhere else.
    """

    BINDINGS: ClassVar = [
        Binding("tab", "take_completion", "complete", show=False, priority=True),
        Binding("down", "next_completion", "next candidate", show=False, priority=True),
        Binding("up", "previous_completion", "previous candidate", show=False, priority=True),
        Binding("escape", "dismiss_completions", "dismiss", show=False, priority=True),
        # Scrollback, reached from the prompt without leaving it — the one thing a
        # shell gives you that a text box does not.
        Binding("pageup", "scroll_output('page_up')", "scroll back", show=False, priority=True),
        Binding(
            "pagedown", "scroll_output('page_down')", "scroll forward", show=False, priority=True
        ),
        Binding("shift+up", "scroll_output('up')", "up a line", show=False, priority=True),
        Binding("shift+down", "scroll_output('down')", "down a line", show=False, priority=True),
        Binding("shift+home", "scroll_output('home')", "to the top", show=False, priority=True),
        Binding("shift+end", "scroll_output('end')", "to the bottom", show=False, priority=True),
    ]

    def action_scroll_output(self, how: str) -> None:
        self._pane().scroll_output(how)

    def action_take_completion(self) -> None:
        self._pane().take_completion()

    def action_next_completion(self) -> None:
        self._pane().move_completion(1)

    def action_previous_completion(self) -> None:
        self._pane().move_completion(-1)

    def action_dismiss_completions(self) -> None:
        """Dismiss the candidates, or — with none open — leave the box.

        Somewhere to go matters: while this input has focus it consumes `1`..`6`,
        `q` and `r`, because those are letters somebody is typing. Escape is the
        way back to keys that switch panes.
        """
        pane = self._pane()
        if pane.completions_open:
            pane.hide_completions()
        else:
            pane.query_one("#command-list").focus()

    def _pane(self) -> CommandsPane:
        for node in self.ancestors_with_self:
            if isinstance(node, CommandsPane):
                return node
        raise LookupError("CommandInput outside a CommandsPane")


class CommandsPane(Vertical):
    """Pick a command, complete it, run it, read the output."""

    BINDINGS: ClassVar = [
        Binding("ctrl+k", "cancel_run", "cancel the run"),
        Binding("ctrl+l", "clear_log", "clear the output"),
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
        self._completions = completion_mod.Completions()
        #: Supplies project ids and operator names to the completer. Replaced on
        #: every reload, so a candidate cannot name a row that has gone.
        self._values_for = None

    # --- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Fixed and small: reference material, not the answer.
        with Horizontal(id="commands-top"):
            yield OptionList(id="command-list")
            yield VerticalScroll(Static(id="command-detail"), id="command-detail-wrap")
        # Everything left over, because this is the part being read.
        #
        # `wrap=False` on purpose. The child is told this pane's width and wraps its
        # own tables and prose to it, so a line arrives already the right length;
        # wrapping again here broke every long line a second time, mid-sentence,
        # with the continuation starting at column zero. A line that is still too
        # long — because the window shrank after the run — scrolls sideways, which
        # is what a terminal does with one.
        yield RichLog(id="command-log", highlight=False, markup=False, wrap=False, max_lines=6000)
        # Directly above the line being typed, where a shell puts them, and zero
        # height whenever there is nothing to offer.
        yield OptionList(id="command-completions")
        yield CommandInput(
            placeholder="type a command — tab completes, up/down choose, enter runs",
            id="command-line",
        )
        yield Static(id="command-status")

    def on_mount(self) -> None:
        self._runner = runner_mod.Runner(self._db_path)
        self.query_one("#command-completions", OptionList).display = False
        self.reload_catalog()

    # --- the catalog ------------------------------------------------------

    def reload_catalog(self) -> None:
        self._commands = catalog.by_name()
        self.fill_list()

    def use_values(self, values_for) -> None:
        """Point the completer at this snapshot's projects and operators."""
        self._values_for = values_for

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
        # Marks rather than words: this column is two dozen rows tall and every
        # character of width it spends is width the command names lose.
        if command.destroys:
            line.append(" x", style="red")
        if command.blocked:
            line.append(" ~", style="dim")
        return line

    def show_command(self, name: str, *, focus_flag: Any = None) -> None:
        """The compact detail: what it is, what it costs, its flags in one line.

        `focus_flag` is the flag being typed or highlighted, and it gets the one
        line the table used to spend twenty on. That is the whole trade: the
        reference appears when it is asked for instead of always being on screen.
        """
        command = self._commands.get(name)
        if command is None:
            return
        head = Text()
        head.append(f"tracker {command.name}", style="bold")
        if command.cost == "llm":
            head.append("   $ spends tokens, needs the name typed back", style="yellow")
        if command.destroys:
            head.append(f"   x {command.destroys}", style="red")
        if command.blocked:
            head.append(f"   cannot run from here: {command.blocked}", style="dim")

        summary = Text(command.help or "", style="dim")

        flags = Text()
        shown = 0
        for flag in command.flags:
            rendered = flag.name + (
                "" if flag.positional or flag.kind == "bool" else completion_mod.default_text(flag)
            )
            # The flag being typed is always printed, however long the line got:
            # hiding the one thing the reader is looking at to honour a budget
            # would defeat the budget's purpose.
            focused = focus_flag is not None and flag.name == getattr(focus_flag, "name", None)
            if len(flags) + len(rendered) > FLAG_BUDGET and not focused:
                continue
            if flags:
                flags.append("  ")
            style = "magenta" if flag.positional else "cyan"
            flags.append(rendered, style=f"reverse {style}" if focused else style)
            shown += 1
        hidden = len(command.flags) - shown
        if hidden > 0:
            flags.append(f"  +{hidden} more", style="dim")
        if not command.flags:
            flags = Text("no options", style="dim")

        parts = [head, summary, Text(), flags]
        if focus_flag is not None and getattr(focus_flag, "help", ""):
            hint = Text()
            hint.append(f"{focus_flag.name}  ", style="bold cyan")
            hint.append(str(focus_flag.help))
            parts += [Text(), hint]
        self.query_one("#command-detail", Static).update(Group(*parts))

    # --- completion -------------------------------------------------------

    def refresh_completions(self) -> None:
        """Recompute the candidates for whatever is in the box."""
        line = self.query_one("#command-line", Input).value
        self._completions = completion_mod.complete(
            line, self._commands, values_for=self._values_for
        )
        widget = self.query_one("#command-completions", OptionList)
        widget.clear_options()
        for candidate in self._completions.items:
            label = Text(candidate.text, style=CANDIDATE_STYLE.get(candidate.kind, ""))
            if candidate.hint:
                label.append(f"   {candidate.hint}", style="dim")
            widget.add_option(Option(label))
        has_items = bool(self._completions.items)
        widget.display = has_items
        if has_items:
            widget.highlighted = 0

        # The picker and the detail follow the box, so the reference material is
        # always about the command being typed rather than the one last clicked.
        command = self._completions.command
        if command is not None:
            self.show_command(command.name, focus_flag=self._completions.context)
        else:
            self.fill_list(self._completions.prefix)

    def move_completion(self, delta: int) -> None:
        widget = self.query_one("#command-completions", OptionList)
        if not widget.display or not self._completions.items:
            return
        count = len(self._completions.items)
        current = widget.highlighted if widget.highlighted is not None else 0
        widget.highlighted = (current + delta) % count
        chosen = self._completions.items[widget.highlighted]
        command = self._completions.command
        if command is not None and chosen.kind == "flag":
            flag = next((f for f in command.flags if f.name == chosen.text), None)
            self.show_command(command.name, focus_flag=flag)

    def take_completion(self) -> None:
        """Insert the highlighted candidate, then offer the next word."""
        if not self._completions.items:
            return
        widget = self.query_one("#command-completions", OptionList)
        index = widget.highlighted if widget.highlighted is not None else 0
        candidate = self._completions.items[index]
        line = self.query_one("#command-line", CommandInput)
        line.value = self._completions.apply(line.value, candidate)
        line.cursor_position = len(line.value)
        self.refresh_completions()

    @property
    def completions_open(self) -> bool:
        return bool(self.query_one("#command-completions", OptionList).display)

    def _columns(self) -> int | None:
        """How wide the child should wrap, in characters.

        The vertical scrollbar is subtracted whether or not it is there yet, and
        that is the whole subtlety: it appears the moment output overflows, which is
        *after* this measurement, so a table sized to the full width lost its last
        column to a scrollbar that arrived later.
        """
        log = self.query_one("#command-log", RichLog)
        usable = log.content_size.width - log.scrollbar_size_vertical
        return max(40, usable) if usable > 0 else None

    def scroll_output(self, how: str) -> None:
        """Move the output, and stop chasing the tail once the reader has looked up.

        A terminal pins to the bottom while output arrives and stays put the moment
        you scroll back — otherwise the next line yanks you away from the thing you
        were reading. `end` puts you back on the tail and re-arms the pinning.
        """
        log = self.query_one("#command-log", RichLog)
        if how == "end":
            log.auto_scroll = True
            log.scroll_end(animate=False)
            return
        log.auto_scroll = False
        {
            "page_up": log.scroll_page_up,
            "page_down": log.scroll_page_down,
            "up": log.scroll_up,
            "down": log.scroll_down,
            "home": log.scroll_home,
        }[how](animate=False)

    def hide_completions(self) -> None:
        self._completions = completion_mod.Completions()
        widget = self.query_one("#command-completions", OptionList)
        widget.clear_options()
        widget.display = False

    # --- events -----------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "command-line":
            self.refresh_completions()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "command-list" and event.option.id:
            self.show_command(event.option.id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "command-list" and event.option.id:
            self.prefill(f"{event.option.id} ")
        elif event.option_list.id == "command-completions":
            self.take_completion()
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
        self.hide_completions()
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
            # Read at start time rather than stored: the window may have been
            # resized since the last run, and the child can only be told once.
            run = self._runner.start(cmd, flags, confirm=confirm, columns=self._columns())
        except runner_mod.Busy as exc:
            self._status(Text(str(exc), style="yellow"))
            return
        except catalog.InvalidRequest as exc:
            self._status(Text(str(exc), style="red"))
            return
        self._clear_confirmation()
        log.auto_scroll = True
        log.write(Text(f"\n$ tracker {cmd}", style="bold cyan"))
        self._status(Text(f"running {cmd} …   ctrl+k cancels", style="cyan"))
        self.query_one("#command-line", Input).focus()
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
        # The log keeps the record; the status line says what to do next. Writing
        # the same sentence to both put two identical lines a row apart, which is
        # how a screen ends up looking like it stuttered.
        log = self.query_one("#command-log", RichLog)
        log.write(line)
        log.auto_scroll = True
        log.scroll_end(animate=False)
        self._status(
            Text.assemble(
                ("ready", "dim"),
                ("   pageup/pagedown scroll back, shift+end returns to the tail", "dim"),
            )
        )
        # A run that wrote rows makes every read pane stale, and a stale pane beside
        # a fresh log is how somebody reads last hour's number as this minute's.
        if touched:
            self.post_message(self.DataChanged())

    def action_cancel_run(self) -> None:
        if self._runner is not None and self._runner.cancel():
            self._status(Text("cancelling …", style="yellow"))
        else:
            self._status(Text("nothing is running", style="dim"))

    def action_clear_log(self) -> None:
        self.query_one("#command-log", RichLog).clear()

    def prefill(self, text: str) -> None:
        """Put a command line in the box, ready to be edited or run."""
        line = self.query_one("#command-line", CommandInput)
        line.value = text
        line.cursor_position = len(text)
        line.focus()
        self.refresh_completions()

    def _status(self, text: Text) -> None:
        self.query_one("#command-status", Static).update(text)


__all__ = ["CANDIDATE_STYLE", "FLAG_BUDGET", "CommandInput", "CommandsPane"]
