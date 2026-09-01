#!/usr/bin/env bash
#
# Work the logic and duplicate backlogs down until they stop moving.
#
# For running unattended for hours. Every other script here does one pass; this
# one loops, because neither backlog is a fixed queue that drains:
#
#   * Ruling a claim out re-derives the row, and a re-derived row can raise a
#     finding the old value hid. Measured: 52 resolutions took the total from 530
#     to 529.
#   * Merging two rows changes the survivor's claim set, which can match a third
#     row that did not match before. Measured: answering 13 pairs took the group
#     count from 47 to 48.
#
# So "solve all of it" is a fixed point, not a pass. This runs rounds until two
# consecutive ones fail to reduce either count, then stops and says what is left.
#
# WHAT IT COSTS, because this is the part to decide before starting it. The agent
# reads whole articles: measured at ~45,000 tokens per logic finding and ~89,000
# for two. With 495 findings unanswered that is ~22M tokens for the first sweep,
# and roughly 10 hours at ~75s each. Later rounds are far cheaper — a finding the
# agent has answered or declined is recorded on the row and never re-offered, so
# round two only pays for what round one created. `--tokens` is a hard ceiling and
# defaults to one; raise it deliberately.
#
# WHAT IT CANNOT DO. `block_label_ambiguous` (92 findings) and its relatives are
# about tranche identity, and the only repair this agent has is superseding a
# claim about a field. It will read the sources and mostly decline them, which
# costs a call each — once, because a decline is recorded too. The real fix is a
# `blocks fold` command that does not exist yet. Do not read a stubborn residue of
# ~250 block findings as a failure of this script.
#
# SAFETY, given it runs while nobody is watching:
#   * A `VACUUM INTO` snapshot before the first merge and every --backup-every
#     rounds. Merges delete rows and no re-crawl recovers them.
#   * One run at a time, enforced here rather than discovered as a lock timeout
#     twenty minutes in.
#   * Do not push to main while this runs. The deploy poller runs `tracker init`
#     on a new commit, which wants the same write lock.
#   * Every phase is resumable. Killing this at 3am loses the round in flight and
#     nothing before it.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PY="$REPO/.venv/bin/python"
tracker() { "$PY" -m tracker "$@"; }

HOURS=8
ROUNDS=20
DRY_ROUNDS=2
BATCH=40
PAIRS=25
MIN_CONF=0.85
DO_MERGE=1
TOKEN_CAP=25000000
BACKUP_EVERY=5
CODES=""

usage() {
  cat <<'USAGE'
usage: overnight.sh [options]

Loops logic + duplicate resolution until both backlogs stop shrinking, or a
ceiling is hit. Intended to be started and left.

  --hours N          wall-clock ceiling (default 8). Checked between rounds.
  --rounds N         max rounds (default 20)
  --dry-rounds N     stop after N rounds with no net reduction (default 2)
  --batch N          logic findings per round (default 40)
  --pairs N          duplicate pairs per round (default 25)
  --min-confidence F floor a merge needs (default 0.85)
  --no-merge         never fold duplicates; park and rule only. No rows deleted.
  --tokens N         stop when estimated spend passes N (default 25,000,000)
  --backup-every N   snapshot every N rounds (default 5). Always before round 1.
  --codes "a,b"      only these finding codes, passed to --code one at a time
  --help

Start it and walk away:

  caffeinate -i nohup ~/dev/tracker/repo/scripts/overnight.sh > /dev/null 2>&1 &

`caffeinate -i` stops the machine idle-sleeping out from under a ten-hour job.
Everything is appended to data/runs/overnight.log; the morning report is the
last thing in it.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --hours)          shift; HOURS="${1:?}" ;;
    --rounds)         shift; ROUNDS="${1:?}" ;;
    --dry-rounds)     shift; DRY_ROUNDS="${1:?}" ;;
    --batch)          shift; BATCH="${1:?}" ;;
    --pairs)          shift; PAIRS="${1:?}" ;;
    --min-confidence) shift; MIN_CONF="${1:?}" ;;
    --tokens)         shift; TOKEN_CAP="${1:?}" ;;
    --backup-every)   shift; BACKUP_EVERY="${1:?}" ;;
    --codes)          shift; CODES="${1:?}" ;;
    --no-merge)       DO_MERGE=0 ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ -x "$PY" ] || { echo "no venv at $PY" >&2; exit 1; }

LOG="${OVERNIGHT_LOG:-$REPO/data/runs/overnight.log}"
mkdir -p "$(dirname "$LOG")"
# Where this run's output starts, so the token tally below reads only its own
# lines. Summing the whole file made the second night start at the first night's
# total and trip `--tokens` on round one.
LOG_FROM=$(( $(wc -l < "$LOG" 2>/dev/null || echo 0) + 1 ))
exec > >(tee -a "$LOG") 2>&1

# One at a time. `mkdir` is the atomic primitive available everywhere; macOS has
# no flock. The pid inside is for the person who finds a stale one.
LOCK="$REPO/data/runs/overnight.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another overnight run holds $LOCK (pid $(cat "$LOCK/pid" 2>/dev/null || echo '?'))"
  echo "if that is stale: rm -rf $LOCK"
  exit 1
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

say() { printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

backup() {
  local dir dest
  if [ -d "$REPO/../backups" ]; then dir="$REPO/../backups"; else dir="$REPO/data/backups"; fi
  mkdir -p "$dir"
  dest="$dir/tracker.backup-overnight-$(date '+%Y%m%d-%H%M%S').db"
  # VACUUM INTO, never cp: WAL mode means a copy of the main file alone opens
  # cleanly and is silently stale. Same reasoning as scripts/sync_db.py.
  "$PY" - "${TRACKER_DB:-$REPO/data/tracker.db}" "$dest" <<'PYEOF'
import sqlite3
import sys

con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
try:
    con.execute("VACUUM INTO '%s'" % sys.argv[2])
finally:
    con.close()
PYEOF
  echo "    snapshot: $dest"
}

# Two numbers, space separated: logic findings not yet answered, and suspected
# duplicate groups. Printed by one process rather than parsed out of two reports,
# because the convergence test is the only thing steering the loop and a regex
# over rendered tables is the wrong thing to hang that on.
counts() {
  "$PY" - <<'PYEOF'
from tracker.audit import settled_codes
from tracker.capex import duplicate_groups, suspected_duplicates
from tracker.db import open_db, session_scope
from tracker.models import Project
from tracker import logic

engine = open_db("data/tracker.db")
with session_scope(engine, commit=False) as s:
    settled, todo = {}, 0
    for f in logic.review(s).findings:
        if f.project_id not in settled:
            row = s.get(Project, f.project_id)
            settled[f.project_id] = settled_codes(row) if row else set()
        if f.code not in settled[f.project_id]:
            todo += 1
    # Groups, not pairs. Nine rows for one campus make 36 pairs and one group,
    # and a loop steering on the pair count would read a single merge as huge
    # progress. `duplicate_groups` takes the pairs, so build them first.
    pairs = [p for p in suspected_duplicates(s) if set(p.kinds) - {"name"}]
    print(todo, len(duplicate_groups(pairs)))
PYEOF
}

# --- start ------------------------------------------------------------------

STARTED=$(date +%s)
DEADLINE=$((STARTED + HOURS * 3600))
SPENT=0
STALE=0

say "overnight starting"
printf '    repo      %s\n' "$REPO"
printf '    ceilings  %sh, %s rounds, %s tokens\n' "$HOURS" "$ROUNDS" "$TOKEN_CAP"
printf '    per round %s findings, %s pairs, merge=%s, min-confidence %s\n' \
  "$BATCH" "$PAIRS" "$DO_MERGE" "$MIN_CONF"

read -r F0 D0 <<<"$(counts)"
printf '    at start  %s logic finding(s) unanswered, %s duplicate group(s)\n' "$F0" "$D0"

say 'snapshot before anything is deleted'
backup

for round in $(seq 1 "$ROUNDS"); do
  now=$(date +%s)
  if [ "$now" -ge "$DEADLINE" ]; then
    say "wall-clock ceiling of ${HOURS}h reached — stopping between rounds"
    break
  fi
  if [ "$SPENT" -ge "$TOKEN_CAP" ]; then
    say "token ceiling reached (~$SPENT) — stopping"
    break
  fi

  say "round $round of $ROUNDS  (~$SPENT tokens so far, $(( (DEADLINE - now) / 60 ))m left)"

  if [ "$round" -gt 1 ] && [ $((round % BACKUP_EVERY)) -eq 1 ]; then
    backup
  fi

  # --- logic ---------------------------------------------------------------
  # `|| true` on every phase: a provider hiccup on one finding must not end the
  # night. The CLI commits per finding, so whatever was settled before the
  # failure is already durable, and the next round picks up from there.
  if [ -n "$CODES" ]; then
    IFS=',' read -ra wanted <<<"$CODES"
    for code in "${wanted[@]}"; do
      printf '  logic --code %s\n' "$code"
      tracker logic resolve --limit "$BATCH" --code "$code" < /dev/null || true
    done
  else
    tracker logic resolve --limit "$BATCH" < /dev/null || true
  fi

  # --- duplicates ----------------------------------------------------------
  if [ "$DO_MERGE" -eq 1 ]; then
    tracker duplicates resolve --merge --limit "$PAIRS" \
      --min-confidence "$MIN_CONF" < /dev/null || true
  else
    tracker duplicates resolve --limit "$PAIRS" < /dev/null || true
  fi

  # --- reconcile and measure ----------------------------------------------
  tracker backfill derive < /dev/null || true
  tracker clean --snapshot --since 1 < /dev/null || true

  # Tokens are read back out of this log, which is the only place both commands
  # report them. Approximate on purpose: it steers a ceiling, not a bill.
  SPENT=$(tail -n "+$LOG_FROM" "$LOG" 2>/dev/null \
    | grep -oE '~[0-9,]+ tokens' | tr -d '~, tokens' | awk '{t+=$1} END {print t+0}')

  read -r F1 D1 <<<"$(counts)"
  printf '\n  round %s: findings %s -> %s (%+d)   groups %s -> %s (%+d)\n' \
    "$round" "$F0" "$F1" "$((F1 - F0))" "$D0" "$D1" "$((D1 - D0))"

  if [ "$F1" -lt "$F0" ] || [ "$D1" -lt "$D0" ]; then
    STALE=0
  else
    STALE=$((STALE + 1))
    printf '  no reduction (%s of %s stale rounds)\n' "$STALE" "$DRY_ROUNDS"
  fi
  F0=$F1
  D0=$D1

  if [ "$F1" -eq 0 ] && [ "$D1" -eq 0 ]; then
    say "both backlogs empty"
    break
  fi
  if [ "$STALE" -ge "$DRY_ROUNDS" ]; then
    say "$DRY_ROUNDS rounds with no reduction — this is the fixed point, stopping"
    break
  fi
done

# --- the morning report -----------------------------------------------------

say 'what is left'
tracker clean < /dev/null || true
echo
tracker logic check < /dev/null 2>&1 | sed -n '1,12p' || true
echo
tracker duplicates < /dev/null 2>&1 | sed -n '1,2p' || true

ELAPSED=$(( ($(date +%s) - STARTED) / 60 ))
say "overnight complete — ${ELAPSED}m, ~$SPENT tokens"
printf '    Anything still listed above needs either a person or a command that\n'
printf '    does not exist yet. The block findings are the second kind: see the\n'
printf '    header of this script.\n'
