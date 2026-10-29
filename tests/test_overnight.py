"""The nightly loop: the shell that holds the lock, and the Python that runs the night.

`scripts/overnight.sh` keeps what the deployer has to see — the lock, `--status`, the
wait for a running deploy — and runs `tracker.overnight` as a child for everything
else. The shell parts are tested in a real bash, with the functions lifted out of the
script text, because the property that once failed there was not what a function
computed but whether calling it could kill its caller. The loop is tested in-process
with a stub in place of each `tracker` command, fake counts, a fake clock and a fake
spend tally.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import httpx
import pytest
import respx

from tracker import overnight
from tracker.overnight import Counts, Log, Night, Options, SnapshotFailed, Spent

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "overnight.sh"


# --- the shell ----------------------------------------------------------------------


def _bash() -> str | None:
    """A bash that can actually run a command here, or None to skip.

    `shutil.which` alone is not enough on Windows, where it can find the WSL
    launcher on a machine with no distribution installed.
    """
    bash = shutil.which("bash")
    if bash is None:
        return None
    try:
        done = subprocess.run(
            [bash, "-c", "echo ok"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bash if done.stdout.strip() == "ok" else None


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no working bash on this machine")


def _function(name: str) -> str:
    """One shell function's source, exactly as the script defines it."""
    text = SCRIPT.read_text(encoding="utf-8")
    found = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.MULTILINE | re.DOTALL)
    assert found, f"{name} is not defined in {SCRIPT.name}"
    return found.group(0)


def _lock(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    """`body` with the script's lock functions defined, the lock under `tmp_path`."""
    script = (
        "set -euo pipefail\nLOCK=lock\n" + _function("lock_holder") + _function("take_lock") + body
    )
    return subprocess.run(
        [BASH, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False
    )


@needs_bash
def test_a_lock_left_by_a_run_the_machine_killed_is_taken_over(tmp_path):
    """2026-10-16: a reset at 19:32 ended the run before its EXIT trap, and the lock it
    left refused every later night. A lock whose pid is not a live overnight run is
    stale, and the next run takes it over and says so."""
    (tmp_path / "lock").mkdir()
    (tmp_path / "lock" / "pid").write_text("999999\n")
    done = _lock(tmp_path, 'take_lock && echo "took $$" && cat lock/pid\n')
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert "stale lock" in lines[0]
    assert lines[1].startswith("took ")
    assert lines[2] == lines[1].removeprefix("took ")


@needs_bash
def test_a_lock_still_being_written_is_left_alone(tmp_path):
    """No pid yet means another run is between its `mkdir` and its `echo`."""
    (tmp_path / "lock").mkdir()
    done = _lock(tmp_path, "take_lock\n")
    assert done.returncode == 1
    assert "another overnight run holds" in done.stdout
    assert not (tmp_path / "lock" / "pid").exists()


@needs_bash
def test_a_live_run_keeps_its_lock(tmp_path):
    probe = subprocess.run(
        [BASH, "-c", "ps -p $$ -o command="], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        pytest.skip("this bash's ps cannot name a process (Git Bash)")
    (tmp_path / "overnight.sh").write_text("sleep 30\n")
    done = _lock(
        tmp_path,
        "bash ./overnight.sh & holder=$!\nmkdir lock && echo $holder > lock/pid\n"
        "take_lock; status=$?; kill $holder; exit $status\n",
    )
    assert done.returncode == 1
    assert "another overnight run holds" in done.stdout


def _deploy_wait(
    tmp_path: Path,
    busy: int,
    wait: int = 30,
    procs: tuple[str, ...] = ("/bin/zsh /srv/tracker/ops/poll.sh",),
) -> subprocess.CompletedProcess:
    """`wait_for_deploy` against a stand-in `pgrep -f` and a made-up process table.

    For its first `busy` questions the stand-in matches the pattern it is given, as an
    extended regex the way `pgrep -f` does, against the command lines in `procs`; after
    that the table is empty, as when the deployer has exited. `sleep` does nothing, so
    the bound is counted rather than waited out.
    """
    script = (
        "set -euo pipefail\n"
        f"DEPLOYER=/srv/tracker/ops/poll.sh\nDEPLOY_WAIT={wait}\nBUSY={busy}\n"
        f"PROCS=({' '.join(shlex.quote(p) for p in procs)})\n"
        "sleep() { :; }\n"
        "pgrep() {\n"
        '  [ "$1" = "-f" ] || return 2\n'
        '  [ "$BUSY" -gt 0 ] || return 1\n'
        "  BUSY=$((BUSY - 1))\n"
        "  local p\n"
        '  for p in "${PROCS[@]}"; do [[ $p =~ $2 ]] && return 0; done\n'
        "  return 1\n"
        "}\n"
        + _function("deploy_running")
        + _function("wait_for_deploy")
        + "wait_for_deploy\necho started\n"
    )
    return subprocess.run(
        [BASH, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False
    )


@needs_bash
def test_a_night_that_starts_beside_a_deploy_waits_for_it_to_finish(tmp_path):
    """2026-10-19: two deploys landed while the night ran, so its later phases ran
    different code from its first. The deployer now holds a commit while the lock is
    live; a deploy that got past that check just before 18:01 is waited out here."""
    done = _deploy_wait(tmp_path, busy=3)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "the deployer is running; the night starts when it has finished",
        "the deployer finished after 10s",
        "started",
    ]


@needs_bash
def test_a_deployer_that_never_finishes_does_not_cost_the_night(tmp_path):
    done = _deploy_wait(tmp_path, busy=10**6, wait=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-2:] == [
        "the deployer is still running after 30s; starting anyway",
        "started",
    ]


@needs_bash
def test_with_no_deploy_under_way_the_night_starts_at_once(tmp_path):
    done = _deploy_wait(tmp_path, busy=0)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["started"]


@needs_bash
@pytest.mark.parametrize(
    ("command", "deploying"),
    [
        ("/bin/zsh /srv/tracker/ops/poll.sh", True),
        ("zsh /srv/tracker/ops/poll.sh", True),
        ("/bin/bash /srv/tracker/ops/poll.sh --verbose", True),
        # Found on the host: matched loosely, an ssh command that only quoted the
        # path read as a deploy, and so would an editor open on the file.
        ('zsh -c for i in 1 2; do pgrep -fl "/srv/tracker/ops/poll.sh"; done', False),
        ("vim /srv/tracker/ops/poll.sh", False),
        ("/bin/zsh /srv/tracker/ops/poll.sh.bak-preloopguard", False),
        ("/bin/bash /srv/tracker/repo/scripts/overnight.sh --hours 10", False),
    ],
)
def test_only_a_shell_running_the_poll_script_is_a_deploy(tmp_path, command, deploying):
    done = _deploy_wait(tmp_path, busy=1, procs=(command,))
    assert done.returncode == 0, done.stderr
    assert ("the deployer is running" in done.stdout) is deploying


def test_the_shell_holds_the_lock_and_runs_the_loop_as_its_child():
    """The deployer reads the lock's pid and looks for `overnight.sh` in that process's
    command line. An `exec` into Python would leave a pid whose command line names
    Python, and the deployer would deploy under a running night."""
    text = SCRIPT.read_text(encoding="utf-8")
    check = text.index('"$PY" -m tracker.overnight check "$@" || exit 2')
    ledger = text.index("export TRACKER_SPEND_LEDGER=")
    allowance = text.index('"$PY" -m tracker.spend allowance "$(dirname "$LOG")"')
    tee = text.index('exec > >(tee -a "$LOG") 2>&1')
    lock = text.index("\ntake_lock || exit 1\n")
    trap = text.index("trap 'rm -rf \"$LOCK\"' EXIT")
    wait = text.index("\nwait_for_deploy\n")
    run = text.index('"$PY" -m tracker.overnight run --repo "$REPO" --allowance "$ALLOWANCE" "$@"')
    assert check < ledger < allowance < tee < lock < trap < wait < run
    assert not re.search(r"^\s*exec\s+\"?\$PY", text, re.M), "the loop must not replace the shell"
    assert re.search(r'^DEPLOYER="\$\(cd "\$REPO/\.\." && pwd\)/ops/poll\.sh"$', text, re.M)


def test_the_shell_names_the_ledger_the_way_the_status_page_reads_it():
    text = SCRIPT.read_text(encoding="utf-8")
    assert (
        f"/{overnight.LEDGER_PREFIX}$(date '+{overnight.LEDGER_STAMP}'){overnight.LEDGER_SUFFIX}"
        in text
    )
    assert overnight.ledger_name(dt.datetime(2026, 10, 16, 18, 1, 1)) == (
        "overnight-20261016-180101.spend"
    )


def test_status_greps_for_what_the_loop_prints():
    text = SCRIPT.read_text(encoding="utf-8")
    assert f"grep -n '\\] {overnight.STARTING}'" in text
    assert f"grep -E '^\\[|^{overnight.ROUND_LINE}'" in text


# --- the loop, driven by stubs ------------------------------------------------------

TZ = dt.timezone(dt.timedelta(hours=-4))
START = dt.datetime(2026, 10, 29, 18, 1, 0, tzinfo=TZ)


class Clock:
    def __init__(self, at: dt.datetime = START):
        self.now = at

    def __call__(self) -> dt.datetime:
        return self.now


class Stub:
    """Stands in for every `tracker` command: records it, and can act as it runs."""

    def __init__(self, clock: Clock, step: dt.timedelta, hook: Callable | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.envs: list[dict[str, str]] = []
        self.heads: dict[tuple[str, ...], int | None] = {}
        self.clock = clock
        self.step = step
        self.hook = hook

    def __call__(self, args: Sequence[str], *, env, head=None) -> int:
        call = tuple(args)
        self.calls.append(call)
        self.envs.append(dict(env))
        self.heads[call] = head
        self.clock.now += self.step
        if self.hook:
            return self.hook(call)
        return 0


@dataclasses.dataclass
class Run:
    night: Night
    stub: Stub
    out: io.BytesIO
    status: int | None = None

    @property
    def text(self) -> str:
        return self.out.getvalue().decode("utf-8")


def make(
    tmp_path: Path,
    argv: Sequence[str] = (),
    *,
    counts: Iterable[tuple[int, int, int]] = ((10, 5, 100),),
    spent: Callable[[], Spent] | None = None,
    hook: Callable | None = None,
    step: dt.timedelta = dt.timedelta(minutes=1),
    snapshot: Callable[[], Path] | None = None,
) -> Run:
    """A night over stubs. `counts` are returned in order, the last one repeated."""
    clock = Clock()
    out = io.BytesIO()
    stub = Stub(clock, step, hook)
    series = [Counts(*c) for c in counts]

    def next_count() -> Counts:
        return series.pop(0) if len(series) > 1 else series[0]

    snap = tmp_path / "before.db"

    def take() -> Path:
        snap.write_bytes(b"")
        return snap

    night = Night(
        options=overnight.parse_options(["--repo", str(tmp_path), *argv]),
        log=Log(out, clock),
        runner=stub,
        counts=next_count,
        spent=spent or (lambda: Spent(0.0, 0)),
        snapshot=snapshot or take,
        clock=clock,
        ledger=tmp_path / "overnight-20261029-180100.spend",
        pid=4242,
    )
    return Run(night, stub, out)


def go(run: Run) -> Run:
    run.status = run.night.main()
    return run


TWIN_SINCE = "2026-10-26 22:01:00"
STARTED_UTC = "2026-10-29 22:01:00"

ROUND_ONE = [
    ("discover", "--no-show"),
    ("news", "--baselines", "300", "--max-queries", "1500"),
    ("ingest", "queue", "--new-first", "--limit", "900", "--screen"),
    ("ingest", "geo"),
    (
        "duplicates", "resolve", "--merge", "--min-confidence", "0.85",
        "--created-since", TWIN_SINCE, "--limit", "60",
    ),
    ("backfill", "dates", "--apply", "--refetch", "--yes", "--limit", "150"),
    ("backfill", "scope", "--apply"),
    ("backfill", "derive"),
    ("logic", "resolve", "--auto", "--apply"),
    ("duplicates", "resolve", "--merge", "--limit", "40", "--min-confidence", "0.85"),
    ("audit", "resolve", "--no-ask", "--limit", "60"),
    ("risks", "confirm", "--limit", "40"),
    ("logic", "resolve", "--limit", "60"),
    ("tranches", "review", "--limit", "20"),
    ("enrich", "--select", "30", "--pipeline", "--budget", "120"),
    ("enrich", "--select", "50", "--t2", "--target", "0", "--budget", "200"),
    ("ingest", "reread", "--cached-only", "--existing-only", "--limit", "120"),
    ("ingest", "recheck", "--cached-only", "--existing-only", "--limit", "120"),
    ("backfill", "derive"),
    ("clean", "--snapshot", "--since", "1"),
]  # fmt: skip

LATER_ROUND = [
    ("backfill", "dates", "--apply"),
    ("ingest", "geo"),
    ("backfill", "scope", "--apply"),
    ("backfill", "derive"),
    ("logic", "resolve", "--auto", "--apply"),
    ("duplicates", "resolve", "--merge", "--limit", "40", "--min-confidence", "0.85"),
    ("audit", "resolve", "--no-ask", "--limit", "60"),
    ("risks", "confirm", "--limit", "40"),
    ("logic", "resolve", "--limit", "60"),
    ("tranches", "review", "--limit", "20"),
    ("backfill", "derive"),
    ("clean", "--snapshot", "--since", "1"),
]


def _report(snap: Path) -> list[tuple[str, ...]]:
    return [
        ("clean",),
        ("logic", "check"),
        ("invariants", "--record"),
        ("duplicates",),
        ("duplicates", "mixed"),
        ("news", "report", "--since", STARTED_UTC),
        ("changes", "--against", str(snap)),
    ]


def test_the_night_runs_every_phase_in_order_and_the_first_round_does_more(tmp_path):
    """The whole night, command by command: the news first, then free, duplicates
    ahead of every phase that pays to read one row, audit, risks, logic, tranches,
    the two enrich passes, reread and recheck in round 1 only, a settle per round,
    then the morning report. Identical to the shell's order except that round 1 no
    longer runs the Census lookup twice with nothing in between that adds a row."""
    run = go(
        make(
            tmp_path,
            ["--rounds", "2"],
            counts=[(10, 5, 100), (12, 6, 101), (11, 5, 99), (10, 5, 99)],
        )
    )
    assert run.status == 0
    assert run.stub.calls == ROUND_ONE + LATER_ROUND + _report(tmp_path / "before.db")
    assert run.stub.heads[("logic", "check")] == 12
    assert run.stub.heads[("duplicates",)] == 2
    assert run.stub.heads[("duplicates", "mixed")] == 2


def test_the_twin_lookback_is_days_before_the_start_in_utc(tmp_path):
    """The database's own clock is naive UTC; the host's is local."""
    run = go(make(tmp_path, ["--rounds", "1"]))
    twins = next(c for c in run.stub.calls if "--created-since" in c)
    assert twins[twins.index("--created-since") + 1] == TWIN_SINCE
    assert ("news", "report", "--since", STARTED_UTC) in run.stub.calls


def test_progress_is_measured_from_after_the_news(tmp_path):
    """On 2026-10-19 round 2 lowered all three counts and still read as no progress,
    because the minimums were taken before the news added rows by design."""
    run = go(
        make(
            tmp_path,
            ["--rounds", "1"],
            counts=[(10, 5, 100), (20, 8, 120), (15, 7, 110)],
        )
    )
    assert "    after news 20 finding(s), 8 duplicate group(s), 120 row(s) below T2\n" in run.text
    assert (
        "\n  round 1: findings 20 -> 15 (-5)  groups 8 -> 7 (-1)  below-T2 120 -> 110 (-10)\n"
        in run.text
    )
    assert "nothing below the night's lowest" not in run.text


def test_a_count_that_wobbles_back_down_is_not_progress(tmp_path):
    """The counts from 2026-09-29, round by round. Compared with the round before,
    every dip read as progress and the night ran seven rounds for nothing; against the
    night's lowest it stops after the third."""
    run = go(
        make(
            tmp_path,
            ["--discover", "0"],
            counts=[(334, 12, 525), (335, 12, 524), (334, 14, 527), (334, 12, 524), (335, 11, 528)],
        )
    )
    rounds = re.findall(r"^  round \d+: .*$", run.text, re.M)
    assert len(rounds) == 3
    assert rounds[1] == (
        "  round 2: findings 335 -> 334 (-1)  groups 12 -> 14 (+2)  below-T2 524 -> 527 (+3)"
    )
    stale = re.findall(r"^  nothing below the night's lowest .*$", run.text, re.M)
    assert stale == [
        "  nothing below the night's lowest (334 findings, 12 groups, 524 below T2): 1 of 2 stale rounds",
        "  nothing below the night's lowest (334 findings, 12 groups, 524 below T2): 2 of 2 stale rounds",
    ]
    assert "] 2 rounds with no reduction — this is the fixed point, stopping\n" in run.text


def test_a_round_with_no_change_prints_plus_zero(tmp_path):
    run = go(make(tmp_path, ["--discover", "0", "--rounds", "1"], counts=[(3, 2, 1)]))
    assert (
        "\n  round 1: findings 3 -> 3 (+0)  groups 2 -> 2 (+0)  below-T2 1 -> 1 (+0)\n" in run.text
    )


def test_the_hours_ceiling_is_checked_between_rounds(tmp_path):
    run = go(
        make(
            tmp_path,
            ["--hours", "1", "--discover", "0"],
            counts=[(50, 50, 50), (40, 40, 40), (30, 30, 30)],
            step=dt.timedelta(minutes=5),
        )
    )
    assert len(re.findall(r"^  round \d+:", run.text, re.M)) == 1
    assert "] wall-clock ceiling of 1h reached — stopping between rounds\n" in run.text
    assert "] overnight complete — " in run.text


def test_the_round_limit_ends_a_night_still_making_progress(tmp_path):
    run = go(
        make(
            tmp_path,
            ["--rounds", "3", "--discover", "0"],
            counts=[(50, 50, 50), (40, 40, 40), (30, 30, 30), (20, 20, 20)],
        )
    )
    assert len(re.findall(r"^  round \d+:", run.text, re.M)) == 3
    assert "fixed point" not in run.text and "everything settled" not in run.text
    assert "] round 3 of 3  (¥0.00, ~0 tokens, " in run.text


def test_a_night_with_nothing_left_stops_settled(tmp_path):
    run = go(make(tmp_path, ["--discover", "0"], counts=[(5, 1, 2), (0, 0, 0)]))
    assert len(re.findall(r"^  round \d+:", run.text, re.M)) == 1
    assert "] everything settled\n" in run.text


# Each paid phase, and the command that runs just before its ceiling check.
CEILINGS = [
    ("news", (), ("news", "--baselines", "300", "--max-queries", "1500"), ("ingest", "queue")),
    ("duplicates", (), ("ingest", "geo"), ("duplicates", "resolve")),
    ("duplicates", ("--discover", "0"), ("logic", "resolve", "--auto", "--apply"), ("duplicates",)),
    ("audit", ("--discover", "0"), ("duplicates", "resolve"), ("audit",)),
    ("risks", ("--discover", "0"), ("audit",), ("risks",)),
    ("logic", ("--discover", "0"), ("risks",), ("logic", "resolve", "--limit")),
    ("tranches", ("--discover", "0"), ("logic", "resolve", "--limit"), ("tranches",)),
    ("pipeline", ("--discover", "0"), ("tranches",), ("enrich",)),
    ("enrich", ("--discover", "0"), ("enrich", "--select", "30"), ("enrich", "--select", "50")),
    ("reread", ("--discover", "0"), ("enrich", "--select", "50"), ("ingest", "reread")),
    ("recheck", ("--discover", "0"), ("ingest", "reread"), ("ingest", "recheck")),
]


@pytest.mark.parametrize(("before", "argv", "trigger", "gated"), CEILINGS)
def test_the_ceiling_is_checked_before_every_paid_phase(tmp_path, before, argv, trigger, gated):
    """The shell once promised a check "between phases" and made one per round, so a
    single round could overshoot by a round's worth of agent runs. A night stopped by
    the ceiling still settles once, and still writes its morning report."""
    over = {"now": False}

    def hook(call):
        if call[: len(trigger)] == trigger:
            over["now"] = True
        return 0

    run = go(
        make(
            tmp_path,
            ["--cny", "5", *argv],
            spent=lambda: Spent(5.0 if over["now"] else 1.0, 123),
            hook=hook,
        )
    )
    assert (
        f"] spend ceiling reached (¥5.00 of ¥5, ~123 of 25000000 tokens) before {before} — stopping\n"
        in run.text
    )
    started = run.stub.calls.index(next(c for c in run.stub.calls if c[: len(trigger)] == trigger))
    after = run.stub.calls[started + 1 :]
    assert not any(c[: len(gated)] == gated for c in after[:2]), "the gated phase ran"
    # One settle, after the ceiling, straight after the phase that crossed it.
    assert run.text.count("  settle — re-derive and score") == 1
    assert run.text.count("  settle — re-derive and score, after the ceiling\n") == 1
    assert after[:2] == [("backfill", "derive"), ("clean", "--snapshot", "--since", "1")]
    assert "] overnight complete — " in run.text


def test_the_token_ceiling_still_stops_the_night(tmp_path):
    run = go(
        make(tmp_path, ["--tokens", "1000", "--discover", "0"], spent=lambda: Spent(0.1, 1000))
    )
    assert (
        "spend ceiling reached (¥0.10 of ¥15, ~1000 of 1000 tokens) before duplicates" in run.text
    )


def test_a_ceiling_reached_after_a_settled_round_settles_once_more(tmp_path):
    """Round 1 settles at its end; round 2 is stopped at its first paid phase, after its
    free phase re-derived rows, so it gets the one settle after the ceiling."""
    rounds = {"n": 0}

    def hook(call):
        if call == ("clean", "--snapshot", "--since", "1"):
            rounds["n"] += 1
        return 0

    run = go(
        make(
            tmp_path,
            ["--cny", "5", "--discover", "0"],
            counts=[(50, 50, 50), (40, 40, 40)],
            spent=lambda: Spent(9.0 if rounds["n"] else 0.0, 0),
            hook=hook,
        )
    )
    assert run.text.count("--- ") and run.text.count("  settle — re-derive and score\n") == 1
    assert run.text.count("  settle — re-derive and score, after the ceiling\n") == 1
    assert run.stub.calls.count(("clean", "--snapshot", "--since", "1")) == 2


def test_a_phase_that_fails_costs_that_phase_not_the_night(tmp_path):
    def hook(call):
        if call[0] == "audit":
            raise OSError("no such interpreter")
        return 1  # every other phase "fails" too

    run = go(make(tmp_path, ["--rounds", "1", "--discover", "0"], hook=hook))
    assert (
        "    could not run `tracker audit resolve --no-ask --limit 60`: no such interpreter\n"
        in run.text
    )
    assert ("risks", "confirm", "--limit", "40") in run.stub.calls
    assert "] overnight complete — " in run.text


def test_a_snapshot_that_fails_stops_the_night_before_anything_is_deleted(tmp_path):
    def broken() -> Path:
        raise SnapshotFailed("integrity_check said 'bad'")

    run = go(make(tmp_path, snapshot=broken))
    assert run.status == 1
    assert run.stub.calls == []
    assert (
        "] the snapshot failed (integrity_check said 'bad') — stopping before anything is deleted\n"
        in run.text
    )


def test_a_later_snapshot_that_fails_ends_the_rounds_but_not_the_report(tmp_path):
    taken = {"n": 0}

    def flaky() -> Path:
        taken["n"] += 1
        if taken["n"] > 1:
            raise SnapshotFailed("disk full")
        path = tmp_path / "before.db"
        path.write_bytes(b"")
        return path

    run = go(
        make(
            tmp_path,
            ["--discover", "0", "--backup-every", "2"],
            counts=[(50, 50, 50), (40, 40, 40), (30, 30, 30)],
            snapshot=flaky,
        )
    )
    assert len(re.findall(r"^  round \d+:", run.text, re.M)) == 2
    assert (
        "] the snapshot failed (disk full) — stopping before anything else is deleted\n" in run.text
    )
    assert ("changes", "--against", str(tmp_path / "before.db")) in run.stub.calls


def test_the_morning_report_prices_the_night_from_its_ledger(tmp_path):
    run = make(tmp_path, ["--rounds", "1", "--discover", "0"])
    run.night.ledger.write_text(
        "t\t1\tlogic resolve\tm\t9000\t1000\t8000\t1000\n"
        "t\t1\tlogic resolve\tm\t9000\t1000\t8000\t1000\n"
        "t\t2\trisks confirm\tm\t500\t100\t0\t0\n"
        "2026-09-26T12:00:00Z\t3\tenrich\tdeepseek-flash\t0\t1000000\t0\t0\textract\n",
        encoding="utf-8",
    )
    go(run)
    tail = run.text[run.text.index("] overnight complete — ") :].splitlines()
    assert tail[0].endswith("on models, ~1,020,600 tokens")
    lines = [line.split() for line in tail[1:4]]
    assert lines[0][0] == "enrich/extract" and "¥4.26" in lines[0]
    assert lines[1][:2] == ["logic", "resolve"] and "~20,000" in lines[1] and "89%" in lines[1]
    assert lines[2][:2] == ["risks", "confirm"] and "n/a" in lines[2]
    assert "] what the night changed\n" in run.text


def test_with_no_snapshot_file_the_report_says_so(tmp_path):
    run = make(
        tmp_path, ["--rounds", "1", "--discover", "0"], snapshot=lambda: tmp_path / "gone.db"
    )
    go(run)
    assert "    no snapshot from before round 1 to compare against\n" in run.text
    assert not any(c[0] == "changes" for c in run.stub.calls)


# --- the ceiling and the tally --------------------------------------------------------


@pytest.mark.parametrize(
    ("cny", "allowance", "expected"),
    [
        (None, "16.09", ("16.09", "the month's allowance")),
        (None, "", ("15", "the fallback, as no allowance could be read")),
        (None, None, ("15", "the fallback, as no allowance could be read")),
        (
            None,
            "Traceback (most recent call last)",
            ("15", "the fallback, as no allowance could be read"),
        ),
        (None, "1.2.3", ("15", "the fallback, as no allowance could be read")),
        ("12", "16.09", ("12", "--cny")),
    ],
)
def test_the_money_ceiling_is_the_allowance_unless_named_and_15_when_unread(
    cny, allowance, expected
):
    o = Options(cny=cny, allowance=allowance)
    assert overnight.money_ceiling(o) == expected


def test_the_night_says_where_its_ceiling_came_from(tmp_path):
    run = go(make(tmp_path, ["--rounds", "1", "--discover", "0", "--allowance", ""]))
    assert (
        "    money     ¥15 tonight, from the fallback, as no allowance could be read\n" in run.text
    )
    assert all(env["TRACKER_SPEND_CEILING_CNY"] == "15" for env in run.stub.envs)


def test_a_night_that_has_spent_nothing_tallies_zero(tmp_path):
    """The regression that ran no phase for twenty-one nights: counting nothing must
    be zero, never an error."""
    assert overnight.ledger_spend(tmp_path / "none-yet.spend") == Spent(0.0, 0)
    assert overnight.ledger_spend(None) == Spent(0.0, 0)


def test_the_tally_is_prompt_plus_completion_across_every_call(tmp_path):
    ledger = tmp_path / "night.spend"
    ledger.write_text(
        "2026-09-24T10:00:00Z\t1\tlogic resolve\tdeepseek-flash\t1000\t200\t900\t100\n"
        "2026-09-24T10:01:00Z\t2\tduplicates resolve\tdeepseek-flash\t40\t2\t0\t40\n",
        encoding="utf-8",
    )
    assert overnight.ledger_spend(ledger).tokens == 1242


def test_the_money_ceiling_fires_on_the_bill_not_the_token_count(tmp_path):
    """The token ceiling mostly counted cached prompt, a fiftieth of the price. Two
    nights with the same token count can differ tenfold in money."""
    # A Saturday, so off-peak: a million cached prompt tokens is ¥0.02; a hundred
    # thousand reply tokens is ¥0.43 (OpenCode Go's $0.60 a million, at ¥7.1).
    cached = tmp_path / "cached.spend"
    cached.write_text(
        "2026-09-26T12:00:00Z\t1\tlogic resolve\tdeepseek-flash\t1000000\t0\t1000000\t0\n",
        encoding="utf-8",
    )
    reply = tmp_path / "reply.spend"
    reply.write_text(
        "2026-09-26T12:00:00Z\t1\tenrich\tdeepseek-flash\t0\t100000\t0\t0\textract\n",
        encoding="utf-8",
    )
    for ledger, over in ((cached, False), (reply, True)):
        run = make(tmp_path, ["--cny", "0.30", "--tokens", "999999999"])
        run.night.spent = lambda ledger=ledger: overnight.ledger_spend(ledger)
        assert run.night.over_ceiling()[0] is over


# --- options and the environment ------------------------------------------------------


def test_the_defaults_are_the_shells():
    o = overnight.parse_options([])
    assert dataclasses.replace(o, repo=Path(".")) == Options(repo=Path("."))
    assert (o.hours, o.rounds, o.dry_rounds) == (10, 20, 2)
    assert (o.findings, o.pairs, o.audit, o.risks, o.nest) == (60, 40, 60, 40, 20)
    assert (o.pipeline, o.pipeline_budget, o.enrich, o.enrich_budget, o.enrich_rounds) == (
        30, 120, 50, 200, 1,
    )  # fmt: skip
    assert (o.discover, o.screen, o.baselines, o.search_cap) == (900, True, 300, 2000)
    assert (o.dates, o.reread, o.recheck, o.min_confidence) == (150, 120, 120, "0.85")
    assert (o.merge, o.cny, o.tokens, o.backup_every) == (True, None, 25_000_000, 5)
    assert (o.judgement_effort, o.extraction_effort, o.agent_effort) == ("high", None, None)
    assert o.local_judgement is False


def test_every_child_gets_the_nights_settings(tmp_path):
    run = go(make(tmp_path, ["--rounds", "1"]))
    expected = {
        "TRACKER_SPEND_LEDGER": str(tmp_path / "overnight-20261029-180100.spend"),
        "TRACKER_SPEND_CEILING_CNY": "15",
        "TRACKER_SPEND_CEILING_TOKENS": "25000000",
        "TRACKER_SEARCH_CAP": "2000",
        "TRACKER_DEEPSEEK_JUDGEMENT_EFFORT": "high",
    }
    assert run.stub.envs and all(env == expected for env in run.stub.envs)


def test_the_effort_and_provider_options_reach_every_child(tmp_path):
    run = go(
        make(
            tmp_path,
            [
                "--rounds", "1", "--cny", "9.5", "--tokens", "100000000", "--search-cap", "300",
                "--judgement-effort", "max", "--extraction-effort", "low",
                "--agent-effort", "low", "--local-judgement",
            ],
        )
    )  # fmt: skip
    env = run.stub.envs[0]
    assert env["TRACKER_SPEND_CEILING_CNY"] == "9.5"
    assert env["TRACKER_SPEND_CEILING_TOKENS"] == "100000000"
    assert env["TRACKER_SEARCH_CAP"] == "300"
    assert env["TRACKER_DEEPSEEK_JUDGEMENT_EFFORT"] == "max"
    assert env["TRACKER_DEEPSEEK_EXTRACTION_EFFORT"] == "low"
    assert env["TRACKER_DEEPSEEK_AGENT_EFFORT"] == "low"
    assert env["TRACKER_JUDGEMENT_PROVIDER"] == "ollama"
    # A search cap at or under the 500 the research keeps is the watch's whole cap.
    assert ("news", "--baselines", "300", "--max-queries", "300") in run.stub.calls
    assert "    judgement max effort, on the local model\n" in run.text
    assert "    agent     low effort\n" in run.text


def test_the_exported_names_are_the_settings_they_mean(monkeypatch, tmp_path):
    """The environment beats `.env` only under the name Settings actually reads."""
    from tracker.config import Settings

    for name, value in {
        "TRACKER_DEEPSEEK_AGENT_EFFORT": "low",
        "TRACKER_DEEPSEEK_JUDGEMENT_EFFORT": "max",
        "TRACKER_DEEPSEEK_EXTRACTION_EFFORT": "low",
        "TRACKER_JUDGEMENT_PROVIDER": "ollama",
        "TRACKER_SEARCH_CAP": "123",
        "TRACKER_SPEND_LEDGER": str(tmp_path / "x.spend"),
    }.items():
        monkeypatch.setenv(name, value)
    s = Settings()
    assert s.deepseek_agent_effort == "low"
    assert s.deepseek_judgement_effort == "max"
    assert s.deepseek_extraction_effort == "low"
    assert s.judgement_provider == "ollama"
    assert s.search_cap == 123
    assert s.spend_ledger == tmp_path / "x.spend"


def test_no_merge_never_folds(tmp_path):
    run = go(make(tmp_path, ["--rounds", "1", "--no-merge"]))
    dupes = [c for c in run.stub.calls if c[:2] == ("duplicates", "resolve")]
    assert dupes == [
        ("duplicates", "resolve", "--created-since", TWIN_SINCE, "--limit", "60"),
        ("duplicates", "resolve", "--limit", "40"),
    ]
    assert "    merge=0  enrich=1  screen=1  min-confidence 0.85\n" in run.text


def test_discover_0_skips_the_news_and_places_rows_in_the_free_phase(tmp_path):
    run = go(make(tmp_path, ["--rounds", "1", "--discover", "0"]))
    firsts = {c[0] for c in run.stub.calls}
    assert "discover" not in firsts
    assert not any(c[:2] in {("ingest", "queue"), ("news", "--baselines")} for c in run.stub.calls)
    assert run.stub.calls[:2] == [
        ("backfill", "dates", "--apply", "--refetch", "--yes", "--limit", "150"),
        ("ingest", "geo"),
    ]
    assert "after news" not in run.text


def test_enrich_0_skips_the_t2_pass_but_not_the_pipeline_pass(tmp_path):
    run = go(make(tmp_path, ["--rounds", "1", "--enrich", "0"]))
    enrich = [c for c in run.stub.calls if c[0] == "enrich"]
    assert enrich == [("enrich", "--select", "30", "--pipeline", "--budget", "120")]
    assert "    merge=1  enrich=0  screen=1  min-confidence 0.85\n" in run.text


def test_pipeline_dates_reread_and_recheck_can_each_be_turned_off(tmp_path):
    run = go(
        make(
            tmp_path,
            ["--rounds", "1", "--pipeline", "0", "--dates", "0", "--reread", "0", "--recheck", "0"],
        )
    )
    assert ("backfill", "dates", "--apply") in run.stub.calls
    assert not any(c[:2] in {("ingest", "reread"), ("ingest", "recheck")} for c in run.stub.calls)
    assert [c for c in run.stub.calls if c[0] == "enrich"] == [
        ("enrich", "--select", "50", "--t2", "--target", "0", "--budget", "200")
    ]


def test_no_screen_reads_the_queue_unscreened(tmp_path):
    run = go(make(tmp_path, ["--rounds", "1", "--no-screen"]))
    assert ("ingest", "queue", "--new-first", "--limit", "900", "--no-screen") in run.stub.calls


def test_enrich_rounds_extends_both_enrich_passes(tmp_path):
    run = go(
        make(
            tmp_path,
            ["--rounds", "2", "--enrich-rounds", "2", "--discover", "0"],
            counts=[(50, 50, 50), (40, 40, 40), (30, 30, 30)],
        )
    )
    assert sum(1 for c in run.stub.calls if c[0] == "enrich") == 4
    assert sum(1 for c in run.stub.calls if c[:2] == ("ingest", "reread")) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["--bogus"],
        ["--hours", "ten"],
        ["--judgement-effort", "medium"],
        ["--agent-effort", "extreme"],
        ["--cny", "lots"],
        ["--min-confidence", "-1"],
        ["--backup-every", "0"],
    ],
)
def test_a_bad_option_is_refused(argv):
    with pytest.raises(SystemExit) as stopped:
        overnight.parse_options(argv)
    assert stopped.value.code == 2


def test_check_parses_and_exits_and_run_without_a_verb_is_refused(capsys):
    assert overnight.main(["check", "--no-merge", "--agent-effort", "low"]) == 0
    assert overnight.main([]) == 2
    with pytest.raises(SystemExit) as stopped:
        overnight.main(["check", "--help"])
    assert stopped.value.code == 0
    assert "--agent-effort" in capsys.readouterr().out


# --- the log contract -------------------------------------------------------------


def _nights(run: Run):
    from tracker.webui import statussite

    return statussite.parse_nights(run.text.splitlines(True), tz=TZ)


def test_the_status_page_reads_a_settled_night(tmp_path):
    run = go(make(tmp_path, ["--discover", "0"], counts=[(5, 1, 2), (0, 0, 0)]))
    [night] = _nights(run)
    assert (night.outcome, night.stop) == ("complete", "settled")
    assert night.started_at == START
    assert night.minutes == int(re.search(r"overnight complete — (\d+)m", run.text)[1]) > 0


def test_the_status_page_reads_a_fixed_point(tmp_path):
    run = go(make(tmp_path, ["--discover", "0"], counts=[(5, 1, 2)]))
    [night] = _nights(run)
    assert (night.outcome, night.stop) == ("complete", "settled")
    assert "fixed point" in run.text


def test_the_status_page_reads_a_ceiling(tmp_path):
    run = go(make(tmp_path, ["--cny", "1"], spent=lambda: Spent(2.0, 0)))
    [night] = _nights(run)
    assert (night.outcome, night.stop) == ("complete", "spend_limit")


def test_the_status_page_reads_the_wall_clock(tmp_path):
    run = go(
        make(
            tmp_path,
            ["--hours", "1", "--discover", "0"],
            counts=[(50, 50, 50), (40, 40, 40)],
            step=dt.timedelta(minutes=10),
        )
    )
    [night] = _nights(run)
    assert (night.outcome, night.stop) == ("complete", "time_limit")


def test_the_status_page_reads_a_night_that_was_cut_off(tmp_path):
    def killed(call):
        if call[0] == "risks":
            raise KeyboardInterrupt  # the machine went down mid-phase
        return 0

    run = make(tmp_path, ["--discover", "0"], hook=killed)
    with pytest.raises(KeyboardInterrupt):
        go(run)
    [night] = _nights(run)
    assert night.outcome == "interrupted"
    assert night.minutes is not None and night.minutes > 0


def test_a_signal_stops_the_night_without_calling_it_complete(tmp_path):
    """A stop asked for by a signal is not a bug in the loop: it must get past the
    loop's own error handling, which would otherwise carry on to the report."""

    def stopped(call):
        if call[0] == "audit":
            overnight._stopped(15, None)
        return 0

    run = make(tmp_path, ["--discover", "0"], hook=stopped)
    with pytest.raises(overnight.Stopped) as caught:
        go(run)
    assert caught.value.signum == 15
    assert ("risks", "confirm", "--limit", "40") not in run.stub.calls
    assert "overnight complete" not in run.text


@needs_bash
def test_a_signal_to_the_shell_reaches_the_loop(tmp_path):
    """Run in the foreground, the loop would make bash hold a `kill` until the whole
    night had finished. The shell passes it on and waits for the loop to go."""
    stub = tmp_path / "loop.sh"
    stub.write_text(
        "trap 'echo \"loop got TERM\"; exit 143' TERM\n"
        "echo started\n"
        "for i in $(seq 1 300); do sleep 0.1; done\n"
        "echo 'ran to the end'\n",
        encoding="utf-8",
        newline="\n",
    )
    script = (
        "set -euo pipefail\nREPO=.\nALLOWANCE=\n"
        "PY=./loop-py\n"
        # `"$PY" -m tracker.overnight run …` runs the stub whatever it is given.
        "printf '#!/usr/bin/env bash\\nexec bash ./loop.sh\\n' > loop-py\nchmod +x loop-py\n"
        + _function("run_night")
        + "( sleep 1; kill -TERM $$ ) &\n"
        + 'run_night --hours 1 || { status=$?; echo "night ended $status"; exit $status; }\n'
        + "echo 'night ended 0'\n"
    )
    done = subprocess.run(
        [BASH, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False
    )
    lines = done.stdout.splitlines()
    assert lines == ["started", "loop got TERM", "night ended 143"], done.stderr
    assert done.returncode == 143


def test_the_line_shapes_are_the_shells(tmp_path):
    run = go(make(tmp_path, ["--rounds", "1", "--discover", "0"]))
    text = run.text
    assert text.startswith("\n[2026-10-29 18:01:00] overnight starting  (pid 4242)\n    repo      ")
    assert re.search(
        r"\n--- \d\d:\d\d:\d\d  free — dates, geo, scope, derive, logic --auto\n", text
    )
    assert re.search(
        r"\n\[2026-10-29 \d\d:\d\d:\d\d\] overnight complete — \d+m, ¥0\.00 on models", text
    )
    assert "    snapshot: " in text


@needs_bash
def test_status_lists_the_latest_night_the_loop_wrote(tmp_path):
    """`overnight.sh --status`, which stays in the shell, over a log the loop wrote."""
    if (ROOT / "data" / "runs" / "overnight.lock").exists():
        pytest.skip("a lock is held in this checkout")
    older = go(make(tmp_path, ["--discover", "0"], counts=[(5, 1, 2), (0, 0, 0)])).text
    newer = go(
        make(tmp_path, ["--discover", "0", "--rounds", "2"], counts=[(9, 9, 9), (8, 8, 8)])
    ).text
    log = tmp_path / "overnight.log"
    log.write_bytes((older + newer).encode("utf-8"))
    done = subprocess.run(
        [BASH, SCRIPT.as_posix(), "--status"],
        capture_output=True,
        timeout=60,
        check=False,
        env={**os.environ, "OVERNIGHT_LOG": log.as_posix()},
    )
    assert done.returncode == 0, done.stderr
    lines = done.stdout.decode("utf-8").splitlines()
    assert lines[0] == "not running"
    assert lines[1].endswith("] overnight starting  (pid 4242)")
    assert sum(1 for line in lines if line.startswith("  round ")) == 2
    assert lines[-1].startswith("[2026-10-29 ")
    assert sum(1 for line in lines if "] overnight complete — " in line) == 1


# --- the real pieces ---------------------------------------------------------------


def test_the_counts_open_the_configured_database(tmp_path, monkeypatch):
    """The shell's count read `data/tracker.db` whatever `TRACKER_DB` said, while its
    snapshot honoured it."""
    from tracker.config import get_settings
    from tracker.db import init_db

    db = tmp_path / "elsewhere.db"
    engine, _ = init_db(db)
    engine.dispose()
    monkeypatch.setenv("TRACKER_DB", str(db))
    get_settings.cache_clear()
    try:
        night = overnight.build(Options(repo=tmp_path), io.BytesIO())
        assert night.counts() == Counts(0, 0, 0)
        dest = night.snapshot()
        assert dest.parent == tmp_path / "data" / "backups"
        assert dest.name.startswith("tracker.backup-overnight-")
    finally:
        get_settings.cache_clear()


def test_the_snapshot_goes_beside_the_checkout_when_the_host_has_a_backups_folder(tmp_path):
    from tracker.db import init_db

    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "backups").mkdir()
    db = tmp_path / "t.db"
    engine, _ = init_db(db)
    engine.dispose()
    dest = overnight.snapshot_db(db, repo, START)
    assert dest == tmp_path / "backups" / "tracker.backup-overnight-20261029-180100.db"
    assert dest.is_file()


def test_a_snapshot_of_an_empty_file_is_refused_and_removed(tmp_path):
    import sqlite3

    db = tmp_path / "t.db"
    sqlite3.connect(db).close()
    with pytest.raises(SnapshotFailed, match="none of project"):
        overnight.snapshot_db(db, tmp_path, START)
    assert list((tmp_path / "data" / "backups").iterdir()) == []


def test_a_phase_runs_as_its_own_process_and_a_report_keeps_its_head(tmp_path):
    out = io.BytesIO()
    log = Log(out, Clock())
    run = overnight.process_runner(log)
    status = run(["--help"], env={"PYTHONPATH": str(ROOT)}, head=2)
    assert status == 0
    assert len(out.getvalue().splitlines()) == 2
    assert run(["no-such-command"], env={"PYTHONPATH": str(ROOT)}, head=1) != 0


# --- the ledger itself -------------------------------------------------------------


@pytest.fixture
def keyed(monkeypatch, tmp_path):
    from tracker.config import get_settings

    monkeypatch.setenv("TRACKER_DEEPSEEK_API_KEY", "test-key-not-real")
    monkeypatch.setenv("TRACKER_SPEND_LEDGER", str(tmp_path / "night.spend"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _completion(content: str = "{}") -> dict:
    return {
        "model": "deepseek-flash",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 80,
            "prompt_cache_hit_tokens": 1000,
            "prompt_cache_miss_tokens": 200,
        },
    }


@respx.mock
def test_a_paid_call_appends_one_ledger_line(keyed, monkeypatch):
    from tracker.llm import DeepSeekExtractor

    monkeypatch.setattr("sys.argv", ["tracker", "risks", "confirm", "--limit", "40"])
    respx.post("https://api.deepseek.com/chat/completions").respond(200, json=_completion())
    DeepSeekExtractor(keyed).complete(system="s", user="u")

    lines = keyed.spend_ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    _when, _pid, command, model, prompt, completion, hit, miss, stage = lines[0].split("\t")
    assert (command, model) == ("risks confirm", "deepseek-flash")
    assert (prompt, completion, hit, miss) == ("1200", "80", "1000", "200")
    assert stage == "-"


@respx.mock
def test_a_stage_names_which_part_of_a_command_spent(keyed, monkeypatch):
    """`enrich` reads articles, settles disputes and runs an agent, and a ledger that
    only said "enrich" could not say which of the three cost ¥14."""
    from tracker.llm import DeepSeekExtractor, spend_stage

    monkeypatch.setattr("sys.argv", ["tracker", "enrich", "--select", "15"])
    respx.post("https://api.deepseek.com/chat/completions").respond(200, json=_completion())
    with spend_stage("settle"):
        DeepSeekExtractor(keyed).complete(system="s", user="u")
    DeepSeekExtractor(keyed).complete(system="s", user="u")

    stages = [line.split("\t")[8] for line in keyed.spend_ledger.read_text("utf-8").splitlines()]
    assert stages == ["settle", "-"]


@respx.mock
def test_a_refused_request_costs_nothing_and_writes_nothing(keyed):
    from tracker.llm import DeepSeekExtractor, LLMError

    respx.post("https://api.deepseek.com/chat/completions").respond(400, text="bad request")
    with pytest.raises(LLMError):
        DeepSeekExtractor(keyed).complete(system="s", user="u")
    assert not keyed.spend_ledger.exists()


def test_no_ledger_is_kept_unless_one_is_named(tmp_path, monkeypatch):
    from tracker.config import Settings
    from tracker.llm import record_spend

    monkeypatch.chdir(tmp_path)
    record_spend(Settings(spend_ledger=None), _completion(), "m")
    assert list(tmp_path.iterdir()) == []


def test_a_ledger_that_cannot_be_written_never_costs_the_answer(tmp_path):
    """The call is already paid for. Raising here would throw its answer away."""
    from tracker.config import Settings
    from tracker.llm import record_spend

    record_spend(Settings(spend_ledger=tmp_path / "missing-dir" / "x.spend"), _completion(), "m")


def test_the_command_column_names_the_subcommand_not_its_options(monkeypatch):
    from tracker.llm import _command

    monkeypatch.setattr("sys.argv", ["tracker", "duplicates", "resolve", "--merge"])
    assert _command() == "duplicates resolve"
    monkeypatch.setattr("sys.argv", ["tracker", "enrich", "--select", "15"])
    assert _command() == "enrich"
    monkeypatch.setattr("sys.argv", ["tracker"])
    assert _command() == "-"


def test_the_ledger_counts_agent_turns_too(keyed, monkeypatch):
    """`converse` is the agent loop's call and the most expensive one in the tool."""
    from tracker.llm import DeepSeekExtractor

    reply = {
        "model": "deepseek-flash",
        "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50_000, "completion_tokens": 900},
    }
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, json=reply)
        )
        DeepSeekExtractor(keyed).converse(system="s", messages=[], tools=[])
    line = keyed.spend_ledger.read_text(encoding="utf-8").strip().split("\t")
    assert line[4:6] == ["50000", "900"]
