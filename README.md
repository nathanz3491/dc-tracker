# dc-tracker

Ingest, normalize and query US data center construction projects, where **every
non-null fact traces back to a source URL with a confidence score**.

SQLite is the source of truth. Ingestion is deterministic and re-runnable. There
is no web UI — this is a backend plus a CLI.

## What it does

Three ingest paths converge on one normalizer and one write path:

| Path | Command | What it is good for |
|---|---|---|
| Hand-curated JSON | `tracker ingest manual --json seed/sample-projects.json` | Projects you know about that no feed carries |
| ISO queue export | `tracker ingest pjm --csv FILE --iso pjm` | Candidate generation and corroborating citations — **not** a project feed, see below |
| News extraction | `tracker ingest crawl --urls urls.txt` | The 12 tracked fields, pulled from articles by an LLM and gated on quoted evidence |

Articles to extract from come from `tracker discover`, which polls news feeds and
queues candidates for triage.

Then query: `tracker list`, `tracker show ID`, `tracker stats`, `tracker review`,
`tracker verify`, `tracker export {md,csv,json}`.

The 12 tracked fields are the ones the PRD requires: project name, city + state,
operator, end customer, planned investment, planned and built MW, first announced,
current phase, latest progress, biggest blocker, expected online date — plus the
citations that support each of them.

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

Two optional extras, neither required:

- `.[iso]` adds `openpyxl`, for the ISO queue exports that ship as XLSX rather
  than CSV. The loader reads CSV and JSON without it.
- `.[crawl]` adds Crawl4AI plus `crawl4ai-setup` (a Chromium download), used only
  as an escalation for pages plain HTTP cannot read. The crawl path defaults to
  `httpx` and works without it.

## Run

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
|  1 | Microsoft | Fairwater        | Mount Pleasant, WI | construction |  - |          - |  2   |   1 |
|  2 | xAI       | Colossus         | Memphis, TN        | operational  |  - |          - |  2   |   1 |
|  3 | Crusoe    | Stargate Abilene | Abilene, TX        | construction |  - |          - |  2   |   1 |
+----+-----------+------------------+--------------------+--------------+----+------------+------+-----+
```

The MW and investment columns are empty because `seed/sample-projects.json` ships
with every figure as the literal string `PLACEHOLDER`. That is deliberate — see
[The seed file](#the-seed-file).

An ISO queue export:

```bash
tracker ingest pjm --csv data/raw/pjm_2025q3.csv --iso pjm --dry-run
tracker ingest pjm --csv data/raw/pjm_2025q3.csv --iso pjm
```

### Setting the API key

Only `ingest crawl` needs it. Copy the example file and fill in one line:

```bash
cp .env.example .env
```

```
TRACKER_MINIMAX_API_KEY=your-key
```

`.env` is gitignored, and it is read by **absolute path** — so it applies no
matter which directory you run `tracker` from. Note the `TRACKER_` prefix: every
setting this tool reads carries it. `TRACKER_MINIMAX_API_KEY` and
`MINIMAX_API_KEY` are not the same variable, and only the first is read.

If your key came from the China platform (`platform.minimaxi.com`, phone signup)
rather than the global one (`platform.minimax.io`, email signup), also set:

```
TRACKER_MINIMAX_BASE_URL=https://api.minimaxi.com/v1
```

The keys are not interchangeable, and the wrong host answers *invalid api key* —
which reads like a bad key rather than a bad URL. `tracker ingest crawl --check`
tells you which you have in one cheap call.

### One command for the whole loop

```bash
tracker sync
```

Four phases: **discover** new candidate articles from the feeds, **extract** them
into the database, **refresh** existing projects by re-reading their sources, then
**list** the result. Needs the API key set as above.

Both crawl phases are capped, because each article costs an LLM call:

```bash
tracker sync --limit 25 --refresh-limit 25 --refresh-days 14
tracker sync --dry-run          # see what a run would do, spend nothing
tracker sync --browser          # escalate blocked pages, needs the 'crawl' extra
tracker sync --skip-discover    # work the existing queue only
tracker sync --skip-refresh     # new projects only
```

The refresh phase is what keeps data current rather than merely growing: articles
get edited, and a campus that was "announced" last quarter is under construction
now. Re-reading a known citation updates every field it supports. It deliberately
bypasses the article cache — serving a cached copy would guarantee the answer is
"nothing changed".

### Depth versus breadth

Every article is one LLM call, so which article you spend it on matters. An
article about a project **already** in the database becomes a second source —
filling fields a single article cannot (no press release names its own blocker)
and lifting confidence from 2 to 3. An article about a new project just adds
another single-source row.

`tracker sync` therefore crawls depth-first by default: queued candidates whose
headline or slug names a tracked project go before the rest. `--breadth-first`
reverses that.

Matching is deliberately strict — the **full** company key plus either the
locality or a genuinely distinctive name token. Looser rules failed badly in
testing: a single company token plus a city matched every Ashburn article by any
operator (154 false hits for one project), and treating "campus" or a repeat of the
company name as distinctive matched every Sabey article to the Sabey *Ashburn*
project, including sites in other states.

### Reaching back for older projects — no API key

A feed only shows what published in the last few days, so a project announced in
2023 never appears in one. Sitemaps fix that for free: they list a site's whole
archive, they carry `lastmod` dates, and they exist precisely so machines can read
them.

```bash
tracker sync --deep
```

One measured run: **799 matching URLs across an archive going back to 2015, of
which 477 were new.** They queue up as a backlog, and each subsequent `tracker
sync` crawls `--limit` of them. Add more archives as `[[sitemap]]` entries in
[seed/feeds.toml](seed/feeds.toml).

This is the recommended way to fill the database. Search (below) is optional.

### Search: an optional alternative that needs keys

Feeds only surface what was published recently, so a project announced two years
ago never appears in them. Search reaches back for it.

```bash
tracker search "Meta Richland Parish Louisiana data center megawatts"
tracker search --from-llm 20            # let MiniMax propose the queries
tracker search --from-llm 20 --print-only   # just show them, search nothing
tracker sync --search 10                # fold searching into the one-command loop
```

Needs two free Google values in `.env` (`TRACKER_GOOGLE_API_KEY` and
`TRACKER_GOOGLE_CSE_ID`); `tracker search` prints exactly where to get them if
they are missing, and `--print-only` works without them so you can run the queries
by hand.

**You probably do not need this.** `--deep` above reaches the same historical
projects with no key and no quota, and it was added after search precisely because
setting up two Google credentials to find articles that a sitemap lists for free is
poor value. Search earns its place only when you want a *specific* project that no
configured site has covered.

**`--from-llm` lets the model guess, and that is safe for one reason: nothing it
says is stored.** Asked for candidate projects it returns names from its training
data — some real, some not. Each becomes a *search query* and nothing more. A
project only becomes a row once a real search returned a real URL, the article
fetched, and the evidence gate found a verbatim quote for every value. If the model
invents a project, the search finds nothing and the run moves on. Discovery is
allowed to be speculative precisely because storage is not.

The official Custom Search JSON API is used rather than scraping result pages.
Scraping would break Google's terms, and it would contradict this project's
decision not to defeat other sites' access controls either — see below.

### Why DataCenterDynamics is discovery-only

Its RSS feed is served freely and its headlines are valuable, so it stays enabled.
But the article pages sit behind Cloudflare bot management and return 403 to any
non-browser client — verified identical for our User-Agent, no User-Agent, and
curl, so it is not a UA filter. Their `robots.txt` also sets
`Content-Signal: search=yes,ai-train=no,use=reference`.

We do not train on the content and we store only short attributed excerpts, but
the Cloudflare block is a deliberate access control and this project does not try
to defeat it. So DCD tells you *which* projects exist and the facts come from the
operator's own release or another outlet. Data Center Knowledge covers the same
beat and does permit fetching, which is why it was added.

Every blocked URL stays visible rather than disappearing:

```bash
tracker queue --failed        # what could not be read, grouped by host
tracker sync --retry-failed   # re-attempt them
```

### Or run the phases separately

```bash
tracker discover --since-days 45
tracker queue
tracker ingest crawl --check
tracker ingest crawl --from-queue --limit 10
```

`discover` polls the feeds in [seed/feeds.toml](seed/feeds.toml), keyword-filters
the headlines and queues what matches. Nothing is fetched or sent to an LLM at
that point — `queue` shows you the headlines so you can drop the noise before
paying for extraction:

```bash
tracker queue --drop --url https://example.com/not-a-project/
```

A representative run over seven feeds saw 150 entries, filtered 132, and queued
18. Of those 18, four were genuine US project announcements. **Precision is
deliberately not the goal** — the queue is a human checkpoint, and it is cheaper
to over-collect and triage than to tune a keyword filter until it silently drops
real projects. Two categories are knowingly left in: industry commentary that
mentions capacity figures, and non-US projects. The latter cost one LLM call and
are then dropped correctly, because `norm_state` will not accept "Islamabad" —
better than a geography keyword rule that also discards real US sites.

You can still supply URLs by hand with `--urls urls.txt` (one per line, `#`
comments allowed), which is the right thing when you already know what to read.

Reviewing and exporting:

```bash
tracker review                    # projects at confidence <= 1, with reasons
tracker review --verify 3         # record that you checked project 3
tracker show 3                    # full detail with every citation
tracker verify                    # progress toward the required project list
tracker export md > tracker.md    # Markdown table, byte-stable across runs
```

Every command takes `--db PATH`. Without it the database is `data/tracker.db`
under the project root, or `$TRACKER_DB` if set.

### Running `tracker` from anywhere

Launcher scripts in `C:\Users\64887\bin` (already on the user PATH, so no PATH
change was needed) make `tracker` available from any directory:

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

579 tests, about 15 seconds. **A fresh clone with no API key and no network access
must produce a green run.** Tests that would hit the network or spend MiniMax
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

## Why

This section records the decisions that are not obvious from the code, and the
places this implementation deliberately diverges from the PRD.

### ISO interconnection queues do not identify data centers

The PRD's first goal is to ingest an "ISO interconnection queue (CSV)" and
"filter to Data Center applicants". **That filter does not exist.** The public
PJM, MISO, ERCOT and CAISO queues are *generator* interconnection queues: they
describe proposed power plants, every one of them has a `Fuel` or `fuelType`
column, and none has a data-center or load-type column. Real large-load queues do
exist — ERCOT's is enormous and PJM built a Large Load Registry — but they are
published aggregated and anonymized, not per project.

So `tracker/ingest/pjm.py` is honest about being a **candidate generator**:

- Matching is a keyword heuristic over project and entity names, so
  `confidence` from this path **caps at 1** and every row lands in `tracker review`.
- Queue MW is generator nameplate, **not** data-center load. It is written to
  `notes` as `gen_queue_mw=`, and `project.mw_planned` is left NULL unless the
  operator passes `--trust-gen-mw` (which still discloses what it did). Writing a
  power plant's rating into a data center's capacity field would fabricate the
  single headline number this tracker exists to report.
- Location granularity is **county**, never city, so it populates
  `project.county`.
- `company` from a queue is a commercial name, often a single-purpose entity
  ("Nova Solar I LLC") or a site label ("MS Mt Pleasant"). Because we already scan
  those columns for operator keywords, the matched operator is promoted to
  `company` and the substitution is disclosed in `notes`.
- Two of the four ISOs do not publish CSV at all: PJM exports XLS and MISO serves
  a JSON API, so the loader reads CSV, XLSX and JSON.

The seam for the day a genuine large-load export appears is
`IsoMap.load_type_col` plus `--filter "column:Load Type=(?i)data"`, which raises
the confidence cap to 2 because the source then actually says "data center"
rather than us guessing.

### MiniMax has no structured output, so the JSON contract is enforced in code

`response_format` — both `json_object` and `json_schema` — is **silently ignored**
on the MiniMax M2.x and M3 models. No error, no warning; you simply get
prose-wrapped JSON as though you had never asked. Anything that claims to enforce
a schema against these models (including Crawl4AI's `LLMExtractionStrategy`
`schema=` parameter) is promising something the provider does not do.

So `tracker/llm.py` owns the contract: strip `<think>` blocks and code fences,
brace-scan for the outermost object, repair the malformations these models
actually emit (trailing commas, smart quotes, a dropped `{` on the first object
inside an array), validate, and allow **exactly one** corrective retry. Cost per
URL is bounded at two calls.

Three MiniMax details that each cost a debugging session if missed:
`max_completion_tokens` not `max_tokens`; `role: "system"` not `"developer"`
(which returns *invalid role* 2013); and the global (`api.minimax.io`) and China
(`api.minimaxi.com`) platforms are separate with **non-interchangeable keys** —
the wrong host answers *invalid api key*, which reads like a bad key rather than
a bad URL. `tracker ingest crawl --check` tells you which you have in one call.

### Discovery reuses `ingest_url` rather than adding a queue table

A candidate lands in `ingest_url` with status `discovered`. That table already
existed to record per-URL crawl outcomes, and "a URL nothing has read yet" is just
one more state in the same lifecycle — so the queue is a status value plus three
metadata columns (migration `0003`), not a subsystem.

The `title` column earns its place: without it, `tracker queue` could only show a
bare URL, which is not enough to judge whether an article is worth an LLM call.

Discovery never touches a URL already in the table, whether it was crawled
successfully or failed. Re-queueing a processed URL would let discovery quietly
undo the crawl path's bookkeeping.

### Feed filtering is two tiers, and one of them can be implied

A `topic` term proves an article is about data centers; a `signal` term proves it
concerns a specific *project*. Both must match. Commentary about AI power demand
passes the first and fails the second, which is right — there is nothing in it to
extract.

Two wrinkles that were only obvious once it ran against real feeds:

- **URL slugs are hyphenated**, so `data-center` has to match the term
  `data cent`, and `900MW` has to match `mw`. `normalize_haystack` folds
  separators and splits digit-letter boundaries. Without it, matching against the
  URL — the only way to filter a sitemap entry or a feed with empty titles — never
  matched anything.
- **The host is not evidence about an article.** `datacenterfrontier.com` contains
  "datacent", so every article on a specialist outlet satisfied the topic tier
  from its domain name alone. Matching now uses the URL *path*, and an outlet that
  genuinely only covers data centers declares `topic_implied = true` — which also
  fixes the opposite error, where a real headline like "Crusoe expands Abilene
  campus to 1.2GW" was dropped for never saying "data center".

Data Center Frontier publishes no working feed at all — `/rss/`, `/feed/` and
`/rss.xml` all 404, and `/?feed=rss2` returns an HTML error page with HTTP 200 —
so it is configured against its article sitemap instead, which is ordered
newest-first with real `lastmod` dates.

### Hedged dates resolve to a quarter, not to NULL

`expected_online` is the field announcements hedge most, and treating every hedge
as unparseable was throwing most of it away. So:

- `late 2027` → 2027-10-01 at **quarter** precision
- `H1 2027` → 2027-01-01 at **half** precision (a half is a coarser bucket than a
  quarter — H1 spans January to June, not January to March)
- `by 2028` → 2028-01-01 at **year** precision, the same as a bare `2028`
- `next spring`, `soon` → still NULL: with no year to anchor to, any date would be
  invented outright
- `before 2028` → still NULL, because it states a *direction* relative to the
  year, so storing the year itself would point the wrong way

Every coarsened value carries a note recording the original phrasing, so `show`
and the Markdown export both make it visible that the date is approximate.

### The evidence gate, not the prompt, is what prevents fabrication

`prompts/extract-v1.txt` asks for a verbatim quote behind every non-null value.
`crawl.evidence_gate` then **discards any value whose quote is missing, or whose
quote is not actually a substring of the fetched article**.

The second check is the one that matters. Requiring *a* quote stops the model
omitting citations. Requiring the quote to really appear in the text stops it
paraphrasing the article into a citation that reads correctly but was never
written — which is precisely how a fabricated number acquires a source. A prompt
instruction is a request; the gate is a mechanism, and a guess is thrown away
regardless of what the model claims about it.

### Crawl4AI fetches, it does not extract

The PRD names Crawl4AI as the extraction framework. Here it is an **optional
extra used only for fetching**, and `httpx` is the default:

- Prompt versioning would otherwise be a fiction. `LLMExtractionStrategy` wraps
  your instruction inside its own template, so the bytes sent to the model are
  not the bytes in `prompts/extract-v1.txt`, and the `source.extractor` stamp
  would be unfalsifiable.
- MiniMax has no schema enforcement (above), so the parse/repair/retry loop has
  to live somewhere testable.
- Crawl4AI hard-pins a third-party fork of litellm. Keeping it optional keeps
  that out of everyone's dependency graph.
- The articles this is aimed at are ordinary server-rendered pages. A headless
  Chromium is real cost for no benefit on those.

It stays for the case where it earns its weight: `should_escalate()` sends a
403/429/503, or an ok-but-suspiciously-thin body, to a real browser.

### Project fields are recomputed from claims, not merged incrementally

Each `source` row records what *it* asserts in `source.claims`. After the sources
are written, every project field is derived afresh from all of them by a declared
policy in `upsert.FIELD_POLICY`. This buys three properties the PRD asks for and
that incremental merging does not give you:

- **Idempotence.** Re-ingesting the same input recomputes the same values, so
  `updated_at` genuinely does not move. `test_reingest_is_idempotent` is the
  load-bearing test of the whole design.
- **Order independence.** PJM-then-news equals news-then-PJM.
- **Open question Q2 for free.** Two conflicting `mw_planned` values both survive
  in their own `source` rows, the project field takes the higher-weighted one,
  and the spread is disclosed in `notes` when it exceeds 20%. Nothing is
  destroyed to make the merge work.

### City versus county is a database invariant, not a convention

The PRD flags "same project, two IDs" as a High risk and cannot solve it with
string matching, because ISO queues report **County** and news reports a
**municipality**. `dedup_key` therefore encodes the *granularity*:
`microsoft|city:mount pleasant|WI` and `microsoft|county:racine|WI` are different
keys, so the UNIQUE index makes "never auto-merge across a county/city boundary"
structural rather than something two ingest paths have to remember.

Ambiguity is surfaced instead of resolved: a candidate match writes
`possible duplicate of project #N` to `notes`, caps confidence at 1, and routes
the row to `tracker review`. `dedup.all_keys` is what connects "Mount Pleasant, WI
(Racine County)" to a queue row that only ever says "Racine" — comparing locality
names alone cannot, because "mount pleasant" and "racine" share nothing.
`--force-new` is the escape hatch for two genuinely separate campuses.

Accepted residual risk: two distinct campuses for one company in one city merge.
That has not bitten yet; the fix if it does is a `campus` column.

### Confidence, and why one source never reaches 3

`0` no citation · `1` weakly cited · `2` solidly cited by one source · `3`
corroborated or operator-verified.

**A placeholder URL is not a citation** and is dropped before any weighting, so
a row seeded with `--allow-placeholders` scores 0 and lands in `tracker review`.
Without that rule a `company_filing` weight on a URL that does not exist handed
a real project confidence 3 on the strength of nothing.

A single company press release is good evidence but it is one party's account of
its own project, so a lone source caps at 2 however authoritative. Independence
is counted by **registrable domain**, not by row: five articles on one outlet are
one source, because aggregators recycle each other's reporting and counting rows
would inflate confidence exactly where it should not be. Any citation at all
floors the score at 1, per the PRD's definition of done.

`updated_at` means "a field changed". `last_verified_at` means "an operator says
this row is right" (PRD open question Q4), and it is the only path from a single
source to 3.

### Four schema additions beyond the PRD's three tables

Each unblocks a stated PRD requirement that the three tables cannot hold:

- **`source.claims`** (JSON) — without it, Q2's "keep both conflicting values" has
  nowhere to keep them and the confidence agreement rule has nothing to compare.
  `source.fields` is *derived* from it, so the two can never disagree.
- **`source.extractor`** — which extractor and which prompt version produced this
  row (`crawl:extract-v1@3f2a91c4:MiniMax-M2.5:httpx`). Without it, "which prompt
  version produced this bad row?" is unanswerable and prompt iteration is
  unmeasurable.
- **`ingest_url` table** — the PRD asks to "mark the source `fetch_error` and
  skip", which is not implementable on `source`: `source_type` is a closed enum
  without such a member, and a source row requires a `project_id` — on a fetch
  failure there is no project. The table also buys idempotent re-runs.
- **`project.county`** — ISO queues report county, not city.

### The database is not committed

The PRD asks for both "SQLite under version control, reproducible from a fresh
clone" and gitignoring `data/tracker.db`. Resolved in favour of treating the
database as a **build artifact**: it is binary, it rewrites on every command, it
spawns `-wal`/`-shm` siblings, and it produces unresolvable merge conflicts.

The *inputs* are version-controlled instead — `seed/*.json` and `data/raw/*.csv`,
which is the literal evidence — and reproducibility is a documented replay:

```bash
tracker init
tracker ingest manual --json seed/sample-projects.json
tracker ingest pjm --csv data/raw/pjm_2025q3.csv --iso pjm
```

### Other decisions worth recording

- **SQL is authoritative at runtime.** `migrations/*.sql` is what `init` applies;
  `models.py` mirrors it for typed queries. `test_models_match_migrations`
  compares column types, defaults, indexes, foreign keys and CHECK constraint
  names, and fails the build on any drift. That test is what makes defining the
  schema twice safe rather than merely duplicated — fix the models, not the test,
  unless the SQL is what changed.
- **`PRAGMA foreign_keys=ON` on every connection.** SQLite silently ignores every
  foreign key by default, and this whole design rests on `source.project_id` and
  `event.source_id` being enforced.
- **Read commands open the database read-only** (`mode=ro`), so the PRD's "never
  modify the DB except for ingest and review" is enforced by SQLite rather than by
  convention. A bug in `export.py` raises instead of corrupting data.
- **Naive UTC everywhere**, matching SQLite's `CURRENT_TIMESTAMP`, from one
  `utcnow()` helper. Mixing aware and naive values yields silently unorderable
  columns.
- **`event` has `UNIQUE(project_id, event_type, event_date)`.** Without it,
  re-running an ingest duplicates every event. Accepted cost: two `expanded`
  events on one date for one project collapse.
- **No `unknown` phase.** `phase` is NOT NULL and the PRD has no such member, so
  it defaults to `announced` — but an ingest path that defaulted it **omits
  `phase` from `source.fields`**, which makes the confidence coverage penalty fire
  and routes the row to review rather than presenting a guess as a cited fact.
- **`source.excerpt` is capped at 500 characters** in `normalize`, enforced by a
  CHECK constraint. Unbounded scraped text is both a copyright and a
  database-size problem.
- **Exports are byte-stable.** Fixed `ORDER BY` on content rather than id, a
  frozen CSV header tuple, `lineterminator="\n"` (Python's csv writes `\r\n` by
  default, which becomes `\r\r\n` on Windows), `sort_keys=True` for JSON, and no
  timestamp in the payload unless you pass `--stamp`.
- **CLI tables use ASCII borders** and an explicit width. This machine's console
  codepage is cp936, where Rich's box-drawing characters render as mojibake, and
  Rich truncates cell text to 80 columns when output is piped — which silently
  loses data for a tool whose whole output story is redirection.
- **`tracker/prompts` is a package, not the PRD's flat `prompts.py`.** A module and
  a directory cannot share a name inside one package, and the PRD asks for both
  `tracker/prompts.py` and `tracker/prompts/*.txt`. `tracker.prompts.load_prompt`
  imports identically either way.
- **Prompt version identity is filename + SHA-1 of the file bytes.** A filename
  alone starts lying the moment you edit the file, which is exactly what iterating
  on a prompt means.
- **Prompts template with `string.Template` (`$var`), not `str.format`.** The
  prompt contains a JSON schema block full of literal braces, which `str.format`
  would raise on.
- **`upsert.py` and `dedup.py` are outside the PRD's file layout.** The PRD names
  no home for dedup matching, merge policy or Q2, and putting them in
  `normalize.py` would break that module's side-effect-free contract.

### The seed file

`seed/sample-projects.json` names three real, widely-reported projects —
Microsoft Fairwater (Mount Pleasant, WI), xAI Colossus (Memphis, TN) and Stargate
Abilene (Abilene, TX, where the operator is Crusoe and the customer is OpenAI).
The three were chosen because each exercises something: the first is the PRD's own
duplicate example, the second is its own "no feed carries this" example, and the
third is the only one where `company != customer`.

**Every MW figure, dollar figure and date in it is the literal string
`PLACEHOLDER`, and the URLs are shape-illustrative rather than verified.** Those
are not facts and must not be treated as any. `tracker ingest manual` refuses the
file until they are replaced; `--allow-placeholders` ingests it for a smoke test,
storing each placeholder as NULL. A seed file that quietly became fabricated data
is the exact failure this system exists to prevent, so it ships unfilled.

`seed/required-projects.txt` ships empty for the same reason. The PRD's definition
of done names 30 specific projects, but that list is not in the PRD text and is
not on disk. `tracker verify` reports progress against a target count today, and
gives a present/missing breakdown the moment the real list is pasted in.

## Known gaps

- The 30 required projects are not populated. `tracker verify` measures the gap,
  and `tracker discover` now supplies candidates to close it.
- **Discovery finds articles, not projects.** It surfaces what the feeds publish
  *now*, so a project announced three years ago will not appear unless an outlet
  writes about it again. Backfilling older projects still needs hand-supplied URLs
  or the ISO queue path.
- ERCOT and CAISO column names in `iso_maps.py` are unverified assumptions;
  PJM's and MISO's are taken from their real exports. A wrong guess fails loudly
  via `assert_headers` rather than ingesting nothing, and `--map-override`
  corrects a rename without a code change.
- The crawl path has been exercised against fixtures, not yet against a live
  MiniMax key.
- `project.country` is a dead column in v1 (`CHECK country='US'`), kept for
  forward compatibility. No CLI filter exposes it.
