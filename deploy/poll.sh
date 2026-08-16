#!/bin/zsh
# Notice a push, take it down, restart the console.
#
# Run by launchd (app.mastri.dctracker.poll) every two minutes.
#
# **Polling rather than a webhook**, because the mini is behind NAT and a webhook
# would need an inbound path into it. A poller needs no inbound surface at all,
# and two minutes is well inside "I pushed, go look" latency.
#
# **The running copy of this script lives outside the repo**, at
# ~/dev/tracker/ops/poll.sh, and is deliberately not updated by the deploy it
# performs. A deployer that deploys itself can be bricked by one bad commit: the
# broken version is what runs next, so it can never pull the fix. The canonical
# copy is here in the repo for review and history; installing an update is a
# conscious `cp`, documented in deploy/README.md.
set -u

REPO="$HOME/dev/tracker/repo"
BRANCH="main"
LABEL="app.mastri.dctracker.serve"
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

cd "$REPO" || { log "no repo at $REPO"; exit 1; }

git fetch --quiet origin "$BRANCH" 2>/dev/null || { log "fetch failed (network?)"; exit 0; }

local_head=$(git rev-parse HEAD)
remote_head=$(git rev-parse "origin/$BRANCH")
[ "$local_head" = "$remote_head" ] && exit 0

log "new commit: ${local_head:0:8} -> ${remote_head:0:8}"

# `reset --hard`, not `pull`. This checkout is a deploy target, never a place
# anyone edits, so there is no local work to preserve and a merge conflict here
# would wedge every future deploy waiting for a human.
git reset --hard --quiet "origin/$BRANCH" || { log "reset failed"; exit 1; }

# Dependencies can change with a commit; pyproject is the record of that.
if ! git diff --quiet "$local_head" "$remote_head" -- pyproject.toml; then
  log "pyproject changed, syncing dependencies"
  uv pip install --quiet -e ".[reader,impersonate,iso]" || { log "dependency sync failed"; exit 1; }
fi

# Migrations, so new code never serves an old schema. `init` takes no flags: it
# applies what is outstanding and recomputes the derived values, and it is a
# write -- but only to this replica, which the next `ship_db.py` overwrites
# wholesale, so nothing can drift here for longer than one deploy.
if ! .venv/bin/python -m tracker init >>"$HOME/dev/tracker/ops/logs/deploy.log" 2>&1; then
  log "WARNING: 'tracker init' failed; schema may be behind the code"
fi

# **Import before restart.** A commit that does not import takes the console down
# and launchd then restarts it into the same failure forever. Catching it here
# leaves the previous process serving, which is the whole point of noticing.
if ! .venv/bin/python -c "import tracker, tracker.webui.server" 2>/dev/null; then
  log "REFUSED: ${remote_head:0:8} does not import. Console left running on the old code."
  git reset --hard --quiet "$local_head"
  exit 1
fi

launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null \
  && log "deployed ${remote_head:0:8}, console restarted" \
  || log "deployed ${remote_head:0:8}, but restart failed -- is the serve agent loaded?"
