#!/usr/bin/env bash
#
# The nightly quality loop, for launchd at 18:01 or for running unattended in tmux.
#
# WHAT THE NIGHT DOES lives in `tracker/overnight.py`, tested: the options, the
# snapshot, the rounds and their phases, the ceilings, the progress counts and stop
# rules, and the morning report. Its docstring is the argument for the phase order,
# the volumes and the ceilings. This script is what has to stay a shell script:
#
#   * THE LOCK. The deployer (outside this repo) decides a night is running by reading
#     data/runs/overnight.lock/pid and checking that process's command line names
#     `overnight.sh`. So this script takes the lock, holds it for the whole night and
#     releases it in its EXIT trap, and runs the Python loop as a child — never `exec`:
#     an exec'd process's command line names Python, and the deployer would read the
#     night as over and deploy under it.
#   * `--status`, which must answer from any shell, with or without a venv.
#   * the wait for a deployer already running when the night starts;
#   * PATH and the venv, which launchd does not give a process;
#   * the night's spend ledger, named here so the status page can find it by the
#     night's start (`overnight-YYYYMMDD-HHMMSS.spend`), and exported so every
#     `tracker` child appends to it;
#   * the read of tonight's money allowance (`tracker.spend allowance`, over every
#     ledger beside tonight's), handed to the loop, which falls back to ¥15 when it
#     reads nothing;
#   * the `tee` into data/runs/overnight.log, so the loop and every phase it runs
#     write to one stream.
#
# RUNNING IT IN TMUX
#
#   tmux new -s tracker
#   caffeinate -i ~/dev/tracker/repo/scripts/overnight.sh --hours 10
#   # detach with ctrl-b then d; come back with `tmux attach -t tracker`
#
# `caffeinate -i` stops the machine idle-sleeping out from under a ten-hour job.
# From anywhere else, `overnight.sh --status` prints where it has got to;
# `overnight.sh --help` lists the options.
#
# The night runs on one version of the code. A deploy resets the checkout under the
# commands not yet started and runs `tracker init`, which wants the same single write
# lock: on 2026-10-19 two deploys landed mid-run, so the night's later phases ran
# different code from its first. The deployer now holds any commit while this run's
# lock is live, and this run waits for a deployer that is already running when it
# starts (`wait_for_deploy`). Still, do not push to main while it runs: the held
# commit goes live unwatched when the night ends.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PY="$REPO/.venv/bin/python"

LOG="${OVERNIGHT_LOG:-$REPO/data/runs/overnight.log}"
LOCK="$REPO/data/runs/overnight.lock"
# The deploy poller, which sits beside the checkout in `ops/`, never inside it.
DEPLOYER="$(cd "$REPO/.." && pwd)/ops/poll.sh"
DEPLOY_WAIT=1800

# Only the two options this script answers itself; every other one is the loop's,
# passed through untouched and checked by it before the lock is taken.
STATUS_ONLY=0
HELP=0
for arg in "$@"; do
  case "$arg" in
    --status)  STATUS_ONLY=1 ;;
    -h|--help) HELP=1 ;;
  esac
done

if [ "$STATUS_ONLY" -eq 1 ]; then
  if [ -d "$LOCK" ] && [ "$(cat "$LOCK/pid" 2>/dev/null)" != "" ] \
      && ! ps -p "$(cat "$LOCK/pid")" -o command= 2>/dev/null | grep -q "overnight.sh"; then
    echo "not running (a stale lock is left from pid $(cat "$LOCK/pid"); the next run takes it over)"
  elif [ -d "$LOCK" ]; then
    echo "running, pid $(cat "$LOCK/pid" 2>/dev/null || echo '?')"
  else
    echo "not running"
  fi
  # The latest run only. The log is appended night after night, and a run that
  # logs fewer than twenty such lines let the night before's "overnight complete"
  # open the listing — on 10-22 it read as if 10-20 were the last run.
  # `|| true`: under pipefail a log with no round lines yet is not an error.
  # The two patterns are `STARTING` and `ROUND_LINE` in tracker/overnight.py.
  if [ -f "$LOG" ]; then
    start="$(grep -n '\] overnight starting' "$LOG" | tail -1 | cut -d: -f1 || true)"
    tail -n +"${start:-1}" "$LOG" | grep -E '^\[|^  round ' | tail -20 || true
  fi
  exit 0
fi

[ -x "$PY" ] || { echo "no venv at $PY" >&2; exit 1; }

if [ "$HELP" -eq 1 ]; then
  "$PY" -m tracker.overnight run --help
  exit 0
fi

# A mistyped option ends here, before the lock and before the log.
"$PY" -m tracker.overnight check "$@" || exit 2

mkdir -p "$(dirname "$LOG")"
# One ledger per night, so the tally reads only this run's calls — summing a shared
# file made a second night start at the first night's total. Exported, so every
# `tracker` child appends to it. The name is `ledger_name` in tracker/overnight.py,
# which the status page reads back.
export TRACKER_SPEND_LEDGER="${TRACKER_SPEND_LEDGER:-$(dirname "$LOG")/overnight-$(date '+%Y%m%d-%H%M%S').spend}"
# Tonight's share of the month, read before tonight's ledger exists: from Go's own
# usage windows, or the ledgers when Go cannot be read. Its one-line why goes to the
# night's log. Empty when it cannot be read at all; the loop then falls back, and
# `--cny` overrides it either way.
ALLOWANCE="$("$PY" -m tracker.spend allowance "$(dirname "$LOG")" 2>>"$LOG" || true)"
exec > >(tee -a "$LOG") 2>&1

# One at a time. `mkdir` is the atomic primitive available everywhere; macOS has no
# flock. The pid inside says whose it is.
#
# A lock outlives its run whenever the run never reaches its EXIT trap — the machine
# loses power or is reset. On 2026-10-16 a reset at 19:32 left one behind, and since
# a held lock refused every later start, no night would ever have run again until
# somebody removed it by hand. So a lock whose pid is not a live overnight run is
# taken over, and the log says so. A lock with no pid yet is a run between its
# `mkdir` and its `echo`, and is left alone.
lock_holder() {
  local pid
  pid=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -z "$pid" ]; then
    echo "starting"
  elif ps -p "$pid" -o command= 2>/dev/null | grep -q "overnight.sh"; then
    echo "$pid"
  else
    echo "stale"
  fi
}

take_lock() {
  if mkdir "$LOCK" 2>/dev/null; then
    echo $$ > "$LOCK/pid"
    return 0
  fi
  local holder
  holder=$(lock_holder)
  if [ "$holder" = "stale" ]; then
    echo "taking over a stale lock: pid $(cat "$LOCK/pid" 2>/dev/null) is not an overnight run (a reset or power loss ended it)"
    rm -rf "$LOCK"
    mkdir "$LOCK" 2>/dev/null || { echo "another run took the lock first"; return 1; }
    echo $$ > "$LOCK/pid"
    return 0
  fi
  echo "another overnight run holds $LOCK (pid $holder)"
  return 1
}

take_lock || exit 1
trap 'rm -rf "$LOCK"' EXIT

# A deploy already under way when the night starts is let finish first. The deployer
# holds any commit while the lock above is live, but one that passed that check a
# moment before the lock was taken still resets the checkout and runs `tracker init`
# under the first phases — and a push made just before 18:01 to catch the night lands
# in exactly that minute. Each side raises its own flag before it looks at the
# other's: this run takes its lock, then looks for the deployer; the deployer is
# running, then looks for the lock. One of the two always sees the other, so they
# never both go ahead. Bounded, because a night that never starts is worse than one
# that starts beside a deployer that has hung.
#
# Only a shell running the script counts: matched loosely, any process whose command
# line merely names the file — an editor on it, an ssh command quoting its path —
# read as a deploy under way.
deploy_running() {
  pgrep -f "^[^ ]*(zsh|bash|sh) $DEPLOYER( |\$)" >/dev/null 2>&1
}

wait_for_deploy() {
  local waited=0
  deploy_running || return 0
  echo "the deployer is running; the night starts when it has finished"
  while deploy_running; do
    if [ "$waited" -ge "$DEPLOY_WAIT" ]; then
      echo "the deployer is still running after ${waited}s; starting anyway"
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "the deployer finished after ${waited}s"
}

wait_for_deploy

# The night. A child, not `exec` — see THE LOCK above. Its own stdin is /dev/null and
# so is every phase's: nothing in the night may wait on a keyboard.
#
# In the background with `wait`, so a signal to this script — launchd stopping the
# job, a `kill` on the lock's pid — is passed on to the loop, which stops the phase it
# is in and exits. Run in the foreground, bash would hold the signal until its child
# finished, which is now the whole night. A trapped signal interrupts `wait`, so it
# waits again until the loop has actually gone, and only then exits and drops the lock.
run_night() {
  local night status=0
  OVERNIGHT_PID=$$ "$PY" -m tracker.overnight run --repo "$REPO" --allowance "$ALLOWANCE" "$@" \
    < /dev/null &
  night=$!
  trap 'kill -TERM "$night" 2>/dev/null || true' TERM INT HUP
  wait "$night" || status=$?
  while kill -0 "$night" 2>/dev/null; do
    wait "$night" || status=$?
  done
  return "$status"
}

run_night "$@" || exit $?
