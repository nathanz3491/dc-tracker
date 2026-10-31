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

**The table is OpenCode Go's, converted to yuan, and will go stale.** Every model
call goes to OpenCode Go (`Settings.reserve_when`, `always` since 2026-10-19; before
that whenever DeepSeek's own balance was low, every night since October), and the
plan's limit is in dollars of OpenCode Go's metering. So the ledger prices at OpenCode Go's rates, which run about
6% above DeepSeek's own: a ceiling read off this module is then a slight overcount
of either bill, never an undercount. When a page changes, change :data:`PRICES`;
nothing else here knows a number. A model the table does not name is priced as the
flash model at the peak rate — the most expensive guess among the models this tool
uses, so an unknown line can make a ceiling fire early but never late.

**Searches are not money from the same pot.** A web search (Serper, prepaid credits)
is a ledger line too (`tracker.llm.record_search`), with the model `serper`
and no tokens. :func:`total` leaves it out — the model ceiling is the model bill —
and :func:`searches` counts it, for the loop's separate search cap. Left out of the
ceiling is not left out of the report, though: the morning's lines price searches
and DeepSeek's fetched pages beside the model spend (:data:`SEARCH_CNY`,
:data:`FETCH_CNY`). On 2026-10-19 the night closed on "¥9.89" while its 780 searches
had spent about ¥5.54 more, over a third of the night's money, and no line said so.

**Peak hours are Beijing time, Monday to Friday, 09:00-12:00 and 14:00-18:00**
(OpenCode Go states them in UTC: 01:00-04:00 and 06:00-10:00), and a call made in
them costs double. Chinese statutory holidays are off-peak all day; they
are not modelled, so a holiday weekday is priced (and guarded, see
`tracker.llm.PeakHours`) as a working day. That errs towards charging too much.

Run as a module for the shell script that reads it::

    python -m tracker.spend total    <ledger>  # CNY, two decimals; 0.00 when empty
    python -m tracker.spend report   <ledger>  # the morning report, by phase
    python -m tracker.spend searches <ledger>  # web searches sent; 0 when none
    python -m tracker.spend summary  <ledger>  # the closing line: models, then searches
    python -m tracker.spend allowance <dir>    # tonight's ceiling from the month's ledgers

**The month.** OpenCode Go allows $100 a month on the flash model, and other work
shares the plan; the tracker keeps to 85% of it (:func:`monthly_budget`). The nightly
loop reads its ceiling from :func:`allowance`, over every ledger in its directory, so
a dear night is paid back by the next ones.

**Mid-run.** Every paid pass asks :func:`ceiling_reached` before each item, against
`TRACKER_SPEND_CEILING_CNY` (and `TRACKER_SPEND_CEILING_TOKENS`), so a pass that
starts under the ceiling stops near it instead of spending its whole batch past it.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: Yuan to the dollar, for OpenCode Go's dollar prices below.
CNY_PER_USD: Final = 7.1

#: CNY per million tokens, off-peak: (prompt served from cache, prompt not cached,
#: reply). Peak is double. OpenCode Go's per-token rates
#: (https://opencode.ai/docs/go/, checked 2026-10-18), in dollars converted at
#: :data:`CNY_PER_USD`: flash $0.003 / $0.15 / $0.60, pro $0.022 / $0.66 / $1.98.
#: DeepSeek's own (api-docs.deepseek.com) are ¥0.02 / ¥1 / ¥4 and ¥0.15 / ¥4.5 /
#: ¥13.5, a few percent lower.
_FLASH: Final = (0.003 * CNY_PER_USD, 0.15 * CNY_PER_USD, 0.60 * CNY_PER_USD)
PRICES: Final[dict[str, tuple[float, float, float]]] = {
    "deepseek-flash": _FLASH,
    "deepseek-v4-flash": _FLASH,
    "deepseek-v4.1-flash": _FLASH,
    "deepseek-v4-pro": (0.022 * CNY_PER_USD, 0.66 * CNY_PER_USD, 1.98 * CNY_PER_USD),
}

#: The model a web search is recorded under. Not a model, and never priced as one.
SEARCH_MODEL: Final = "serper"

#: What one web search costs, in yuan: Serper's prepaid credit at $50 for 50,000
#: searches, $0.001 each — about ¥0.007, the figure `scripts/overnight.sh` budgets
#: with. Prepaid, so it is reported beside the model spend and never counted against
#: the night's money ceiling; `--search-cap` bounds it instead.
SEARCH_CNY: Final = 0.001 * CNY_PER_USD

#: The model a page DeepSeek's Fetch tool returned is recorded under. DeepSeek's
#: money, not the model ceiling's: 5 yuan per 1,000 pages, from the DeepSeek account.
FETCH_MODEL: Final = "deepseek-fetch"

#: What one page DeepSeek's Fetch tool returned costs, in yuan. See `FETCH_MODEL`.
FETCH_CNY: Final = 5 / 1000

#: Lines that are a paid tool, not a model call: counted, never priced as tokens.
_TOOLS: Final = frozenset({SEARCH_MODEL, FETCH_MODEL})

#: What a line naming a model outside :data:`PRICES` is charged, before the peak
#: multiplier: flash's rates, doubled. See the module docstring for why doubled.
UNKNOWN_MODEL: Final = (2 * _FLASH[0], 2 * _FLASH[1], 2 * _FLASH[2])

BEIJING: Final = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")

#: Beijing hours, [start, end), Monday to Friday, at double the price.
PEAK_WINDOWS: Final = ((9, 12), (14, 18))

PEAK_MULTIPLIER: Final = 2.0

#: OpenCode Go's monthly allowance for the flash model, in dollars: $100 from
#: 2026-10-31, $80 from 2026-10-18. On 10-18 Go also capped $40 a week and $16 every
#: five hours.
GO_MONTHLY_USD: Final = 100.0

#: The tracker's share of that allowance. Other work runs on the same plan, and the
#: user set the tracker at about 85% of it (2026-10-25).
TRACKER_SHARE: Final = 0.85

#: The days the allowance is spread over: a rolling month, which keeps a calendar
#: month inside the share as well.
WINDOW_DAYS: Final = 30

#: The most one night may spend, whatever budget is passed in. Above a night's share
#: of the month (¥20.12 since 10-31) so it never trims the share, and inside the
#: tracker's share of Go's other two caps as they stood on 10-18: seven nights at this
#: are ¥168 against 85% of $40 a week (about ¥241), and one is under 85% of $16 per
#: five hours (about ¥97).
NIGHT_MAX_CNY: Final = 24.0

#: How fast the nights pay back what the last 29 days spent over their share -- a
#: phase that ran past the ceiling, a day of hand-run work recorded in a ledger: a
#: seventh of the excess a night, so no one night is left with nothing to spend, and
#: a night at nothing reads no news.
CATCH_UP_DAYS: Final = 7


def monthly_budget() -> float:
    """What the tracker may spend in a rolling month, in yuan: $100 x ¥7.1 x 85% = ¥603.50.

    The nights and the daytime together: since 2026-10-26 a command run by hand and
    the console's AI panels write `daytime-YYYYMM.spend` beside the nights' ledgers
    (`tracker.cli._shared._daytime_ledger`), and :func:`allowance` reads them all, so
    a busy day comes out of the next nights. Until then the daytime spend was
    unrecorded and ¥1 a day was held back for it on a guess.
    """
    return GO_MONTHLY_USD * CNY_PER_USD * TRACKER_SHARE


def allowance(calls: Iterable[Call], *, now: dt.datetime, budget: float | None = None) -> float:
    """Tonight's money ceiling, in yuan, from what the last 29 days spent.

    A night's share of the month (:func:`monthly_budget` / 30, ¥20.12), less a
    seventh of whatever the previous 29 days spent over their share; never below
    nothing or above :data:`NIGHT_MAX_CNY`. Nights that spend their allowance close a
    month at the budget, and a dear stretch is paid back over the next week.

    **Unspent money is not carried over.** Letting a quiet fortnight's savings raise
    the next nights was tried on the 10-25 ledgers and failed: four weeks ran above
    the share on October's underspend, and when the cheap nights left the window the
    month was used up and two nights had nothing to spend.
    """
    month = monthly_budget() if budget is None else budget
    share = month / WINDOW_DAYS
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    since = now - dt.timedelta(days=WINDOW_DAYS - 1)
    spent = total(c for c in calls if c.when is not None and since <= c.when <= now)
    over = max(0.0, spent - share * (WINDOW_DAYS - 1))
    return max(0.0, min(NIGHT_MAX_CNY, share - over / CATCH_UP_DAYS))


def read_all(directory: Path | str) -> list[Call]:
    """Every call in every ledger in `directory`: the nights, and any hand-run ledger."""
    try:
        paths = sorted(Path(directory).glob("*.spend"))
    except OSError:
        return []
    return [call for path in paths for call in read(path)]


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
    """What the model calls cost, in yuan. Searches are another bill; see the module."""
    return sum(call.cost() for call in calls if call.model not in _TOOLS)


def searches(calls: Iterable[Call]) -> int:
    """How many web searches were sent."""
    return sum(1 for call in calls if call.model == SEARCH_MODEL)


def fetches(calls: Iterable[Call]) -> int:
    """How many pages DeepSeek's Fetch tool returned."""
    return sum(1 for call in calls if call.model == FETCH_MODEL)


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
        if call.model in _TOOLS:
            continue
        row = rows.setdefault(call.phase, PhaseSpend(call.phase))
        row.calls += 1
        row.tokens += call.tokens
        row.cny += call.cost()
        row.cache_hit += call.cache_hit
        row.cache_miss += call.cache_miss
    return sorted(rows.values(), key=lambda r: (-r.cny, r.phase))


def summary(calls: Sequence[Call]) -> str:
    """The night's money in one line, for the loop's closing line.

    The model bill first, because it is the one the ceiling counts, then each paid tool
    at its own price. "about": a search is priced at the plan's list rate, not read
    off Serper's invoice.
    """
    models = [c for c in calls if c.model not in _TOOLS]
    parts = [f"¥{total(models):.2f} on models, ~{sum(c.tokens for c in models):,} tokens"]
    sent = searches(calls)
    if sent:
        parts.append(f"{sent:,} web search(es), about ¥{sent * SEARCH_CNY:.2f} of prepaid credit")
    pages = fetches(calls)
    if pages:
        parts.append(f"{pages:,} page(s) through DeepSeek's fetch, about ¥{pages * FETCH_CNY:.2f}")
    return "; ".join(parts)


def report(calls: Sequence[Call]) -> list[str]:
    """The morning report's lines: one per phase, then the night's peak-hour share."""
    found = [c for c in calls if c.model == SEARCH_MODEL]
    fetched = sum(1 for c in calls if c.model == FETCH_MODEL)
    calls = [c for c in calls if c.model not in _TOOLS]
    lines = []
    if fetched:
        lines.append(
            f"    {fetched} page(s) read through DeepSeek's fetch, "
            f"about ¥{fetched * FETCH_CNY:.2f} from the DeepSeek account"
        )
    if found:
        by_command: dict[str, int] = {}
        for call in found:
            by_command[call.phase] = by_command.get(call.phase, 0) + 1
        split = ", ".join(f"{n} {phase}" for phase, n in sorted(by_command.items()))
        lines.append(
            f"    {len(found)} web search(es), about ¥{len(found) * SEARCH_CNY:.2f} of prepaid "
            f"credit outside the ¥ ceiling: {split}"
        )
    if not calls:
        return [*lines, "    no paid model calls"]
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


@dataclass
class _Tally:
    """A ledger read so far: where the last whole line ended, and what it summed to."""

    offset: int = 0
    cny: float = 0.0
    tokens: int = 0


#: One running tally per ledger path. See :func:`ledger_spent`.
_TALLIES: dict[str, _Tally] = {}
_TALLY_LOCK = threading.Lock()


def ledger_spent(path: Path | str | None) -> tuple[float, int]:
    """What a ledger's model calls have cost so far: (yuan, tokens).

    The same sum :func:`total` makes over :func:`read` — searches and fetched pages
    are another bill and count for neither — but **read incrementally**, because a
    paid pass asks before every item and a night's ledger grows to thousands of
    lines. The ledger is append-only, so each call parses only the whole lines
    written since the last one; a file that shrank (a new night, a rotated ledger)
    is read again from the start. A missing or unreadable ledger has spent nothing.
    """
    if path is None:
        return 0.0, 0
    key = str(path)
    with _TALLY_LOCK:
        tally = _TALLIES.setdefault(key, _Tally())
        try:
            size = os.stat(key).st_size
        except OSError:
            _TALLIES[key] = _Tally()
            return 0.0, 0
        if size < tally.offset:
            tally = _TALLIES[key] = _Tally()
        if size > tally.offset:
            try:
                with open(key, "rb") as handle:
                    handle.seek(tally.offset)
                    chunk = handle.read(size - tally.offset)
            except OSError:
                return tally.cny, tally.tokens
            # Only whole lines: a writer may be half way through one.
            end = chunk.rfind(b"\n") + 1
            for raw in chunk[:end].decode("utf-8", errors="replace").splitlines():
                call = parse(raw)
                if call is None or call.model in _TOOLS:
                    continue
                tally.cny += call.cost()
                tally.tokens += call.tokens
            tally.offset += end
        return tally.cny, tally.tokens


def ceiling_reached(settings: Any = None) -> str:
    """Why the night's money is spent, or "" while there is room.

    Compares the current ledger (`TRACKER_SPEND_LEDGER`) against
    `TRACKER_SPEND_CEILING_CNY` and `TRACKER_SPEND_CEILING_TOKENS`; either set and
    reached stops. Neither set, or no ledger, is never reached. Cheap enough to ask
    before every item: see :func:`ledger_spent`.

    **Checked before work starts, never during it.** A call already in flight is
    paid for whatever happens next, so a pass that finds the ceiling reached keeps
    everything it has done and starts nothing new — the overshoot is bounded by the
    calls already running, one wave of a pass, instead of by the pass's whole batch.
    """
    if settings is None:
        from tracker.config import get_settings

        settings = get_settings()
    cny_cap = getattr(settings, "spend_ceiling_cny", None)
    token_cap = getattr(settings, "spend_ceiling_tokens", None)
    if cny_cap is None and token_cap is None:
        return ""
    cny, tokens = ledger_spent(getattr(settings, "spend_ledger", None))
    if cny_cap is not None and cny >= cny_cap:
        return f"¥{cny:.2f} spent against a ¥{cny_cap:.2f} ceiling"
    if token_cap is not None and tokens >= token_cap:
        return f"~{tokens:,} tokens spent against a {token_cap:,}-token ceiling"
    return ""


#: The words every command prints when it stops for :func:`ceiling_reached`. The
#: nightly loop and a person reading a log both look for them; one constant, so no
#: command can say it differently.
CEILING_REACHED: Final = "spend ceiling reached"


def main(argv: Sequence[str] | None = None) -> int:
    """`total`, `report`, `searches` or `summary` of a ledger, or tonight's
    `allowance` from a directory of them.

    Never fails: a shell under `set -e` reads it.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in {"total", "report", "searches", "summary", "allowance"}:
        print(
            "usage: python -m tracker.spend {total|report|searches|summary} <ledger>\n"
            "       python -m tracker.spend allowance <ledger directory>",
            file=sys.stderr,
        )
        return 2
    if args[0] == "allowance":
        print(f"{allowance(read_all(args[1]), now=dt.datetime.now(dt.UTC)):.2f}")
        return 0
    calls = read(args[1])
    if args[0] == "total":
        print(f"{total(calls):.2f}")
    elif args[0] == "searches":
        print(searches(calls))
    else:
        # The ledger is UTF-8 and so is the ¥; a console that is not must not end
        # the morning report.
        out = (summary(calls) if args[0] == "summary" else "\n".join(report(calls))) + "\n"
        sys.stdout.buffer.write(out.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BEIJING",
    "CATCH_UP_DAYS",
    "CEILING_REACHED",
    "CNY_PER_USD",
    "FETCH_CNY",
    "FETCH_MODEL",
    "GO_MONTHLY_USD",
    "NIGHT_MAX_CNY",
    "PEAK_WINDOWS",
    "PRICES",
    "SEARCH_CNY",
    "SEARCH_MODEL",
    "TRACKER_SHARE",
    "UNKNOWN_MODEL",
    "WINDOW_DAYS",
    "Call",
    "PhaseSpend",
    "allowance",
    "by_phase",
    "ceiling_reached",
    "fetches",
    "is_peak",
    "ledger_spent",
    "monthly_budget",
    "next_off_peak",
    "parse",
    "read",
    "read_all",
    "report",
    "searches",
    "summary",
    "total",
]
