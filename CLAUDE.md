# Operating rules

Two machines run this project. Which one you are on decides what you may do.

| | this repo on the dev machine | the production host (`ssh $PROD`) |
| --- | --- | --- |
| role | write code | **write data**, serve production |
| runs | tests, read-only commands | everything, including ingest |
| database | a copy, pulled when needed | **the authoritative one** |
| serves | nothing | the public console |

---

## 1. Code goes out through GitHub. Never copy it by hand.

```
edit here ─▶ git push ─▶ github ─▶ host polls (≤2 min) ─▶ restart ─▶ console
```

Push to `main` and the host takes it within two minutes. Nothing else is needed
and nothing else is correct: an `scp` of a source file produces a host whose code
matches no commit, and the next poll silently reverts it (`git reset --hard`).

To skip the wait: `ssh $PROD '~/dev/tracker/ops/poll.sh'`

**A commit that does not import is refused** and the checkout rolled back, so the
console keeps serving the previous code. That is the point of noticing a push
rather than trusting it.

**Confirm what is actually live** — a restart is not proof it picked up your
commit:

```bash
ssh $PROD 'cd ~/dev/tracker/repo && git rev-parse --short HEAD'
tail -5 <(ssh $PROD 'cat ~/dev/tracker/ops/logs/deploy.log')
```

`GET /api/health` returns the same commit, behind the login.

## 2. Data is made on the production host. Ingest here writes nothing that survives.

Run every writing command there:

```bash
ssh $PROD 'tracker ingest crawl'
ssh $PROD 'tracker enrich 42'
ssh $PROD 'tracker merge …'   # and infer, backfill, logic resolve
```

`tracker` is on `PATH` there and works from any directory.

**Reads run anywhere** — `gaps`, `sources`, `overview`, `export`, `capex`,
`logic check`. Those are safe on either machine.

**Why that host and not here.** It is always on, which is what a job measured in
hours wants, and it serves the console from the same file it writes. SQLite takes
one writer and any number of readers, so the console stays up through a crawl.

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
mini's carries the API keys because it ingests.

**The console cannot spawn a command at all** — the runner was deleted, so there
is no `--no-run` to pass and no palette behind the public URL. `--ai` survives and
still means what it meant: the model panels *read* a row and spend a token, which
was always a different risk from mutating the database, and `tracker infer` writes
nothing.

Do not copy `.env` between machines wholesale. If one value is needed on the
other side, move that one line, on the machine itself.

**Console passwords are no longer in `.env` at all.** `TRACKER_CONSOLE_PASSWORD`
was one shared secret for every reader; accounts replaced it and a password now
lives hashed in the database, per person. Make one with `tracker users add`, on
the host, where the database is.

The console is public and account-gated. Publishing with **no accounts** is
refused by the tool, because a tunnel bypasses the loopback-only check by design —
every request arrives from 127.0.0.1, so the bind-address rule never fires and the
sign-in is what replaces it.

## 5. Paths outside the project, and why each is there

The rule in the user's global instructions is to keep work inside the project
directory, and to ask before putting anything outside it — then record where it
went. These are the agreed exceptions:

| path | why it cannot live in the repo |
| --- | --- |
| `~/Library/LaunchAgents/<app-id>.*.plist` | launchd only reads agents from here |
| `~/.ssh/config`, `~/.ssh/<deploy-key>` | ssh only reads keys and aliases from here |
| `~/.cloudflared/cert.pem` | written by `cloudflared tunnel login`, per account |
| `~/.cloudflared/<uuid>.json` | written by `cloudflared tunnel create`, per tunnel |
| `~/.local/bin/tracker` | a symlink so the CLI is on `PATH`; that directory was already on it |

Everything else lives under `~/dev/tracker/` — `repo/` (the checkout), `ops/`
(the deployer and its logs), `backups/`.

`ops/` sits beside the checkout, not inside it, so a bad commit cannot replace
the deployer that would deploy the fix.

---

## 6. Only technical documentation is committed. Everything else stays local.

This repo is public on GitHub. Two kinds of file must never reach it, and both are
covered by patterns in `.gitignore` rather than by remembering:

**Working documents and client deliverables.** Plans, reviews, feedback and the
session log are dated snapshots of a decision *in progress*; they go stale the
moment the code lands, and a reader who finds one cannot tell whether it describes
the system or an argument somebody had about it. The Chinese-language documents are
deliverables for the client, not documentation of the repo.

```
docs/plan-*.md   docs/*-plan.md   docs/review-*.md
docs/*-review-*.md   docs/feedback-*.md   docs/*.zh-CN.md   HANDOFF.md
```

**Anything naming the production host.** No hostname, no domain, no ssh alias, no
launchd label, no deploy-key filename — in code, comments, tests, changelog or
docs. `deploy/` is ignored wholesale, because its plist *filenames* carry the
domain and its runbook is procedure for one machine. Where a doc has to refer to
the host, it says `$PROD`, `<app-id>`, `<deploy-key>`; where code needs the real
value it reads `.env` (`tunnel_name`, `tunnel_hostname`), which is also ignored.
Tests use `console.example`.

What *is* committed: `README.md`, `CHANGELOG.md`, this file, and technical docs
under `docs/` — architecture, and design decisions a person reading the code needs
(`docs/government-sources.md` is one: it records why a whole class of data source
is absent, which is not inferable from the code).

Before pushing, this should print nothing:

```bash
git ls-files -z | grep -zv '^CLAUDE.md$' | xargs -0 grep -lE 'mastri|Mac ?mini|ssh mm' 2>/dev/null
```

(This file is excluded because the line above names the patterns it forbids.)

History is not rewritten by any of this — these files were tracked before, so they
remain in earlier commits. The rule governs what goes out from here.

---

## 7. Change a staged command, change its workflow page in the same commit.

`docs/workflows/` documents the four commands that are pipelines rather than
operations — `enrich`, `sync`, `duplicates` (with `park`, `unpark`, `parked`,
`resolve`) and `logic` (with `check`, `conflicts`, `resolve`). Each has a page and a
full-page diagram of its stages.

| you touched | update |
| --- | --- |
| `enrich`, `tracker/ingest/enrich.py`, `gapfill` | `docs/workflows/enrich.md` + `enrich.svg` |
| `sync`, `discover`, `prospect`, `crawl`, `gatekeeper`, `derive`, `scripts/sync_db.py` | `docs/workflows/sync.md` + `sync.svg` |
| `duplicates*`, `pairs`, `dupresolve`, `triage.pair_*`, `capex.suspected_duplicates`, `merge` | `docs/workflows/duplicates.md` + `duplicates.svg` |
| `logic*`, `tracker/logic.py`, `conflicts`, `triage.triage`, `audit.free_answer` | `docs/workflows/logic.md` + `logic.svg` |

**The source map at the foot of each page is the checklist.** It names the
functions the diagram was drawn from; if your change touched one of them, that page
is in scope. This is narrower than "update the docs" — a rename inside a harvester
does not move a box, but adding a harvester, reordering phases, changing a default
or adding a rail does.

The diagrams are generated, so updating one is an edit to a declarative block plus
a command:

```bash
python scripts/render_workflow_diagrams.py enrich   # or: no argument for all four
```

Commit the regenerated `.svg` with the code. It is checked in deliberately, against
the usual rule about build output: GitHub renders `![](enrich.svg)` and does not run
a build step, so a diagram that is not committed is a diagram nobody reading the
repo can see.

**Why the rule is worth its weight.** These four commands spend money and delete
rows, and every stage in them exists because of something that went wrong once — a
budget consumed by the first five of thirty projects, an archive swept for a row
already finished, a merge that destroyed two rows. A stale diagram of that is not a
neutral omission; it is read as the current design, and the next person plans a
change around a pipeline that no longer exists.

---

`deploy/README.md` — local, not in the repo — is the runbook: what to do when
something breaks, how to read the logs, how to publish or unpublish. This file is
the rules; that one is the procedures.
