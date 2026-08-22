# The terminal interface

`tracker tui` — six panes over the same database and the same commands, for the machine the data actually lives on.

Part of the [dc-tracker documentation](README.md).

---

## Why a third interface

There are now three, and each answers something the others cannot.

| | good for | needs |
|---|---|---|
| the CLI | one question, scriptable, pipeable | a shell |
| the console (`tracker serve`) | showing somebody, from anywhere | a browser, a password, a tunnel |
| the TUI (`tracker tui`) | working the data where it lives | an ssh session |

The CLI prints a snapshot and forgets it: comparing two projects is two commands
and a scroll, and a filter worth keeping is a shell history entry. The console
keeps state and draws properly, but the machine that owns the database is reached
by ssh — and a browser is not what you have there.

```bash
tracker tui
```

## The panes

```
1 overview   the headline numbers, field coverage drawn, open obstacles ranked
2 projects   the table, filtered live, one project opened beside it
3 coverage   rostered operators against the rows we hold
4 capex      who is buying the capacity, and which year it lands
5 queue      what is waiting to be read, and what could not be
6 run        every CLI command, its whole flag surface, and its output
```

`1`–`6` switch panes, `r` re-reads the database, `q` quits, `/` jumps straight to
the projects filter. On a highlighted row, `e` and `s` prefill `enrich` and `show`
in the run pane, and on the coverage pane `p` prefills `prospect` for the operator
under the cursor. They *prefill* rather than run: `enrich` spends money, and the
confirmation for that lives in one place.

**Bars, not columns of percentages.** Twelve field-coverage numbers are something
a reader compares by hand; the same twelve as bars are a shape. That is most of the
"better visualization" here — field coverage, obstacle exposure by cited capacity,
per-operator capacity, and the five progress tracks on an opened project, which is
the one view that makes the project model legible: site control and permits can be
finished while power is years out, so they are drawn side by side rather than
summed into a single ladder.

**Provenance travels with every value.** A figure in the detail pane carries its
tier — `derived` came from a Census lookup nobody said out loud, `unconfirmed` was
extracted and could not be quoted, `inferred` is a model's judgement. Absence is
drawn as absence.

## The run pane has every command, and that is structural

The list is read out of the live Typer app through `webui.catalog` — the same
introspection the console's palette uses — so a command added to `cli.py` appears
here on the next start with its real flags, types, defaults, choices and help.
Nothing in `tracker/tui/` holds a list of commands that could fall behind, and
`tests/test_tui.py` asserts the pane offers exactly what the catalog does.

Type a command line and press Enter. **There is no shell here at any point:**
`catalog.parse_command_line` turns the text into a `(command, flags)` pair,
`catalog.build_argv` validates that pair and returns an argv list, and the
subprocess is spawned with no shell in between. `gaps; rm -rf /` is not sanitized —
`rm` is a word no command has, and it is refused by name.

Runs go through `webui.runner`, which is the console's executor, so the TUI
inherits its three properties rather than reimplementing them: no shell, one writer
at a time (SQLite takes one, and a second run would fail partway through after
paying for its LLM calls), and a confirmation ritual for anything that spends
tokens or deletes rows — the command's own name, typed back. A terminal is not a
reason to be laxer about either loss. Every run also lands in the same
`data/runs/` history the console lists.

Output arrives with its colour intact, because the child is given `FORCE_COLOR`
and the pane renders the escapes: red is a rejection, amber is 待确认, dim is a
hint, and stripping that throws away the CLI's own signalling at the moment
somebody is reading it.

## Verifying it without sitting at a terminal

The interface deploys to a host reached by ssh, so "does it work there" has to be
answerable with nobody watching:

```bash
tracker tui --check
tracker tui --screenshot frame.svg --pane coverage
```

`--check` boots the real app against the real database headlessly, fills every
pane, and exits non-zero if any of them failed — a smoke test that fails on a
renamed column or a broken query, not a version string. `--screenshot` writes the
frame it rendered as an SVG, which is what makes a remote check something you can
actually look at.

```bash
ssh $PROD 'tracker tui --check'
ssh $PROD 'tracker tui --screenshot /tmp/frame.svg --pane overview'
```

## Installing

Textual is a required dependency, so a normal install has it:

```bash
python -m pip install -e ".[dev]"
```

It is nonetheless imported lazily, inside `tracker.tui.run`. The deployer refuses a
commit that does not import and leaves the console serving the previous code — so a
host whose dependency sync has not run yet has to be able to take this code
anyway. Without Textual, `tracker tui` says what to install and exits 2; nothing
else notices.

## What it deliberately does not do

- **No editing of rows.** Every write goes through a command, with that command's
  gates. A grid you can type into would be a fourth write path with no citation
  attached to what it changed.
- **No judgements of its own.** Panes render what `webui.dataset.build` and
  `roster.measure` decided, for the reason `docs/architecture.md` gives about the
  browser: a second implementation of "which years the capex grid shows" is a
  second opinion, free to disagree, with nothing to say when it starts to.
- **It is not offered in the console.** `tui` is in the catalog's blocked list: a
  full-screen terminal app cannot render into a browser, and starting one there
  would hold the single run slot until the timeout while nothing appeared.
