# Operating rules

**What this checkout is decides what you may do.** Not which machine you are on —
that question has a wrong answer now. Code lives in four kinds of place, agents
work in one of them, and the production checkout sits on the same machine as
some of the others.

```bash
ls .production            # present => this checkout IS production
python scripts/prod.py --where
```

| | a checkout carrying `.production` | every other checkout |
| --- | --- | --- |
| role | **writes data**, serves the public console | writes code |
| database | **the authoritative one** | its own, empty until you fill it |
| runs | everything, including ingest | tests, reads, and its own data |
| edited by | nobody — the poller resets it hard | you, and the agents |

There is exactly one of the first kind. The rest are worktrees on the machine
that serves the console, clones on a laptop, or a fresh checkout somewhere new;
the rules do not distinguish between them, which is the point. This used to be a
rule about two machines, and it was enforced by distance — the dev machine
*could not* reach production's data. Agents now run on the production machine, so
distance is gone and the rules have to say what they mean.

Throughout, `python` means *this checkout's* interpreter: `.venv/bin/python` on
macOS, `.venv/Scripts/python` on Windows. That is the only difference between
machines these rules cannot hide.

---

## 1. Code goes out through GitHub. Never copy it by hand.

```
commit here ─▶ git push ─▶ github ─▶ production polls (≤2 min) ─▶ restart ─▶ console
```

Push to `main` and production takes it within two minutes. Nothing else is needed
and nothing else is correct. **Two ways to get this wrong, and the second is new:**
an `scp` from another machine, or a `cp` from another directory on the same one —
now that a workspace and production share a disk, copying is suddenly easy. Both
produce a checkout whose code matches no commit, and the next poll silently
reverts it (`git reset --hard`).

To skip the wait: `python scripts/prod.py '~/dev/tracker/ops/poll.sh'`

**A commit that does not import is refused** and the checkout rolled back, so the
console keeps serving the previous code. That is the point of noticing a push
rather than trusting it.

**Confirm what is actually live** — a restart is not proof it picked up your
commit:

```bash
python scripts/prod.py 'git rev-parse --short HEAD'
python scripts/prod.py 'tail -5 ~/dev/tracker/ops/logs/deploy.log'
```

`GET /api/health` returns the same commit, behind the login — but it reports
`null` from a git worktree, which every agent task runs in, until
`deployed_commit()` learns to follow a `.git` file. Trust `git rev-parse` there.

The full procedure, including what to check before pushing and how to read a
failed fetch, is `/deploy` (`.claude/commands/deploy.md`) — committed, so every
checkout has it.

## 2. Data is made in the production checkout. A workspace writes only its own.

Run every writing command through the script that lands in the right place:

```bash
python scripts/prod.py tracker ingest crawl
python scripts/prod.py tracker enrich 42
python scripts/prod.py tracker merge …   # and infer, backfill, logic resolve
```

That works unchanged from a laptop and from a worktree on the production machine
— it reads `TRACKER_PROD_HOST` and either sends the command over ssh or runs it
locally, then `cd`s into the production checkout. **The `cd` is not cosmetic**:
it is what the guard below requires.

**Reads run from any checkout, anywhere** — `gaps`, `sources`, `overview`,
`export`, `capex`, `logic check`, `audit check`. Never gated.

**This used to be guaranteed by distance, and now it is not.** The old rule read
"ingest *here* writes nothing that survives", and it was true because *here* was
a different machine that could not reach production's database. Agents now work
in worktrees on the machine that owns it. Worse, the rule as written became an
instruction to go and write it. Three things replace the distance:

- **`tracker` on `PATH` on the production machine is a wrapper that refuses.**
  It has to be, because of how `home()` resolves: an editable install answers
  with the checkout its *own source* lives in, before the working directory is
  ever consulted, so a `tracker` found on `PATH` addresses production's database
  from whatever directory you are standing in. The wrapper allows reads from
  anywhere and refuses everything else unless you are inside the checkout
  carrying `.production`. It names the alternative in its refusal.
- **A workspace has its own `.venv` and its own empty `data/`.** Run
  `.venv/bin/tracker` there and `home()` resolves to that checkout, so a write
  lands in a database nobody is serving. `tracker paths` says which one you have.
- **A workspace never receives production's `.env`.** No keys is what makes a
  stray `ingest crawl` in a worktree unable to spend money even if it runs.

**Why the production checkout and not a workspace.** It is always on, which is
what a job measured in hours wants, and it serves the console from the same file
it writes. SQLite takes one writer and any number of readers, so the console
stays up through a crawl.

**Why not two of them.** There is no merge. A whole file replaces a whole file,
so two checkouts ingesting independently means the later copy silently destroys
the other's work. The schema *could* support a real merge — projects have a
content-derived `dedup_key`, sources are keyed by URL, and every derived value is
a pure function of the attached sources, which is what `backfill derive` relies
on — but the tool does not exist, and `project.id` is autoincrement, so both
would hand out the same id to different campuses.

**One writer at a time, and the deployer respects it.** A writing command takes a
lock file beside the database. The poller needs that lock too, for the
`tracker init` it runs on every deploy, so when a long job holds it the poller
logs the deferral and leaves the checkout alone until the next cycle. A deploy
that looks two minutes late during a `sync` is that, working as intended.

## 3. Sync the database with the script, never with `scp`.

```bash
python scripts/sync_db.py            # pull the authoritative one down to here
python scripts/sync_db.py --push     # only to seed a host or restore one
```

Both directions **refuse when the destination holds rows the source does not**,
which is what losing an ingest looks like from the other end. `--force` overrides
it deliberately.

It takes the host from `TRACKER_PROD_HOST` and **refuses when that is empty**,
which is the case on the production machine itself. That refusal is deliberate
rather than a missing default: this script replaces a whole file with another
whole file, and "here" on both ends would mean copying the authoritative
database over itself. A workspace that wants real data to develop against is
asking for a pull, from a machine that is not production.

**Never `cp` or `scp` the database.** It runs in WAL mode: committed data sits in
`tracker.db-wal` until a checkpoint folds it back, so a copy of `tracker.db`
alone opens cleanly, reports no error, and is silently out of date. Measured
here: 16.3 MB of main file against a 7.9 MB WAL. That mistake has already
produced wrong figures in this project once. `sync_db.py` uses `VACUUM INTO`,
which asks SQLite for a consistent single-file snapshot, and verifies it before
it replaces anything.

## 4. Secrets stay where they are.

`.env` is gitignored everywhere and holds different things by design. The
production checkout's carries the API keys because it ingests; **a workspace's
must not**, which is §2's third safeguard.

It also carries the four values that let a committed instruction talk about the
production host without naming it — `TRACKER_PROD_HOST` (empty on production
itself, meaning "here"), `TRACKER_PROD_CHECKOUT`, `TRACKER_SERVE_LABEL` and
`TRACKER_TUNNEL_HOSTNAME`. None is a secret; all four are per-machine, which is
why they live here and not in the repo (§6).

**The console cannot spawn a command at all** — the runner was deleted, so there
is no `--no-run` to pass and no palette behind the public URL. `--ai` survives and
still means what it meant: the model panels *read* a row and spend a token, which
was always a different risk from mutating the database, and `tracker infer` writes
nothing.

Do not copy `.env` between machines or between checkouts wholesale. If one value
is needed on the other side, move that one line, on the machine itself. Copying
production's into a workspace hands an agent the API keys.

**Console passwords are no longer in `.env` at all.** `TRACKER_CONSOLE_PASSWORD`
was one shared secret for every reader; accounts replaced it and a password now
lives hashed in the database, per person. Make one with
`python scripts/prod.py tracker users add`, which lands in the checkout that
holds the database.

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
| `~/dev/tracker/ops/tracker-guard.sh` | §2's wrapper — what that symlink now points at |
| `<production checkout>/.production` | the marker that says which checkout is production |

Everything else lives under `~/dev/tracker/` — `repo/` (the checkout), `ops/`
(the deployer, the guard, and their logs), `backups/`.

**The guard is untracked on purpose, and that is a real cost.** A reader of this
repo cannot see it, and a fresh checkout does not install it — the only record
that it exists is this table. It sits in `ops/` for the same reason the deployer
does, below: a guard that a bad commit could replace is not a guard. Weigh that
before moving it.

**`.production` is untracked because exactly one checkout in the world may carry
it.** A committed copy would tell every clone it was the writer, which is the
opposite of what it is for.

**Where the tool itself thinks its data lives** is `tracker paths`, and for the
production checkout the answer is unchanged: the deployer installs editable inside
`repo/`, so the package sits in the checkout and that checkout is `home()` — the
same directory the old `install_root()` returned, holding the same
`data/tracker.db` and the same `.env`. `TRACKER_HOME` can move it and is not set
there. Run `python scripts/prod.py tracker paths` after deploying anything that
touches path resolution; it is one command and it answers the question directly.

**Ask it in a workspace too, and expect a different answer.** Rule 2 of `home()`
follows the *package source*, so an editable install in a worktree resolves to
that worktree — which is what makes a workspace safe — while a `tracker` borrowed
from another checkout's venv resolves to that other checkout, which is what §2's
wrapper exists to catch. `tracker paths` is how you tell those two apart, and it
is worth running before any command you would not want aimed at production.

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
value it reads `.env` — `tunnel_name`, `tunnel_hostname`, and the
`TRACKER_PROD_*` values §4 lists — which is also ignored. Tests use
`console.example`.

What *is* committed: `README.md`, `CHANGELOG.md`, this file, technical docs under
`docs/` — architecture, and design decisions a person reading the code needs
(`docs/government-sources.md` is one: it records why a whole class of data source
is absent, which is not inferable from the code) — and, since the collapse to one
machine, **`.claude/commands/` and `.claude/skills/`**.

**Why the instructions are committed now.** `.claude/` used to be ignored
wholesale as machine-specific state, and that quietly cost us the thing it was
protecting: agents run in worktrees, a worktree contains exactly what is
committed, so the only real deploy procedure this project had lived on one laptop
and no agent had ever read it. Two machines were following different rules because
only one of them had been given any. The split is by kind now, not by directory —
`commands/` and `skills/` describe the project and travel with it; a launch
config, one person's granted permissions and a session lock stay out.

That is also why the host's identity had to move into `.env` first: a committed
instruction cannot say `ssh <alias>`, and `ssh $PROD` only works if every reader
has exported it. `scripts/prod.py` reads the alias from configuration, which is
how `/deploy` can be both committed and correct.

Before pushing, this must print nothing and exit 0:

```bash
python scripts/check_no_host_names.py
```

It replaced a one-line grep pinned in this file, for two reasons. The patterns
now need to appear in more than one place, and the grep had already failed
silently: it looked for the alias only in `ssh <alias>` form, so the same alias
sat in a tracked file for weeks as a default value — in the one script whose job
is talking to that host — and the check passed every time. The script carries the
pattern list, exempts itself and this file, and prints the reason for each hit.

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

**This file is the rules; `/deploy` is the procedure.**
`.claude/commands/deploy.md` is committed and carries the steps — what to check
before pushing, how to read a failed fetch, how to verify what is actually live.
It replaced a `deploy/README.md` that was local-only and therefore unread by
anyone but its author.

What remains genuinely local and unwritten-down is the host's own furniture: the
launchd plists, the tunnel credentials, and `ops/`. §5 is the record of where
those live.
