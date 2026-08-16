# Deploying to the Mac mini

```
this machine ──git push──▶ github ──poll 2min──▶ mac mini ──▶ mastri.app
     ▲                                          (the writer:
     └────── scripts/sync_db.py over ssh ────────  ingest, enrich, merge)
```

**Code flows out; data flows back.** Code goes through GitHub, so every deploy is
a commit somebody can read. The database never goes through git — it is
gitignored, and a 16 MB binary in history is a mistake that compounds.

**The mini is the writer, and there is exactly one.** It is always on, which is
what a job measured in hours wants, and it serves the console from the same file
it writes. Every command that changes data runs there: `ingest`, `enrich`,
`merge`, `infer`, `backfill`. This machine develops against a copy and pulls a
fresh one when it wants current numbers.

That split matters more than it looks. There is **no merge** — a whole file
replaces a whole file — so two machines ingesting independently would mean the
later copy silently destroys the earlier one's work. Most of the CLI is
unaffected: `gaps`, `sources`, `overview`, `export` and `capex` only read, and
run anywhere.

The console opens the database read-only (`serve --no-run`) while ingest writes
it. SQLite in WAL mode takes one writer and any number of readers, so the site
stays up through a crawl — verified with a `backfill derive` pass running against
a live `mastri.app`.

---

## The parts

Everything lives under one project directory, `~/dev/tracker`:

| Where | What |
| --- | --- |
| `~/dev/tracker/repo` | the clone, reset to `origin/main` on every deploy |
| `~/dev/tracker/ops/serve.sh` | starts the console; publishes if it can |
| `~/dev/tracker/ops/poll.sh` | notices a push, takes it down, restarts |
| `~/dev/tracker/ops/logs/` | `serve.log`, `deploy.log` |

`ops/` sits beside the checkout rather than inside it, which is what lets the
deployer survive the deploy — see below — while still living under the project.

**The CLI is on `PATH` as `tracker`**, via a symlink at `~/.local/bin/tracker`
pointing into the venv. That directory was already on the login shell's `PATH`
and did not exist, so creating it took no shell-config edit — nothing in
`~/.zshrc` or `~/.zprofile` was touched. Symlinking the one entry point rather
than putting the whole `.venv/bin` on `PATH` keeps the venv's `python` and `pip`
from shadowing the system's.

`tracker` works from any directory: the settings file is found relative to the
installed package, not the working directory, and `TRACKER_DB` in it is absolute.

```bash
ssh mm 'tracker gaps'
ssh mm 'tracker ingest crawl'
```

**Four paths are outside the project, and only because the OS dictates them.**
Nothing else is:

| Path | Dictated by |
| --- | --- |
| `~/Library/LaunchAgents/app.mastri.dctracker.{serve,poll}.plist` | launchd loads user agents only from here |
| `~/.ssh/dc_tracker_deploy`, and a `github-dctracker` block in `~/.ssh/config` | ssh reads keys and host aliases only from here |
| `~/.cloudflared/cert.pem` | written by `cloudflared tunnel login`; per account, not per project |
| `~/.cloudflared/<tunnel-uuid>.json` | written by `cloudflared tunnel create`; the tunnel will not run without it |
| `~/.local/bin/tracker` | a symlink, so the CLI is on `PATH`; that directory was already on it |

None of the five holds anything this repository could carry instead. The plists
are committed here in `deploy/` and hold no secrets; the other three are
credentials and are not committed anywhere.

**The running `poll.sh` is a copy, not the repo's file.** A deployer that deploys
itself can be bricked by one bad commit — the broken version is what runs next,
so it can never pull the fix. Updating it is a conscious step:

```bash
scp deploy/poll.sh mm:/tmp/ && ssh mm 'install -m 755 /tmp/poll.sh ~/dev/tracker/ops/'
```

**Shell scripts must stay LF in the working tree.** They are shipped to macOS
verbatim by `scp`, and a CRLF file fails at the shebang — `/bin/zsh
` is not a
file that exists, so the error blames the interpreter and says nothing about line
endings. `.gitattributes` pins `*.sh` and `*.plist` to `eol=lf`; a Python rewrite
on Windows reintroduced CRLF once and took the console down.

---

## Publishing it — the commands only you can run

The console is on loopback until these are done, and says which step is missing
in `serve.log`. They are yours because each outlives the process: a browser
sign-in, a credential written to your home directory, a record written to your
DNS zone. `tunnel.py` refuses to do them on your behalf for the same reason.

```bash
ssh mm
cloudflared tunnel login
cloudflared tunnel create dc-console
cloudflared tunnel route dns dc-console mastri.app
```

**`mastri.app` used to be served by a second tunnel running on the development
machine** — `mastri-app`, `fc20e7c6…`, created 2026-05-24. That one is retired.
The hostname now routes to `dc-console` on the mini, and the mini is the only
machine that should hold a running tunnel for it. Two tunnels claiming one
hostname is a coin toss over which one answers.

Two things that look like failures and are not, both met while setting this up:

**"mastri.app is already configured to route to your tunnel" is success.** It is
an `INF` line from an idempotent command. Check the `tunnelID=` it prints against
`cloudflared tunnel list`; if it is the tunnel you want, there is nothing to do.
Only if it names a *different* tunnel do you need `--overwrite-dns`.

**A tunnel can exist, route correctly, and still be unrunnable here.** `login`
writes `~/.cloudflared/cert.pem`, which is per *account*. `create` writes
`~/.cloudflared/<UUID>.json`, which is per *tunnel* and lands only on the machine
that ran it. `dc-console` had been created on the development machine, so the
mini could list it and route DNS to it but had no credentials to run it — with no
error until start-up. `serve.sh` checks for both and names whichever is missing.
To move an existing tunnel to a new machine, copy that one file:

```bash
scp ~/.cloudflared/<UUID>.json mm:~/.cloudflared/ && ssh mm 'chmod 600 ~/.cloudflared/<UUID>.json'
```

Then delete it from the machine that no longer runs the tunnel.

Then set the console password — required, because a tunnel bypasses the
loopback-only check by design and the password is what replaces it:

```bash
ssh mm 'vi ~/dev/tracker/repo/.env'      # TRACKER_CONSOLE_PASSWORD=
ssh mm 'launchctl kickstart -k gui/$(id -u)/app.mastri.dctracker.serve'
```

`serve.log` should then say `publishing through named tunnel 'dc-console'`.

**Cloudflare Tunnel is free.** The named tunnel is not a paid upgrade over a
`trycloudflare.com` URL — it is the same product with a hostname you own, and it
survives restarts, which a quick-tunnel URL does not.

---

## Deploying

```bash
git push                                    # code goes out
ssh mm 'cd ~/dev/tracker/repo && .venv/bin/tracker ingest crawl'   # data is made there
python scripts/sync_db.py                   # and pulled back here
```

Within two minutes `deploy.log` shows the new commit and a restart. To skip the
wait:

```bash
ssh mm '~/dev/tracker/ops/poll.sh'
```

`sync_db.py` pulls by default, because that is now the safe direction. `--push`
exists for seeding a new host or restoring one, and **refuses when the far end
holds rows this database does not** — which is what losing an ingest looks like
from here. `--force` overrides it deliberately.

Neither direction copies the file. `VACUUM INTO` runs on whichever machine is the
source and asks SQLite for a consistent single-file snapshot with the WAL folded
in; a plain copy of a WAL-mode database opens cleanly and is silently out of
date, which has cost this project a wrong answer once already. The result is
checked (`integrity_check`, and no table smaller than the source) before it
replaces anything, and a pull also clears the old `-wal`/`-shm` siblings, which
otherwise describe a database that no longer exists here.

---

## When it goes wrong

```bash
ssh mm 'tail -30 ~/dev/tracker/ops/logs/deploy.log'
ssh mm 'tail -30 ~/dev/tracker/ops/logs/serve.log'
ssh mm 'launchctl list | grep mastri'          # 2nd column is the last exit code
ssh mm 'curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/api/health'
```

`poll.sh` refuses a commit that does not import, and rolls the checkout back to
the previous one. The console keeps serving the old code, which is the point of
noticing a push rather than trusting it.

Stop or start the service:

```bash
ssh mm 'launchctl bootout gui/$(id -u)/app.mastri.dctracker.serve'
ssh mm 'launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/app.mastri.dctracker.serve.plist'
```

---

## Access

The mini pulls with a **read-only deploy key** (`~/.ssh/dc_tracker_deploy`, and a
`github-dctracker` host alias in `~/.ssh/config`). It can fetch this repository
and nothing else, and it cannot push. Revoke it from the repository's
Settings → Deploy keys.
