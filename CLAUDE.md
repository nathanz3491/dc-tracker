# Operating rules

Two machines run this project. Which one you are on decides what you may do.

| | this repo on the dev machine | the Mac mini (`ssh mm`) |
| --- | --- | --- |
| role | write code | **write data**, serve production |
| runs | tests, read-only commands | everything, including ingest |
| database | a copy, pulled when needed | **the authoritative one** |
| serves | nothing | `mastri.app` |

---

## 1. Code goes out through GitHub. Never copy it by hand.

```
edit here ─▶ git push ─▶ github ─▶ mini polls (≤2 min) ─▶ restart ─▶ mastri.app
```

Push to `main` and the mini takes it within two minutes. Nothing else is needed
and nothing else is correct: an `scp` of a source file produces a host whose code
matches no commit, and the next poll silently reverts it (`git reset --hard`).

To skip the wait: `ssh mm '~/dev/tracker/ops/poll.sh'`

**A commit that does not import is refused** and the checkout rolled back, so the
console keeps serving the previous code. That is the point of noticing a push
rather than trusting it.

**Confirm what is actually live** — a restart is not proof it picked up your
commit:

```bash
ssh mm 'cd ~/dev/tracker/repo && git rev-parse --short HEAD'
tail -5 <(ssh mm 'cat ~/dev/tracker/ops/logs/deploy.log')
```

`GET /api/health` returns the same commit, behind the login.

## 2. Data is made on the mini. Ingest here writes nothing that survives.

Run every writing command there:

```bash
ssh mm 'tracker ingest crawl'
ssh mm 'tracker enrich 42'
ssh mm 'tracker merge …'      # and infer, backfill, logic resolve
```

`tracker` is on `PATH` there and works from any directory.

**Reads run anywhere** — `gaps`, `sources`, `overview`, `export`, `capex`,
`logic check`. Those are safe on either machine.

**Why the mini and not here.** It is always on, which is what a job measured in
hours wants, and it serves the console from the same file it writes. SQLite takes
one writer and any number of readers, so `mastri.app` stays up through a crawl.

**Why not both.** There is no merge. A whole file replaces a whole file, so two
machines ingesting independently means the later copy silently destroys the
other's work. The schema *could* support a real merge — projects have a
content-derived `dedup_key`, sources are keyed by URL, and every derived value is
a pure function of the attached sources, which is what `backfill derive` relies
on — but the tool does not exist, and `project.id` is autoincrement, so both
machines would hand out the same id to different campuses.

## 3. Sync the database with the script, never with `scp`.

```bash
python scripts/sync_db.py            # pull the real one down here
python scripts/sync_db.py --push     # only to seed or restore a host
```

Both directions **refuse when the destination holds rows the source does not**,
which is what losing an ingest looks like from the other end. `--force` overrides
it deliberately.

**Never `cp` or `scp` the database.** It runs in WAL mode: committed data sits in
`tracker.db-wal` until a checkpoint folds it back, so a copy of `tracker.db`
alone opens cleanly, reports no error, and is silently out of date. Measured
here: 16.3 MB of main file against a 7.9 MB WAL. That mistake has already
produced wrong figures in this project once. `sync_db.py` uses `VACUUM INTO`,
which asks SQLite for a consistent single-file snapshot, and verifies it before
it replaces anything.

## 4. Secrets stay where they are.

`.env` is gitignored on both machines and holds different things by design. The
mini's carries the API keys because it ingests; nothing about the console needs
them, and the console is served `--no-run` so the public page cannot spend them.

Do not copy `.env` between machines wholesale. If one value is needed on the
other side, move that one line, on the machine itself.

`mastri.app` is public and password-gated. Publishing without a password is
refused by the tool, because a tunnel bypasses the loopback-only check by design.

## 5. Paths outside the project, and why each is there

The rule in the user's global instructions is to keep work inside the project
directory, and to ask before putting anything outside it — then record where it
went. These are the agreed exceptions:

| path | why it cannot live in the repo |
| --- | --- |
| `~/Library/LaunchAgents/app.mastri.dctracker.*.plist` | launchd only reads agents from here |
| `~/.ssh/config`, `~/.ssh/dc_tracker_deploy` | ssh only reads keys and aliases from here |
| `~/.cloudflared/cert.pem` | written by `cloudflared tunnel login`, per account |
| `~/.cloudflared/<uuid>.json` | written by `cloudflared tunnel create`, per tunnel |
| `~/.local/bin/tracker` | a symlink so the CLI is on `PATH`; that directory was already on it |

Everything else lives under `~/dev/tracker/` — `repo/` (the checkout), `ops/`
(the deployer and its logs), `backups/`.

`ops/` sits beside the checkout, not inside it, so a bad commit cannot replace
the deployer that would deploy the fix.

---

`deploy/README.md` is the runbook: what to do when something breaks, how to read
the logs, how to publish or unpublish. This file is the rules; that one is the
procedures.
