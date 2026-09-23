#!/usr/bin/env bash
#
# The whole quality loop, for running unattended in tmux.
#
# Every other script here does one pass over one thing. This runs every phase that
# can improve a row, in dependency order, in rounds, until they stop improving it.
#
# WHY ROUNDS. Neither backlog is a queue that drains. Ruling a claim out re-derives
# the row and a re-derived row can raise a finding the old value hid — 52
# resolutions once took the total from 530 to 529. Merging changes the survivor's
# claim set and can match a third row that did not match before — answering 13 pairs
# took the group count from 47 to 48. So the target is a fixed point, and the stop
# condition is consecutive rounds with no reduction.
#
# THE PHASE ORDER IS THE ARGUMENT, cheapest and most-blocking first:
#
#   free      dates, geo, scope, derive, logic --auto. No model, no cost. `scope`
#             re-gates stored labels against their own quotes; `derive` re-applies
#             every derived value, and both are pure functions of what is stored.
#   audit     THE biggest tier lever and the one this script used to miss entirely:
#             `audit_clear` is a T1 gate failing 68 rows and the SOLE blocker on 54.
#             One call per finding on the fixed-menu path, so it is also cheap.
#   risks     reads the article behind each unquoted obstacle. Also T3, also cheap.
#   logic     the agent: reads sources, rules wrong claims out of the merge.
#   duplicates the agent: reads both rows, folds or parks. The only step that
#             deletes anything, so it goes behind a snapshot.
#   enrich    the agent, last and separately budgeted, because it is the most
#             expensive rung (~77,000 tokens a row) AND the largest tier lever
#             there is: every one of the 283 rows sitting at T1 is held there by
#             `fields_present` alone. Those rows are not wrong, they are empty, and
#             nothing above this line can move one.
#
# WHAT IT COSTS. Measured: ~45,000-260,000 tokens per agent finding depending on how
# many articles it reads, and one HTTP request per article on a cache miss. The free
# phases and `audit` cost almost nothing. `--tokens` is a hard ceiling, checked
# before every paid phase, and defaults to one.
#
# THE CEILING READS A LEDGER, NOT THE LOG. Every paid call appends a line to the file
# `TRACKER_SPEND_LEDGER` names (`tracker.llm.record_spend`), so a phase is counted
# whether or not its command prints a token summary. The tally used to grep the log
# for `~N tokens`, which three commands print and five paid phases did not — and
# under `set -o pipefail` a grep that matched nothing *failed*, so from 2026-09-03 the
# very first tally of every night, taken before any phase had printed anything,
# ended the script straight after its snapshot. Twenty-one nights ran no phase at
# all and logged no error. The morning report now breaks the night's spend down by
# command, from the same file.
#
# WHAT IT CANNOT DO. Roughly 250 findings are about tranche identity —
# `block_label_ambiguous` and relatives — and the only repair an agent has is
# superseding a claim about a field. It will read the sources and decline them, once
# each. The real fix is a `blocks fold` command that does not exist. A stubborn
# residue of block findings is not this script failing.
#
# RUNNING IT IN TMUX
#
#   tmux new -s tracker
#   caffeinate -i ~/dev/tracker/repo/scripts/overnight.sh --hours 10
#   # detach with ctrl-b then d; come back with `tmux attach -t tracker`
#
# `caffeinate -i` stops the machine idle-sleeping out from under a ten-hour job.
# From anywhere else, `overnight.sh --status` prints where it has got to.
#
# SAFETY, since nobody is watching:
#   * a `VACUUM INTO` snapshot before the first merge and every --backup-every rounds
#   * one run at a time, enforced here rather than discovered as a lock timeout
#   * every phase is `|| true`: a provider failure at 3am loses that phase, not the
#     night, and every command commits as it goes
#   * do not push to main while this runs — the deploy poller runs `tracker init`,
#     which wants the same single write lock

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PY="$REPO/.venv/bin/python"
tracker() { "$PY" -m tracker "$@"; }

HOURS=10
ROUNDS=20
DRY_ROUNDS=2
FINDINGS=40
PAIRS=25
AUDIT=60
RISKS=40
ENRICH=15
ENRICH_BUDGET=60
MIN_CONF=0.85
DO_MERGE=1
DO_ENRICH=1
TOKEN_CAP=25000000
BACKUP_EVERY=5
STATUS_ONLY=0

LOG="${OVERNIGHT_LOG:-$REPO/data/runs/overnight.log}"

usage() {
  cat <<'USAGE'
usage: overnight.sh [options]
       overnight.sh --status        # where a running one has got to

Runs every quality phase in rounds until they stop improving anything. Meant to be
started in tmux and left.

  --hours N          wall-clock ceiling (default 10). Checked between rounds.
  --rounds N         max rounds (default 20)
  --dry-rounds N     stop after N rounds with no reduction (default 2)
  --findings N       logic findings per round (default 40)
  --pairs N          duplicate pairs per round (default 25)
  --audit N          audit findings per round (default 60)
  --risks N          obstacles per round (default 40)
  --enrich N         projects to enrich per round (default 15); 0 to skip
  --enrich-budget N  articles the enrich phase may read per round (default 60)
  --min-confidence F floor a duplicate fold needs (default 0.85)
  --no-merge         never fold duplicates; park and rule only. Deletes nothing.
  --tokens N         stop when estimated spend passes N (default 25,000,000)
  --backup-every N   snapshot every N rounds (default 5). Always before round 1.
  --status           print the tail of the log and exit
  --help

In tmux:

  tmux new -s tracker
  caffeinate -i ~/dev/tracker/repo/scripts/overnight.sh --hours 10
  # ctrl-b d to detach, `tmux attach -t tracker` to return

Everything is appended to data/runs/overnight.log; the morning report is the last
thing in it.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --hours)          shift; HOURS="${1:?}" ;;
    --rounds)         shift; ROUNDS="${1:?}" ;;
    --dry-rounds)     shift; DRY_ROUNDS="${1:?}" ;;
    --findings)       shift; FINDINGS="${1:?}" ;;
    --pairs)          shift; PAIRS="${1:?}" ;;
    --audit)          shift; AUDIT="${1:?}" ;;
    --risks)          shift; RISKS="${1:?}" ;;
    --enrich)         shift; ENRICH="${1:?}" ;;
    --enrich-budget)  shift; ENRICH_BUDGET="${1:?}" ;;
    --min-confidence) shift; MIN_CONF="${1:?}" ;;
    --tokens)         shift; TOKEN_CAP="${1:?}" ;;
    --backup-every)   shift; BACKUP_EVERY="${1:?}" ;;
    --no-merge)       DO_MERGE=0 ;;
    --status)         STATUS_ONLY=1 ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ "$ENRICH" -gt 0 ] 2>/dev/null || DO_ENRICH=0

LOCK="$REPO/data/runs/overnight.lock"

if [ "$STATUS_ONLY" -eq 1 ]; then
  if [ -d "$LOCK" ]; then
    echo "running, pid $(cat "$LOCK/pid" 2>/dev/null || echo '?')"
  else
    echo "not running"
  fi
  # `|| true`: under pipefail a log with no round lines yet is not an error.
  [ -f "$LOG" ] && grep -E '^\[|^  round ' "$LOG" | tail -20 || true
  exit 0
fi

[ -x "$PY" ] || { echo "no venv at $PY" >&2; exit 1; }

mkdir -p "$(dirname "$LOG")"
# One ledger per night, so the tally reads only this run's calls — summing a shared
# file made a second night start at the first night's total. Exported, so every
# `tracker` child below appends to it.
export TRACKER_SPEND_LEDGER="${TRACKER_SPEND_LEDGER:-$(dirname "$LOG")/overnight-$(date '+%Y%m%d-%H%M%S').spend}"
exec > >(tee -a "$LOG") 2>&1

# One at a time. `mkdir` is the atomic primitive available everywhere; macOS has no
# flock. The pid inside is for whoever finds a stale one.
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another overnight run holds $LOCK (pid $(cat "$LOCK/pid" 2>/dev/null || echo '?'))"
  echo "if that is stale: rm -rf $LOCK"
  exit 1
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

say()   { printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
phase() { printf '\n--- %s  %s\n' "$(date '+%H:%M:%S')" "$*"; }

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

# Three numbers from one process: logic findings not yet answered, duplicate
# GROUPS, and rows below T2. Groups rather than pairs because nine rows for one
# campus are 36 pairs and one group, and a loop counting pairs would read a single
# merge as huge progress. Rows-below-T2 is what the enrich phase moves and neither
# other number can see.
counts() {
  "$PY" - <<'PYEOF'
from tracker import clean as cl
from tracker import logic
from tracker.audit import settled_codes
from tracker.capex import duplicate_groups, suspected_duplicates
from tracker.db import open_db, session_scope
from tracker.models import Project

engine = open_db("data/tracker.db")
with session_scope(engine, commit=False) as s:
    settled, todo = {}, 0
    for f in logic.review(s).findings:
        if f.project_id not in settled:
            row = s.get(Project, f.project_id)
            settled[f.project_id] = settled_codes(row) if row else set()
        if f.code not in settled[f.project_id]:
            todo += 1
    pairs = [p for p in suspected_duplicates(s) if set(p.kinds) - {"name"}]
    below = sum(1 for c in cl.scan(s).cards if c.tier < 2)
    print(todo, len(duplicate_groups(pairs)), below)
PYEOF
}

# Prompt plus completion tokens across every paid call this run made. awk alone, and
# never a pipeline: this runs under `set -euo pipefail`, where a stage that finds
# nothing to match exits non-zero and takes the whole script with it — which is
# exactly how this function used to end every night before its first phase. A
# missing or empty ledger is a night that has spent nothing, and prints 0.
spent_so_far() {
  if [ ! -s "$TRACKER_SPEND_LEDGER" ]; then
    echo 0
    return 0
  fi
  awk -F'\t' '{t += $5 + $6} END {printf "%d\n", t}' "$TRACKER_SPEND_LEDGER"
}

# The night's spend by command, largest first — which phase the money went to.
spend_by_command() {
  [ -s "$TRACKER_SPEND_LEDGER" ] || { echo "    no paid calls"; return 0; }
  awk -F'\t' '{t[$3] += $5 + $6; n[$3]++}
    END {for (c in t) printf "%d\t%d\t%s\n", t[c], n[c], c}' "$TRACKER_SPEND_LEDGER" \
    | sort -rn | awk -F'\t' '{printf "    %-22s %6d call(s)  ~%d tokens\n", $3, $2, $1}'
}

# True when the ceiling is reached. Called before every paid phase rather than once a
# round: a round is nine phases and the agent ones cost millions, so a check at the
# top of the round let one round overshoot the ceiling by a round.
over_ceiling() {
  SPENT=$(spent_so_far)
  [ "$SPENT" -ge "$TOKEN_CAP" ]
}

# --- start ------------------------------------------------------------------

STARTED=$(date +%s)
DEADLINE=$((STARTED + HOURS * 3600))
SPENT=0
STALE=0
CAPPED=0

say "overnight starting  (pid $$)"
printf '    repo      %s\n' "$REPO"
printf '    ceilings  %sh, %s rounds, %s tokens\n' "$HOURS" "$ROUNDS" "$TOKEN_CAP"
printf '    per round %s findings, %s audit, %s risks, %s pairs, %s enrich\n' \
  "$FINDINGS" "$AUDIT" "$RISKS" "$PAIRS" "$ENRICH"
printf '    merge=%s  enrich=%s  min-confidence %s\n' "$DO_MERGE" "$DO_ENRICH" "$MIN_CONF"
printf '    spend     %s\n' "$TRACKER_SPEND_LEDGER"

read -r F0 D0 B0 <<<"$(counts)"
printf '    at start  %s finding(s), %s duplicate group(s), %s row(s) below T2\n' "$F0" "$D0" "$B0"

say 'snapshot before anything is deleted'
backup

# Before each paid phase. `break` leaves the round loop from inside its body, so the
# settle below still runs once for whatever the night managed.
capped() {
  if over_ceiling; then
    say "token ceiling reached (~$SPENT of $TOKEN_CAP) before $1 — stopping"
    CAPPED=1
    return 0
  fi
  return 1
}

for round in $(seq 1 "$ROUNDS"); do
  now=$(date +%s)
  if [ "$now" -ge "$DEADLINE" ]; then
    say "wall-clock ceiling of ${HOURS}h reached — stopping between rounds"; break
  fi
  SPENT=$(spent_so_far)

  say "round $round of $ROUNDS  (~$SPENT tokens, $(( (DEADLINE - now) / 60 ))m left)"
  if [ "$round" -gt 1 ] && [ $((round % BACKUP_EVERY)) -eq 1 ]; then backup; fi

  # --- free: no model, no cost --------------------------------------------
  # `|| true` throughout: one phase failing must not end the night, and every
  # command below commits as it goes, so whatever succeeded is already durable.
  phase 'free — dates, geo, scope, derive, logic --auto'
  tracker backfill dates --apply < /dev/null || true
  tracker ingest geo < /dev/null || true
  tracker backfill scope --apply < /dev/null || true
  tracker backfill derive < /dev/null || true
  tracker logic resolve --auto --apply < /dev/null || true

  # --- audit: a T1 gate, and cheap ----------------------------------------
  if capped audit; then break; fi
  phase "audit — implausible figures, $AUDIT at a time"
  tracker audit resolve --no-ask --limit "$AUDIT" < /dev/null || true

  if capped risks; then break; fi
  phase "risks — unquoted obstacles, $RISKS at a time"
  tracker risks confirm --limit "$RISKS" < /dev/null || true

  # --- the agent phases ---------------------------------------------------
  if capped logic; then break; fi
  phase "logic — a model reads the sources, $FINDINGS at a time"
  tracker logic resolve --limit "$FINDINGS" < /dev/null || true

  if capped duplicates; then break; fi
  phase "duplicates — $PAIRS pair(s)"
  if [ "$DO_MERGE" -eq 1 ]; then
    tracker duplicates resolve --merge --limit "$PAIRS" \
      --min-confidence "$MIN_CONF" < /dev/null || true
  else
    tracker duplicates resolve --limit "$PAIRS" < /dev/null || true
  fi

  # --- enrich: last, and the largest lever --------------------------------
  # Every row sitting at T1 is held there by `fields_present` alone. Nothing above
  # this line can move one, because they are not wrong — they are empty.
  if [ "$DO_ENRICH" -eq 1 ]; then
    if capped enrich; then break; fi
    phase "enrich — $ENRICH thinnest row(s), $ENRICH_BUDGET article budget"
    tracker enrich --select "$ENRICH" --target 0 --budget "$ENRICH_BUDGET" \
      < /dev/null || true
  fi

  # --- reconcile and measure ----------------------------------------------
  phase 'settle — re-derive and score'
  tracker backfill derive < /dev/null || true
  tracker clean --snapshot --since 1 < /dev/null || true

  SPENT=$(spent_so_far)
  read -r F1 D1 B1 <<<"$(counts)"
  printf '\n  round %s: findings %s -> %s (%+d)  groups %s -> %s (%+d)  below-T2 %s -> %s (%+d)\n' \
    "$round" "$F0" "$F1" "$((F1 - F0))" "$D0" "$D1" "$((D1 - D0))" "$B0" "$B1" "$((B1 - B0))"

  if [ "$F1" -lt "$F0" ] || [ "$D1" -lt "$D0" ] || [ "$B1" -lt "$B0" ]; then
    STALE=0
  else
    STALE=$((STALE + 1))
    printf '  no reduction (%s of %s stale rounds)\n' "$STALE" "$DRY_ROUNDS"
  fi
  F0=$F1; D0=$D1; B0=$B1

  if [ "$F1" -eq 0 ] && [ "$D1" -eq 0 ] && [ "$B1" -eq 0 ]; then
    say "everything settled"; break
  fi
  if [ "$STALE" -ge "$DRY_ROUNDS" ]; then
    say "$DRY_ROUNDS rounds with no reduction — this is the fixed point, stopping"; break
  fi
done

# A round cut short by the ceiling skipped its settle. It is free, and it is what
# re-derives the rows the phases that did run just changed.
if [ "$CAPPED" -eq 1 ]; then
  phase 'settle — re-derive and score, after the ceiling'
  tracker backfill derive < /dev/null || true
  tracker clean --snapshot --since 1 < /dev/null || true
fi

# --- the morning report -----------------------------------------------------

say 'what is left'
tracker clean < /dev/null || true
echo
tracker logic check < /dev/null 2>&1 | sed -n '1,12p' || true
echo
tracker duplicates < /dev/null 2>&1 | sed -n '1,2p' || true

ELAPSED=$(( ($(date +%s) - STARTED) / 60 ))
say "overnight complete — ${ELAPSED}m, ~$(spent_so_far) tokens"
spend_by_command
printf '    Anything still listed needs either a person or a command that does not\n'
printf '    exist yet. The block findings are the second kind — see this header.\n'
