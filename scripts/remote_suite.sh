#!/usr/bin/env bash
# The production host's half of scripts/remote_suite.py, which pipes this file to
# `ssh $PROD bash -s -- <mode> ...` for every step, so the version that runs is the
# one in the working tree being tested.
#
#   prepare <dir> <origin-url>         create the scratch clone if missing; fetch origin
#   start <dir> <sha> <workers> [args] check the snapshot out; start the run, detached
#   follow <dir> <offset>              print the run's log from a byte offset until it ends
#   stop <dir>                         end the run in progress
#   profile <home> [port ...]          print the sandbox profile (what the tests read)
#
# The run is detached because the link from the dev machine drops now and then: a
# run tied to one ssh session died with it. `follow` reattaches at the byte where the
# last one stopped. The run itself is `job`, started by `start` from the snapshot.
#
# <dir> is the scratch clone, relative to $HOME unless absolute. It must be named
# tracker-dev and may not be, or sit inside, production's checkout, deployer or
# backups; anything else is refused before a file is touched.
#
# Written for the /bin/bash macOS ships (3.2): no associative arrays, no mapfile.
# docs/testing-on-the-host.md says what each guard is for.
set -uo pipefail

# Under ~/dev/tracker, beside the scratch clone: what a test may neither read nor write.
PRODUCTION_DIRS=(repo ops backups)

die() {
  printf 'remote suite: %s\n' "$*" >&2
  exit 2
}

# The sandbox the tests run in. Everything is allowed except: production's files,
# the credential directories, the network past loopback, and every port already
# listening on this machine -- the console among them. A server a test starts is
# on a port opened later, so it stays reachable. Later rules override earlier ones.
profile() {
  local home="$1"
  shift
  printf '(version 1)\n(allow default)\n'
  printf '(deny network-outbound (remote ip "*:*"))\n'
  printf '(allow network-outbound (remote ip "localhost:*"))\n'
  local port
  for port in "$@"; do
    printf '(deny network-outbound (remote ip "localhost:%s"))\n' "$port"
  done
  local d
  for d in "${PRODUCTION_DIRS[@]}"; do
    printf '(deny file-read* file-write* (subpath "%s/dev/tracker/%s"))\n' "$home" "$d"
  done
  printf '(deny file-read* file-write* (subpath "%s/.ssh") (subpath "%s/.cloudflared"))\n' \
    "$home" "$home"
  # The interpreter, uv's cache and Homebrew are shared with production: readable,
  # since the test venv runs on that interpreter, but never written.
  printf '(deny file-write* (subpath "%s/Library/LaunchAgents") (subpath "%s/.local")' \
    "$home" "$home"
  printf ' (subpath "%s/.cache/uv") (subpath "/opt/homebrew"))\n' "$home"
  printf '(deny process-exec (literal "/bin/launchctl"))\n'
}

# Every TCP port something listens on, whoever owns it. netstat rather than lsof,
# which shows a user only their own processes.
listening_ports() {
  netstat -an -p tcp 2>/dev/null | awk '$6 == "LISTEN" {print $4}' |
    sed -E 's/.*[.:]([0-9]+)$/\1/' | sort -un
}

# The scratch clone's absolute path, or a refusal. Shell built-ins only, so that the
# refusal cannot depend on what happens to be on PATH.
resolve_dir() {
  local dir="$1" d physical home
  case "$dir" in
    /*) ;;
    *) dir="$HOME/$dir" ;;
  esac
  dir="${dir%/}"
  [ "${dir##*/}" = "tracker-dev" ] ||
    die "refusing $dir: the scratch clone must be a directory named tracker-dev"
  # Where a symlink really leads, against where production really is: on macOS a
  # path under /var is physically under /private/var.
  physical="$dir"
  if [ -e "$dir" ]; then
    physical="$(cd -P "$dir" && pwd)" || die "cannot resolve $dir"
  fi
  home="$(cd -P "$HOME" && pwd)" || die "cannot resolve $HOME"
  for d in "${PRODUCTION_DIRS[@]}"; do
    case "$dir/" in "$HOME/dev/tracker/$d/"*) die "refusing $dir: it is inside production's $d/" ;; esac
    case "$physical/" in "$home/dev/tracker/$d/"*) die "refusing $dir: it leads into production's $d/" ;; esac
  done
  printf '%s\n' "$dir"
}

# Enter an existing scratch clone, or stop.
enter() {
  local dir
  dir="$(resolve_dir "$1")" || exit 2
  [ -d "$dir/.git" ] || die "no scratch clone at $dir (the prepare step makes it)"
  cd "$dir" || die "cannot enter $dir"
  mkdir -p .suite
}

prepare() {
  local dir origin="$2"
  dir="$(resolve_dir "$1")" || exit 2
  if [ ! -d "$dir/.git" ]; then
    [ -e "$dir" ] && die "$dir exists and is not a git clone; move it aside"
    mkdir -p "$dir" && git -C "$dir" init --quiet && git -C "$dir" remote add origin "$origin" ||
      die "could not create the scratch clone at $dir"
    # No housekeeping in the background after a fetch: it would hold the ssh
    # session open until it finished.
    git -C "$dir" config gc.auto 0
    git -C "$dir" config maintenance.auto false
    echo "created the scratch clone at $dir"
  fi
  # So the push that follows sends only what GitHub does not already have.
  git -C "$dir" fetch --quiet origin </dev/null ||
    echo "fetching origin failed; the push sends more instead"
}

# The pid holding the lock, if that process is alive.
holder() {
  local pid
  pid="$(cat .suite/lock/pid 2>/dev/null)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    printf '%s\n' "$pid"
  fi
}

# One run at a time: two would check different snapshots out over each other.
take_lock() {
  local waited=0 pid
  while ! mkdir .suite/lock 2>/dev/null; do
    pid="$(holder)"
    # Gone: its process has ended, or it never wrote one and a minute has passed.
    if [ -z "$pid" ] && { [ -s .suite/lock/pid ] || [ -n "$(find .suite/lock -maxdepth 0 -mmin +1)" ]; }; then
      echo "taking over the lock of a run that is gone"
      rm -rf .suite/lock
      continue
    fi
    [ "$waited" -eq 0 ] && echo "another run is in progress here (pid ${pid:-?}); waiting for it"
    [ "$waited" -ge 1800 ] && die "still busy after 30 minutes"
    sleep 5
    waited=$((waited + 5))
  done
  echo $$ >.suite/lock/pid
}

start() {
  local sha="$2" workers="$3" pid
  enter "$1"
  shift 3
  take_lock
  if ! git checkout --quiet --detach --force "$sha"; then
    rm -rf .suite/lock
    die "snapshot $sha is not here"
  fi
  # Kept between runs: the environment, the downloads, and pytest's record of what
  # failed last, which is what makes `-- --lf` work.
  git clean -fdq -e .suite -e .venv -e .cache -e .pytest_cache
  rm -f .suite/run.done .suite/run.log
  # Its own process group (set -m), so `stop` can end pytest and its workers at once;
  # nothing tied to this ssh session, so a dropped link does not end it.
  set -m
  nohup bash scripts/remote_suite.sh job "$PWD" "$sha" "$workers" "$@" \
    >.suite/run.log 2>&1 </dev/null &
  pid=$!
  set +m
  echo "$pid" >.suite/lock/pid
  echo "started on the host (pid $pid)"
}

# The test environment, rebuilt only when pyproject.toml or requirements.lock changes.
# Its own uv cache, so building it never writes to the one production installs from.
ensure_venv() {
  local uv want
  uv="$(command -v uv || echo /opt/homebrew/bin/uv)"
  export UV_CACHE_DIR="$PWD/.suite/uv-cache"
  want="$(cat pyproject.toml requirements.lock | shasum -a 256 | cut -c1-16)"
  if [ -x .venv/bin/python ] && [ "$(cat .venv/.suite-deps 2>/dev/null)" = "$want" ]; then
    return 0
  fi
  echo "building the test environment (first run, or the dependencies changed)"
  # 3.13 because production runs 3.13.
  [ -x .venv/bin/python ] || "$uv" venv --quiet --python 3.13 .venv || return 1
  "$uv" pip install --quiet --python .venv/bin/python -r requirements.lock || return 1
  "$uv" pip install --quiet --python .venv/bin/python --no-deps -e . || return 1
  printf '%s\n' "$want" >.venv/.suite-deps
}

# The run's own home and temp directory, made fresh by `job` and removed after it.
# Outside the clone on purpose: inside it, every test's tmp_path sits in a git
# checkout with a pyproject.toml above it, and the tests that ask "is this a
# checkout?" of a temp directory get the clone's answer instead of "no".
RUN_TMP=""

# A command inside the sandbox, at low priority, with nothing of this login's
# environment: no keys, no proxy, no TRACKER_* pointing anywhere but the clone, and
# a home of its own so that `~` never leads to production either. No COLUMNS: piped
# output is 200 wide by the CLI's own rule, as it is in a local run.
sandboxed() {
  nice -n 10 sandbox-exec -f "$PWD/.suite/profile.sb" env -i \
    HOME="$RUN_TMP/home" TMPDIR="$RUN_TMP/tmp" \
    PATH="$PWD/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 TERM=dumb \
    TRACKER_HOME="$PWD" \
    GIT_AUTHOR_NAME=suite GIT_AUTHOR_EMAIL=suite@example.invalid \
    GIT_COMMITTER_NAME=suite GIT_COMMITTER_EMAIL=suite@example.invalid \
    "$@"
}

# Run inside the sandbox before any test: each guard tried for real. One that does
# not hold stops the run with nothing executed.
PROBE="$(
  cat <<'PY'
import os, socket, sys
from pathlib import Path

prod, here = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
ports = [int(p) for p in sys.argv[3:]]
failed = []


def check(label, ok):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        failed.append(label)


def refused(action):
    try:
        action()
    except OSError:
        return True
    return False


def connects(host, port):
    try:
        socket.create_connection((host, port), timeout=2).close()
    except OSError:
        return False
    return True


check("production's checkout cannot be listed", refused(lambda: os.listdir(prod / "repo")))
check("production's .env cannot be read", refused(lambda: (prod / "repo" / ".env").read_bytes()))
check(
    "production's database cannot be opened",
    refused(lambda: open(prod / "repo" / "data" / "tracker.db", "rb").close()),
)
check("the deployer cannot be listed", refused(lambda: os.listdir(prod / "ops")))
probe = prod / "backups" / ".suite-probe"
wrote = not refused(lambda: probe.write_bytes(b""))
if wrote:
    probe.unlink()
check("the backups cannot be written", not wrote)
reached = [p for p in ports if connects("127.0.0.1", p)]
check(
    f"none of the {len(ports)} services listening here can be reached"
    + (f" (reached {reached})" if reached else ""),
    not reached,
)
check("the internet cannot be reached", not connects("1.1.1.1", 443))

sys.path.insert(0, str(here))
from tracker.config import get_settings, home

check(f"tracker's home is the scratch clone ({home()})", home() == here)
db = get_settings().resolve_db()
check(f"its database is inside the scratch clone ({db})", here in db.parents)
check("the scratch clone holds no .env", not (here / ".env").exists())
sys.exit(1 if failed else 0)
PY
)"

# The run's exit code, global so the EXIT trap still sees it after `job` returns.
RUN_CODE=2

# The run, detached by `start`, in the checked-out snapshot. Its last act, whatever
# happens, is to write its exit code to .suite/run.done and give up the lock.
job() {
  local sha="$2" workers="$3" ports started
  enter "$1"
  shift 3
  trap 'printf "%s\n" "$RUN_CODE" >.suite/run.done; rm -rf .suite/lock ${RUN_TMP:+"$RUN_TMP"}' EXIT
  trap 'RUN_CODE=130; exit 130' INT TERM HUP

  if ! ensure_venv; then
    echo "remote suite: could not build the test environment"
    return
  fi
  RUN_TMP="$(mktemp -d -t tracker-suite)" || return
  mkdir -p "$RUN_TMP/home" "$RUN_TMP/tmp"
  ports="$(listening_ports | tr '\n' ' ')"
  # shellcheck disable=SC2086  # one argument per port
  profile "$HOME" $ports >.suite/profile.sb
  if pgrep -f scripts/overnight.sh >/dev/null 2>&1; then
    echo "the nightly loop is running; the suite runs beside it at low priority"
  fi

  echo "isolation check:"
  # shellcheck disable=SC2086
  if ! sandboxed .venv/bin/python -c "$PROBE" "$HOME/dev/tracker" "$PWD" $ports; then
    echo "remote suite: an isolation check failed, so no test was run"
    return
  fi

  started=$SECONDS
  sandboxed .venv/bin/python -m pytest -n "$workers" --color=no -rfE "$@"
  RUN_CODE=$?
  printf 'SUITE-RESULT exit=%s sha=%s seconds=%s\n' "$RUN_CODE" "$sha" "$((SECONDS - started))"
  git update-ref -d "refs/snapshots/$sha" 2>/dev/null
}

# The run's log from byte `offset` on, as it grows, until the run has ended and all
# of it is out. The last line is `SUITE-DONE exit=<code>`, which is not in the log.
follow() {
  local offset="$2" size ended alive
  enter "$1"
  while :; do
    # In this order, against the run's last three acts (its final output, then
    # run.done, then dropping the lock): a lock gone with no run.done after it means
    # the run died unrecorded, and a run.done seen before the size means all the
    # output is in the size.
    alive="$(holder)"
    ended=""
    [ -e .suite/run.done ] && ended=yes
    size="$(stat -f %z .suite/run.log 2>/dev/null || echo 0)"
    if [ "$size" -gt "$offset" ]; then
      tail -c "+$((offset + 1))" .suite/run.log | head -c "$((size - offset))"
      offset="$size"
    fi
    if [ -n "$ended" ]; then
      printf 'SUITE-DONE exit=%s\n' "$(cat .suite/run.done)"
      return 0
    fi
    if [ -z "$alive" ]; then
      printf 'SUITE-DONE exit=lost\n'
      return 0
    fi
    sleep 1
  done
}

stop() {
  local pid
  enter "$1"
  pid="$(holder)"
  if [ -z "$pid" ]; then
    echo "no run in progress"
    return 0
  fi
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid"
  echo "stopped the run (pid $pid)"
}

main() {
  local mode="${1:-}"
  [ $# -gt 0 ] && shift
  case "$mode" in
    prepare) prepare "$@" ;;
    start) start "$@" ;;
    job) job "$@" ;;
    follow) follow "$@" ;;
    stop) stop "$@" ;;
    profile) profile "$@" ;;
    *) die "usage: remote_suite.sh prepare|start|follow|stop|profile ..." ;;
  esac
}

# When piped over ssh, stdin is this script itself; nothing main starts may read the
# rest of it.
main "$@" </dev/null
exit $?
