#!/usr/bin/env bash
#
# Settle the two report families that need a judgement: logic collisions and
# suspected duplicates. A model decides; nothing here waits on you except the
# one confirmation before rows are deleted.
#
# This is the narrow companion to scripts/settle.sh. `settle.sh` runs the whole
# quality loop including the free phases and the audit; this runs only the two
# families where the question is "which of these two claims is right" and the
# answer costs an LLM call. Run it when you want those answered and nothing else.
#
# WHAT EACH PHASE CAN AND CANNOT DO, measured on the live database
#
#   logic --auto     Free, and currently a NO-OP on this database. It reports 48
#                    rows whose phase drifted, then applies the repair by calling
#                    `recompute_from_sources`, which re-derives phase and lands
#                    back on the stored value -- so it prints 48 repairs and
#                    writes none, every run. `check_collisions`'s winner and
#                    `_resolve`'s answer for `phase` disagree. Kept in because it
#                    costs nothing and starts working the day that is fixed.
#
#   logic --llm      The collisions arithmetic could not settle -- roughly 518 of
#                    them. One call each, and it skips when the evidence does not
#                    clearly favour one option, so a skip is an answer and not a
#                    failure. Resumable: findings already answered on their row
#                    are not re-offered, which is what makes the open count fall.
#
#   duplicates       47 suspected groups. Two halves, and they are not equally
#                    reachable. The *park* half needs no rails: capex holds one
#                    row of every suspected group out of the published totals
#                    until the pair is ruled out, so a model ruling a pair
#                    `different` releases real capacity. The *merge* half is
#                    gated -- of the 47, 28 are city-vs-county and 8 are
#                    name-overlap, and `merge_blocked` refuses both unattended by
#                    design. Expect roughly a quarter to fold and the rest to be
#                    parked or left. That is the tool working, not failing.
#
# The order is not arbitrary: logic first, because a collision settled on a row
# changes the claim set a duplicate judgement is made against, and duplicates
# last because it is the only step that deletes anything.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# launchd and cron hand a process a minimal PATH, and the `tracker` shim lives in
# ~/.local/bin which is not on it. Go through the venv interpreter, as ops/serve.sh
# does, so this behaves the same run by hand and run by a scheduler.
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PY="$REPO/.venv/bin/python"
tracker() { "$PY" -m tracker "$@"; }

LIMIT="${RESOLVE_LIMIT:-50}"
DO_MERGE=1
ASSUME_YES=0
DRY=0

usage() {
  cat <<'USAGE'
usage: resolve.sh [--limit N] [--no-merge] [--yes] [--dry-run]

Settles logic collisions and suspected duplicates with a model. One LLM call per
finding. Resumable -- anything already answered on its row is not re-offered, so
running it again works through the backlog rather than repeating itself.

  --limit N    findings per phase (default 50, or $RESOLVE_LIMIT). NOT 0 --
               every phase slices findings[:limit], so 0 means nothing at all.
  --no-merge   decide and park, but never fold. No rows are deleted and no
               backup is taken. The safest way to see what a model concludes.
  --yes        do not ask before folding duplicates. For unattended runs.
  --dry-run    where a command supports it, judge and discard the transaction.
               The LLM calls are still paid for -- that is what makes it honest.

Before spending anything it prints what is open, so you can Ctrl-C.
Everything is appended to data/runs/resolve.log ($RESOLVE_LOG to move it).

A merge is the one thing here no re-crawl recovers, so a VACUUM INTO snapshot is
taken first, unconditionally, and its path is printed.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --limit)    shift; LIMIT="${1:?--limit needs a number}" ;;
    --no-merge) DO_MERGE=0 ;;
    --yes|-y)   ASSUME_YES=1 ;;
    --dry-run)  DRY=1 ;;
    -h|--help)  usage; exit 0 ;;
    *)          echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ "$LIMIT" -gt 0 ] 2>/dev/null || {
  echo "--limit must be a positive number (every phase slices findings[:limit])" >&2
  exit 2
}
[ -x "$PY" ] || { echo "no venv at $PY -- let the deploy poller run once" >&2; exit 1; }

PHASE="startup"
on_error() {
  local code=$1
  printf '\n!!! resolve failed during "%s" (exit %d)\n' "$PHASE" "$code"
  printf '    Phases that completed are already written; this is resumable.\n'
  printf '    A write-lock message means another tracker run is in flight.\n'
  exit "$code"
}
trap 'on_error $?' ERR

# Bold only when stdout is a terminal. Decided BEFORE the tee below replaces
# stdout with a pipe, or it would always be false.
if [ -t 1 ]; then BOLD=$'\033[1m'; PLAIN=$'\033[0m'; else BOLD=""; PLAIN=""; fi

# Append everything to a log as well as the terminal. This spends money on
# judgements, and a run whose only record was terminal scrollback could not
# answer "did the logic phase settle anything, or decline everything?" -- which
# is exactly the question the first real run raised. The confirmation prompt
# still works: it reads from /dev/tty, not stdin.
LOG="${RESOLVE_LOG:-$REPO/data/runs/resolve.log}"
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

phase() { PHASE="$1"; printf '\n%s=== %s  %s%s\n' "$BOLD" "$(date '+%H:%M:%S')" "$1" "$PLAIN"; }

# --- what is open, before anything is spent -------------------------------

phase 'what is open right now'
# Just the summary table at the top of each report; the per-project detail below
# it is what the resolve phases are about to work through.
#
# `sed -n '1,Np'` rather than `head -N` deliberately. `head` closes the pipe as
# soon as it has its lines, tracker takes SIGPIPE, and `set -o pipefail` turns
# that into a failed pipeline that trips the ERR trap -- the script would die
# during its own preflight. sed reads to EOF, so nothing is signalled.
tracker logic check 2>&1 | sed -n '1,12p'
tracker duplicates 2>&1 | sed -n '1p'
printf '\nAbout to spend up to %s LLM call(s) per phase. Ctrl-C now to stop.\n' "$LIMIT"

# --- logic ------------------------------------------------------------------

phase 'logic --auto -- free, and a no-op until the phase-winner bug is fixed'
if [ $DRY -eq 1 ]; then
  tracker logic resolve --auto < /dev/null
else
  tracker logic resolve --auto --apply < /dev/null
fi

phase "logic --llm -- a model settles the collisions, $LIMIT at a time"
if [ $DRY -eq 1 ]; then
  printf '    skipped: logic resolve --llm has no --dry-run, and it writes\n'
else
  # stdin on /dev/null: --llm is non-interactive, and an unexpected prompt should
  # hit EOF and stop rather than sit holding the write lock.
  tracker logic resolve --llm --limit "$LIMIT" < /dev/null
fi

# --- duplicates -------------------------------------------------------------

if [ $DO_MERGE -eq 1 ]; then
  if [ $ASSUME_YES -eq 0 ]; then
    if [ -t 0 ]; then
      phase 'about to fold duplicates -- this deletes rows'
      printf '    A wrong merge destroys two rows and no re-crawl recovers them.\n'
      printf '    A snapshot is taken first either way. The rails still refuse\n'
      printf '    any pair whose only evidence is granularity or a shared word.\n'
      printf '\n    Fold the pairs a model rules one campus? [y/N] '
      read -r answer < /dev/tty || answer=""
      case "$answer" in
        [yY]|[yY][eE][sS]) ;;
        *) printf '    declined -- continuing without folding\n'; DO_MERGE=0 ;;
      esac
    else
      phase 'duplicates'
      printf '    no terminal to confirm on, and --yes was not given, so nothing\n'
      printf '    will be folded. Pass --yes for an unattended run.\n'
      DO_MERGE=0
    fi
  fi
fi

if [ $DO_MERGE -eq 1 ]; then
  phase 'backup -- a consistent snapshot before anything is deleted'
  DB="${TRACKER_DB:-$REPO/data/tracker.db}"
  if [ -n "${RESOLVE_BACKUP_DIR:-}" ]; then
    BACKUP_DIR="$RESOLVE_BACKUP_DIR"
  elif [ -d "$REPO/../backups" ]; then
    BACKUP_DIR="$REPO/../backups"
  else
    BACKUP_DIR="$REPO/data/backups"
  fi
  mkdir -p "$BACKUP_DIR"
  DEST="$BACKUP_DIR/tracker.backup-before-resolve-$(date '+%Y%m%d-%H%M%S').db"
  # VACUUM INTO, never cp. WAL mode means committed data sits in tracker.db-wal
  # until a checkpoint folds it back, so a copy of the main file alone opens
  # cleanly while being silently out of date. Same reasoning as scripts/sync_db.py.
  "$PY" - "$DB" "$DEST" <<'PYEOF'
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
con = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
try:
    con.execute("VACUUM INTO '%s'" % dst)   # VACUUM INTO takes no parameters
finally:
    con.close()
PYEOF
  printf '    %s\n' "$DEST"
  printf '    restore with: python scripts/sync_db.py --push --force  (from a copy)\n'

  phase "duplicates -- a model decides, the rails decide what it may do"
  if [ $DRY -eq 1 ]; then
    tracker duplicates resolve --merge --limit "$LIMIT" --dry-run < /dev/null
  else
    tracker duplicates resolve --merge --limit "$LIMIT" < /dev/null
  fi
else
  phase 'duplicates -- deciding and parking only, nothing folded'
  # Worth running even without folding: a pair ruled `different` is parked, and
  # that is what releases the capacity capex is holding back. Omitting `--merge`
  # is how you decline the folding -- there is no `--no-merge`.
  if [ $DRY -eq 1 ]; then
    tracker duplicates resolve --limit "$LIMIT" --dry-run < /dev/null
  else
    tracker duplicates resolve --limit "$LIMIT" < /dev/null
  fi
fi

# --- reconcile and score ----------------------------------------------------

phase 'derive -- re-derive what the changed claim sets now imply'
if [ $DRY -eq 1 ]; then
  printf '    skipped: backfill derive has no dry run\n'
else
  tracker backfill derive
fi

phase 'clean -- what moved, against the previous snapshot'
if [ $DRY -eq 1 ]; then
  tracker clean
else
  tracker clean --snapshot --since 1
fi
