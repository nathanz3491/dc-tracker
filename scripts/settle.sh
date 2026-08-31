#!/usr/bin/env bash
#
# The settlement loop: every quality command `tracker sync` does not run.
#
# `tracker sync` calls its sixth phase "settle", and that phase is two calls --
# `derive.run()` and `recompute_confidence()`. Both are pure recomputations. Every
# command that actually settles an open question -- publication dates, geocoding,
# logic collisions, unquoted obstacles, implausible figures, suspected duplicates
# -- is a separate command, and nothing chains them. Measured on the live database
# before this script existed: 471 rows, 27 at T2 COMPLETE, 0 at T3 SETTLED.
#
# Each of those commands already decides without a person: `duplicates resolve`
# defaults to `--llm` on and `--ask` off, `risks confirm` has no `--ask` at all,
# and `audit resolve` goes straight to the model when there is no terminal. The
# gap was never the automation. It was that nobody ran them in order.
#
# WHY THIS ORDER, AND NOT ANOTHER
#
#   dates before derive      64% of queued URLs carry no publication date, so a
#                            merge tiebreak falls back to crawl order. Settling
#                            collisions first records the wrong winner as settled.
#   geo before duplicates    a merge rail refuses a pair whose stored coordinates
#                            are more than 25 km apart. With coordinates on 39% of
#                            rows that rail cannot fire on most pairs at all.
#   derive after both        every derived value is a pure function of the
#                            citations and is only recomputed when something
#                            writes to the row.
#   duplicates last          it is the only step here that deletes rows.
#
# THREE TIERS, following the convention `tracker sync` already sets -- cheap by
# default, the expensive phases off unless asked for:
#
#   (default)   free. No LLM calls. Network only for the two Census files.
#   --llm       adds the model-decided phases. One call per finding.
#   --merge     adds `duplicates resolve --merge`, which DELETES rows. Implies
#               --llm, and takes a VACUUM INTO snapshot first.
#
# Steady-state cost is far below the per-run limit: every LLM phase skips findings
# a previous run already answered, so the first run pays for the backlog and later
# runs pay only for what arrived since.
#
# NOT IN HERE YET: `tracker blocks` finds 110 mergeable tranche groups across 69
# projects and has no write path -- there is no `blocks fold`. It is the largest
# remaining unattended win and it needs code, not a line in this script.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# launchd hands a process a minimal PATH, and the `tracker` shim lives in
# ~/.local/bin which is not on it. The venv interpreter is reachable because we
# just resolved the repo, so go through it rather than the shim -- the same
# reasoning ops/serve.sh uses for the same reason.
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PY="$REPO/.venv/bin/python"
tracker() { "$PY" -m tracker "$@"; }

WITH_LLM=0
WITH_MERGE=0
DRY=0
SKIP_CENSUS=0
REFETCH_DATES=0
LIMIT="${SETTLE_LIMIT:-200}"
DATE_TRANCHE="${SETTLE_DATE_TRANCHE:-200}"

usage() {
  cat <<'USAGE'
usage: settle.sh [--llm] [--merge] [--dry-run] [--skip-census]
                 [--refetch-dates] [--limit N]

Runs the quality commands `tracker sync` leaves out, in dependency order, and
records the tier movement so consecutive runs are comparable.

  --llm           run the model-decided phases (risks, audit, logic --llm)
  --merge         also fold suspected duplicates. Deletes rows. Implies --llm.
  --dry-run       write nothing where a command supports it. Still pays for the
                  LLM calls -- `duplicates resolve --dry-run` is explicit that
                  the calls happen and only the transaction is discarded.
  --skip-census   do not fetch the two Census reference files when absent
  --refetch-dates ask publishers for the dates that are not in the URL path.
                  No LLM, but one HTTP request per URL, so it goes in tranches
                  and is off by default. 965 URLs wanted this when measured.
  --limit N       findings per LLM phase (default 200, or $SETTLE_LIMIT)

environment:
  SETTLE_LOG           where to append the run log
  SETTLE_BACKUP_DIR    where --merge puts its snapshot
  SETTLE_DATE_TRANCHE  URLs per --refetch-dates run (default 200)
  TRACKER_DB           the database, if not data/tracker.db

Read the header of this file for why the phase order is what it is.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --llm)         WITH_LLM=1 ;;
    --merge)       WITH_MERGE=1; WITH_LLM=1 ;;
    --dry-run)     DRY=1 ;;
    --skip-census)   SKIP_CENSUS=1 ;;
    --refetch-dates) REFETCH_DATES=1 ;;
    --limit)       shift; LIMIT="${1:?--limit needs a number}" ;;
    -h|--help)     usage; exit 0 ;;
    *)             echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# Checked after the options, so --help works on a machine with no venv.
[ -x "$PY" ] || { echo "no venv at $PY -- let the deploy poller run once" >&2; exit 1; }

LOG="${SETTLE_LOG:-$REPO/data/runs/settle.log}"
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

PHASE="startup"
on_error() {
  local code=$1
  printf '\n!!! settle failed during "%s" (exit %d)\n' "$PHASE" "$code"
  printf '    Nothing after this phase ran. Every phase is resumable: fix the\n'
  printf '    cause and run again, and the phases that already succeeded stay\n'
  printf '    written. A write-lock message means another tracker run is in\n'
  printf '    flight -- wait for it rather than forcing anything.\n'
  exit "$code"
}
trap 'on_error $?' ERR

phase() { PHASE="$1"; printf '\n=== %s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }
skip()  { printf '    skipped: %s\n' "$1"; }

printf '=== %s  settle starting\n' "$(date '+%Y-%m-%d %H:%M:%S')"
printf '    repo   %s\n' "$REPO"
printf '    tiers  free'
[ $WITH_LLM -eq 1 ]   && printf ' + llm'
[ $WITH_MERGE -eq 1 ] && printf ' + merge'
[ $DRY -eq 1 ]        && printf '   (dry run)'
printf '\n    limit  %s findings per LLM phase\n' "$LIMIT"

# --- free phases ------------------------------------------------------------

phase 'dates -- fill publication dates so merges rank on them, not crawl order'
if [ $DRY -eq 1 ]; then
  tracker backfill dates              # without --apply it reports and writes nothing
else
  tracker backfill dates --apply
fi

if [ $REFETCH_DATES -eq 1 ]; then
  phase "dates --refetch -- ask publishers for the rest, $DATE_TRANCHE at a time"
  # Free of LLM calls but one HTTP request per URL, which is why it is opt-in and
  # tranched rather than part of the default run. Resumable: a URL whose date is
  # already stored is skipped, so repeated runs walk through the backlog.
  if [ $DRY -eq 1 ]; then
    tracker backfill dates --refetch --limit "$DATE_TRANCHE" < /dev/null
  else
    tracker backfill dates --refetch --apply --limit "$DATE_TRANCHE" < /dev/null
  fi
fi

phase 'geo -- derive county and coordinates from Census reference data'
CENSUS="$REPO/data/raw/census"
COUNTY_FILE="$CENSUS/place_by_county2020.txt"
GAZ_FILE="$CENSUS/gaz_place.zip"
if [ ! -f "$COUNTY_FILE" ] || [ ! -f "$GAZ_FILE" ]; then
  if [ $SKIP_CENSUS -eq 1 ]; then
    skip 'Census reference data absent and --skip-census given'
  else
    # Free, no API key, byte-identical for everyone and re-downloadable from a
    # stable URL -- which is why data/raw/census/ is the one gitignore exception
    # among reference data. `tracker ingest geo` names these two URLs itself when
    # they are missing; this fetches exactly those, and only when they are absent.
    #
    # Non-fatal on purpose. Geocoding is one phase of several, and a fetch that
    # fails because the network is down should not take the free phases either
    # side of it with it. curl -f leaves no partial file behind, so the existence
    # check below is what decides whether geo runs.
    printf '    Census reference data absent -- fetching (free, no API key)\n'
    mkdir -p "$CENSUS"
    [ -f "$COUNTY_FILE" ] || curl -fsSL -o "$COUNTY_FILE" \
      'https://www2.census.gov/geo/docs/reference/codes2020/national_place_by_county2020.txt' \
      || printf '    could not fetch place_by_county2020.txt\n'
    [ -f "$GAZ_FILE" ] || curl -fsSL -o "$GAZ_FILE" \
      'https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_place_national.zip' \
      || printf '    could not fetch gaz_place.zip\n'
  fi
fi
if [ -f "$COUNTY_FILE" ] && [ -f "$GAZ_FILE" ]; then
  if [ $DRY -eq 1 ]; then tracker ingest geo --dry-run; else tracker ingest geo; fi
else
  skip 'no Census reference data, so county and coordinates stay as they are'
fi

phase 'derive -- re-apply every derived value from what the citations now imply'
if [ $DRY -eq 1 ]; then
  skip 'backfill derive has no dry run; it only recomputes pure functions'
else
  tracker backfill derive
fi

phase 'logic --auto -- the collisions that re-running the merge policy settles'
if [ $DRY -eq 1 ]; then
  tracker logic resolve --auto        # without --apply it reports and writes nothing
else
  tracker logic resolve --auto --apply
fi

# --- model-decided phases ---------------------------------------------------
#
# stdin is /dev/null on every one of these. Each has a non-interactive path, and
# an unexpected prompt should hit EOF and stop rather than hang a scheduled run
# forever while holding the write lock.

if [ $WITH_LLM -eq 1 ]; then

  phase 'risks -- read the article behind each unquoted obstacle and settle it'
  if [ $DRY -eq 1 ]; then
    tracker risks confirm --limit "$LIMIT" --dry-run < /dev/null
  else
    tracker risks confirm --limit "$LIMIT" < /dev/null
  fi

  phase 'audit -- settle the implausible figures'
  if [ $DRY -eq 1 ]; then
    skip 'audit resolve has no --dry-run, and it writes'
  else
    tracker audit resolve --no-ask --limit "$LIMIT" < /dev/null
  fi

  phase 'logic --llm -- the collisions arithmetic could not settle'
  if [ $DRY -eq 1 ]; then
    skip 'logic resolve --llm has no --dry-run, and it writes'
  else
    tracker logic resolve --llm --limit "$LIMIT" < /dev/null
  fi

else
  phase 'model-decided phases'
  skip 'pass --llm to run risks, audit and logic --llm'
fi

# --- duplicates -------------------------------------------------------------

if [ $WITH_MERGE -eq 1 ]; then

  phase 'backup -- a consistent snapshot before the one step that deletes rows'
  DB="${TRACKER_DB:-$REPO/data/tracker.db}"
  if [ -n "${SETTLE_BACKUP_DIR:-}" ]; then
    BACKUP_DIR="$SETTLE_BACKUP_DIR"
  elif [ -d "$REPO/../backups" ]; then
    BACKUP_DIR="$REPO/../backups"
  else
    BACKUP_DIR="$REPO/data/backups"
  fi
  mkdir -p "$BACKUP_DIR"
  DEST="$BACKUP_DIR/tracker.backup-before-settle-$(date '+%Y%m%d-%H%M%S').db"
  # VACUUM INTO, never cp. The database runs in WAL mode, so committed data sits
  # in tracker.db-wal until a checkpoint folds it back, and a copy of the main
  # file alone opens cleanly while being silently out of date. Same reasoning as
  # scripts/sync_db.py, and the same one-line implementation.
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

  phase 'duplicates -- a model decides, the rails decide what it may do'
  printf '    Of 43 suspected groups measured on the live database, 28 are\n'
  printf '    city-vs-county and 8 are name-overlap, and the rails refuse to\n'
  printf '    merge those unattended by design. The park half needs no rails\n'
  printf '    and is the useful one: capex holds one row of every suspected\n'
  printf '    group out of the totals until the pair is ruled out.\n'
  if [ $DRY -eq 1 ]; then
    tracker duplicates resolve --merge --limit "$LIMIT" --dry-run < /dev/null
  else
    tracker duplicates resolve --merge --limit "$LIMIT" < /dev/null
  fi

elif [ $WITH_LLM -eq 1 ]; then
  phase 'duplicates'
  skip 'pass --merge to fold them; without it nothing is parked or merged'
fi

# --- the scorecard ----------------------------------------------------------

phase 'clean -- record the tier movement, and diff against the previous run'
if [ $DRY -eq 1 ]; then
  tracker clean
else
  # The snapshot is written first, so --since 1 diffs this run against the
  # previous one. On a first run it says it needs two snapshots, and carries on.
  tracker clean --snapshot --since 1
fi

printf '\n=== %s  settle complete\n' "$(date '+%Y-%m-%d %H:%M:%S')"
