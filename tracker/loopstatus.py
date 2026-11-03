"""`tracker loop`: the nightly loop's progress, read from its log, drawn for a person.

`overnight.sh --status` prints the latest night's raw round lines, which answers
"is it running" and little else: how much of tonight's money is gone, which phase it
is in and for how long, whether the rounds are still moving anything. This reads the
same log (`data/runs/overnight.log`, whose formats are the contract at the top of
`tracker.overnight`) and the night's spend ledger, and draws the answers as bars.

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

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from tracker import overnight, spend

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
_WANTED: Final = ("[", "--- ", overnight.ROUND_LINE, "    ceilings", "    at start", "    spend ")

#: How many past nights the history shows.
NIGHTS_SHOWN: Final = 7

#: Why a night stopped, in the words the log uses and the words a person wants.
#: (what the log says, short form for the history, long form for the header)
_STOPS: Final = (
    (overnight.FIXED_POINT, "fixed point", "fixed point — the rounds stopped moving anything"),
    (overnight.SETTLED, "settled", "everything settled"),
    (overnight.SPEND_CEILING, "money ceiling", "hit the money ceiling"),
    (overnight.TOKEN_CEILING, "token ceiling", "hit the token ceiling"),
    (overnight.WALL_CLOCK, "time limit", "hit the time limit"),
)
_WHY: Final = {short: long for _, short, long in _STOPS}
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
    return nights


# --- the files ------------------------------------------------------------------------


@dataclass
class Live:
    """What the files say beside the log: the lock, and the night's ledger."""

    running: bool
    cny: float | None = None
    tokens: int | None = None
    searches: int | None = None


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
        except Exception:  # a pricing bug costs the figure, never the page
            pass
    return state


# --- drawing --------------------------------------------------------------------------
#
# Each section answers one question, in words first and a bar second: is it running,
# how much of the night's allowance is gone, where the time went, whether it helped.
# Bars are thin lines (━ used, ─ unused) rather than shaded blocks, which several
# terminal fonts draw as one solid slab, so the used part could not be told apart.

#: Colours for the time line's segments, longest phase first; the rest is "other".
_PALETTE: Final = ("dodger_blue1", "orange1", "medium_purple1", "spring_green2", "gold1")

_DIM: Final = "grey35"


def bar(
    fraction: float, width: int = 30, *, colour: str | None = None, ascii_only: bool = False
) -> Text:
    """A thin bar. Without `colour`: green, then yellow past 70%, red past 90%."""
    fraction = max(0.0, min(1.0, fraction))
    full, empty = ("=", "-") if ascii_only else ("━", "─")
    filled = round(fraction * width)
    if fraction > 0 and filled == 0:
        filled = 1  # a sliver of use still shows
    colour = colour or ("green" if fraction < 0.7 else "yellow" if fraction < 0.9 else "red")
    out = Text(full * filled, style=colour)
    out.append(empty * (width - filled), style=_DIM)
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


def _section(title: str, note: str = "") -> Text:
    out = Text(f"\n{title}", style="bold")
    if note:
        out.append(f"   {note}", style="grey50")
    return out


def _headline(night: Night, state: Live, now: dt.datetime) -> Group:
    """Three sentences: what state it is in, why it stopped or what it is doing, the money."""
    end = _end(night, state, now)
    if state.running:
        badge = Text(" ● RUNNING ", style="bold black on green")
        when = f"since {night.started:%a %m-%d %H:%M}, {duration(end - night.started)} so far"
    elif night.complete:
        badge = Text(" ✓ FINISHED ", style="bold black on cyan")
        when = (
            f"{night.started:%a %m-%d}, {night.started:%H:%M} → {end:%H:%M}"
            f"  ({duration(end - night.started)})"
        )
    else:
        badge = Text(" ✗ CUT OFF ", style="bold white on red")
        when = f"started {night.started:%a %m-%d %H:%M}, last heard of at {end:%H:%M}"
    lines = [Text.assemble(badge, "  ", (when, "bold"))]
    phase = night.current_phase() if state.running else None
    if phase is not None:
        lines.append(
            Text.assemble(
                "Now in ",
                (phase.name, "bold green"),
                f" for {duration(now - phase.started)}",
                (f" — {phase.detail}" if phase.detail else "", "grey50"),
            )
        )
    elif night.stop:
        lines.append(Text(f"Stopped: {_WHY.get(night.stop, night.stop)}."))
    elif not night.complete:
        lines.append(
            Text("It ended without finishing: a reset, a power cut or a kill.", style="red")
        )
    cny = _spent(night, state)
    if cny is not None and night.cny_cap:
        left = night.cny_cap - cny
        verb = "has spent" if state.running else "spent"
        tail = f"; ¥{left:.2f} {'still available' if state.running else 'went unused'}"
        lines.append(Text(f"It {verb} ¥{cny:.2f} of tonight's ¥{night.cny_cap:.2f}{tail}."))
    return Group(*lines)


def _budget(night: Night, state: Live, now: dt.datetime, ascii_only: bool) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", min_width=8)
    table.add_column(justify="right", min_width=4)
    table.add_column(no_wrap=True)
    table.add_column(style="grey50")

    def row(label: str, used: float, cap: float | None, shown: str) -> None:
        if cap:
            table.add_row(label, f"{used / cap:.0%}", bar(used / cap, ascii_only=ascii_only), shown)
        else:
            table.add_row(label, "", Text("no ceiling", style="grey50"), shown)

    cny = _spent(night, state)
    if cny is not None:
        cap = f" of ¥{night.cny_cap:.2f}" if night.cny_cap else ""
        row("money", cny, night.cny_cap, f"¥{cny:.2f}{cap}")
    if state.tokens is not None:
        cap = f" of {_millions(night.tokens_cap)}" if night.tokens_cap else ""
        row("tokens", state.tokens, night.tokens_cap, f"{_millions(state.tokens)}{cap}")
    if state.searches is not None:
        cap = f" of {night.searches_cap:,}" if night.searches_cap else ""
        row("searches", state.searches, night.searches_cap, f"{state.searches:,}{cap}")
    took = _end(night, state, now) - night.started
    if night.hours:
        row(
            "time",
            took / dt.timedelta(hours=night.hours),
            1.0,
            f"{duration(took)} of {night.hours:g}h",
        )
    if night.rounds_cap:
        row(
            "rounds",
            len(night.rounds),
            night.rounds_cap,
            f"{len(night.rounds)} of {night.rounds_cap}",
        )
    return table


def _time_spent(night: Night, state: Live, now: dt.datetime) -> list[tuple[str, float]]:
    """Seconds per phase name over the whole night, longest first."""
    totals: dict[str, float] = {}
    end = _end(night, state, now)
    for phase in [p for r in night.rounds for p in r.phases] + night.after:
        seconds = ((phase.ended or end) - phase.started).total_seconds()
        totals[phase.name] = totals.get(phase.name, 0.0) + max(0.0, seconds)
    return sorted(totals.items(), key=lambda kv: -kv[1])


def _timeline(night: Night, state: Live, now: dt.datetime, ascii_only: bool, width: int = 60):
    """One bar for the night, a coloured stretch per kind of phase, with its legend."""
    spent = _time_spent(night, state, now)
    total = sum(s for _, s in spent)
    if total <= 0:
        return None
    named = spent[: len(_PALETTE)]
    other = sum(s for _, s in spent[len(_PALETTE) :])
    segments = [(name, s, colour) for (name, s), colour in zip(named, _PALETTE, strict=False)]
    if other:
        segments.append(("other", other, "grey50"))
    # In the order they ran would be truer, but phases repeat every round; grouped by
    # kind, the longest first, is what answers "where did the time go".
    line = Text()
    used = 0
    for i, (_, seconds, colour) in enumerate(segments):
        cells = width - used if i == len(segments) - 1 else max(1, round(seconds / total * width))
        cells = max(0, min(cells, width - used))
        line.append(("#" if ascii_only else "█") * cells, style=colour)
        used += cells
    legend = Text()
    for name, seconds, colour in segments:
        if legend:
            legend.append("   ")
        legend.append("■ " if not ascii_only else "# ", style=colour)
        legend.append(name, style="bold")
        legend.append(f" {duration(dt.timedelta(seconds=seconds))}", style="grey50")
    return Group(line, legend)


def _arrow(before: int, after: int, ascii_only: bool) -> Text:
    diff = after - before
    if diff == 0:
        return Text("   no change", style="grey50")
    # Every count here is work left, so down is good.
    if diff < 0:
        return Text(f"{'v' if ascii_only else '▼'} {-diff:>5,} fewer", style="green")
    return Text(f"{'^' if ascii_only else '▲'} {diff:>5,} more", style="yellow")


def _progress(night: Night, ascii_only: bool) -> Table | None:
    start, latest = night.at_start, night.latest
    if start is None or latest is None:
        return None
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", min_width=18)
    table.add_column(justify="right")
    table.add_column()
    table.add_column(justify="right")
    table.add_column()
    for label, a, b in (
        ("findings", start.findings, latest.findings),
        ("duplicate groups", start.groups, latest.groups),
        ("rows below T2", start.below, latest.below),
    ):
        table.add_row(
            label, f"{a:,}", Text("→", style="grey50"), f"{b:,}", _arrow(a, b, ascii_only)
        )
    return table


def _rounds(night: Night, state: Live, now: dt.datetime, ascii_only: bool) -> Table:
    table = Table(box=None, padding=(0, 2), show_edge=False, header_style="grey50")
    for name, justify in (
        ("#", "right"),
        ("start", "left"),
        ("took", "right"),
        ("cost", "right"),
        ("what it changed", "left"),
    ):
        table.add_column(name, justify=justify)  # type: ignore[arg-type]
    final_cny = _spent(night, state)
    end = _end(night, state, now)
    for i, rnd in enumerate(night.rounds):
        following = night.rounds[i + 1].cny_at_start if i + 1 < len(night.rounds) else final_cny
        cost = f"¥{following - rnd.cny_at_start:.2f}" if following is not None else "-"
        open_ = rnd.ended is None and state.running
        took = Text(duration((rnd.ended or end) - rnd.started), style="bold green" if open_ else "")
        if open_:
            effect = Text("under way", style="bold green")
        elif rnd.before and rnd.after:
            effect = Text()
            for label, a, b in (
                ("findings", rnd.before.findings, rnd.after.findings),
                ("groups", rnd.before.groups, rnd.after.groups),
                ("below T2", rnd.before.below, rnd.after.below),
            ):
                cell = f"{label} {b - a:+,}" if a != b else ""
                effect.append(f"{cell:<16}", style="green" if b < a else "yellow")
            effect.rstrip()
            if not effect.plain:
                effect = Text("nothing", style="grey50")
        else:
            effect = Text("-", style="grey50")
        table.add_row(str(rnd.number), f"{rnd.started:%H:%M}", took, cost, effect)
    return table


def _phases(night: Night, now: dt.datetime, ascii_only: bool) -> Table | None:
    """The round under way, phase by phase: done, and the one running now."""
    if not night.rounds or not night.rounds[-1].phases:
        return None
    table = Table.grid(padding=(0, 2))
    table.add_column()
    table.add_column(style="bold", no_wrap=True)
    table.add_column(no_wrap=True, style="grey50")
    table.add_column(justify="right", no_wrap=True)
    table.add_column(style="grey50", overflow="ellipsis", no_wrap=True, max_width=56)
    for p in night.rounds[-1].phases:
        running = p.ended is None
        mark = (">" if running else "+") if ascii_only else ("▶" if running else "✓")
        took = duration((p.ended or now) - p.started)
        table.add_row(
            Text(mark, style="bold green" if running else "cyan"),
            Text(p.name, style="bold green" if running else "bold"),
            f"{p.started:%H:%M}",
            Text(took + (" so far" if running else ""), style="bold green" if running else ""),
            p.detail,
        )
    return table


def _history(nights: list[Night], state: Live, now: dt.datetime, ascii_only: bool) -> Table:
    table = Table(box=None, padding=(0, 2), show_edge=False, header_style="grey50")
    table.add_column("night", no_wrap=True)
    table.add_column("took", justify="right")
    table.add_column("spent", justify="right")
    table.add_column("of its ceiling", no_wrap=True)
    table.add_column("ended", no_wrap=True)
    shown = nights[-NIGHTS_SHOWN:]
    top = max((n.cny_closing or 0 for n in shown), default=0) or 1
    for n in shown:
        tonight = n is nights[-1] and state.running
        if tonight:
            mark, colour, why = ("▶", "green", "still running")
        elif n.complete:
            mark, colour, why = ("✓", "cyan", n.stop or "finished")
        else:
            mark, colour, why = ("✗", "red", "cut off")
        if ascii_only:
            mark = {"▶": ">", "✓": "+", "✗": "x"}[mark]
        cny = state.cny if tonight else n.cny_closing
        took = duration(_end(n, state if tonight else Live(False), now) - n.started)
        if cny is None:
            share = Text("-", style="grey50")
        elif n.cny_cap:
            share = bar(cny / n.cny_cap, 20, colour="dodger_blue1", ascii_only=ascii_only)
            share.append(f" {cny / n.cny_cap:.0%}", style="grey50")
        else:
            share = bar(cny / top, 20, colour="grey50", ascii_only=ascii_only)
        table.add_row(
            Text.assemble((f"{mark} ", colour), f"{n.started:%a %m-%d}"),
            took,
            f"¥{cny:.2f}" if cny is not None else "-",
            share,
            Text(why, style=colour if tonight or not n.complete else ""),
        )
    return table


def render(
    nights: list[Night], state: Live, *, now: dt.datetime | None = None, ascii_only: bool = False
) -> RenderableType:
    """The whole page for the latest night, with the nights before it underneath."""
    now = now or dt.datetime.now()
    if not nights:
        return Text("the nightly log holds no nights yet", style="yellow")
    night = nights[-1]
    parts: list[RenderableType] = [_headline(night, state, now)]
    parts += [
        _section("How much of tonight's allowance is used"),
        _budget(night, state, now, ascii_only),
    ]
    timeline = _timeline(night, state, now, ascii_only)
    if timeline is not None:
        took = duration(_end(night, state, now) - night.started)
        parts += [_section("Where the time went", took), timeline]
    progress = _progress(night, ascii_only)
    if progress is not None:
        counted = any(r.after for r in night.rounds)
        note = (
            "work left, at the start → now"
            if counted
            else "counted when a round ends; none has ended yet"
        )
        parts += [_section("Did it help?", note), progress]
    if night.rounds:
        parts += [_section("Round by round"), _rounds(night, state, now, ascii_only)]
    if state.running:
        phases = _phases(night, now, ascii_only)
        if phases is not None:
            parts += [_section(f"Round {night.rounds[-1].number} so far"), phases]
    if len(nights) > 1:
        parts += [
            _section(f"The last {min(len(nights), NIGHTS_SHOWN)} nights"),
            _history(nights, state, now, ascii_only),
        ]
    return Panel(
        Group(*parts),
        title="[bold]nightly loop[/bold]",
        title_align="left",
        border_style="green" if state.running else "cyan" if night.complete else "red",
        padding=(1, 2),
        expand=False,
    )
