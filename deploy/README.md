# Deploying to the Mac mini

```
this machine (sole writer) ──git push──▶ github ──poll 2min──▶ mac mini
        └────────── scripts/ship_db.py over ssh ─────────────▶ serve --no-run
                                                               └─▶ mastri.app
```

Two things travel, by different roads, and that split is the design:

- **code** goes through GitHub, so every deploy is a commit somebody can read;
- **data** goes straight over SSH, because the database is gitignored and a
  16 MB binary in git history would be a mistake that compounds.

The development machine stays the **only writer**. Production never runs a model,
never searches, never writes a row — so it holds no API keys at all.

---

## The parts

| Where | What |
| --- | --- |
| `~/dc-tracker` | the clone, reset to `origin/main` on every deploy |
| `~/dc-tracker-ops/serve.sh` | starts the console; publishes if it can |
| `~/dc-tracker-ops/poll.sh` | notices a push, takes it down, restarts |
| `~/dc-tracker-ops/logs/` | `serve.log`, `deploy.log` |
| `app.mastri.dctracker.serve` | launchd, `KeepAlive` |
| `app.mastri.dctracker.poll` | launchd, every 120s |

**The running `poll.sh` is a copy, not the repo's file.** A deployer that deploys
itself can be bricked by one bad commit — the broken version is what runs next,
so it can never pull the fix. Updating it is a conscious step:

```bash
scp deploy/poll.sh mm:/tmp/ && ssh mm 'install -m 755 /tmp/poll.sh ~/dc-tracker-ops/'
```

---

## Publishing it — three commands only you can run

The console is on loopback until these are done, and says so in `serve.log`.
They are yours because each one outlives the process: a browser sign-in, a
credential written to your home directory, a record written to your DNS zone.
`tunnel.py` refuses to do them on your behalf for the same reason.

```bash
ssh mm
cloudflared tunnel login
cloudflared tunnel create dc-console
cloudflared tunnel route dns dc-console mastri.app
```

Then set the console password — required, because a tunnel bypasses the
loopback-only check by design and the password is what replaces it:

```bash
ssh mm 'vi ~/dc-tracker/.env'      # TRACKER_CONSOLE_PASSWORD=
ssh mm 'launchctl kickstart -k gui/$(id -u)/app.mastri.dctracker.serve'
```

`serve.log` should then say `publishing through named tunnel 'dc-console'`.

**Cloudflare Tunnel is free.** The named tunnel is not a paid upgrade over a
`trycloudflare.com` URL — it is the same product with a hostname you own, and it
survives restarts, which a quick-tunnel URL does not.

---

## Deploying

```bash
git push                                    # code
python scripts/ship_db.py                   # data
```

Within two minutes `deploy.log` shows the new commit and a restart. To skip the
wait:

```bash
ssh mm '~/dc-tracker-ops/poll.sh'
```

`ship_db.py` never copies the database file. It runs `VACUUM INTO`, which asks
SQLite for a consistent single-file snapshot with the WAL folded in; a plain copy
of a WAL-mode database opens cleanly and is silently out of date, which has cost
this project a wrong answer once already. The snapshot is checked
(`integrity_check`, and no table smaller than the source) before it is sent, and
lands under a temporary name that is then renamed, so a reader sees one database
or the other and never a half-written one.

---

## When it goes wrong

```bash
ssh mm 'tail -30 ~/dc-tracker-ops/logs/deploy.log'
ssh mm 'tail -30 ~/dc-tracker-ops/logs/serve.log'
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
