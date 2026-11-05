"""What each step of each night cost and did: one JSON line per step, on every night.

Before this the loop knew what each *command* cost (the spend ledger's command and
stage columns) and what each *round* did (findings, groups, rows below T2), and
nothing joined the two: no line said "this step, tonight, cost X and produced Y". A
director choosing between steps cannot be better than that line, and the morning
report could not say which step a night's progress came from either.

So every step the night runs — fixed or directed — appends one record to
`data/runs/steps.jsonl`:

* **cost**, sliced from the night's spend ledger by byte offset: the ledger is
  append-only and the night runs one step at a time, so the lines written between a
  step's start and its end are that step's, exactly;
* **time** and the **exit status** of each command — a phase that crashed is not a
  phase that found nothing, and a director told only "nothing" would stop running it;
* **counts before and after** (:class:`Metrics`): the loop's three, plus the stored
  values and the rows. A step's *result* is the change in the count its kind is read
  in (`steps.Kind.result`), signed so that more is better;
* the director's **prediction**, when there was one, so predicted and actual sit on
  one line.

:func:`prices` turns the last fourteen nights of records into the price list the
director is briefed with. :func:`backfill` rebuilds cost and time (not results) for
nights before the record existed, from `overnight.log` and the nights' ledgers.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, NamedTuple

from tracker import spend

#: The record, beside the log.
FILENAME: Final = "steps.jsonl"
#: Nights the price list reads.
PRICE_NIGHTS: Final = 14


class Metrics(NamedTuple):
    """The counts a step is measured by. Lower is better for the first three."""

    findings: int
    groups: int
    below: int
    #: Stored values across the tracked fields: a value found raises it, a ruling
    #: that empties one lowers it. A field marked "searched, nothing found" does not.
    values: int
    rows: int


#: Which way is better, per count.
BETTER_LOWER: Final = frozenset({"findings", "groups", "below"})


def probe(db_path: Path) -> Metrics:
    """The counts, from a fresh read-only session: the loop's three and two more."""
    from sqlalchemy import case, func, literal, select

    from tracker.db import open_db, session_scope
    from tracker.models import Project
    from tracker.quality import night_counts
    from tracker.vocab import TRACKED_FIELDS

    engine = open_db(db_path)
    try:
        with session_scope(engine, commit=False) as session:
            three = night_counts(session)
            filled = sum(
                (case((getattr(Project, f).is_not(None), 1), else_=0) for f in TRACKED_FIELDS),
                start=literal(0),
            )
            values, rows = session.execute(
                select(func.coalesce(func.sum(filled), 0), func.count(Project.id))
            ).one()
            return Metrics(three.findings, three.groups, three.below_t2, int(values), int(rows))
    finally:
        engine.dispose()


def result(kind_result: str, before: Metrics | None, after: Metrics | None) -> int | None:
    """A step's result in its kind's count, signed so that more is better."""
    if before is None or after is None:
        return None
    was, now = getattr(before, kind_result), getattr(after, kind_result)
    return was - now if kind_result in BETTER_LOWER else now - was


# --- the ledger, sliced -------------------------------------------------------------


def ledger_size(ledger: Path | None) -> int:
    try:
        return ledger.stat().st_size if ledger is not None else 0
    except OSError:
        return 0


@dataclass(frozen=True)
class Cost:
    cny: float = 0.0
    tokens: int = 0
    searches: int = 0
    fetches: int = 0
    calls: int = 0


def cost_of(calls: Iterable[spend.Call]) -> Cost:
    calls = list(calls)
    models = [c for c in calls if c.model not in (spend.SEARCH_MODEL, spend.FETCH_MODEL)]
    return Cost(
        cny=round(spend.total(calls), 4),
        tokens=sum(c.tokens for c in models),
        searches=spend.searches(calls),
        fetches=spend.fetches(calls),
        calls=len(models),
    )


def ledger_slice(ledger: Path | None, start: int, end: int | None = None) -> list[spend.Call]:
    """The calls written to `ledger` between byte offsets `start` and `end` (now)."""
    if ledger is None:
        return []
    try:
        with open(ledger, "rb") as handle:
            handle.seek(start)
            chunk = handle.read() if end is None else handle.read(max(0, end - start))
    except OSError:
        return []
    whole = chunk[: chunk.rfind(b"\n") + 1]
    return [
        c
        for c in map(spend.parse, whole.decode("utf-8", errors="replace").splitlines())
        if c is not None
    ]


# --- one record -------------------------------------------------------------------------


@dataclass
class StepRecord:
    night: str
    mode: str
    round: int
    index: int
    kind: str
    origin: str
    size: int
    started: str
    minutes: float = 0.0
    commands: list[list[str]] = field(default_factory=list)
    exits: list[int] = field(default_factory=list)
    budget: int = 0
    fresh: bool = False
    rows: list[int] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    effort: str | None = None
    note: str = ""
    cny: float = 0.0
    tokens: int = 0
    searches: int = 0
    fetches: int = 0
    before: dict[str, int] | None = None
    after: dict[str, int] | None = None
    #: In the kind's count, more is better. None when a count could not be taken.
    result: int | None = None
    expect: dict[str, float] = field(default_factory=dict)
    why: str = ""
    version: str = ""
    #: Set when the night stopped inside this step (a ceiling).
    stopped: str = ""

    @property
    def failed(self) -> bool:
        return any(code != 0 for code in self.exits)

    def as_json(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in self.__dict__.items()
            if v not in (None, "", [], {}) or k in ("result", "round", "index", "size")
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> StepRecord:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for name in ("night", "mode", "kind", "origin", "started"):
            known.setdefault(name, "")
        for name in ("round", "index", "size"):
            known.setdefault(name, 0)
        return cls(**known)


def append(path: Path, record: StepRecord) -> None:
    """One line. A record that cannot be written is lost, never the night."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.as_json(), ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def read(path: Path) -> list[StepRecord]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(StepRecord.from_json(json.loads(line)))
        except (ValueError, TypeError):
            continue
    return out


# --- the price list ---------------------------------------------------------------------


@dataclass
class Price:
    """One kind's measured price: per unit, over the nights the list reads."""

    kind: str
    records: int = 0
    nights: int = 0
    size: float | None = None
    cny: tuple[float, float, float] | None = None  # p10, median, p90 per unit
    searches: tuple[float, float, float] | None = None
    minutes: tuple[float, float, float] | None = None
    #: Result per unit, with the steps whose result is known.
    result: tuple[float, float, float] | None = None
    #: Result per unit for steps at or under the median size, and over it: whether
    #: yield shrinks with size.
    small: float | None = None
    large: float | None = None
    failed: int = 0
    versions: tuple[str, ...] = ()

    def line(self) -> str:
        def spread(t: tuple[float, float, float] | None, fmt: str) -> str:
            if t is None:
                return "n/a"
            return f"{t[1]:{fmt}} ({t[0]:{fmt}}-{t[2]:{fmt}})"

        parts = [
            f"{self.kind}: {self.records} step(s) over {self.nights} night(s)",
            f"median size {self.size:g}" if self.size is not None else "",
            f"per unit ¥{spread(self.cny, '.4f')}",
            f"searches {spread(self.searches, '.2f')}",
            f"minutes {spread(self.minutes, '.2f')}",
            f"result {spread(self.result, '.3f')}",
        ]
        if self.small is not None and self.large is not None:
            parts.append(f"result/unit small {self.small:.3f} vs large {self.large:.3f}")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if len(self.versions) > 1:
            parts.append(f"code versions {', '.join(self.versions[-3:])}")
        return "; ".join(p for p in parts if p)


def _spread(values: Sequence[float]) -> tuple[float, float, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))]  # noqa: E731
    return pick(0.1), statistics.median(ordered), pick(0.9)


def recent(records: Sequence[StepRecord], nights: int = PRICE_NIGHTS) -> list[StepRecord]:
    """The records of the last `nights` nights, by night stamp."""
    stamps = sorted({r.night for r in records})[-nights:]
    keep = set(stamps)
    return [r for r in records if r.night in keep]


def prices(records: Sequence[StepRecord], nights: int = PRICE_NIGHTS) -> dict[str, Price]:
    """Each kind's price per unit over the last `nights` nights of records."""
    from tracker.night.steps import KINDS

    by_kind: dict[str, list[StepRecord]] = defaultdict(list)
    for r in recent(records, nights):
        by_kind[r.kind].append(r)
    out: dict[str, Price] = {}
    for kind, rows in sorted(by_kind.items()):
        units = [max(1, r.size or 1) for r in rows]
        price = Price(kind, records=len(rows), nights=len({r.night for r in rows}))
        sizes = [r.size for r in rows if r.size]
        price.size = statistics.median(sizes) if sizes else None
        price.cny = _spread([r.cny / u for r, u in zip(rows, units, strict=True)])
        price.searches = _spread([r.searches / u for r, u in zip(rows, units, strict=True)])
        price.minutes = _spread([r.minutes / u for r, u in zip(rows, units, strict=True)])
        known = [(r, u) for r, u in zip(rows, units, strict=True) if r.result is not None]
        price.result = _spread([r.result / u for r, u in known])
        if known and price.size is not None and KINDS.get(kind) and KINDS[kind].hi:
            small = [r.result / u for r, u in known if r.size <= price.size]
            large = [r.result / u for r, u in known if r.size > price.size]
            if small and large:
                price.small, price.large = statistics.median(small), statistics.median(large)
        price.failed = sum(1 for r in rows if r.failed)
        price.versions = tuple(dict.fromkeys(r.version for r in rows if r.version))
        out[kind] = price
    return out


def per_night(records: Sequence[StepRecord], mode: str | None = "fixed") -> dict[str, list[int]]:
    """Each kind's units per night, over nights of `mode` (None: every night)."""
    totals: dict[tuple[str, str], int] = defaultdict(int)
    for r in records:
        if mode is None or r.mode == mode:
            totals[(r.night, r.kind)] += r.size
    out: dict[str, list[int]] = defaultdict(list)
    for (_night, kind), units in sorted(totals.items()):
        out[kind].append(units)
    return dict(out)


# --- the nights before the record --------------------------------------------------------

#: The phase lines a fixed night printed, and the kind and size each names.
_PHASES: Final = (
    (re.compile(r"^news — .*?, (\d+) queued article"), "news"),
    (re.compile(r"^duplicates — one attempt per row created"), "twins"),
    (re.compile(r"^free — "), "free"),
    (re.compile(r"^duplicates — (\d+) pair"), "duplicates"),
    (re.compile(r"^audit — implausible figures, (\d+)"), "audit"),
    (re.compile(r"^risks — unquoted obstacles, (\d+)"), "risks"),
    (re.compile(r"^logic — a model reads the sources, (\d+)"), "logic"),
    (re.compile(r"^suspects — a model re-reads the (\d+)"), "suspects"),
    (re.compile(r"^tranches — a model nests the tranches of (\d+)"), "tranches"),
    (re.compile(r"^enrich — (\d+) campus\(es\) being built.*?, (\d+) article budget"), "pipeline"),
    (re.compile(r"^enrich — (\d+) row\(s\) below T2, (\d+) article budget"), "enrich"),
    (re.compile(r"^enrich — (\d+) row\(s\) the director chose.*?, (\d+) article budget"), "rows"),
    (re.compile(r"^reread — (\d+) article"), "reread"),
    (re.compile(r"^recheck — (\d+) article"), "recheck"),
    (re.compile(r"^settle — "), ""),
    (re.compile(r"^losses — "), ""),
)
_STAMPED: Final = re.compile(r"^\[(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)\] (.*)$")
_PHASE: Final = re.compile(r"^--- (\d\d:\d\d:\d\d)  (.*)$")
_ROUND: Final = re.compile(r"^round (\d+) of ")


def _kind_of(text: str) -> tuple[str, int, int] | None:
    for pattern, kind in _PHASES:
        found = pattern.search(text)
        if found:
            numbers = [int(g) for g in found.groups()]
            return kind, (numbers[0] if numbers else 0), (numbers[1] if len(numbers) > 1 else 0)
    return None


def backfill(log_path: Path, ledgers: Path, tz: dt.tzinfo, *, since: str = "") -> list[StepRecord]:
    """Cost and time per step for the nights in `log_path`, from their ledgers.

    Results are left unknown: a round's counts cannot be split between its steps after
    the fact. Each night's ledger is the `overnight-*.spend` file whose stamp is the
    night's start; calls are given to the phase whose window holds their time.
    """
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    nights: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    day: dt.date | None = None
    last_time: dt.time | None = None
    number = 0
    for line in lines:
        stamped = _STAMPED.match(line)
        if stamped:
            when = dt.datetime.fromisoformat(f"{stamped.group(1)} {stamped.group(2)}")
            message = stamped.group(3)
            if message.startswith("overnight starting"):
                current = {"start": when, "phases": []}
                nights.append(current)
                day, last_time, number = when.date(), when.time(), 0
            elif current is not None:
                day, last_time = when.date(), when.time()
                found = _ROUND.match(message)
                if found:
                    number = int(found.group(1))
                elif message.startswith(("overnight complete", "what is left", "stopped")):
                    current["phases"].append(("", 0, 0, when, number))
            continue
        phase = _PHASE.match(line)
        if phase and current is not None and day is not None:
            at = dt.time.fromisoformat(phase.group(1))
            if last_time is not None and at < last_time:
                day += dt.timedelta(days=1)
            last_time = at
            found = _kind_of(phase.group(2))
            if found is not None:
                kind, size, budget = found
                current["phases"].append((kind, size, budget, dt.datetime.combine(day, at), number))
    out: list[StepRecord] = []
    for night in nights:
        stamp = night["start"].strftime("%Y%m%d-%H%M%S")
        if since and stamp < since:
            continue
        candidates = sorted(ledgers.glob(f"overnight-{night['start']:%Y%m%d}-*.spend"))
        ledger = next(
            (
                p
                for p in candidates
                if p.stem.split("-", 1)[1] >= night["start"].strftime("%Y%m%d-%H%M")
            ),
            candidates[0] if candidates else None,
        )
        calls = spend.read(ledger)
        phases = night["phases"]
        index = 0
        for i, (kind, size, budget, start, number) in enumerate(phases):
            if not kind:
                continue
            end = phases[i + 1][3] if i + 1 < len(phases) else None
            lo = start.replace(tzinfo=tz).astimezone(dt.UTC)
            hi = end.replace(tzinfo=tz).astimezone(dt.UTC) if end else None
            mine = [
                c
                for c in calls
                if c.when is not None and c.when >= lo and (hi is None or c.when < hi)
            ]
            cost = cost_of(mine)
            index += 1
            if kind == "twins":
                # Its phase line names no size; it has always asked about at most this
                # many rows, and priced per step it read as ¥1.56 "a unit".
                from tracker.night.steps import TWINS

                size = TWINS
            out.append(
                StepRecord(
                    night=stamp,
                    mode="fixed",
                    round=number,
                    index=index,
                    kind=kind,
                    origin="backfill",
                    size=size if kind != "free" else 0,
                    budget=budget,
                    started=start.isoformat(timespec="seconds"),
                    minutes=round(((end - start).total_seconds() / 60) if end else 0.0, 2),
                    cny=cost.cny,
                    tokens=cost.tokens,
                    searches=cost.searches,
                    fetches=cost.fetches,
                )
            )
    return out


__all__ = [
    "FILENAME",
    "PRICE_NIGHTS",
    "Cost",
    "Metrics",
    "Price",
    "StepRecord",
    "append",
    "backfill",
    "cost_of",
    "ledger_size",
    "ledger_slice",
    "per_night",
    "prices",
    "probe",
    "read",
    "recent",
    "result",
]
