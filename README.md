# dc-tracker

Ingest, normalize and query US data center construction projects, where **every
non-null fact traces back to a source URL with a confidence score**.

SQLite is the source of truth. Ingestion is deterministic and re-runnable. Three
interfaces over one dataset: the CLI is primary, `tracker tui` is a full-screen
terminal interface with every command in it, and `tracker serve` exposes the same
dataset as a live console for anyone who would rather read than type — sign-in
optional on loopback, one watchlist per person once it is not.

## What it does

Three ingest paths converge on one normalizer and one write path:

| Path | Command | What it is good for |
|---|---|---|
| Hand-curated JSON | `tracker ingest manual --json seed/sample-projects.json` | Projects you know about that no feed carries |
| ISO queue export | `tracker ingest pjm --csv FILE --iso pjm` | Candidate generation and corroborating citations — **not** a project feed ([why](docs/design-decisions.md)) |
| News extraction | `tracker ingest crawl --urls urls.txt` | The 12 tracked fields, pulled from articles by an LLM and gated on quoted evidence |
| SEC filings | `tracker ingest edgar` | Investment, in-service dates and named tenants, from the one publisher that cannot refuse us |

Articles to extract from come from `tracker discover`, which polls news feeds and
queues candidates for triage — and from `tracker prospect`, which starts from the
opposite end: `seed/operators.toml` names the operators this database is supposed
to know about, `tracker coverage` says which of them have no rows, and `prospect`
goes and looks for those specifically.

Then query: `tracker list`, `tracker show ID`, `tracker stats`, `tracker capex`,
`tracker coverage`, `tracker duplicates`, `tracker blocks`, `tracker review`,
`tracker verify`,
`tracker export {md,csv,json}`. `tracker merge` folds rows that turned out to be
one campus; it is the only command here that deletes anything. `tracker point
"<name>"` goes and gets one named data center on demand — matching it to an
existing row (then `enrich`ing it) or building a fresh profile — instead of
waiting for the batch; add `--url` (repeatable) to read a link you already have
rather than searching for one. `tracker logic check` finds values that contradict
each other or themselves; `tracker logic resolve` walks through fixing them.

`h200_equivalent` restates a site's capacity as accelerators, because megawatts
is what gets reported and compute is what people are actually asking about. It is
derived from MW at **1.3 kW per H200** (~770 per MW) and tiered `derived` — the
ratio comes from the H200's 700 W board, the DGX H200's 8.5 kW for eight GPUs
(1.06 kW per GPU of node-level IT load), and a 1.2 PUE for a liquid-cooled hall.
An article that states a chip count outright beats the conversion and carries its
own quote. A site nobody has sized stays empty rather than zero. `TRACKER_KW_PER_H200`
re-bases it; `tracker init` recomputes. It is deliberately *not* a thirteenth
tracked field, because "9 of 12" is quoted throughout.

The 12 tracked fields are the ones the PRD requires: project name, city + state,
operator, end customer, planned investment, planned and built MW, first announced,
current phase, latest progress, biggest blocker, expected online date — plus the
citations that support each of them.

---

## Quick start

```bash
tracker init
tracker ingest manual --json seed/sample-projects.json --allow-placeholders
tracker list
```

```text
                              3 project(s)
+----+-----------+------------------+--------------------+--------------+----+------------+------+-----+
| id | company   | name             | location           | phase        | MW | investment | conf | src |
+----+-----------+------------------+--------------------+--------------+----+------------+------+-----+
|  1 | Microsoft | Fairwater        | Mount Pleasant, WI | construction |  - |          - |  0   |   1 |
|  2 | xAI       | Colossus         | Memphis, TN        | operational  |  - |          - |  0   |   1 |
|  3 | Crusoe    | Stargate Abilene | Abilene, TX        | construction |  - |          - |  0   |   1 |
+----+-----------+------------------+--------------------+--------------+----+------------+------+-----+
```

The MW and investment columns are empty because `seed/sample-projects.json` ships
with every figure as the literal string `PLACEHOLDER`. That is deliberate — see
[The seed file](docs/design-decisions.md#the-seed-file). The confidence column is
`0` for the same reason: a placeholder URL is not a citation, so it earns nothing,
which is what stops a real project inheriting trust from a fake one.

Then the real loop, which needs one API key — or a local Ollama model instead,
`--llm-provider ollama` on any command that spends LLM calls — see
[Ingesting](docs/ingesting.md):

```bash
tracker tui           # all of the below, on screen, in six panes
tracker discover      # poll news feeds, queue candidates
tracker ingest crawl  # extract the tracked fields, gated on quoted evidence
tracker gaps          # see what is thin
tracker coverage      # which operators we hold no rows for at all
tracker users add you@example.com   # who may read the console
tracker watch add xAI --user you@example.com   # what you want to be told about
tracker digest --user you@example.com          # what changed on it, good and bad
tracker serve         # the same dataset as a live console
```

`tracker sync` is the one command for all of it: discover → prospect → extract →
refresh → enrich → settle → list. A bare run does the cheap five and `--full` does
every phase, because the two that hunt for what is absent — `--prospect` for
operators we have no rows for, `--enrich` for rows that are thin — are the two that
can spend without a ceiling in sight. See [Ingesting](docs/ingesting.md).

## The three ideas

Everything on screen is shaped by these, and they are also in the console's own
Help tab.

**A model's answer is not a fact.** Every stored value carries a tier saying what
it rests on: `reported` (a verbatim sentence in a fetched article, checked against
that article), `derived` (a Census lookup — deterministic, but nobody said it),
`unconfirmed` (待确认 — extracted and unquotable, kept but never counted),
`inferred` (a model's judgement), `defaulted` (nobody said anything and the column
is NOT NULL). Absence is a fifth answer, and it is often the correct one.

**Progress is five tracks, not one ladder.** Site control, permits, power,
construction, commercial. A campus can own its land outright and be four years
into an interconnection queue. Power is never inferred from the others, because
building ahead of grid connection is normal and a finished shell waiting on a
substation is the most valuable signal here.

**Confidence is recomputed, never stored,** and one source can never reach 3
however authoritative — independence is counted by domain.

**"New" means new to us.** A crawl reads one article and imports a project's whole
back-history, so stored milestones run from 1997 to 2040 while the rows themselves
arrived last night. `tracker digest` and the console's landing page filter on when
we learned a fact and print both dates, because either one alone reads as a
different claim than the evidence supports.

**Coverage is a question the sources cannot answer.** Discovery finds what was
published, so an operator nobody wrote about last month is indistinguishable from
one that does not exist. Measured here: 300 projects, 102 company spellings, and no
Nebius row at all. `seed/operators.toml` is the checked-in opinion about who ought
to be here, and it is the only thing in the system capable of noticing an absence.

## Documentation

This file is the tour. The detail lives in [`docs/`](docs/README.md):

| | What |
| --- | --- |
| [Ingesting](docs/ingesting.md) | The API key, the one-command loop, the operators we are missing, depth versus breadth, operator press releases, optional search, SEC filings |
| [Sources and feeds](docs/sources-and-feeds.md) | Which publishers are worth crawling, what discovery costs, and the command that acts on the measurement |
| [Data quality](docs/data-quality.md) | Numbers that cannot be true, contradictions, and what each stored value actually rests on |
| [Backfill and gaps](docs/backfill-and-gaps.md) | Finding thin data and filling it — capacity blocks, county and coordinates |
| [Analysis](docs/analysis.md) | Who is buying the capacity, what could stop these projects, slippage |
| [The terminal interface](docs/tui.md) | `tracker tui` — the six panes, why the run pane cannot fall behind the CLI, verifying it over ssh |
| [The console, and exporting](docs/console-and-export.md) | The live console, accounts and invites, driving it without a browser, publishing it |
| [Architecture](docs/architecture.md) | How the CLI, the database and the console fit together |
| [Design decisions](docs/design-decisions.md) | Why it works the way it does, and where it diverges from the PRD |
| [Government sources](docs/government-sources.md) | Four routes to bulk permit data, all measured, all rejected — read before going looking |

## Install

Requires Python 3.11+. Developed and tested on 3.13.2 on Windows 11.

PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Git Bash:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
```

`requirements.lock` pins the full transitive set for a reproducible install
(there is no uv or poetry on this machine); `pyproject.toml` holds the supported
ranges. Regenerate the lock after any dependency change:

```bash
.venv/Scripts/python -m pip freeze --exclude-editable > requirements.lock
```

`textual` comes with the base install because `tracker tui` is one of the three
interfaces, not an add-on. It is still imported lazily, so an environment without
it keeps every other command working — see [the terminal interface](docs/tui.md).

Four optional extras, none required:

- `.[iso]` adds `openpyxl`, for the ISO queue exports that ship as XLSX rather
  than CSV. The loader reads CSV and JSON without it.
- `.[impersonate]` adds `curl_cffi`, which presents a browser's TLS fingerprint.
  **This is the one worth installing.** A growing share of hosts answer 403 to
  `httpx` and 200 to `curl` for the same URL and the same User-Agent — the block
  is on the TLS handshake, not on who we say we are — and one live `enrich` run
  lost six of about thirty-one fetches that way. It costs one ordinary request, so
  it is used automatically once installed, with no flag.
- `.[crawl]` adds Crawl4AI plus a Chromium download (`python -m playwright
  install chromium`), the last escalation rung, for pages that assemble themselves
  after load. Reached only with `--browser`. Heavy — Chromium plus ~70 transitive
  packages — which is why it stays opt-in, and why the cheap rung above it exists.
- `.[reader]` adds `readability-lxml`, which is what the console's sources page
  uses to show a cited article. Ten of the fifteen most-cited publishers refuse to
  be framed, so the modal extracts the article from their HTML and renders it
  here, with the sentences the database quotes marked in it. Purely display —
  nothing in the pipeline reads it, and without the extra the modal falls back to
  the stored text.

**Escalation is a ladder, cheapest rung first**, the same ordering `enrich` uses
for its harvesters. Measured on three hosts whose `robots.txt` permits us and
whose WAF refuses `httpx` anyway:

| URL | httpx | `curl_cffi` | Chromium |
|---|---|---|---|
| `entergy.com` news release | 403 | **10,266 chars of prose** | not needed |
| `lailluminator.com` brief | 403 | **2,844** | not needed |
| Meta/Blue Owl IR release | 403 | 200, but 106 chars | **8,638** |

Nothing here defeats an access control, and the distinction is the whole
justification: those sites' `robots.txt` files permit crawling —
`investor.atmeta.com` says `Allow: /` with `Crawl-delay: 10` — so an over-broad
WAF rule is not a policy. Where a site genuinely refuses crawlers, as
DataCenterDynamics does with Cloudflare bot management, it stays discovery-only —
see [Ingesting](docs/ingesting.md).

### Running `tracker` from anywhere

Launcher scripts in a directory already on your PATH make `tracker` available from any directory:

- `tracker.cmd` for cmd.exe and PowerShell
- `tracker` for Git Bash

Both call the project's own virtualenv, so the CLI works without activating it and
without putting that venv's `python`/`pip` on PATH where they would shadow the
system interpreter. Both also default `TRACKER_DB` to the project's database —
otherwise `tracker list` run from elsewhere would resolve `data/tracker.db`
relative to the current directory and create a stray empty database instead of
showing your data. An exported `TRACKER_DB` wins over that default, and `--db`
wins over both.

Assets that ship with the code (`migrations/`, the prompt files, the article
cache) are located relative to the installed package rather than the current
directory, which is what lets `tracker init` work from anywhere.

## Test

```bash
.venv/Scripts/python -m pytest
```

2,585 tests, about twelve minutes. **A fresh clone with no API key and no network access
must produce a green run.** Tests that would hit the network or spend DeepSeek
tokens are marked `network` / `llm` and deselected by default; run them
explicitly with `-m network` or `-m llm`.

The coverage gate the PRD asks for:

```bash
.venv/Scripts/python -m pytest --cov=tracker.normalize --cov=tracker.confidence --cov-fail-under=80
```

Both modules sit above 90%. Lint and format:

```bash
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m ruff format --check .
```

---

---

## Known gaps

- The 30 required projects are not populated. `tracker verify` measures the gap,
  and `tracker discover` now supplies candidates to close it.
- **Discovery finds articles, not projects.** It surfaces what the feeds publish
  *now*, so a project announced three years ago will not appear unless an outlet
  writes about it again. `tracker prospect` and `--deep` reach back for it;
  otherwise backfilling older projects needs hand-supplied URLs or the ISO queue
  path.
- **The roster is hand-written, so it goes stale by itself.** `seed/operators.toml`
  covers the operators known when it was written; a company founded next quarter is
  absent until somebody adds it. `tracker coverage` prints the reverse gap —
  companies in the database that no entry claims — which is the mechanism for
  growing it, but nothing proposes new entries on its own.
- ERCOT and CAISO column names in `iso_maps.py` are unverified assumptions;
  PJM's and MISO's are taken from their real exports. A wrong guess fails loudly
  via `assert_headers` rather than ingesting nothing, and `--map-override`
  corrects a rename without a code change.
- The crawl path has been run live against a provider, not only fixtures: an initial
  live run surfaced four defects (fixed in `de4821c`), and `tracker enrich` was
  verified live on project #93 (OpenAI Stargate, Abilene).
- `project.country` is a dead column in v1 (`CHECK country='US'`), kept for
  forward compatibility. No CLI filter exposes it.
