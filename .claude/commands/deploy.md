---
description: Ship code to production — push to main, let production poll it, verify the console is serving it
---

Deploy the current work end to end: **push → production picks it up from GitHub →
verify it is live on the public console.**

This file is committed, and that is deliberate. It used to be local-only because
it named the host, which meant the only real deploy procedure lived on one laptop
and no agent had ever read it. The host's identity now comes from `.env`
(`TRACKER_PROD_HOST`, `TRACKER_PROD_CHECKOUT`, `TRACKER_SERVE_LABEL`,
`TRACKER_TUNNEL_HOSTNAME`) and `scripts/prod.py` reads it, so every command below
is correct from any checkout on any machine. **Keep it that way**: no hostname,
no ssh alias, no launchd label, no domain in this file — see `CLAUDE.md` §6.

Throughout, `python` means *this checkout's* interpreter — `.venv/bin/python` on
macOS, `.venv/Scripts/python` on Windows. That is the only difference between the
two machines that these instructions cannot hide.

## 0. Know which checkout you are in

```bash
ls .production            # present => this checkout IS production
python scripts/prod.py --where
```

A checkout carrying `.production` serves the console and owns the authoritative
database. Every other checkout — an agent's worktree, a laptop's clone — is a
workspace: it may run the whole suite and write its own database, and it may not
write production's. On the production machine, `tracker` on `PATH` refuses a
writing command from outside that checkout and tells you what to use instead.

## 1. Before pushing

**The leak guard.** This must print nothing and exit 0:

```bash
python scripts/check_no_host_names.py
```

It has caught a real leak (a `git reset` silently restored ignored files to the
index), and the one-line grep it replaced *missed* one for weeks: the host's ssh
alias sat in a tracked file as a script's default value, while the pattern looked
for that alias only in `ssh <alias>` form. If it prints a file, redact it
(`$PROD`, `<app-id>`, `<deploy-key>`, `console.example` in tests) or read the
value from `.env` the way `scripts/prod.py` does.

The script — not a grep pinned in a doc — is why this file can be committed at
all: a pattern list written into prose has to name what it forbids, and would
flag itself the moment the doc became a tracked file.

Then:

```bash
python -m pytest -q
python -m ruff check tracker/ tests/ scripts/
```

The suite must be green. One known failure is acceptable **only** in a git
worktree — `test_health_reports_the_commit_it_is_serving`, because `.git` is a
file there rather than a directory, so `deployed_commit()` cannot read HEAD.
Confirm that is the failure before shrugging at it. Note this now bites more
often than it used to: every agent task runs in a worktree.

Commit rules: atomic commits, imperative subject ≤72 chars, body says *why*.
Update `CHANGELOG.md` (`## [Unreleased]`, newest first, grouped Added/Changed/
Fixed/Removed) and any doc the change makes stale, **in the same commit** —
including the workflow page named in `CLAUDE.md` §7 if you touched a staged
command. Never work on `main` directly; branch, then push to `main`.

## 2. Push

```bash
git merge-base --is-ancestor origin/main HEAD && echo "fast-forward safe"
git push origin HEAD:main
```

Never force-push. If that is not a fast-forward, stop and work out why.

**Code only ever travels through GitHub.** Never copy a source file to
production — not by `scp` from another machine, and not by `cp` from another
directory on the same machine, which is the new way to make this mistake. Either
produces a checkout whose code matches no commit, and the next poll silently
reverts it with `git reset --hard`.

## 3. Let production pick it up

The poller runs every two minutes. To skip the wait:

```bash
python scripts/prod.py '~/dev/tracker/ops/poll.sh'
```

Three things it does that you should expect:

- **It refuses a commit that does not import**, rolling the checkout back so the
  console keeps serving the previous code. That is the point of noticing a push
  rather than trusting it.
- **It runs `tracker init`** — migrations plus a recompute. A change to the merge
  engine therefore moves production values on deploy. Check it (step 5).
- **It defers rather than races.** `tracker init` needs the single write lock, so
  if a long job is holding it the poller logs the deferral and leaves the
  checkout alone; the next cycle retries. A deploy that seems two minutes late
  during a `sync` or an overnight run is this, working.

## 4. If the fetch fails

```bash
python scripts/prod.py 'tail -20 ~/dev/tracker/ops/logs/deploy.log'
```

`fetch failed (network?)` appears intermittently and is usually nothing: the line
is logged on any fetch error and the next cycle succeeds. What matters is whether
it is *every* cycle. Compare the two heads:

```bash
python scripts/prod.py 'git rev-parse --short HEAD origin/main'
```

If they differ and stay differing, diagnose:

```bash
python scripts/prod.py 'git remote get-url origin && git fetch origin'
```

**Known cause, hit once for four days.** With an SSH remote you get
`kex_exchange_identification: Connection closed by remote host` — the TCP
connection opens and GitHub closes it before the version exchange. It failed
identically on port 22 and on `ssh.github.com:443`, so it is not a blocked port.
The fix was to stop using SSH for a read-only fetch of a public repo; the remote
is HTTPS now, which needs no credential to read. Push is a different matter and
does need one.

## 5. Verify — a restart is not proof

```bash
python scripts/prod.py 'git rev-parse --short HEAD'
python scripts/prod.py 'tail -5 ~/dev/tracker/ops/logs/deploy.log'
```

The commit must match what you pushed, and `deploy.log` should read
`deployed <sha>, console restarted`. Then the public endpoint:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "$(python scripts/prod.py --console-url)"
curl -s "$(python scripts/prod.py --console-url)" | grep -o "<title>[^<]*</title>"
```

Expect `200` and `dc-tracker console`. `GET /api/health` returns the serving
commit but sits **behind the login**, so it answers `{"error": "sign in first"}`
unauthenticated — that response is itself proof the new code is serving. Do not
go digging a password out of the database to check it; confirm in a browser.

**Run this check from a machine that is not production if you have one.** From
production itself the request goes out to Cloudflare and back down the machine's
own tunnel: it proves the round-trip, but not that anyone else can reach it.

If the console is down:

```bash
python scripts/prod.py 'pgrep -fl cloudflared'
python scripts/prod.py --restart-console
```

The serve service starts the tunnel, pointed at loopback. There is a second,
unrelated tunnel on that machine — leave it alone.

## 6. Rules that still apply

- **Data is written from the production checkout**, by way of
  `python scripts/prod.py tracker …`, which lands there so the guard permits it.
  `ingest`, `enrich`, `merge`, `infer`, `backfill`, `logic resolve`,
  `audit resolve`. Reads (`gaps`, `sources`, `overview`, `export`, `capex`,
  `logic check`, `audit check`) run from any checkout, anywhere.
- **`ops/` sits beside the production checkout, never inside it**, so a bad
  commit cannot replace the deployer — or the `tracker` guard — that a fix would
  have to travel through. Nothing in `ops/` is in this repo, by the same
  argument.
- **The database moves only through the script**, never `cp`/`scp` — it runs in
  WAL mode, so a copy of `tracker.db` alone opens cleanly and is silently stale:
  ```bash
  python scripts/sync_db.py          # pull production down to this machine
  python scripts/sync_db.py --push   # only to seed a host or restore one
  ```
  Both refuse when the destination holds rows the source does not.
- **`.env` stays put.** It differs per machine by design — production's carries
  the API keys because it ingests, and the four `TRACKER_PROD_*`/tunnel values
  differ everywhere. Move one line by hand if needed, never the file. A workspace
  must never receive production's `.env`: no keys is what makes a stray command
  in a worktree unable to spend money.

## 7. Report

Say what commit is live, what the deploy log said, what the public URL returned,
and — if the deploy ran a recompute — what moved in the data:

```bash
python scripts/prod.py 'tracker logic check 2>&1 | tail -3; tracker audit check 2>&1 | tail -3'
```

If figures moved in a way a person should confirm, say so plainly rather than
running `logic conflicts --apply` or `audit resolve` unasked — those change
published numbers.
