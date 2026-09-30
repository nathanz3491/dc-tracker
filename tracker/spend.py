"""What the spend ledger's calls cost in money, and when the provider charges double.

`tracker.llm.record_spend` writes one line per paid call: tokens, split into the
prompt the provider served from its cache, the prompt it did not, and the reply.
Counting those tokens is how the overnight loop used to bound a night, and it bounded
the wrong thing. On this workload about nine tokens in ten are cached prompt, which
DeepSeek bills at a fiftieth of an uncached one, and about 93% of the money goes on
the reply — mostly the model's own reasoning. So a night's token count said almost
nothing about its bill: 25,000,000 tokens was anywhere from ¥20 to ¥31, and the ceiling
set in tokens never fired on a night that cost ¥34.

This module prices each line instead, from the provider's published table and the
hour the call was made, so a ceiling can be written in the currency the bill is in.

**The table is copied from DeepSeek's pricing page and will go stale.** When the page
changes, change :data:`PRICES`; nothing else here knows a number. A model the table
does not name is priced as the flash model at the peak rate — the most expensive
guess among the models this tool uses, so an unknown line can make a ceiling fire
early but never late.

**Peak hours are Beijing time, Monday to Friday, 09:00-12:00 and 14:00-18:00**, and a
call made in them costs double. Chinese statutory holidays are off-peak all day; they
are not modelled, so a holiday weekday is priced (and guarded, see
`tracker.llm.PeakHours`) as a working day. That errs towards charging too much.

Run as a module for the shell script that reads it::

    python -m tracker.spend total  <ledger>    # CNY, two decimals; 0.00 when empty
    python -m tracker.spend report <ledger>    # the morning report, by phase
"""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: CNY per million tokens, off-peak: (prompt served from cache, prompt not cached,
#: reply). Peak is double. From https://api-docs.deepseek.com/zh-cn/quick_start/pricing
#: as of 2026-09. The reserve's model is priced as flash: OpenCode Go bills a
#: subscription allowance rather than tokens, and its listed per-token rates are
#: within a few percent of these.
PRICES: Final[dict[str, tuple[float, float, float]]] = {
    "deepseek-flash": (0.02, 1.0, 4.0),
    "deepseek-v4-flash": (0.02, 1.0, 4.0),
    "deepseek-v4.1-flash": (0.02, 1.0, 4.0),
    "deepseek-v4-pro": (0.15, 4.5, 13.5),
}

#: What a line naming a model outside :data:`PRICES` is charged, before the peak
#: multiplier: flash's rates, doubled. See the module docstring for why doubled.
UNKNOWN_MODEL: Final = (0.04, 2.0, 8.0)

BEIJING: Final = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")

#: Beijing hours, [start, end), Monday to Friday, at double the price.
PEAK_WINDOWS: Final = ((9, 12), (14, 18))

PEAK_MULTIPLIER: Final = 2.0


def is_peak(when: dt.datetime) -> bool:
    """Whether a call made at `when` is billed at the peak rate.

    A naive `when` is taken to be UTC, which is what the ledger writes.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    local = when.astimezone(BEIJING)
    if local.weekday() >= 5:
        return False
    return any(start <= local.hour < end for start, end in PEAK_WINDOWS)


def next_off_peak(when: dt.datetime) -> dt.datetime:
    """The first moment at or after `when` that is not peak, in Beijing time."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    local = when.astimezone(BEIJING)
    for _ in PEAK_WINDOWS:
        ends = next((end for start, end in PEAK_WINDOWS if start <= local.hour < end), None)
        if ends is None or not is_peak(local):
            break
        local = local.replace(hour=ends, minute=0, second=0, microsecond=0)
    return local


@dataclass(frozen=True)
class Call:
    """One ledger line, parsed."""

    when: dt.datetime | None
    command: str
    model: str
    prompt: int
    completion: int
    cache_hit: int
    cache_miss: int
    #: Which part of a command made the call — `extract`, `settle`, `agent` — or ""
    #: on a line written before the ledger carried one.
    stage: str = ""

    @property
    def tokens(self) -> int:
        return self.prompt + self.completion

    @property
    def phase(self) -> str:
        """`enrich/extract`, or the bare command when no stage was recorded."""
        return f"{self.command}/{self.stage}" if self.stage else self.command

    def cost(self) -> float:
        """CNY. An unsplit prompt is charged as uncached, the dearer of the two."""
        hit_rate, miss_rate, out_rate = PRICES.get(self.model, UNKNOWN_MODEL)
        hit, miss = self.cache_hit, self.cache_miss
        if hit + miss == 0:
            miss = self.prompt
        multiplier = PEAK_MULTIPLIER if self.when is not None and is_peak(self.when) else 1.0
        return multiplier * (hit * hit_rate + miss * miss_rate + self.completion * out_rate) / 1e6


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


def parse(line: str) -> Call | None:
    """One ledger line, or None for one too short to be a call."""
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 8:
        return None
    try:
        when = dt.datetime.strptime(parts[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        when = None
    return Call(
        when=when,
        command=parts[2],
        model=parts[3],
        prompt=_int(parts[4]),
        completion=_int(parts[5]),
        cache_hit=_int(parts[6]),
        cache_miss=_int(parts[7]),
        stage=parts[8] if len(parts) > 8 and parts[8] != "-" else "",
    )


def read(path: Path | str | None) -> list[Call]:
    """Every call in a ledger. A missing or unreadable file is a night that spent nothing."""
    if path is None:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    return [call for call in map(parse, text.splitlines()) if call is not None]


def total(calls: Iterable[Call]) -> float:
    return sum(call.cost() for call in calls)


@dataclass
class PhaseSpend:
    phase: str
    calls: int = 0
    tokens: int = 0
    cny: float = 0.0
    cache_hit: int = 0
    cache_miss: int = 0

    @property
    def cache_rate(self) -> float | None:
        seen = self.cache_hit + self.cache_miss
        return self.cache_hit / seen if seen else None


def by_phase(calls: Iterable[Call]) -> list[PhaseSpend]:
    """Spend per command and stage, dearest first."""
    rows: dict[str, PhaseSpend] = {}
    for call in calls:
        row = rows.setdefault(call.phase, PhaseSpend(call.phase))
        row.calls += 1
        row.tokens += call.tokens
        row.cny += call.cost()
        row.cache_hit += call.cache_hit
        row.cache_miss += call.cache_miss
    return sorted(rows.values(), key=lambda r: (-r.cny, r.phase))


def report(calls: Sequence[Call]) -> list[str]:
    """The morning report's lines: one per phase, then the night's peak-hour share."""
    if not calls:
        return ["    no paid calls"]
    lines = []
    for row in by_phase(calls):
        cache = "cache n/a" if row.cache_rate is None else f"cache {row.cache_rate:.0%}"
        lines.append(
            f"    {row.phase:24} {row.calls:6d} call(s)  ~{row.tokens:,} tokens  "
            f"¥{row.cny:.2f}  {cache}"
        )
    peak = [c for c in calls if c.when is not None and is_peak(c.when)]
    if peak:
        lines.append(
            f"    {len(peak)} call(s) at peak rates, ¥{total(peak):.2f} — half of that "
            "was the peak surcharge"
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """`total <ledger>` or `report <ledger>`. Never fails: a shell under `set -e` reads this."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in {"total", "report"}:
        print("usage: python -m tracker.spend {total|report} <ledger>", file=sys.stderr)
        return 2
    calls = read(args[1])
    if args[0] == "total":
        print(f"{total(calls):.2f}")
    else:
        # The ledger is UTF-8 and so is the ¥; a console that is not must not end
        # the morning report.
        out = "\n".join(report(calls)) + "\n"
        sys.stdout.buffer.write(out.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BEIJING",
    "PEAK_WINDOWS",
    "PRICES",
    "UNKNOWN_MODEL",
    "Call",
    "PhaseSpend",
    "by_phase",
    "is_peak",
    "next_off_peak",
    "parse",
    "read",
    "report",
    "total",
]
