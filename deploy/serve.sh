#!/bin/zsh
# Start the production console, published if it can be, loopback if it cannot.
#
# Run by launchd (app.mastri.dctracker.serve), restarted on exit.
#
# **The fallback is the point.** `tracker cloudflare` needs a named tunnel that
# only a human can create: `cloudflared tunnel login` is a browser sign-in, and
# `create`/`route dns` write credentials into a home directory and a record into
# a DNS zone. Neither is something a deploy script should do on somebody's
# behalf, and tunnel.py refuses to do them for the same reason. So until those
# three commands have been run, this serves on loopback and says why, rather
# than crash-looping under launchd every ten seconds with an error nobody reads.
set -u

REPO="$HOME/dc-tracker"
PORT=8765
cd "$REPO" || exit 1

# launchd hands a process a minimal PATH; Homebrew is not on it.
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

python="$REPO/.venv/bin/python"
[ -x "$python" ] || { echo "no venv at $python -- run deploy/poll.sh once"; exit 1; }

# Read-only, always. A public URL in front of a process that spawns CLI commands
# is what the tunnel module's own documentation warns about, and production has
# no reason to write: the development machine is the only writer.
mode=(--no-run --port "$PORT")

name="${TRACKER_TUNNEL_NAME:-dc-console}"
if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
  echo "not published: no Cloudflare login yet. See deploy/README.md."
  exec "$python" -m tracker serve "${mode[@]}"
fi

# The tunnel has to exist *and* this machine has to hold its credentials, and
# those are two different things. `cloudflared tunnel create` writes
# ~/.cloudflared/<UUID>.json on whichever machine ran it, and only `cert.pem`
# comes from `login` — so a tunnel created elsewhere lists fine here, routes DNS
# fine here, and cannot be run here. Checking only the login missed exactly that:
# `dc-console` existed, `mastri.app` already pointed at it, and its credentials
# were still on the development machine.
id=$(cloudflared tunnel list --output json 2>/dev/null \
  | /usr/bin/python3 -c "import json,sys;print(next((t['id'] for t in json.load(sys.stdin) if t['name']=='$name'),''))" 2>/dev/null)
if [ -z "$id" ]; then
  echo "not published: no tunnel named '$name'. See deploy/README.md."
  exec "$python" -m tracker serve "${mode[@]}"
fi
if [ ! -f "$HOME/.cloudflared/$id.json" ]; then
  echo "not published: tunnel '$name' ($id) exists but its credentials are not on this machine."
  echo "  Copy ~/.cloudflared/$id.json from wherever it was created, or recreate the tunnel here."
  exec "$python" -m tracker serve "${mode[@]}"
fi

# **A password is a precondition for publishing, so it is checked with the rest.**
# `tracker cloudflare` refuses without one, correctly -- but refusing means
# exiting, and exiting under KeepAlive means launchd restarts it into the same
# refusal about every ten seconds. That took the console down: every other
# precondition had been met, so this script stopped falling back and started
# handing launchd a command that could only fail. A missing password is a reason
# not to publish, never a reason not to serve.
password=$(sed -n 's/^TRACKER_CONSOLE_PASSWORD=//p' "$REPO/.env" 2>/dev/null | tr -d '"'"'"' \r')
if [ -z "${TRACKER_CONSOLE_PASSWORD:-$password}" ]; then
  echo "not published: TRACKER_CONSOLE_PASSWORD is empty in $REPO/.env."
  echo "  A tunnel bypasses the loopback-only check by design; the password replaces it."
  exec "$python" -m tracker serve "${mode[@]}"
fi

echo "publishing through named tunnel '$name'"
exec "$python" -m tracker cloudflare --name "$name" "${mode[@]}"
