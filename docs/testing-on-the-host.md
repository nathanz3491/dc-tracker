# Running the test suite on the production host

The full suite is slow on the dev machine, and that machine is in use for other work
while it runs. The production host's CPU is several times faster and mostly idle. So
lint runs here and every test runs there, with one command:

```bash
.venv/Scripts/python scripts/remote_suite.py              # from the main checkout
../../../.venv/Scripts/python scripts/remote_suite.py     # from a worktree
```

It ends with a short verdict, and exits non-zero if either half failed:

```
lint     clean
suite    5041 passed, 11 skipped in 23.01s
took     30s in all
log      C:\...\data\runs\remote-suite.log
```

That was the first green run, on 2026-10-29: 30 seconds from the command to the
verdict, of which 23 were pytest. The same suite takes about two minutes here on four
workers at low priority. The very first run also builds the test environment, which
added about five minutes once.

`data/runs/remote-suite.log` (ignored by git) keeps the whole of the host's output,
failures and warnings included, for reading after the terminal has scrolled.

## What it does, in order

1. **Lint, here.** `ruff check --fix` and `ruff format` over the checkout, the same
   as `ruff check .` and `ruff format .` by hand. They rewrite files, which is why
   they run here: run on the host, the fixed files would have to be copied back. What
   ruff cannot fix is printed, and the run carries on. A lint failure and a test
   failure are both worth knowing from one run. `--check` reports without changing a
   file; `--no-lint` skips the step.
2. **Snapshot.** The working tree becomes a commit object: staged or not, untracked
   files included, ignored files (`.env`, `data/`, `.venv/`) left out. In other words,
   exactly what `git add -A && git commit` would record. It is built on a throwaway
   copy of the index, so the real index, HEAD, the branch and the stash are untouched
   and nothing appears in `git log`. A tree identical to HEAD's is tested as HEAD.
3. **Prepare the scratch clone** at `~/dev/tracker/tracker-dev` on the host. The first
   run creates it (`git init`, then a fetch from GitHub, which the host does quickly
   on its own connection). Every run fetches again, so the next step sends only what
   GitHub does not have.
4. **Send.** `git push` of the snapshot to `refs/snapshots/<sha>` in the scratch
   clone. Usually a few kilobytes: the commits not yet on GitHub, plus the snapshot.
5. **Start, detached.** On the host: take the lock, check the snapshot out, remove
   what the previous snapshot left (keeping the environment, the downloads and
   pytest's cache), and start the run in the background. The run then:
   - builds the test environment if `pyproject.toml` or `requirements.lock` changed
     since the last build (the first run takes a minute or two). It uses `uv` with
     Python 3.13, as production does, and a uv cache of its own.
   - writes the sandbox profile and runs **the isolation check** (below);
   - runs `pytest -n 10` inside the sandbox at `nice 10`, then records the exit
     code.
6. **Follow.** Its log streams here as it is written. The link from the dev machine
   to the host drops now and then. When it does, the command reattaches and resumes
   at the byte where it stopped, up to thirty times. The run never notices.

## Options

| | |
| --- | --- |
| `-- <pytest args>` | anything after `--` goes to pytest there: `-- -k cancelled`, `-- tests/test_clean.py`, `-- -x` |
| `-- --lf` | only the tests that failed in the last run there (its pytest cache is kept between runs) |
| `--check` | lint without changing a file |
| `--no-lint` | skip lint |
| `--workers N` | pytest-xdist workers on the host (default 10, its performance cores) |
| `--attach` | follow the run in progress again: after Ctrl-C, or after giving up on a long outage |
| `--stop` | end the run in progress, pytest's workers with it |
| `--host`, `--dir` | the ssh alias (default: `scripts/sync_db.py`'s) and the scratch clone, relative to the host's home |

Ctrl-C here stops *following*; the run goes on. One run at a time: a second waits up
to thirty minutes for the first, and takes over a lock whose run has gone.

## Why it cannot touch production

The tests already guard themselves: `tests/conftest.py` refuses network access and
gives every test a temporary `TRACKER_HOME`. That depends on every test being written
right. The layers below do not. Each holds without the others.

**A directory of its own.** The host half refuses before touching a file unless the
directory is named `tracker-dev`. It also refuses one that is, or leads by a symlink
into, production's `repo/`, `ops/` or `backups/`. The clone is never served, never
polled by the deployer, and never pushes anywhere. Production's code still changes only
through a push to `main` (CLAUDE.md §1).

**An environment of its own.** The tests run under `env -i`: nothing of the login's
environment, so no API keys, no proxy variables, and no `TRACKER_*` pointing anywhere
else. `HOME` and `TMPDIR` are fresh directories made for the run and removed after it,
so `~` never leads to production either. They sit *outside* the clone: inside it,
every test's temporary directory would have a git checkout and a `pyproject.toml`
above it, and the tests that ask "is this a checkout?" would get the clone's answer.
`TRACKER_HOME` is the clone. It has no `.env`, since ignored files never travel. No
`COLUMNS` either, so the CLI's piped output is 200 wide, as in a local run.

**An OS sandbox.** The tests run under macOS's `sandbox-exec` with a profile
written fresh for each run. Everything is allowed except:

| denied | what it covers |
| --- | --- |
| reading or writing `~/dev/tracker/repo`, `ops`, `backups` | production's checkout, database and `.env`; the deployer; the backups |
| reading or writing `~/.ssh`, `~/.cloudflared` | the deploy key and the tunnel credentials |
| writing `~/Library/LaunchAgents`, `~/.local`, `~/.cache/uv`, `/opt/homebrew` | the launchd agents, the shared Python interpreter and uv cache, Homebrew |
| any connection past loopback | the internet, and through it any API |
| a connection to **any port already listening** on the host | the console, the proxy, every other service running there; the list is taken from `netstat` at the start of each run |
| running `launchctl` | starting or stopping any agent |

A server a test starts itself listens on a port opened after the list was taken, so
it stays reachable.

**Proved on every run.** Before the first test, the run tries each guard from inside
the sandbox:

- list production's checkout;
- read production's `.env`;
- open production's database;
- list the deployer;
- write into the backups;
- connect to every listening port;
- reach the internet.

It also checks that `tracker`'s home and database resolve inside the clone, and that
the clone holds no `.env`. Every attempt must be refused or come out right. One that
is not stops the run before any test starts:

```
isolation check:
  ok   production's checkout cannot be listed
  ok   production's .env cannot be read
  ...
  FAIL none of the 41 services listening here can be reached (reached [8765])
remote suite: an isolation check failed, so no test was run
```

A failure there is a hole in the sandbox. Fix its cause. Never route around it.

**Low priority.** `nice 10` and ten workers leave the efficiency cores to the console
and the nightly loop. The suite may run while the loop runs, and says so at the
start.

## When it goes wrong

| message | meaning |
| --- | --- |
| `could not prepare the scratch clone` | ssh to the host failed, or creating the clone did. The output above it says which. |
| `could not send the snapshot` | the `git push` failed. Usually the link, so run it again. |
| `refusing …` | the host half refused the directory. `--dir` must name a `tracker-dev` outside production. |
| `another run is in progress here` | a second run is waiting for the first. `--attach` shows the first; `--stop` ends it. |
| `could not build the test environment` | `uv` failed to install `requirements.lock` on macOS. The lock is frozen on Windows, so a package without a macOS build would fail here: fix the lock. |
| `an isolation check failed` | see above. No test ran. |
| `lost the link to the host` | following gave up after thirty reattaches. The run goes on; `--attach` picks it up. |
| `ended … without recording a result` | the run was killed without its exit trap, e.g. the host rebooted. Run it again. |
| a test passes here and fails there, or the reverse | a real portability bug. Production runs on that platform, so trust the host. |

Deleting `~/dev/tracker/tracker-dev` is always safe. The next run rebuilds it.

## Files

| | |
| --- | --- |
| `scripts/remote_suite.py` | this machine's half: lint, snapshot, push, start, follow |
| `scripts/remote_suite.sh` | the host's half: piped over ssh for every step, never installed there; the detached run executes the snapshot's own copy |
| `tests/test_remote_suite.py` | the snapshot, argument quoting, reattachment, the sandbox profile and the directory refusals |
| `~/dev/tracker/tracker-dev/` | on the host: the clone, `.venv/`, `.pytest_cache/`, and `.suite/`, which holds the lock, the sandbox profile, the run's log and its uv cache |
