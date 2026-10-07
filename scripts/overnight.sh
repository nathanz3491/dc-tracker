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
# "Reduction" means a count falling BELOW THE LOWEST IT HAS BEEN THIS NIGHT, not below
# last round's. The counts wobble on their own — a merge splits one group into two,
# a re-derived row drops below T2 and climbs back — and measured against the previous
# round every wobble back down read as progress: 2026-09-29 ran seven rounds with
# duplicate groups going 12, 14, 12, 11 and rows below T2 525, 527, 524, 528, and
# stopped with nothing moved. Against the running minimum it stops after three.
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
#             nothing above this line can move one. It runs in the FIRST round only
#             (`--enrich-rounds`): what it reads changes when articles are ingested,
#             not when a merge or a ruling lands, and on the nights of 2026-09-24 to
#             09-29 it was 86% of the bill, round after round, for one new fact.
#             It picks rows with `--t2` — the ones below T2 for missing fields,
#             fewest missing first — where `--target 0` alone had sorted the
#             FULLEST rows first and spent every round on rows already past the bar.
#   discover  the news and new campuses, first round only: polls the feeds (free —
#             no fetch, no model), asks each closed feed again once a week, looks
#             up DCD/DCF headlines elsewhere (≤40 web searches, ~$0.04, no model)
#             and reads `--discover` queued articles through
#             the ordinary crawl, whose identity check asks before it inserts.
#             Everything published within the email's two-month window goes first,
#             newest first, whatever campus it names, because what is read tonight
#             is mailed tomorrow: a 09-21 lawsuit against a tracked campus once
#             waited behind the backlog until 10-03 and was mailed as news. Then
#             articles naming no tracked campus — enrich reads for one row and
#             creates none, so this is where new campuses come from.
#
# WHAT IT COSTS, AND THE CEILING. The ceiling is money: `--cny` (default ¥12), priced
# from the spend ledger by `tracker.spend` at DeepSeek's published rates for the hour
# each call was made. A ceiling in tokens (`--tokens`, still honoured) mostly counted
# cached prompt, which is billed at a fiftieth of the rate; about 93% of the money is
# the reply, most of it reasoning. 25,000,000 tokens was anything from ¥20 to ¥31, and
# on the six nights to 2026-09-29 the loop cost ¥9-34 without the token ceiling ever
# firing. The check runs before every paid phase, so a night ends below `--cny` plus
# one phase.
#
# THE VOLUME, raised on 2026-10-05. The cost fixes of 09-30 left nights at ¥1.76-2.75
# against a ¥12 ceiling, so the loop was doing a fifth of what it was allowed to:
# 15 rows enriched on a 60-article budget, 10 queued articles read for new campuses,
# while 419 rows sat below T2 with something still worth searching for and 1,659
# readable articles waited in the queue. Now 40 rows on 160 articles (the enrich pass
# at ~¥0.12 a row, about ¥5), 40 queued articles (~¥0.01 each), 18 feeds, and 60 findings and
# 40 pairs a round. About ¥7 a night, so the ceiling still has room; the enrich pass
# is the largest single phase, which bounds the overshoot at about `--cny` + ¥5.
#
# Later rounds are cheaper than the first: every paid phase records what it answered
# or could not decide, keyed on the evidence it was shown, and does not re-offer it
# until that evidence changes or a month passes (`tracker/declines.py`).
#
# The per-item judgements — risks, audit, enrich's settle step — run at `high`
# reasoning effort (`--judgement-effort`). They ran at `low` for one night,
# 2026-09-30, and two of the audit's three model decisions that night were wrong in
# ways that move published totals: it replaced a campus's $600M with a statewide
# "$20 billion+ in Ohio", and kept a land price as a campus's build investment while
# saying in its own reason that it was the land price. The step cost ¥0.18 that
# night; `high` costs a few tenths of a yuan more, which is not the place to save.
# `--local-judgement` sends them to the local model (TRACKER_JUDGEMENT_PROVIDER=ollama).
#
# The morning report ends with every value the night changed (`tracker changes`
# against the snapshot taken before round 1), each with the sentence now behind it,
# so a person can read what the models did in a couple of minutes.
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
FINDINGS=60
PAIRS=40
AUDIT=60
RISKS=40
ENRICH=40
ENRICH_BUDGET=160
ENRICH_ROUNDS=1
DISCOVER=40
MIN_CONF=0.85
DO_MERGE=1
DO_ENRICH=1
DO_DISCOVER=1
TOKEN_CAP=25000000
CNY_CAP=12
JUDGEMENT_EFFORT=high
EXTRACTION_EFFORT=
LOCAL_JUDGEMENT=0
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
  --findings N       logic findings per round (default 60)
  --pairs N          duplicate pairs per round (default 40)
  --audit N          audit findings per round (default 60)
  --risks N          obstacles per round (default 40)
  --enrich N         projects to enrich (default 40); 0 to skip
  --enrich-budget N  articles the enrich phase may read (default 160)
  --enrich-rounds N  enrich in the first N rounds only (default 1)
  --discover N       queued articles to read, the news first (default 40); 0 to skip
  --min-confidence F floor a duplicate fold needs (default 0.85)
  --no-merge         never fold duplicates; park and rule only. Deletes nothing.
  --cny N            stop when the night's spend, priced, reaches N yuan (default 12)
  --tokens N         also stop when prompt+reply tokens pass N (default 25,000,000)
  --judgement-effort E  reasoning for risks, audit and settle: low|high|max (default high)
  --extraction-effort E reasoning for reading articles (default: whatever .env says)
  --local-judgement  send risks, audit and settle to the local model (Ollama)
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
    --enrich-rounds)  shift; ENRICH_ROUNDS="${1:?}" ;;
    --discover)       shift; DISCOVER="${1:?}" ;;
    --min-confidence) shift; MIN_CONF="${1:?}" ;;
    --cny)            shift; CNY_CAP="${1:?}" ;;
    --tokens)         shift; TOKEN_CAP="${1:?}" ;;
    --judgement-effort)  shift; JUDGEMENT_EFFORT="${1:?}" ;;
    --extraction-effort) shift; EXTRACTION_EFFORT="${1:?}" ;;
    --local-judgement) LOCAL_JUDGEMENT=1 ;;
    --backup-every)   shift; BACKUP_EVERY="${1:?}" ;;
    --no-merge)       DO_MERGE=0 ;;
    --status)         STATUS_ONLY=1 ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ "$ENRICH" -gt 0 ] 2>/dev/null || DO_ENRICH=0
[ "$DISCOVER" -gt 0 ] 2>/dev/null || DO_DISCOVER=0

effort_ok() { case "$1" in low|high|max) return 0 ;; *) return 1 ;; esac; }
effort_ok "$JUDGEMENT_EFFORT" || { echo "--judgement-effort must be low, high or max" >&2; exit 2; }
[ -z "$EXTRACTION_EFFORT" ] || effort_ok "$EXTRACTION_EFFORT" \
  || { echo "--extraction-effort must be low, high or max" >&2; exit 2; }
# Exported, so every `tracker` child reads them. The environment wins over `.env`,
# which is the point: these are the night's settings, not the machine's.
export TRACKER_DEEPSEEK_JUDGEMENT_EFFORT="$JUDGEMENT_EFFORT"
[ -z "$EXTRACTION_EFFORT" ] || export TRACKER_DEEPSEEK_EXTRACTION_EFFORT="$EXTRACTION_EFFORT"
[ "$LOCAL_JUDGEMENT" -eq 0 ] || export TRACKER_JUDGEMENT_PROVIDER=ollama

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
  # Not `local`: the morning report compares the database with the first one.
  SNAPSHOT="$dest"
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

# What this run's paid calls cost, in yuan with two decimals, priced by
# `tracker.spend` at the rate for the hour each was made. `|| echo 0.00` rather than
# letting a failure through: this is read under `set -e`, and a pricing problem must
# cost the night its ceiling check, never the night itself.
spent_cny() {
  "$PY" -m tracker.spend total "$TRACKER_SPEND_LEDGER" 2>/dev/null || echo 0.00
}

# The night's spend by phase — command, and the stage within it where one was
# recorded — dearest first, with how much of the prompt the provider served from
# cache. A phase whose hit rate falls is one whose prompt prefix is being disturbed.
spend_by_command() {
  "$PY" -m tracker.spend report "$TRACKER_SPEND_LEDGER" 2>/dev/null || echo "    (no report)"
}

# True when any of the three counts — findings, groups, rows below T2 — fell below the
# lowest it has been tonight, and lowers that minimum. Against the minimum rather than
# last round's value, so a count that wobbles up and back down is not progress; see
# WHY ROUNDS in the header. `return $moved` is an answer for an `if`, never an exit.
progressed() {
  local moved=1
  if [ "$1" -lt "$MIN_F" ]; then MIN_F=$1; moved=0; fi
  if [ "$2" -lt "$MIN_D" ]; then MIN_D=$2; moved=0; fi
  if [ "$3" -lt "$MIN_B" ]; then MIN_B=$3; moved=0; fi
  return $moved
}

# True when either ceiling is reached: yuan, or tokens. Called before every paid
# phase rather than once a round: a round is several phases, so a check at the top of
# the round let one round overshoot the ceiling by a round.
over_ceiling() {
  SPENT=$(spent_so_far)
  CNY=$(spent_cny)
  if [ "$SPENT" -ge "$TOKEN_CAP" ]; then return 0; fi
  awk -v spent="$CNY" -v cap="$CNY_CAP" 'BEGIN { exit !(spent + 0 >= cap + 0) }'
}

# --- start ------------------------------------------------------------------

STARTED=$(date +%s)
# The moment rows count as new tonight, in the database's own clock (naive UTC).
STARTED_UTC=$(date -u '+%Y-%m-%d %H:%M:%S')
DEADLINE=$((STARTED + HOURS * 3600))
SPENT=0
CNY=0.00
STALE=0
CAPPED=0

say "overnight starting  (pid $$)"
printf '    repo      %s\n' "$REPO"
printf '    ceilings  %sh, %s rounds, ¥%s, %s tokens\n' "$HOURS" "$ROUNDS" "$CNY_CAP" "$TOKEN_CAP"
printf '    per round %s findings, %s audit, %s risks, %s pairs\n' \
  "$FINDINGS" "$AUDIT" "$RISKS" "$PAIRS"
printf '    enrich    %s row(s) below T2, %s article budget, first %s round(s)\n' \
  "$ENRICH" "$ENRICH_BUDGET" "$ENRICH_ROUNDS"
printf '    merge=%s  enrich=%s  min-confidence %s\n' "$DO_MERGE" "$DO_ENRICH" "$MIN_CONF"
if [ "$LOCAL_JUDGEMENT" -eq 1 ]; then WHERE=', on the local model'; else WHERE=''; fi
printf '    judgement %s effort%s\n' "$JUDGEMENT_EFFORT" "$WHERE"
printf '    spend     %s\n' "$TRACKER_SPEND_LEDGER"

read -r F0 D0 B0 <<<"$(counts)"
printf '    at start  %s finding(s), %s duplicate group(s), %s row(s) below T2\n' "$F0" "$D0" "$B0"
# The lowest each count has been tonight. Progress is a count falling below its
# minimum, not below last round's value — see WHY ROUNDS in the header.
MIN_F=$F0; MIN_D=$D0; MIN_B=$B0

say 'snapshot before anything is deleted'
SNAPSHOT=""
backup
FIRST_SNAPSHOT="$SNAPSHOT"

# Before each paid phase. `break` leaves the round loop from inside its body, so the
# settle below still runs once for whatever the night managed.
capped() {
  if over_ceiling; then
    say "spend ceiling reached (¥$CNY of ¥$CNY_CAP, ~$SPENT of $TOKEN_CAP tokens) before $1 — stopping"
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
  CNY=$(spent_cny)

  say "round $round of $ROUNDS  (¥$CNY, ~$SPENT tokens, $(( (DEADLINE - now) / 60 ))m left)"
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
  # this line can move one, because they are not wrong — they are empty. First
  # round(s) only: a second pass the same night finds the same pages, and a page it
  # has read is not read again, so it was paying to be told so. `--t2` picks the rows
  # that tier fails, fewest missing first.
  if [ "$DO_ENRICH" -eq 1 ] && [ "$round" -le "$ENRICH_ROUNDS" ]; then
    if capped enrich; then break; fi
    phase "enrich — $ENRICH row(s) below T2, $ENRICH_BUDGET article budget"
    tracker enrich --select "$ENRICH" --t2 --target 0 --budget "$ENRICH_BUDGET" \
      < /dev/null || true
  fi

  # --- discover: new campuses, through the identity check -----------------
  # The one phase that adds rows. First round only, for the reason enrich is: the
  # queue does not change between rounds. Polling the feeds costs nothing; each
  # article read is one extraction (about ¥0.03), and the crawl's identity check —
  # one call, only when a record nearly matches a row — is what keeps a campus
  # already held under another name from becoming its twin. Rounds after this one
  # run `duplicates` over whatever it added.
  if [ "$DO_DISCOVER" -eq 1 ] && [ "$round" -eq 1 ]; then
    if capped discover; then break; fi
    phase "discover — poll the feeds, read $DISCOVER queued article(s), the news first"
    tracker discover < /dev/null || true
    tracker ingest crawl --from-queue --new-first --limit "$DISCOVER" < /dev/null || true

    # One attempt to fold per row tonight added, each against its likeliest twin
    # wherever that twin is filed (`duplicates resolve --created-since`). The
    # regular pass only sees pairs within one town or one company, and on
    # 2026-10-05 a new 10 GW PORTS campus row sat unpaired beside its duplicate
    # because one row named the town and a different company, the other the county.
    if capped duplicates; then break; fi
    phase "duplicates — one attempt per row created tonight"
    if [ "$DO_MERGE" -eq 1 ]; then
      tracker duplicates resolve --merge --min-confidence "$MIN_CONF" \
        --created-since "$STARTED_UTC" < /dev/null || true
    else
      tracker duplicates resolve --created-since "$STARTED_UTC" < /dev/null || true
    fi
  fi

  # --- reconcile and measure ----------------------------------------------
  phase 'settle — re-derive and score'
  tracker backfill derive < /dev/null || true
  tracker clean --snapshot --since 1 < /dev/null || true

  SPENT=$(spent_so_far)
  read -r F1 D1 B1 <<<"$(counts)"
  printf '\n  round %s: findings %s -> %s (%+d)  groups %s -> %s (%+d)  below-T2 %s -> %s (%+d)\n' \
    "$round" "$F0" "$F1" "$((F1 - F0))" "$D0" "$D1" "$((D1 - D0))" "$B0" "$B1" "$((B1 - B0))"

  if progressed "$F1" "$D1" "$B1"; then
    STALE=0
  else
    STALE=$((STALE + 1))
    printf '  nothing below the night'"'"'s lowest (%s findings, %s groups, %s below T2): %s of %s stale rounds\n' \
      "$MIN_F" "$MIN_D" "$MIN_B" "$STALE" "$DRY_ROUNDS"
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
say "overnight complete — ${ELAPSED}m, ¥$(spent_cny), ~$(spent_so_far) tokens"
spend_by_command

# Every value the night changed, each with the sentence now behind it. The quality
# counts above cannot see a wrong value that has a real quote; a reader can, in the
# minutes this list takes to read. `tracker logic rule-out` takes back a bad one.
say 'what the night changed'
if [ -n "$FIRST_SNAPSHOT" ] && [ -f "$FIRST_SNAPSHOT" ]; then
  tracker changes --against "$FIRST_SNAPSHOT" < /dev/null || true
else
  echo "    no snapshot from before round 1 to compare against"
fi
echo
printf '    Anything still listed needs either a person or a command that does not\n'
printf '    exist yet. The block findings are the second kind — see this header.\n'
