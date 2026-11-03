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
                    night.stop = short
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


def bar(fraction: float, width: int = 24, *, ascii_only: bool = False) -> Text:
    """A filled bar; green, then yellow past 70%, red past 90%."""
    fraction = max(0.0, min(1.0, fraction))
    full = "#" if ascii_only else "█"
    empty = "." if ascii_only else "░"
    filled = round(fraction * width)
    colour = "green" if fraction < 0.7 else "yellow" if fraction < 0.9 else "red"
    out = Text(full * filled, style=colour)
    out.append(empty * (width - filled), style="grey37")
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


def _change(before: int, after: int) -> Text:
    diff = after - before
    if diff == 0:
        return Text("±0", style="grey50")
    # Every count here is work left, so down is good.
    return Text(f"{diff:+,}", style="green" if diff < 0 else "yellow")


def _header(night: Night, state: Live, now: dt.datetime) -> Text:
    if state.running:
        badge = Text(" ● RUNNING ", style="bold black on green")
    elif night.complete:
        badge = Text(" ✓ FINISHED ", style="bold black on cyan")
    else:
        badge = Text(" ✗ CUT OFF ", style="bold white on red")
    out = Text.assemble(badge, "  ", ("started ", "grey50"), f"{night.started:%a %m-%d %H:%M}")
    if state.running:
        out.append("  ·  ", style="grey50")
        out.append(f"{duration(now - night.started)} in")
    elif night.ended:
        out.append("  ·  ", style="grey50")
        out.append(f"took {duration(night.ended - night.started)}")
        out.append(f", ended {night.ended:%H:%M}", style="grey50")
    else:
        out.append("  ·  last heard of ", style="grey50")
        out.append(f"{night.last_seen:%H:%M}" if night.last_seen else "-")
    if night.pid:
        out.append(f"  ·  pid {night.pid}", style="grey50")
    return out


def _budget(night: Night, state: Live, now: dt.datetime, ascii_only: bool) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", min_width=8)
    table.add_column()
    table.add_column(justify="right")
    table.add_column(justify="right", style="grey50")

    def row(label: str, used: float, cap: float | None, shown: str) -> None:
        if cap:
            table.add_row(
                label, bar(used / cap, ascii_only=ascii_only), shown, f"{used / cap:4.0%}"
            )
        else:
            table.add_row(label, Text("no ceiling", style="grey50"), shown, "")

    cny = state.cny if state.cny is not None else night.cny_closing
    if cny is None and night.rounds:
        cny = night.rounds[-1].cny_at_start
    if cny is not None:
        cap = f" of ¥{night.cny_cap:.2f}" if night.cny_cap else ""
        row("money", cny, night.cny_cap, f"¥{cny:.2f}{cap}")
    if state.tokens is not None:
        cap = f" of {_millions(night.tokens_cap)}" if night.tokens_cap else ""
        row("tokens", state.tokens, night.tokens_cap, f"{_millions(state.tokens)}{cap}")
    if state.searches is not None:
        cap = f" of {night.searches_cap:,}" if night.searches_cap else ""
        row("searches", state.searches, night.searches_cap, f"{state.searches:,}{cap}")
    end = night.ended or (now if state.running else night.last_seen) or night.started
    if night.hours:
        cap_td = dt.timedelta(hours=night.hours)
        row(
            "time",
            (end - night.started) / cap_td,
            1.0,
            f"{duration(end - night.started)} of {night.hours:g}h",
        )
    if night.rounds_cap:
        done = len(night.rounds)
        row("rounds", done, night.rounds_cap, f"{done} of {night.rounds_cap}")
    return table


def _progress(night: Night) -> Table | None:
    start, latest = night.at_start, night.latest
    if start is None or latest is None:
        return None
    table = Table(box=None, padding=(0, 2), show_edge=False, header_style="grey50")
    table.add_column("", style="bold")
    table.add_column("at start", justify="right")
    table.add_column("now", justify="right")
    table.add_column("change", justify="right")
    for label, a, b in (
        ("findings", start.findings, latest.findings),
        ("duplicate groups", start.groups, latest.groups),
        ("rows below T2", start.below, latest.below),
    ):
        table.add_row(label, f"{a:,}", f"{b:,}", _change(a, b))
    return table


def _rounds(night: Night, state: Live, now: dt.datetime, ascii_only: bool) -> Table:
    table = Table(box=None, padding=(0, 1), show_edge=False, header_style="grey50")
    for name, justify in (
        ("round", "right"),
        ("start", "left"),
        ("took", "right"),
        ("", "left"),
        ("cost", "right"),
        ("findings", "right"),
        ("groups", "right"),
        ("below T2", "right"),
    ):
        table.add_column(name, justify=justify)  # type: ignore[arg-type]
    spans = []
    for rnd in night.rounds:
        end = rnd.ended or (now if state.running else night.last_seen)
        spans.append(end - rnd.started if end else None)
    longest = max((s.total_seconds() for s in spans if s), default=1) or 1
    final_cny = state.cny if state.cny is not None else night.cny_closing
    for i, (rnd, span) in enumerate(zip(night.rounds, spans, strict=True)):
        following = night.rounds[i + 1].cny_at_start if i + 1 < len(night.rounds) else final_cny
        cost = f"¥{following - rnd.cny_at_start:.2f}" if following is not None else "-"
        open_ = rnd.ended is None and state.running
        took = Text(duration(span), style="bold green" if open_ else "")
        size = bar((span.total_seconds() if span else 0) / longest, 14, ascii_only=ascii_only)
        size.stylize("cyan")
        moved = (
            [
                _change(rnd.before.findings, rnd.after.findings),
                _change(rnd.before.groups, rnd.after.groups),
                _change(rnd.before.below, rnd.after.below),
            ]
            if rnd.before and rnd.after
            else [Text("…" if open_ else "-", style="grey50")] * 3
        )
        table.add_row(str(rnd.number), f"{rnd.started:%H:%M}", took, size, cost, *moved)
    return table


def _phases(night: Night, state: Live, now: dt.datetime, ascii_only: bool) -> Table | None:
    if not night.rounds:
        return None
    rnd = night.rounds[-1]
    if not rnd.phases:
        return None
    table = Table(box=None, padding=(0, 1), show_edge=False, header_style="grey50")
    table.add_column("")
    table.add_column("phase", style="bold", no_wrap=True)
    table.add_column("start", no_wrap=True)
    table.add_column("took", justify="right", no_wrap=True)
    table.add_column("", no_wrap=True, min_width=14)
    table.add_column("what", style="grey50", overflow="ellipsis", no_wrap=True, max_width=56)

    def span(p: Phase) -> dt.timedelta:
        end = p.ended or (now if state.running else night.last_seen) or p.started
        return end - p.started

    longest = max((span(p).total_seconds() for p in rnd.phases), default=1) or 1
    for p in rnd.phases:
        running = p.ended is None and state.running
        mark = Text("▶" if running else "✓", style="bold green" if running else "cyan")
        if ascii_only:
            mark = Text(">" if running else "+", style=mark.style)
        size = bar(span(p).total_seconds() / longest, 14, ascii_only=ascii_only)
        size.stylize("bold green" if running else "cyan")
        took = Text(duration(span(p)), style="bold green" if running else "")
        table.add_row(mark, p.name, f"{p.started:%H:%M}", took, size, p.detail)
    return table


def _history(nights: list[Night], state: Live, now: dt.datetime, ascii_only: bool) -> Table:
    table = Table(box=None, padding=(0, 1), show_edge=False, header_style="grey50")
    table.add_column("night")
    table.add_column("")
    table.add_column("took", justify="right")
    table.add_column("", no_wrap=True, min_width=16)
    table.add_column("cost", justify="right")
    table.add_column("rounds", justify="right")
    table.add_column("stopped", style="grey50")
    shown = nights[-NIGHTS_SHOWN:]

    def end(n: Night) -> dt.datetime | None:
        if n is nights[-1] and state.running:
            return now
        return n.ended or n.last_seen

    spans = [(end(n) - n.started) if end(n) else None for n in shown]
    longest = max((s.total_seconds() for s in spans if s), default=1) or 1
    for n, s in zip(shown, spans, strict=True):
        tonight = n is nights[-1] and state.running
        if tonight:
            mark, colour, why = ("▶", "green", "running")
        elif n.complete:
            mark, colour, why = ("✓", "cyan", n.stop or "")
        else:
            mark, colour, why = ("✗", "red", "cut off")
        if ascii_only:
            mark = {"▶": ">", "✓": "+", "✗": "x"}[mark]
        size = bar((s.total_seconds() if s else 0) / longest, 16, ascii_only=ascii_only)
        size.stylize(colour)
        cny = state.cny if tonight else n.cny_closing
        cost = f"¥{cny:.2f}" if cny is not None else "-"
        table.add_row(
            f"{n.started:%a %m-%d}",
            Text(mark, style=colour),
            duration(s),
            size,
            cost,
            str(len(n.rounds)),
            Text(why, style=colour if tonight or not n.complete else ""),
        )
    return table


def _section(title: str) -> Text:
    return Text(f"\n{title}", style="bold underline")


def render(
    nights: list[Night], state: Live, *, now: dt.datetime | None = None, ascii_only: bool = False
) -> RenderableType:
    """The whole page for the latest night, with the nights before it underneath."""
    now = now or dt.datetime.now()
    if not nights:
        return Text("the nightly log holds no nights yet", style="yellow")
    night = nights[-1]
    parts: list[RenderableType] = [_header(night, state, now)]
    phase = night.current_phase() if state.running else None
    if phase is not None:
        parts.append(
            Text.assemble(
                ("\nnow  ", "grey50"),
                (phase.name, "bold green"),
                f"  for {duration(now - phase.started)}",
                (f"  — {phase.detail}" if phase.detail else "", "grey50"),
            )
        )
    elif night.stop:
        parts.append(
            Text.assemble(("\nstopped  ", "grey50"), (_WHY.get(night.stop, night.stop), "bold"))
        )
    parts += [_section("Budget"), _budget(night, state, now, ascii_only)]
    progress = _progress(night)
    if progress is not None:
        parts += [_section("Work left"), progress]
    if night.rounds:
        parts += [_section("Rounds"), _rounds(night, state, now, ascii_only)]
    phases = _phases(night, state, now, ascii_only)
    if phases is not None:
        parts += [_section(f"Round {night.rounds[-1].number}, phase by phase"), phases]
    if len(nights) > 1:
        parts += [
            _section(f"Last {min(len(nights), NIGHTS_SHOWN)} nights"),
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
