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
if ! cloudflared tunnel list 2>/dev/null | grep -qw "$name"; then
  echo "not published: no tunnel named '$name'. See deploy/README.md."
  exec "$python" -m tracker serve "${mode[@]}"
fi

echo "publishing through named tunnel '$name'"
exec "$python" -m tracker cloudflare --name "$name" "${mode[@]}"
