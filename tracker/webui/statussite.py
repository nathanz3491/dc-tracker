"""The public status page: do the public addresses answer, did last night's update
run, and how fresh is the data.

Three sources, none of them the dataset's own tables:

* **Checks.** `Prober` asks each public address — the console, the API, the docs,
  the map — through the internet, the way a reader reaches it, every
  `Settings.status_interval_s`. Through the tunnel rather than to the local port,
  because the tunnel is the part that fails without the process noticing.
* **The nightly log.** Whether the night's update started, finished, stopped at a
  ceiling or was cut off, read from `data/runs/overnight.log` — so the history is
  there from the first day, not from the day this module shipped.
* **The database**, read-only, for when an article was last read and a value last
  changed.

**The checks live in their own file**, `status.db` beside `tracker.db`, written with
the standard library. Not a migration and not the dataset: a check every five
minutes would otherwise queue behind the nightly loop's writes, and a write that
waited past its slot would leave a gap that reads as an outage. The file is
operational telemetry, safe to delete, and `scripts/sync_db.py` never sees it.

**A gap is counted as downtime.** The prober runs in the same process that serves
every public address, so the only thing that silences it for more than a few
minutes is that process, or the machine under it, being down — which is exactly
when the addresses are down too. Counting a gap as "no data" instead would make the
page report its best uptime on the days the server was off. A gap that ends in a
fresh boot of the machine is labelled a restart, from the boot time each start
records. The page says all of this in a sentence a reader can find.

Times are stored and sent in UTC with an offset; the browser shows them in the
reader's own zone. Days are bucketed in the host's zone, which is where the nightly
update's schedule is set.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

log = logging.getLogger(__name__)

#: The public addresses, in the order the page lists them, and the path each is
#: asked for. A health path where there is one, the front page otherwise.
SERVICES: Final[tuple[tuple[str, str], ...]] = (
    ("console", "/"),
    ("api", "/v1/health"),
    ("docs", "/"),
    ("map", "/"),
)

#: Days of history the page draws, and the store keeps.
HISTORY_DAYS: Final = 90

#: A check slower than this answered, but not well enough to call operational.
SLOW_MS: Final = 4000

#: Seconds a check may take before it counts as failed.
CHECK_TIMEOUT_S: Final = 15.0

#: Consecutive failed checks before a service is "down" rather than "degraded" —
#: one failed request is a blip as often as it is an outage.
DOWN_AFTER: Final = 2

#: Nights the page lists.
NIGHTS_SHOWN: Final = 14

#: A run that another run follows within this was restarted by hand, not cut off.
FALSE_START: Final = dt.timedelta(hours=3)

#: How late a round may start, past its interval and a half, before the stretch
#: without it is a gap: room for the round before it to have taken its time.
GAP_SLACK: Final = dt.timedelta(minutes=1)

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS check_result (
    id          INTEGER PRIMARY KEY,
    round_at    TEXT    NOT NULL,
    service     TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    http_status INTEGER,
    latency_ms  INTEGER,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_check_round ON check_result (round_at);
CREATE TABLE IF NOT EXISTS monitor_start (
    id         INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    booted_at  TEXT
);
CREATE TABLE IF NOT EXISTS note (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    starts_at  TEXT,
    ends_at    TEXT,
    service    TEXT,
    title      TEXT NOT NULL,
    title_zh   TEXT
);
"""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


# --- the store --------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    service: str
    ok: bool
    http_status: int | None = None
    latency_ms: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class Note:
    id: int
    created_at: dt.datetime
    title: str
    title_zh: str | None = None
    starts_at: dt.datetime | None = None
    ends_at: dt.datetime | None = None
    service: str | None = None


class Store:
    """`status.db`: the checks, each start of the prober, and notes an operator wrote.

    A connection per call: the prober's thread and every request thread use it, and
    a `sqlite3` connection belongs to the thread that opened it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.executescript(_SCHEMA)
        return conn

    def record(self, round_at: dt.datetime, checks: Iterable[Check]) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO check_result (round_at, service, ok, http_status, latency_ms, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (_iso(round_at), c.service, int(c.ok), c.http_status, c.latency_ms, c.error)
                    for c in checks
                ],
            )

    def started(self, at: dt.datetime, booted_at: dt.datetime | None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO monitor_start (started_at, booted_at) VALUES (?, ?)",
                (_iso(at), _iso(booted_at)),
            )

    def prune(self, before: dt.datetime) -> None:
        """Forget checks and starts older than `before`. Notes are kept."""
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM check_result WHERE round_at < ?", (_iso(before),))
            conn.execute("DELETE FROM monitor_start WHERE started_at < ?", (_iso(before),))

    def checks_since(self, since: dt.datetime) -> list[tuple[dt.datetime, Check]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT round_at, service, ok, http_status, latency_ms, error FROM check_result"
                " WHERE round_at >= ? ORDER BY round_at, id",
                (_iso(since),),
            ).fetchall()
        return [(_parse(r[0]), Check(r[1], bool(r[2]), r[3], r[4], r[5])) for r in rows]

    def starts_since(self, since: dt.datetime) -> list[tuple[dt.datetime, dt.datetime | None]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT started_at, booted_at FROM monitor_start WHERE started_at >= ?"
                " ORDER BY started_at",
                (_iso(since),),
            ).fetchall()
        return [(_parse(a), _parse(b)) for a, b in rows]

    def add_note(
        self,
        title: str,
        *,
        title_zh: str | None = None,
        starts_at: dt.datetime | None = None,
        ends_at: dt.datetime | None = None,
        service: str | None = None,
    ) -> int:
        title = " ".join(title.split())
        if not title:
            raise ValueError("a note needs a title")
        if service is not None and service not in dict(SERVICES):
            raise ValueError(f"no service {service!r}; one of {', '.join(dict(SERVICES))}")
        if starts_at and ends_at and ends_at < starts_at:
            raise ValueError("a note cannot end before it starts")
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO note (created_at, starts_at, ends_at, service, title, title_zh)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _iso(utc_now()),
                    _iso(starts_at),
                    _iso(ends_at),
                    service,
                    title,
                    " ".join((title_zh or "").split()) or None,
                ),
            )
            return int(cursor.lastrowid)

    def remove_note(self, note_id: int) -> bool:
        with closing(self._connect()) as conn, conn:
            return conn.execute("DELETE FROM note WHERE id = ?", (note_id,)).rowcount > 0

    def notes(self) -> list[Note]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, created_at, title, title_zh, starts_at, ends_at, service FROM note"
                " ORDER BY COALESCE(starts_at, created_at) DESC"
            ).fetchall()
        return [
            Note(r[0], _parse(r[1]), r[2], r[3], _parse(r[4]), _parse(r[5]), r[6]) for r in rows
        ]


# --- checking ---------------------------------------------------------------------


def boot_time() -> dt.datetime | None:
    """When this machine last booted, or None where that cannot be read."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5
            ).stdout
            found = re.search(r"sec = (\d+)", out)
            return dt.datetime.fromtimestamp(int(found.group(1)), dt.UTC) if found else None
        if sys.platform.startswith("linux"):
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    return dt.datetime.fromtimestamp(int(line.split()[1]), dt.UTC)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def targets(hosts: dict[str, str | None]) -> list[tuple[str, str]]:
    """(service, URL) for each public address this server has a hostname for."""
    return [
        (service, f"https://{hosts[service]}{path}")
        for service, path in SERVICES
        if hosts.get(service)
    ]


def check(service: str, url: str, *, timeout: float = CHECK_TIMEOUT_S, client=None) -> Check:
    """Ask one address once. A 2xx or 3xx answer is up; anything else is not."""
    import httpx

    started = time.perf_counter()
    try:
        own = client is None
        client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        try:
            response = client.get(url, headers={"User-Agent": "dc-tracker-status"})
        finally:
            if own:
                client.close()
    except httpx.HTTPError as exc:
        return Check(service, False, None, None, f"{type(exc).__name__}: {exc}"[:300])
    latency = int((time.perf_counter() - started) * 1000)
    ok = 200 <= response.status_code < 400
    return Check(
        service, ok, response.status_code, latency, None if ok else f"HTTP {response.status_code}"
    )


class Prober(threading.Thread):
    """Checks every public address each interval, for as long as the server runs."""

    def __init__(
        self,
        store: Store,
        addresses: list[tuple[str, str]],
        *,
        interval_s: int,
        checker: Callable[[str, str], Check] = check,
        clock: Callable[[], dt.datetime] = utc_now,
    ) -> None:
        super().__init__(name="status-prober", daemon=True)
        self.store = store
        self.addresses = addresses
        self.interval_s = interval_s
        self.checker = checker
        self.clock = clock
        self.halt = threading.Event()

    def round(self) -> None:
        """One round: every address, recorded under one timestamp."""
        at = self.clock()
        self.store.record(at, [self.checker(service, url) for service, url in self.addresses])

    def run(self) -> None:
        try:
            self.store.started(self.clock(), boot_time())
        except sqlite3.Error as exc:
            log.error("status: cannot open %s: %s", self.store.path, exc)
            return
        pruned_on: dt.date | None = None
        # A round first, then the wait: a server that has just come back up says so
        # at once, rather than an interval later.
        while True:
            try:
                self.round()
                today = self.clock().date()
                if pruned_on != today:
                    self.store.prune(self.clock() - dt.timedelta(days=HISTORY_DAYS + 1))
                    pruned_on = today
            except Exception:  # a check must never take the server down with it
                log.exception("status: a round of checks failed")
            if self.halt.wait(self.interval_s):
                return

    def stop(self) -> None:
        self.halt.set()


# --- the nightly log --------------------------------------------------------------

_STAMPED = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$")
_PHASE = re.compile(r"^--- (\d\d:\d\d:\d\d)  ")
_MINUTES = re.compile(r"overnight complete — (\d+)m")


@dataclass
class Night:
    started_at: dt.datetime
    ended_at: dt.datetime | None = None
    last_seen_at: dt.datetime | None = None
    outcome: str = "interrupted"  # complete | interrupted | running
    stop: str | None = None  # settled | spend_limit | time_limit
    minutes: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "last_seen_at": _iso(self.last_seen_at),
            "outcome": self.outcome,
            "stop": self.stop,
            "minutes": self.minutes,
        }


def parse_nights(lines: Iterable[str], *, tz: dt.tzinfo, running: bool = False) -> list[Night]:
    """Every night the log records, oldest first.

    A night is from one "overnight starting" to the next. One that never printed
    "overnight complete" was cut off — unless it is the last and `running` says the
    loop still holds its lock. The log's clock is the host's, given as `tz`.
    """
    nights: list[Night] = []
    for raw in lines:
        line = raw.rstrip("\n")
        stamped = _STAMPED.match(line)
        if stamped:
            at = dt.datetime.strptime(stamped[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            text = stamped[2]
            if text.startswith("overnight starting"):
                nights.append(Night(started_at=at, last_seen_at=at))
                continue
            if not nights:
                continue
            night = nights[-1]
            night.last_seen_at = at
            if text.startswith("overnight complete"):
                night.ended_at = at
                night.outcome = "complete"
                minutes = _MINUTES.search(text)
                night.minutes = int(minutes[1]) if minutes else None
            elif "spend ceiling reached" in text or "token ceiling reached" in text:
                night.stop = "spend_limit"
            elif "wall-clock ceiling" in text:
                night.stop = "time_limit"
            elif "fixed point" in text or "everything settled" in text:
                night.stop = night.stop or "settled"
            continue
        phase = _PHASE.match(line)
        if phase and nights:
            night = nights[-1]
            clock = dt.datetime.strptime(phase[1], "%H:%M:%S").time()
            seen = dt.datetime.combine(night.started_at.date(), clock, tzinfo=tz)
            if seen < night.started_at:
                seen += dt.timedelta(days=1)
            night.last_seen_at = max(night.last_seen_at or seen, seen)
    # A start that another start follows within hours was stopped by hand and run
    # again, not cut off: it is not a night anybody missed.
    nights = [
        night
        for night, following in zip(nights, [*nights[1:], None], strict=True)
        if night.outcome == "complete"
        or following is None
        or following.started_at - night.started_at > FALSE_START
    ]
    for night in nights:
        if night.outcome != "complete" and night.last_seen_at:
            night.minutes = int((night.last_seen_at - night.started_at).total_seconds() // 60)
    if nights and running and nights[-1].outcome != "complete":
        nights[-1].outcome = "running"
    return nights


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def loop_running(runs_dir: Path) -> bool:
    """Whether the nightly loop holds its lock with a live process."""
    try:
        pid = int((runs_dir / "overnight.lock" / "pid").read_text().strip())
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


_night_cache: dict[str, Any] = {}


def _ledger_last(runs_dir: Path, night: Night) -> dt.datetime | None:
    """The last paid call the night's spend ledger records, if there is a ledger.

    The log prints a line per phase, and a phase can run for an hour without one:
    the 2026-10-16 night's last phase line was at 18:03 and it was working until
    19:26. Its ledger (`overnight-YYYYMMDD-HHMMSS.spend`, named by the script when it
    starts, one tab-separated line per model call, UTC first) knows better.
    """
    local = night.started_at
    best: Path | None = None
    for path in runs_dir.glob(f"overnight-{local:%Y%m%d}-*.spend"):
        try:
            named = dt.datetime.strptime(path.stem[len("overnight-") :], "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if abs((named.replace(tzinfo=local.tzinfo) - local).total_seconds()) <= 300:
            best = path
            break
    if best is None:
        return None
    try:
        with best.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4096))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            return _parse(line.split("	", 1)[0].strip())
        except ValueError:
            continue
    return None


def read_nights(runs_dir: Path) -> list[Night]:
    """The nightly log's nights, re-read only when the log changes.

    Each of the last `NIGHTS_SHOWN` nights is then given its last moment of work
    from its spend ledger, which moves while the log is quiet.
    """
    path = runs_dir / "overnight.log"
    running = loop_running(runs_dir)
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (str(path), stat.st_mtime_ns, stat.st_size, running)
    if _night_cache.get("key") != key:
        tz = dt.datetime.now().astimezone().tzinfo
        with path.open(encoding="utf-8", errors="replace") as handle:
            # Only the lines this reads, so a 13 MB log costs one pass and no copy.
            wanted = (line for line in handle if line.startswith(("[", "--- ")))
            _night_cache["nights"] = parse_nights(wanted, tz=tz, running=running)
        _night_cache["key"] = key
    nights = [Night(**vars(night)) for night in _night_cache["nights"]]
    for night in nights[-NIGHTS_SHOWN:]:
        if night.outcome == "complete":
            continue
        last = _ledger_last(runs_dir, night)
        if last and (night.last_seen_at is None or last > night.last_seen_at):
            night.last_seen_at = last.astimezone(night.started_at.tzinfo)
            night.minutes = int((night.last_seen_at - night.started_at).total_seconds() // 60)
    return nights


# --- the page's numbers -----------------------------------------------------------


@dataclass
class _Timeline:
    """Per service, the seconds up and observed in each local day."""

    up: dict[dt.date, float] = field(default_factory=dict)
    seen: dict[dt.date, float] = field(default_factory=dict)

    def add(self, start: dt.datetime, end: dt.datetime, ok: bool, tz: dt.tzinfo) -> None:
        cursor = start
        while cursor < end:
            local = cursor.astimezone(tz)
            next_midnight = dt.datetime.combine(
                local.date() + dt.timedelta(days=1), dt.time(), tzinfo=tz
            )
            stop = min(end, next_midnight)
            seconds = (stop - cursor).total_seconds()
            day = local.date()
            self.seen[day] = self.seen.get(day, 0.0) + seconds
            if ok:
                self.up[day] = self.up.get(day, 0.0) + seconds
            cursor = stop


def _rounds(
    checks: list[tuple[dt.datetime, Check]],
) -> list[tuple[dt.datetime, dict[str, Check]]]:
    rounds: dict[dt.datetime, dict[str, Check]] = {}
    for at, result in checks:
        rounds.setdefault(at, {})[result.service] = result
    return sorted(rounds.items())


def _gaps(
    rounds: list[tuple[dt.datetime, dict[str, Check]]],
    starts: list[tuple[dt.datetime, dt.datetime | None]],
    *,
    interval: dt.timedelta,
    now: dt.datetime,
) -> list[dict[str, Any]]:
    """Stretches with no checks at all: the server was not running.

    One missed round is enough: a round is due one interval after the last finished,
    so rounds sit an interval plus a round's own time apart, and anything past half
    an interval more — `GAP_SLACK` beyond that — is a round that never ran. A restart
    of the machine takes about ten minutes, which a looser rule missed.

    A stretch counts from one interval after the last round before it, which is when
    the next round was due, to the first round after it. The current silence, if the
    page is being served while the prober is not reporting, is one too, still open.
    """
    gaps: list[dict[str, Any]] = []
    limit = interval * 1.5 + GAP_SLACK
    times = [at for at, _ in rounds]
    for before, after in pairwise(times):
        if after - before > limit:
            boot = next(
                (b for s, b in starts if b and before <= b <= after and before <= s <= after),
                None,
            )
            gaps.append(
                {
                    "start": before + interval,
                    "end": after,
                    "cause": "restart" if boot else "offline",
                    "booted_at": boot,
                }
            )
    if times and now - times[-1] > limit:
        gaps.append(
            {"start": times[-1] + interval, "end": None, "cause": "silent", "booted_at": None}
        )
    return gaps


def _service_state(results: list[Check]) -> str:
    if not results:
        return "unknown"
    latest = results[-1]
    if not latest.ok:
        failing = 0
        for result in reversed(results):
            if result.ok:
                break
            failing += 1
        return "outage" if failing >= DOWN_AFTER else "degraded"
    if latest.latency_ms is not None and latest.latency_ms > SLOW_MS:
        return "degraded"
    return "operational"


def _outages(
    rounds: list[tuple[dt.datetime, dict[str, Check]]], services: list[str]
) -> list[dict[str, Any]]:
    """Runs of `DOWN_AFTER` or more failed checks, merged where they overlap."""
    spans: list[dict[str, Any]] = []
    for service in services:
        run: list[dt.datetime] = []
        for at, results in rounds:
            result = results.get(service)
            if result is None:
                continue
            if not result.ok:
                run.append(at)
                continue
            if len(run) >= DOWN_AFTER:
                spans.append({"start": run[0], "end": at, "services": [service]})
            run = []
        if len(run) >= DOWN_AFTER:
            spans.append({"start": run[0], "end": None, "services": [service]})
    spans.sort(key=lambda s: s["start"])
    merged: list[dict[str, Any]] = []
    for span in spans:
        last = merged[-1] if merged else None
        if last and (last["end"] is None or span["start"] <= last["end"]):
            last["services"] = sorted({*last["services"], *span["services"]})
            if last["end"] is not None:
                last["end"] = None if span["end"] is None else max(last["end"], span["end"])
            continue
        merged.append(dict(span))
    return merged


def payload(
    store: Store,
    *,
    services: list[str],
    interval_s: int,
    nights: list[Night],
    data: dict[str, Any],
    links: dict[str, str | None],
    now: dt.datetime | None = None,
    tz: dt.tzinfo | None = None,
) -> dict[str, Any]:
    """Everything the status page draws, as one JSON object."""
    now = now or utc_now()
    tz = tz or dt.datetime.now().astimezone().tzinfo
    interval = dt.timedelta(seconds=interval_s)
    since = now - dt.timedelta(days=HISTORY_DAYS)
    rounds = _rounds(store.checks_since(since))
    starts = store.starts_since(since - dt.timedelta(days=1))
    gaps = _gaps(rounds, starts, interval=interval, now=now)
    today = now.astimezone(tz).date()
    days = [today - dt.timedelta(days=n) for n in range(HISTORY_DAYS - 1, -1, -1)]

    rows: list[dict[str, Any]] = []
    for service in services:
        timeline = _Timeline()
        results: list[Check] = []
        observed = [(at, r[service]) for at, r in rounds if service in r]
        for index, (at, result) in enumerate(observed):
            results.append(result)
            following = observed[index + 1][0] if index + 1 < len(observed) else now
            timeline.add(at, min(following, at + interval), result.ok, tz)
        for gap in gaps:
            if observed and gap["start"] >= observed[0][0]:
                timeline.add(gap["start"], gap["end"] or now, False, tz)
        state = _service_state(results)
        if gaps and gaps[-1]["end"] is None:
            state = "unknown"
        seen = sum(timeline.seen.values())
        up = sum(timeline.up.values())
        latest = results[-1] if results else None
        rows.append(
            {
                "id": service,
                "state": state,
                "url": links.get(service),
                "checked_at": _iso(observed[-1][0]) if observed else None,
                "latency_ms": latest.latency_ms if latest else None,
                "uptime": round(up / seen, 5) if seen else None,
                "days": [
                    {
                        "date": day.isoformat(),
                        "uptime": round(timeline.up.get(day, 0.0) / timeline.seen[day], 5)
                        if timeline.seen.get(day)
                        else None,
                    }
                    for day in days
                ],
            }
        )

    incidents: list[dict[str, Any]] = []
    for gap in gaps:
        incidents.append(
            {
                "kind": gap["cause"],
                "start": _iso(gap["start"]),
                "end": _iso(gap["end"]),
                "services": services,
                "booted_at": _iso(gap["booted_at"]),
            }
        )
    for outage in _outages(rounds, services):
        incidents.append(
            {
                "kind": "outage",
                "start": _iso(outage["start"]),
                "end": _iso(outage["end"]),
                "services": outage["services"],
            }
        )
    for night in nights:
        if night.outcome == "interrupted" and night.started_at >= since:
            incidents.append(
                {
                    "kind": "night_interrupted",
                    "start": _iso(night.started_at),
                    "end": _iso(night.last_seen_at),
                    "services": [],
                }
            )
    for note in store.notes():
        if (note.starts_at or note.created_at) < since:
            continue
        incidents.append(
            {
                "kind": "note",
                "start": _iso(note.starts_at or note.created_at),
                "end": _iso(note.ends_at),
                "services": [note.service] if note.service else [],
                "title": note.title,
                "title_zh": note.title_zh,
            }
        )
    incidents.sort(key=lambda i: i["start"] or "", reverse=True)

    states = [row["state"] for row in rows]
    if not rows or all(s == "unknown" for s in states):
        overall = "unknown"
    elif all(s == "outage" for s in states):
        overall = "outage"
    elif any(s == "outage" for s in states):
        overall = "partial_outage"
    elif any(s in ("degraded", "unknown") for s in states):
        overall = "degraded"
    else:
        overall = "operational"

    shown = nights[-NIGHTS_SHOWN:]
    first = rounds[0][0] if rounds else None
    return {
        "generated_at": _iso(now),
        "overall": overall,
        "interval_s": interval_s,
        "monitoring_since": _iso(first),
        "history_days": HISTORY_DAYS,
        "services": rows,
        "nightly": {
            "latest": shown[-1].as_json() if shown else None,
            "nights": [n.as_json() for n in reversed(shown)],
        },
        "data": data,
        "incidents": incidents,
        "links": links,
    }


def freshness(session: Any) -> dict[str, Any]:
    """When the data last moved: campuses held, last article read, last value changed."""
    from sqlalchemy import func, select

    from tracker.models import IngestUrl, Project

    def stamp(value: dt.datetime | None) -> str | None:
        return _iso(value.replace(tzinfo=dt.UTC)) if value else None

    return {
        "campuses": session.scalar(select(func.count()).select_from(Project)) or 0,
        "last_read_at": stamp(
            session.scalar(
                select(func.max(IngestUrl.last_tried_at)).where(
                    IngestUrl.status.in_(("ok", "no_project"))
                )
            )
        ),
        "last_change_at": stamp(session.scalar(select(func.max(Project.updated_at)))),
    }


_payload_cache: dict[str, Any] = {}


def cached(
    key: Any, compute: Callable[[], dict[str, Any]], *, seconds: float = 30.0
) -> dict[str, Any]:
    """`compute()`, reused for `seconds`: the numbers move once a check interval."""
    now = time.monotonic()
    hit = _payload_cache.get("value")
    if (
        hit is not None
        and _payload_cache.get("key") == key
        and now - _payload_cache["at"] < seconds
    ):
        return hit
    value = compute()
    _payload_cache.update(key=key, value=value, at=now)
    return value


__all__ = [
    "HISTORY_DAYS",
    "SERVICES",
    "Check",
    "Night",
    "Note",
    "Prober",
    "Store",
    "boot_time",
    "cached",
    "check",
    "freshness",
    "loop_running",
    "parse_nights",
    "payload",
    "read_nights",
    "targets",
]
