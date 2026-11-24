"""`tracker loop`: the nightly loop's progress, read from its log, drawn for a person.

`overnight.sh --status` prints the latest night's raw round lines, which answers
"is it running" and little else: how much of tonight's money is gone, which phase it
is in and for how long, whether the rounds are still moving anything. This reads the
same log (`data/runs/overnight.log`, whose formats are the contract at the top of
`tracker.overnight`) and the night's spend ledger, and draws the answers as bars.

On a directed night (`tracker.night`) it also says who is choosing the steps, whether
the director had to hand them back and why, what the director itself has spent, every
change the referee made to its plans, the accuracy check, and — from the step record
(`data/runs/steps.jsonl`) — what each step cost and did against what was predicted.
Once a night has written its morning report, one line says what it did about rules
that apply to a place: pages read, rules kept and how many are new, and the campuses
a rule reached for the first time.

The parse is pure — lines in, a `Night` out — so the tests feed it a log; reading the
files and the lock, and drawing, are the two thin layers either side.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from tracker import overnight, spend, theme

_CEILINGS: Final = re.compile(
    r"^\s+ceilings\s+(?P<hours>[\d.]+)h, (?P<rounds>\d+) rounds, ¥(?P<cny>[\d.]+)"
    r"(?:, (?P<tokens>\d+) tokens)?(?:, (?P<searches>\d+) searches)?"
)
_AT_START: Final = re.compile(
    r"^\s+at start\s+(?P<findings>\d+) finding\(s\), (?P<groups>\d+) duplicate group\(s\), "
    r"(?P<below>\d+) row\(s\) below T2"
)
_LEDGER: Final = re.compile(r"^\s+spend\s+(?P<path>\S+\.spend)\s*$")
_PID: Final = re.compile(r"\(pid (\d+)\)")
_ROUND_START: Final = re.compile(r"^round (\d+) of (\d+)\s+\(¥([\d.]+)")
_ROUND_RESULT: Final = re.compile(
    r"^  round (?P<n>\d+): findings (?P<f0>\d+) -> (?P<f1>\d+).*?"
    r"groups (?P<g0>\d+) -> (?P<g1>\d+).*?below-T2 (?P<b0>\d+) -> (?P<b1>\d+)"
)
_PHASE: Final = re.compile(r"^--- (\d\d:\d\d:\d\d)  (.*)$")
_CLOSING_CNY: Final = re.compile(re.escape(overnight.COMPLETE) + r" — \d+m, ¥([\d.]+)")

#: Lines the parse reads; everything else in the log is the phases' own output.
_WANTED: Final = (
    "[",
    "--- ",
    overnight.ROUND_LINE,
    "    ceilings",
    "    at start",
    "    spend ",
    overnight.STEPS_LINE,
    overnight.REFEREE_LINE,
    overnight.ACCURACY_LINE,
    overnight.RULES_LINE,
)

#: How many past nights the history shows.
NIGHTS_SHOWN: Final = 7

#: Why a night stopped, in the words the log uses and the words a person wants.
#: (what the log says, the name for it, what the name means)
_STOPS: Final = (
    (overnight.FIXED_POINT, "fixed point", "the rounds moved nothing more"),
    (overnight.SETTLED, "settled", "nothing was left to do"),
    (overnight.SPEND_CEILING, "money ceiling", "the night's money was spent"),
    (overnight.TOKEN_CEILING, "token ceiling", "the night's tokens were spent"),
    (overnight.WALL_CLOCK, "time limit", "the hours ran out"),
    (overnight.DIRECTOR_STOPPED, "director stopped", ""),
)
_WHY: Final = {name: means for _, name, means in _STOPS}
#: `spend ceiling reached (¥16.06 of ¥43.18, ~25280576 of 25000000 tokens)`: one line
#: for both ceilings, so which of the two it was is read from the figures.
_BOTH: Final = re.compile(r"¥([\d.]+) of ¥([\d.]+), ~(\d+) of (\d+) tokens")


def _which_ceiling(text: str) -> str | None:
    both = _BOTH.search(text)
    if both is None:
        return None
    if float(both[1]) < float(both[2]) and int(both[3]) >= int(both[4]):
        return "token ceiling"
    return "money ceiling"


@dataclass
class Counts:
    findings: int
    groups: int
    below: int


@dataclass
class Phase:
    name: str
    detail: str
    started: dt.datetime
    ended: dt.datetime | None = None


@dataclass
class Round:
    number: int
    started: dt.datetime
    cny_at_start: float
    phases: list[Phase] = field(default_factory=list)
    before: Counts | None = None
    after: Counts | None = None
    ended: dt.datetime | None = None


@dataclass
class Night:
    started: dt.datetime
    pid: int | None = None
    hours: float | None = None
    rounds_cap: int | None = None
    cny_cap: float | None = None
    tokens_cap: int | None = None
    searches_cap: int | None = None
    at_start: Counts | None = None
    ledger: Path | None = None
    rounds: list[Round] = field(default_factory=list)
    #: Phases and steps after the rounds: the losses pass, the morning report.
    after: list[Phase] = field(default_factory=list)
    stop: str | None = None
    ended: dt.datetime | None = None
    cny_closing: float | None = None
    last_seen: dt.datetime | None = None
    #: Who chose the steps (`tracker.night`): fixed, director, or fixed with the
    #: director predicting in the background.
    chosen_by: str = "fixed"
    #: Why the director handed the night to the fixed schedule, and when.
    fell_back: str = ""
    fell_back_at: dt.datetime | None = None
    #: The director's reason for ending the night.
    stop_why: str = ""
    #: Every change the referee made to the director's plans, in order.
    referee: list[str] = field(default_factory=list)
    #: The latest accuracy check.
    accuracy: str = ""
    #: The morning report's line on rules that apply to a place.
    rules: str = ""

    @property
    def complete(self) -> bool:
        return self.ended is not None

    @property
    def latest(self) -> Counts | None:
        for rnd in reversed(self.rounds):
            if rnd.after:
                return rnd.after
        return self.at_start

    def current_phase(self) -> Phase | None:
        """The phase still under way, if the night is."""
        if self.complete:
            return None
        phases = [p for r in self.rounds for p in r.phases] + self.after
        return phases[-1] if phases and phases[-1].ended is None else None


def wanted(line: str) -> bool:
    return line.startswith(_WANTED)


def _close(night: Night, at: dt.datetime) -> None:
    """End whatever phase (and round step) is open at `at`."""
    for phase in [p for r in night.rounds for p in r.phases] + night.after:
        if phase.ended is None:
            phase.ended = at


def parse(lines: Iterable[str]) -> list[Night]:
    """Every night in the log, oldest first, each with its rounds and phases."""
    nights: list[Night] = []
    for raw in lines:
        line = raw.rstrip("\n")
        stamped = overnight.STAMPED.match(line)
        if stamped:
            at = dt.datetime.strptime(stamped[1], "%Y-%m-%d %H:%M:%S")
            text = stamped[2]
            if text.startswith(overnight.STARTING):
                pid = _PID.search(text)
                nights.append(Night(started=at, pid=int(pid[1]) if pid else None, last_seen=at))
                continue
            if not nights:
                continue
            night = nights[-1]
            night.last_seen = at
            # The director's own lines say who is choosing; they end no phase.
            if text.startswith(overnight.DIRECTOR_FAILED) or text.startswith(
                "the director could not be set up"
            ):
                why = text.partition("(")[2].rpartition(")")[0] or text
                night.fell_back, night.fell_back_at = why, at
                continue
            _close(night, at)
            rnd = _ROUND_START.match(text)
            if rnd:
                if night.rounds:
                    night.rounds[-1].ended = at
                night.rounds.append(Round(int(rnd[1]), at, float(rnd[3])))
                if night.rounds_cap is None:
                    night.rounds_cap = int(rnd[2])
                continue
            if night.rounds and night.rounds[-1].ended is None:
                night.rounds[-1].ended = at
            if text.startswith(overnight.COMPLETE):
                night.ended = at
                cny = _CLOSING_CNY.search(text)
                night.cny_closing = float(cny[1]) if cny else None
                continue
            for needle, short, _ in _STOPS:
                if needle in text:
                    night.stop = _which_ceiling(text) or short
                    if needle == overnight.DIRECTOR_STOPPED:
                        night.stop_why = text.partition(": ")[2]
                    break
            else:
                if night.rounds and not night.complete:
                    night.after.append(Phase(text, "", at))
            continue
        if not nights:
            continue
        night = nights[-1]
        phase = _PHASE.match(line)
        if phase:
            clock = dt.datetime.strptime(phase[1], "%H:%M:%S").time()
            base = night.last_seen or night.started
            at = dt.datetime.combine(base.date(), clock)
            if at < base - dt.timedelta(hours=1):  # past midnight
                at += dt.timedelta(days=1)
            night.last_seen = max(base, at)
            _close(night, at)
            name, _, detail = phase[2].partition(" — ")
            target = (
                night.rounds[-1].phases
                if night.rounds and night.rounds[-1].ended is None
                else night.after
            )
            target.append(Phase(name.strip(), detail.strip(), at))
            continue
        result = _ROUND_RESULT.match(line)
        if result and night.rounds:
            rnd = night.rounds[-1]
            rnd.before = Counts(int(result["f0"]), int(result["g0"]), int(result["b0"]))
            rnd.after = Counts(int(result["f1"]), int(result["g1"]), int(result["b1"]))
            continue
        if (m := _CEILINGS.match(line)) is not None:
            night.hours = float(m["hours"])
            night.rounds_cap = int(m["rounds"])
            night.cny_cap = float(m["cny"])
            night.tokens_cap = int(m["tokens"]) if m["tokens"] else None
            night.searches_cap = int(m["searches"]) if m["searches"] else None
        elif (m := _AT_START.match(line)) is not None:
            night.at_start = Counts(int(m["findings"]), int(m["groups"]), int(m["below"]))
        elif (m := _LEDGER.match(line)) is not None:
            night.ledger = Path(m["path"])
        elif line.startswith(overnight.STEPS_LINE):
            night.chosen_by = "director" if "director (" in line else "fixed, predicted"
        elif line.startswith(overnight.REFEREE_LINE):
            night.referee.append(line[len(overnight.REFEREE_LINE) :].strip())
        elif line.startswith(overnight.ACCURACY_LINE):
            night.accuracy = line[len(overnight.ACCURACY_LINE) :].strip()
        elif line.startswith(overnight.RULES_LINE):
            night.rules = line[len(overnight.RULES_LINE) :].strip()
    return nights


# --- the files ------------------------------------------------------------------------


@dataclass
class Live:
    """What the files say beside the log: the lock, and the night's ledger."""

    running: bool
    cny: float | None = None
    tokens: int | None = None
    searches: int | None = None
    #: What the director's own calls cost tonight.
    director_cny: float | None = None
    #: Tonight's steps from the record (`data/runs/steps.jsonl`), in the order they ran.
    steps: list = field(default_factory=list)


def read_log(path: Path) -> list[Night]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return parse(line for line in handle if wanted(line))
    except OSError:
        return []


def loop_running(runs: Path) -> bool:
    from tracker.db import _pid_alive

    try:
        pid = int((runs / "overnight.lock" / "pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def live(night: Night, runs: Path) -> Live:
    """The lock's state, and what the night's ledger has spent so far.

    The ledger rather than the log: the log prints the money only at the start of
    each round, and the first round runs for hours.
    """
    state = Live(running=loop_running(runs) and not night.complete)
    ledger = night.ledger
    if ledger is not None and not ledger.exists():
        ledger = runs / ledger.name  # the log names the host's path
    if ledger is not None and ledger.exists():
        try:
            calls = spend.read(ledger)
            state.cny = spend.total(calls)
            state.tokens = sum(c.tokens for c in calls)
            state.searches = spend.searches(calls)
            state.director_cny = spend.total(c for c in calls if c.command == "director")
        except Exception:  # a pricing bug costs the figure, never the page
            pass
    state.steps = night_steps(night, runs)
    return state


def night_steps(night: Night, runs: Path) -> list:
    """The record's steps for `night`: its stamp is the night's start, give or take the
    seconds between the shell naming it and the loop logging it."""
    from tracker.night import record

    out = []
    for r in record.read(runs / record.FILENAME):
        try:
            stamp = dt.datetime.strptime(r.night, "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if abs((stamp - night.started).total_seconds()) <= 300:
            out.append(r)
    return out


# --- drawing --------------------------------------------------------------------------
#
# The style is the host's login banner's: no box; a title with a note set against the
# right edge and a thin rule under it; labels in amber, values light, everything
# secondary dim; a meter as a line with "used / ceiling" and a percentage beside it.
# What is drawn is the loop's own. Left, the night in words and its allowances as
# meters. Right, the night as a line of time: one strip from its start to now, coloured
# by the phase that was running, the clock above it and the rounds below, then each
# kind of phase with its share. Under both, one titled section per table.
#
# A bar is a heavy line for the used part and a thin one for the rest, rather than
# shaded blocks, which several terminal fonts draw as one solid slab, and rather than
# one line in two colours, which a pipe or a colourless terminal shows as a full bar.

LABEL_W: Final = 10
BAR_W: Final = 20
VALUE_W: Final = 17  # "¥12.43 / ¥12.43"
PCT_W: Final = 4
INFO_W: Final = LABEL_W + BAR_W + 2 + VALUE_W + PCT_W
GAP: Final = 3
PANEL_W: Final = 62
#: The narrowest terminal that takes the two columns side by side.
WIDE: Final = 1 + INFO_W + GAP + PANEL_W
#: Kinds of phase the time line names; the rest are "other".
KINDS_NAMED: Final = 5
NAME_W: Final = 12
#: Steps the step-by-step table shows; the earliest are summarised above it.
STEPS_SHOWN: Final = 16
#: Changes by the referee the page lists, the latest.
REFEREE_SHOWN: Final = 4


@dataclass(frozen=True)
class Glyphs:
    full: str = "━"
    half: str = "╸"
    track: str = "─"
    block: str = "█"
    swatch: str = "■"
    dot: str = "·"
    arrow: str = "→"
    up: str = "▲"
    down: str = "▼"
    live: str = "●"
    running: str = "▶"
    done: str = "✓"
    failed: str = "✗"
    tick: str = "╵"
    more: str = "…"


#: For a console that cannot encode the line and block characters.
ASCII: Final = Glyphs(
    full="=",
    half="",
    track="-",
    block="#",
    swatch="#",
    dot="|",
    arrow="->",
    up="^",
    down="v",
    live="*",
    running=">",
    done="+",
    failed="x",
    tick="'",
    more="...",
)
UNICODE: Final = Glyphs()


def _level(look: theme.Look, fraction: float) -> str:
    """How full a meter is, as a colour: green, yellow past 70%, red past 90%."""
    return look.success if fraction < 0.7 else look.warning if fraction < 0.9 else look.danger


def bar(
    fraction: float,
    width: int = BAR_W,
    *,
    colour: str | None = None,
    ascii_only: bool = False,
    look: theme.Look = theme.XTERM,
) -> Text:
    """A thin bar, to the half cell. Without `colour`: green, yellow past 70%, red past 90%."""
    g = ASCII if ascii_only else UNICODE
    fraction = max(0.0, min(1.0, fraction))
    steps = int(fraction * width * 2 + 0.5)
    if fraction > 0 and steps == 0:
        steps = 1  # a sliver of use still shows
    full, half = divmod(steps, 2)
    if not g.half:
        full, half = min(width, full + half), 0
    out = Text(g.full * full + g.half * half, style=colour or _level(look, fraction))
    out.append(g.track * (width - full - half), style=look.track)
    return out


def duration(delta: dt.timedelta | None) -> str:
    if delta is None:
        return "-"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return f"{int(delta.total_seconds())}s"
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _millions(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 100_000 else f"{n:,}"


def _end(night: Night, state: Live, now: dt.datetime) -> dt.datetime:
    if state.running:
        return now
    return night.ended or night.last_seen or night.started


def _spent(night: Night, state: Live) -> float | None:
    if state.cny is not None:
        return state.cny
    if night.cny_closing is not None:
        return night.cny_closing
    return night.rounds[-1].cny_at_start if night.rounds else None


def _time_spent(night: Night, state: Live, now: dt.datetime) -> list[tuple[str, float]]:
    """Seconds per phase name over the whole night, longest first."""
    totals: dict[str, float] = {}
    end = _end(night, state, now)
    for phase in [p for r in night.rounds for p in r.phases] + night.after:
        seconds = ((phase.ended or end) - phase.started).total_seconds()
        totals[phase.name] = totals.get(phase.name, 0.0) + max(0.0, seconds)
    return sorted(totals.items(), key=lambda kv: -kv[1])


@dataclass
class _Page:
    """One drawing of the latest night: what is known, and what it is drawn with."""

    nights: list[Night]
    state: Live
    now: dt.datetime
    look: theme.Look
    g: Glyphs
    width: int
    night: Night = field(init=False)
    end: dt.datetime = field(init=False)

    def __post_init__(self) -> None:
        self.night = self.nights[-1]
        self.end = _end(self.night, self.state, self.now)

    # -- pieces every part is made of --

    def _fit(self, line: Text, width: int) -> Text:
        if line.cell_len > width:
            line.truncate(width - len(self.g.more), overflow="crop")
            line.append(self.g.more, style=self.look.muted)
        return line

    def _head(self, title: str, note: str, width: int) -> list[Text]:
        """A title, its note against the right edge, and the rule under both."""
        line = Text(title, style=f"bold {self.look.primary}")
        line.append(" " * max(2, width - line.cell_len - cell_len(note)))
        line.append(note, style=self.look.muted)
        return [self._fit(line, width), Text(self.g.track * width, style=self.look.track)]

    def _row(self, label: str, *parts: tuple[str, str]) -> Text:
        """A label and what it says, cut to the column."""
        line = Text(f"{label:<{LABEL_W}}", style=self.look.primary)
        for text, style in parts:
            line.append(text, style=style)
        return self._fit(line, INFO_W)

    def _meter(self, label: str, used: float, cap: float | None, current: str, of: str) -> Text:
        look = self.look
        line = Text(f"{label:<{LABEL_W}}", style=look.primary)
        if not cap:
            line.append(self.g.track * BAR_W, style=look.track)
            line.append(f"  {current}", style=f"bold {look.foreground}")
            line.append(f" {self.g.dot} no ceiling", style=look.muted)
            return line
        fraction = used / cap
        line.append_text(bar(fraction, ascii_only=self.g is ASCII, look=look))
        line.append(f"  {current}", style=f"bold {look.foreground}")
        line.append(f" / {of}", style=look.muted)
        line.append(" " * max(0, VALUE_W - cell_len(current) - cell_len(of) - 3))
        line.append(f"{fraction:.0%}".rjust(PCT_W), style=_level(look, min(1.0, fraction)))
        return line

    # -- the left column: the night in words, its allowances as meters --

    def _info(self) -> list[Text]:
        night, state, now, look, g = self.night, self.state, self.now, self.look, self.g
        lines = self._head("nightly loop", f"{now:%a %b} {now.day} {g.dot} {now:%H:%M}", INFO_W)
        took = duration(self.end - night.started)
        if state.running:
            mark, word, colour, detail = g.live, "running", look.success, f"{took} so far"
            span = f"{night.started:%H:%M} {g.arrow} now"
        elif night.complete:
            mark, word, colour, detail = g.done, "finished", look.primary, f"took {took}"
            span = f"{night.started:%H:%M} {g.arrow} {self.end:%H:%M}"
        else:
            mark, word, colour = g.failed, "cut off", look.danger
            detail = f"last heard of at {self.end:%H:%M}"
            span = f"started {night.started:%H:%M}"
        lines.append(
            self._row(
                "State", (f"{mark} {word}", f"bold {colour}"), (f" {g.dot} {detail}", look.muted)
            )
        )
        lines.append(
            self._row(
                "Night",
                (f"{night.started:%a %m-%d}", look.foreground),
                (f" {g.dot} {span}", look.muted),
            )
        )
        phase = night.current_phase() if state.running else None
        if phase is not None:
            parts = [
                (phase.name, f"bold {look.success}"),
                (f" {g.dot} {duration(now - phase.started)}", look.foreground),
            ]
            if phase.detail:
                parts.append((f" {g.dot} {phase.detail}", look.muted))
            lines.append(self._row("Phase", *parts))
        elif night.stop:
            note = _WHY.get(night.stop, "")
            lines.append(
                self._row(
                    "Stopped",
                    (night.stop, look.foreground),
                    (f" {g.dot} {note}" if note else "", look.muted),
                )
            )
            if night.stop_why:
                lines.append(self._row("", (night.stop_why, look.muted)))
        elif not night.complete:
            lines.append(self._row("Ended", ("by a reset, a power cut or a kill", look.danger)))
        lines += self._chooser()
        lines.append(Text())
        lines += self._meters()
        return lines

    def _chooser(self) -> list[Text]:
        """Who chose tonight's steps, and whether the director had to hand them back."""
        night, look, g = self.night, self.look, self.g
        if night.chosen_by == "director":
            if not night.fell_back:
                return [
                    self._row(
                        "Steps by",
                        ("the director", look.foreground),
                        (f" {g.dot} inside the same ceilings", look.muted),
                    )
                ]
            until = f" until {night.fell_back_at:%H:%M}" if night.fell_back_at else ""
            return [
                self._row(
                    "Steps by",
                    (f"the director{until}", look.warning),
                    (f" {g.dot} then fixed", look.muted),
                ),
                self._row("", (f"it failed: {night.fell_back}", look.muted)),
            ]
        if night.chosen_by == "fixed, predicted":
            rows = [
                self._row(
                    "Steps by",
                    ("the fixed schedule", look.foreground),
                    (f" {g.dot} the director predicting", look.muted),
                )
            ]
            if night.fell_back:
                rows.append(
                    self._row("", (f"its predictions stopped: {night.fell_back}", look.muted))
                )
            return rows
        rows = [self._row("Steps by", ("the fixed schedule", look.foreground))]
        if night.fell_back:
            rows.append(self._row("", (night.fell_back, look.muted)))
        return rows

    def _meters(self) -> list[Text]:
        night, state = self.night, self.state
        rows = []
        cny = _spent(night, state)
        if cny is not None:
            cap = f"¥{night.cny_cap:.2f}" if night.cny_cap else ""
            rows.append(self._meter("Money", cny, night.cny_cap, f"¥{cny:.2f}", cap))
        if state.tokens is not None:
            cap = _millions(night.tokens_cap) if night.tokens_cap else ""
            rows.append(
                self._meter("Tokens", state.tokens, night.tokens_cap, _millions(state.tokens), cap)
            )
        if state.searches is not None:
            cap = f"{night.searches_cap:,}" if night.searches_cap else ""
            rows.append(
                self._meter(
                    "Searches", state.searches, night.searches_cap, f"{state.searches:,}", cap
                )
            )
        if night.chosen_by != "fixed" and state.director_cny is not None:
            from tracker.night.director import BUDGET_CNY

            rows.append(
                self._meter(
                    "Director",
                    state.director_cny,
                    BUDGET_CNY,
                    f"¥{state.director_cny:.2f}",
                    f"¥{BUDGET_CNY:g}",
                )
            )
        took = self.end - night.started
        if night.hours:
            rows.append(
                self._meter(
                    "Time",
                    took / dt.timedelta(hours=night.hours),
                    1.0,
                    duration(took),
                    f"{night.hours:g}h",
                )
            )
        if night.rounds_cap:
            rows.append(
                self._meter(
                    "Rounds",
                    len(night.rounds),
                    night.rounds_cap,
                    str(len(night.rounds)),
                    str(night.rounds_cap),
                )
            )
        return rows

    # -- the right column: the night as a line of time --

    def _kinds(self) -> list[tuple[str, float, str]]:
        """Each kind of phase with its seconds and its colour, longest first; past the
        named few, one "other"."""
        spent = _time_spent(self.night, self.state, self.now)
        out = [
            (name, seconds, colour)
            for (name, seconds), colour in zip(spent[:KINDS_NAMED], self.look.chart, strict=False)
        ]
        other = sum(seconds for _, seconds in spent[len(out) :])
        if other:
            out.append(("other", other, self.look.muted))
        return out

    def _column(self, at: dt.datetime) -> int:
        """Where on the strip a moment of the night falls."""
        span = (self.end - self.night.started).total_seconds()
        return int((at - self.night.started).total_seconds() / span * PANEL_W)

    def _clock(self) -> Text:
        """The start, the hours that fit between, and the end, above the strip."""
        start, end = self.night.started, self.end
        cells = [" "] * PANEL_W
        first = f"{start:%H:%M}"
        last = "now" if self.state.running else f"{end:%H:%M}"
        cells[: len(first)] = first
        cells[PANEL_W - len(last) :] = last
        free, until = len(first) + 2, PANEL_W - len(last) - 2
        hours = (end - start).total_seconds() / 3600
        step = next((s for s in (1, 2, 3, 4, 6, 12) if s * PANEL_W / hours >= 8), 24)
        tick = start.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        while tick < end:
            text, at = f"{tick:%H:%M}", self._column(tick)
            if tick.hour % step == 0 and free <= at and at + len(text) <= until:
                cells[at : at + len(text)] = text
                free = at + len(text) + 2
            tick += dt.timedelta(hours=1)
        return Text("".join(cells), style=self.look.muted)

    def _strip(self, colours: dict[str, str]) -> Text:
        """The night from its start to now, each cell the phase running then; the thin
        stretches are the time between phases."""
        night, end = self.night, self.end
        span = (end - night.started).total_seconds()
        phases = [p for r in night.rounds for p in r.phases] + night.after
        line = Text()
        for x in range(PANEL_W):
            at = night.started + dt.timedelta(seconds=span * (x + 0.5) / PANEL_W)
            phase = next((p for p in phases if p.started <= at < (p.ended or end)), None)
            if phase is None:
                line.append(self.g.track, style=self.look.track)
            else:
                line.append(self.g.block, style=colours.get(phase.name, self.look.muted))
        return line

    def _round_marks(self) -> Text | None:
        """Where each round began, under the strip."""
        if not self.night.rounds:
            return None
        cells = [" "] * PANEL_W
        free = 0
        for i, rnd in enumerate(self.night.rounds):
            text = f"{self.g.tick}{'' if i else 'round '}{rnd.number}"
            at = min(self._column(rnd.started), PANEL_W - len(text))
            if at >= free:
                cells[at : at + len(text)] = text
                free = at + len(text) + 1
        return Text("".join(cells), style=self.look.muted)

    def _panel(self) -> list[Text]:
        night, look, g = self.night, self.look, self.g
        kinds = self._kinds()
        total = sum(seconds for _, seconds, _ in kinds)
        if total <= 0 or self.end <= night.started:
            return []
        until = "now" if self.state.running else f"{self.end:%H:%M}"
        note = (
            f"{night.started:%H:%M} {g.arrow} {until} {g.dot} {duration(self.end - night.started)}"
        )
        lines = self._head("Where the time went", note, PANEL_W)
        lines.append(self._clock())
        lines.append(self._strip({name: colour for name, _, colour in kinds}))
        marks = self._round_marks()
        if marks is not None:
            lines.append(marks)
        lines.append(Text())
        current = night.current_phase() if self.state.running else None
        bar_w = PANEL_W - (2 + NAME_W + 1) - (2 + 7 + 1 + PCT_W)
        for name, seconds, colour in kinds:
            under_way = current is not None and current.name == name
            line = Text(f"{g.swatch} ", style=colour)
            line.append(
                f"{name[:NAME_W]:<{NAME_W}} ",
                style=f"bold {look.success}" if under_way else look.foreground,
            )
            line.append_text(
                bar(seconds / total, bar_w, colour=colour, ascii_only=g is ASCII, look=look)
            )
            line.append(f"  {duration(dt.timedelta(seconds=seconds)):>7}", style=look.foreground)
            line.append(f" {seconds / total:.0%}".rjust(1 + PCT_W), style=look.muted)
            lines.append(line)
        return lines

    def columns(self) -> list[Text]:
        """Both columns from the same first row; one under the other where the
        terminal is too narrow for them side by side."""
        info, panel = self._info(), self._panel()

        def margin(line: Text) -> Text:
            out = Text(" ", no_wrap=True, overflow="crop")
            out.append_text(line)
            return out

        if not panel or self.width < WIDE:
            stacked = info + ([Text(), *panel] if panel and self.width > PANEL_W else [])
            return [margin(line) for line in stacked]
        out = []
        for i in range(max(len(info), len(panel))):
            left = info[i] if i < len(info) else Text()
            line = margin(left)
            if i < len(panel):
                line.append(" " * (INFO_W - left.cell_len + GAP))
                line.append_text(panel[i])
            out.append(line)
        return out

    # -- under both: one titled section per table --

    def _section(self, title: str, note: str, body: RenderableType) -> list[RenderableType]:
        width = max(INFO_W, min(self.width - 2, WIDE - 1))
        head = [Padding(line, (0, 0, 0, 1)) for line in self._head(title, note, width)]
        return [Text(), *head, Padding(body, (0, 0, 0, 1))]

    def _table(self, *columns: tuple[str, str]) -> Table:
        table = Table(
            box=None, padding=(0, 2), pad_edge=False, show_edge=False, header_style=self.look.muted
        )
        for name, justify in columns:
            table.add_column(name, justify=justify)  # type: ignore[arg-type]
        return table

    def _moved(self, before: int, after: int) -> Text:
        diff = after - before
        if diff == 0:
            return Text("no change", style=self.look.muted)
        # Every count here is work left, so down is good.
        if diff < 0:
            return Text(f"{self.g.down} {-diff:,} fewer", style=self.look.success)
        return Text(f"{self.g.up} {diff:,} more", style=self.look.warning)

    def _helped(self) -> RenderableType | None:
        """Work left at the start against now, and what the night's own checks said."""
        night, look = self.night, self.look
        start, latest = night.at_start, night.latest
        counts = Table.grid(padding=(0, 2))
        counts.add_column(style=look.primary, min_width=16)
        counts.add_column(justify="right", style=look.foreground)
        counts.add_column(style=look.muted)
        counts.add_column(justify="right", style=f"bold {look.foreground}")
        counts.add_column()
        if start is not None and latest is not None:
            for label, a, b in (
                ("Findings", start.findings, latest.findings),
                ("Duplicate groups", start.groups, latest.groups),
                ("Rows below T2", start.below, latest.below),
            ):
                counts.add_row(label, f"{a:,}", self.g.arrow, f"{b:,}", self._moved(a, b))
        checks = Table.grid(padding=(0, 2))
        checks.add_column(style=look.primary, min_width=16)
        checks.add_column(style=look.foreground)
        if night.accuracy:
            checks.add_row("Accuracy", night.accuracy)
        if night.rules:
            checks.add_row("Rules for a place", night.rules)
        parts = [t for t in (counts, checks) if t.row_count]
        return Group(*parts) if parts else None

    def _phases(self) -> Table | None:
        """The round under way, phase by phase: done, and the one running now."""
        night, look, g = self.night, self.look, self.g
        if not night.rounds or not night.rounds[-1].phases:
            return None
        table = Table.grid(padding=(0, 2))
        table.add_column()
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True, style=look.muted)
        table.add_column(justify="right", no_wrap=True)
        table.add_column(style=look.muted, overflow="ellipsis", no_wrap=True)
        for p in night.rounds[-1].phases:
            running = p.ended is None
            live = f"bold {look.success}"
            took = duration((p.ended or self.now) - p.started)
            table.add_row(
                Text(g.running if running else g.done, style=live if running else look.primary),
                Text(p.name, style=live if running else f"bold {look.foreground}"),
                f"{p.started:%H:%M}",
                Text(took + (" so far" if running else ""), style=live if running else ""),
                p.detail,
            )
        return table

    def _rounds(self) -> Table:
        night, state, look = self.night, self.state, self.look
        table = self._table(
            ("#", "right"),
            ("start", "left"),
            ("took", "right"),
            ("cost", "right"),
            ("what it changed", "left"),
        )
        final_cny = _spent(night, state)
        for i, rnd in enumerate(night.rounds):
            following = night.rounds[i + 1].cny_at_start if i + 1 < len(night.rounds) else final_cny
            cost = f"¥{following - rnd.cny_at_start:.2f}" if following is not None else "-"
            open_ = rnd.ended is None and state.running
            live = f"bold {look.success}"
            took = Text(
                duration((rnd.ended or self.end) - rnd.started), style=live if open_ else ""
            )
            if open_:
                effect = Text("under way", style=live)
            elif rnd.before and rnd.after:
                effect = Text()
                for label, a, b in (
                    ("findings", rnd.before.findings, rnd.after.findings),
                    ("groups", rnd.before.groups, rnd.after.groups),
                    ("below T2", rnd.before.below, rnd.after.below),
                ):
                    cell = f"{label} {b - a:+,}" if a != b else ""
                    effect.append(f"{cell:<16}", style=look.success if b < a else look.warning)
                effect.rstrip()
                if not effect.plain:
                    effect = Text("nothing", style=look.muted)
            else:
                effect = Text("-", style=look.muted)
            table.add_row(str(rnd.number), f"{rnd.started:%H:%M}", took, cost, effect)
        return table

    def _steps(self) -> Table | None:
        """Tonight's steps from the record: what each cost and did, against the prediction."""
        look = self.look
        if not self.state.steps:
            return None
        from tracker.night.steps import KINDS

        table = self._table(
            ("#", "right"),
            ("step", "left"),
            ("size", "right"),
            ("by", "left"),
            ("cost", "right"),
            ("searches", "right"),
            ("took", "right"),
            ("result", "left"),
        )
        for r in self.state.steps[-STEPS_SHOWN:]:
            kind = KINDS.get(r.kind)
            unit = kind.result if kind else ""
            if r.failed:
                result = Text(f"failed (exit {', '.join(map(str, r.exits))})", style=look.danger)
            elif r.result is None:
                result = Text("-", style=look.muted)
            else:
                colour = (
                    look.success if r.result > 0 else look.warning if r.result < 0 else look.muted
                )
                result = Text(f"{r.result:+,} {unit}", style=colour)
            expected = r.expect.get("result")
            if expected is not None:
                result.append(f"  (expected {expected:+g})", style=look.muted)
            table.add_row(
                str(r.round),
                Text(r.kind, style=f"bold {look.foreground}"),
                str(r.size) if r.size else "",
                Text(r.origin, style=look.muted),
                f"¥{r.cny:.2f}",
                f"{r.searches:,}" if r.searches else "",
                duration(dt.timedelta(minutes=r.minutes)),
                result,
            )
        return table

    def _history(self) -> Table:
        nights, state, look, g = self.nights, self.state, self.look, self.g
        table = self._table(
            ("night", "left"),
            ("took", "right"),
            ("spent", "right"),
            ("of its ceiling", "left"),
            ("ended", "left"),
            ("steps by", "left"),
        )
        shown = nights[-NIGHTS_SHOWN:]
        top = max((n.cny_closing or 0 for n in shown), default=0) or 1
        for n in shown:
            tonight = n is nights[-1] and state.running
            if tonight:
                mark, colour, why = g.running, look.success, "still running"
            elif n.complete:
                mark, colour, why = g.done, look.primary, n.stop or "finished"
            else:
                mark, colour, why = g.failed, look.danger, "cut off"
            cny = state.cny if tonight else n.cny_closing
            took = duration(_end(n, state if tonight else Live(False), self.now) - n.started)
            if cny is None:
                share = Text("-", style=look.muted)
            elif n.cny_cap:
                share = bar(cny / n.cny_cap, colour=look.primary, ascii_only=g is ASCII, look=look)
                share.append(f" {cny / n.cny_cap:.0%}".rjust(1 + PCT_W), style=look.muted)
            else:
                share = bar(cny / top, colour=look.muted, ascii_only=g is ASCII, look=look)
            by_director = n.chosen_by == "director"
            table.add_row(
                Text.assemble((f"{mark} ", colour), f"{n.started:%a %m-%d}"),
                took,
                f"¥{cny:.2f}" if cny is not None else "-",
                share,
                Text(why, style=colour if tonight or not n.complete else ""),
                Text(
                    "director" + (", fell back" if n.fell_back else "") if by_director else "fixed",
                    style="" if by_director else look.muted,
                ),
            )
        return table

    def sections(self) -> list[RenderableType]:
        night, state, g = self.night, self.state, self.g
        out: list[RenderableType] = []
        helped = self._helped()
        if helped is not None:
            counted = any(r.after for r in night.rounds)
            note = (
                f"work left, at the start {g.arrow} now"
                if counted
                else "counted when a round ends; none has ended yet"
            )
            out += self._section("Did it help?", note, helped)
        phases = self._phases() if state.running else None
        if phases is not None:
            out += self._section(
                f"Round {night.rounds[-1].number} so far", "phase by phase", phases
            )
        if night.rounds:
            cap = f" of {night.rounds_cap}" if night.rounds_cap else ""
            out += self._section("Round by round", f"{len(night.rounds)}{cap}", self._rounds())
        steps = self._steps()
        if steps is not None:
            hidden = len(state.steps) - STEPS_SHOWN
            note = (
                f"the last {STEPS_SHOWN} of {len(state.steps)}"
                if hidden > 0
                else "from the step record"
            )
            out += self._section("Step by step", note, steps)
        if night.referee:
            note = f"{len(night.referee)} change(s) to the director's plans"
            if len(night.referee) > REFEREE_SHOWN:
                note += f"; the latest {REFEREE_SHOWN}"
            changes = Group(*(Text(change) for change in night.referee[-REFEREE_SHOWN:]))
            out += self._section("The referee", note, changes)
        if len(self.nights) > 1:
            shown = min(len(self.nights), NIGHTS_SHOWN)
            out += self._section(
                f"The last {shown} nights", "money spent of each night's ceiling", self._history()
            )
        return out


def render(
    nights: list[Night],
    state: Live,
    *,
    now: dt.datetime | None = None,
    ascii_only: bool = False,
    width: int = WIDE + 1,
    look: theme.Look = theme.XTERM,
) -> RenderableType:
    """The whole page for the latest night, with the nights before it underneath."""
    if not nights:
        return Text("the nightly log holds no nights yet", style=look.warning)
    page = _Page(
        nights, state, now or dt.datetime.now(), look, ASCII if ascii_only else UNICODE, width
    )
    return Group(Text(), *page.columns(), *page.sections(), Text())
