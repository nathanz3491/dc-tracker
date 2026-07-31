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

Within that first group, the articles that also carry an obstacle term go first.
That is the sharpest version of the same argument: the parenthesis above — no press
release names its own blocker — means an adversarial second source is the *only*
thing that can record one, so those calls buy something no other article can.

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

### Operator press releases, and why they matter most

The three hardest fields — `investment_usd`, `expected_online`, `first_announced` —
are hardest because a trade-press article rewrites an announcement and drops the
numbers. The press release itself opens with all three: *"X announces a $N billion
campus, online in 2027."*

Measured across the database before adding any: trade press yielded **0.57** of
those three fields per citation, the two company sources in it yielded **1.50**,
and `company_filing` is weight 3 against trade press's 2. The vein was untouched
rather than exhausted — 2 citations out of 124.

Newsrooms are `[[sitemap]]` entries carrying a `company`:

```toml
[[sitemap]]
name = "cologix-newsroom"
url = "https://cologix.com/news-sitemap.xml"
source_type = "company_filing"
company = "Cologix"
topic_implied = true
```

`company` does two things. It tells `classify_source_type` the host is first-party,
without which `www.operator.com/news/…` scores `general_media` — ranking the
announcement below the rewrite. And it lets `matches_known_project` stop requiring
the company in the slug, since the domain already proves it: a release titled "New
Hillsboro campus announced" names the city, not the company. Measured, that took
the yield from 15 articles over 8 projects to 28 over 13. The
locality-or-name-token requirement stays, so a careers page still matches nothing.

Several operators serve their child sitemaps only to browser-like clients — curl
gets 200 and httpx 403 on the same URL, a TLS-fingerprint rule. Their robots.txt
explicitly permits crawling and advertises the sitemap, so this is an over-broad
WAF rule rather than a policy; `--browser` reaches them once the `[crawl]` extra is
installed. They are listed with their status in
[seed/feeds.toml](seed/feeds.toml) rather than failing every run.

### Search: an optional alternative that needs keys

Feeds only surface what was published recently, so a project announced two years
ago never appears in them. Search reaches back for it.

```bash
tracker search "Meta Richland Parish Louisiana data center megawatts"
tracker search --from-llm 20            # let MiniMax propose the queries
tracker search --from-llm 20 --print-only   # just show them, search nothing
tracker sync --search 10                # fold searching into the one-command loop
```

Needs one search key in `.env`. Three backends are supported, and whichever key
you add is picked up automatically:

| backend | variables | free tier | card to sign up | index |
|---|---|---|---|---|
| **Serper** | `TRACKER_SERPER_API_KEY` | 2500 queries | no | Google |
| Google | `TRACKER_GOOGLE_API_KEY` + `TRACKER_GOOGLE_CSE_ID` | 100/day | no | Google |
| Brave | `TRACKER_BRAVE_API_KEY` | 2000/month | yes | Brave's own |

Pin one explicitly with `TRACKER_SEARCH_PROVIDER=serper`. `tracker search` prints
exactly where to get each key if none is set, and `--print-only` works without any
of them so you can run the queries by hand.

**Serper is the least friction and the best default here.** It returns Google's
index, which has the deepest coverage of US data center trade press, and its
signup states "2,500 free queries, no credit card required" — one variable, done.
Google's own Custom Search API is the same index for free but caps at 100/day and
takes two setup steps (a Cloud API key plus a Programmable Search Engine set to
the whole web).

Brave earns its place only when you want results Google does not have: it runs an
independent index rather than reselling one, so it genuinely widens coverage
rather than the quota. That is a real advantage, but its free tier asks for a card
at signup, so reach for it second.

**There is no Bing backend, because the Bing Search API no longer exists.**
Microsoft retired the standalone Bing Search APIs on 2025-08-11 — their own
documentation page carries `is_retired: true` — so no new subscription key can be
created. The successor, Grounding with Bing Search in Azure AI Foundry, is
licensed for grounding a model's reply rather than for building a stored database
of facts and citations, which is exactly what this tool does; it is the wrong
instrument here regardless of the plumbing. Asking for it by name
(`TRACKER_SEARCH_PROVIDER=bing`) prints that explanation rather than "unknown
provider".

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

### Completing one project, cost no object

```bash
tracker enrich 93 --dry-run     # what it would read, spending nothing
tracker enrich 93
```

`tracker sync` spreads a budget across the whole database. `tracker enrich` inverts
that: pick one project and recruit every retrieval method the system has, in rounds,
until a round stops paying.

Six harvesters, cheapest and most certain first, so an expensive one never runs for a
field a free one would have filled:

| harvester | cost | what it contributes |
|---|---|---|
| derive | free | `county`, `lat`, `lon` from Census data |
| queue | free | already-discovered candidates whose slug names this project |
| retry | fetch | this project's URLs that previously failed |
| archive | fetch | sitemap sweep filtered to this project — the key-free search |
| search | API | Google CSE, queried against this project's *own* gaps |
| refresh | fetch | re-read its existing citations, which get edited |

Each round harvests, extracts, re-measures, and repeats. It stops when a round fills
nothing new, when no harvester has an unread article, or when every field is filled —
not at a fixed article count. `--max-rounds` and `--max-articles` exist only so a bug
cannot spend without limit.

Search queries are **templates anchored on the project**, not LLM-invented: the
project is already known, so there is nothing to infer, and an unanchored query like
"data center investment billion" returns the industry rather than the site. Every
query carries the quoted company and locality.

`--dry-run` harvests and reports without fetching or extracting. That matters here
specifically: `crawl.run(dry_run=True)` still fetches every page and still pays for
every LLM call — it only declines to commit — so on the most expensive command in the
tool, extraction is skipped outright instead.

**Its reach is capped by the corpus, not by the budget.** Measured on a 94-project
database with no search key: 17 projects had unread archive articles and 77 had none,
because the configured archives simply never covered them. A search key is what lifts
that ceiling — see the backend table below; without one, `enrich` on a project the
trade press ignored will honestly report that it found nothing to read.

### The whole dataset as one page

```bash
tracker export html --out data/exports/tracker.html
```

One self-contained file: open it by double-click. Sortable table over the 12
fields, a five-segment track strip per project so stage is scannable down the
column, filters for state / phase / blocked-on / confidence / quoted-only, a drawer
per project with its citations and milestones, capacity-behind-an-obstacle bars,
and a coordinate plot.

**No network requests at all** — no CDN, no webfont, no map tiles — so it works
offline and survives being emailed. The dataset is inlined rather than fetched from
a sibling `.json` on purpose: `fetch()` from a `file://` page is blocked as
cross-origin, so a two-file build would open to an empty table unless the reader
happened to be running a web server.

Two deliberate choices worth knowing. **Colour means trust, never category** — the
five tracks get a neutral ordinal ramp, and every hue is reserved for how much to
believe a value (amber = 待确认, blue = inferred) or for something being wrong
(rose = a blocked track). And the header strip is a **provenance ledger** that
recomputes on every filter, so the trust composition of whatever you are looking at
is always visible rather than something you have to go and check.

The coordinate panel is a plot, not a map: there is no coastline because there is no
boundary data here, and drawing one would be illustration rather than reporting.
Positions are city centres, not sites.

### Seeing where the data is thin

```bash
tracker gaps
```

Per-field coverage measured against the rows where the field can legitimately be
set — not against every project. That distinction matters more than it sounds:
`mw_built` looked 13% covered while 61 of the 90 projects were merely *announced*,
so nothing was built on any of them and a NULL was the correct answer. Counting
those as misses pointed effort at work that could never succeed. Fields whose
absence carries no information (`blocker`, `customer`) report `n/a` rather than a
low score, and the run ends with the measurable fields that have the most rows left
to fill.

### Filling county and coordinates without an LLM

```bash
tracker ingest geo --dry-run
tracker ingest geo
```

Derives `county`, `lat` and `lon` from US Census reference data — no API key, no
LLM, no per-row cost. The two files are gitignored (3.8 MB of national lookup
tables); the command prints their download URLs if they are missing. See
"Deriving county and coordinates" below for why this is a lookup rather than a
search problem, and what it deliberately refuses to guess.

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

### What could stop these projects being built

```bash
tracker risks                          # every open obstacle, grouped by kind
tracker risks --severity blocking      # only the ones that have stopped work
tracker list --risk transmission       # projects waiting on grid work
tracker stats                          # includes MW at risk per category
tracker exposure --by company          # capacity behind an obstacle, rolled up
```

```text
transmission  1 project(s), 900 MW
  #1 Microsoft — Fairwater (Mount Pleasant, WI)  material  900 MW
    Two 345-kilovolt upgrades outstanding before full load.
    "must complete two 345-kilovolt upgrades"

water  1 project(s), 900 MW  (+1 with no cited capacity)
  #1 Microsoft — Fairwater (Mount Pleasant, WI)  watch  900 MW
    Cooling draw questioned by the county board.
    uncited — confirm in `tracker review`
```

Categories map onto the PRD's obstacle list — `grid_capacity`, `transmission`,
`permitting`, `environmental`, `equipment_supply`, `chip_supply`, `financing`,
`offtake`, `community_opposition`, `water` — which is what makes the read-through
countable: MW blocked on `transmission` is a power and utility signal, MW blocked on
`offtake` or `chip_supply` is a cloud and semiconductor one.

Each obstacle shows the verbatim quote behind it, and one that has none says
`uncited` rather than sitting silently beside the evidenced ones.

`tracker exposure` is the rollup, and it deliberately **does not produce a single
"MW at risk" number**:

```text
open-risk exposure by company
+-----------+----------+-------------+-------------+----------+----------+-------+
| company   | projects | blocking MW | material MW | watch MW | total MW | no MW |
+-----------+----------+-------------+-------------+----------+----------+-------+
| Microsoft |        1 |           0 |         900 |        0 |      900 |     0 |
| Sabey     |        1 |          70 |           0 |        0 |       70 |     0 |
+-----------+----------+-------------+-------------+----------+----------+-------+
```

Collapsing those three columns into one requires deciding how much a `watch` is
worth against a `blocking`, and that is a judgement rather than anything a source
stated. `--weighted` adds the single number for whoever wants it, and prints the
weights it used on the same screen. Projects with an open risk but no cited capacity
sit in their own `no MW` column rather than being averaged in as zero.

Grouping by anything other than `category` counts each project once, in its most
severe open category. Grouping by `category` deliberately does not — a project
obstructed three ways belongs under all three — so that view says so rather than
inviting you to add the column up.

### Slippage is measured, but only where it is unambiguous

When `expected_online` moves later, a `delayed` event records both dates and
`delay_days` lands on the project's most severe open risk:

```text
risks (2)
  transmission material  2026-02-01  +881d
    Two 345-kilovolt upgrades outstanding before full load.

events (1)
  2031-06-01  delayed  expected_online moved from 2029-01-01 to 2031-06-01 (+881 days)
```

`expected_online` keeps its `PREFER_WEIGHT` merge policy — the strongest source
still wins the value, and the movement is recorded as history beside it. Switching
to newest-wins to make slips visible would have discarded the source-quality
ordering everywhere else.

**The number is only attached across a year boundary.** The column stores no
precision, and hedged dates get coarsened into it: a bare `2027` becomes 2027-01-01
and `late 2027` becomes 2027-10-01, so a source simply restating the same year more
precisely is indistinguishable from a 273-day delay. Every coarsening stays inside
the stated year, so a move into a later year cannot be an artefact while a move
within one might be. The event is written either way and says which case it is; only
the unambiguous one is counted.

And no risk is invented from a date change. A slipping date says the timeline moved,
not why.

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

### Feed filtering is three tiers, and two of them can be implied or absent

A `topic` term proves an article is about data centers; a `signal` term proves it
concerns a specific *project*. Both must match. Commentary about AI power demand
passes the first and fails the second, which is right — there is nothing in it to
extract.

A `risk_signal` term satisfies the second tier on its own. That tier exists because
every `signal` term is announcement-shaped — `announce`, `expand`, `invest`,
`build`, `campus`, `megawatt` — so the filter silently discarded every article
about a project going *wrong*. Measured against the real filter, all of these were
dropped for having "no project signal":

```text
Loudoun supervisors reject data center rezoning application
Georgia Power says transmission upgrades delay data center energization
Transformer shortage pushes back hyperscale data center timelines
Moratorium halts new data center development in Fayetteville
Water use concerns stall Tucson data center vote
```

The corpus the extractor ever saw was therefore announcements only, and no schema
change recovers an obstacle from an article that was never queued. The `topic` tier
still has to match, so a transformer shortage at a steel mill is still dropped, and
`exclude` still runs first — commentary, analyst notes and share-price coverage stay
out. The tier is absent-by-default in `load_config`, so a `feeds.toml` predating it
behaves exactly as before.

Measured over one live poll of all eight feeds: **200 entries, 38 kept by the two
tiers, 44 by the three.** Of the six the risk tier added, two were real project
obstacles ("New Jersey town sued for $300m by data center developer", "Behind the New
York data center pause is legislation…") and four were topical commentary. That ratio
is the point rather than a disappointment — see the triage note above: the queue is a
human checkpoint, and over-collecting is cheaper than tuning a filter until it
silently drops real obstacles.

Short terms are padded (`" sue "`, not `"sue"`) for the same reason `" mw"` is in the
signal tier: terms are plain substrings, and an unpadded `sue` matched *is·sue·d*,
which put "PJM Issued First Backup-Generator Warnings" in the queue as litigation
news on the first live run.

**This is not a lever for `blocker` coverage.** `tracker gaps` reports that field as
unmeasurable because absence is usually the truth: most projects have no blocker,
and chasing a percentage there rewards inventing obstacles. The point of the tier is
to stop throwing away the articles where an obstacle is genuinely reported.

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

**The gate matches values, not field labels.** It used to require a quote *tagged*
with each field's own name, which quietly made the model's bookkeeping a condition
of truth. T5@Augusta returned `mw_planned: 200` together with the sentence "…a
140-acre, 200 megawatt campus in Georgia", filed that quote under `name`, and lost
the capacity. Measured across the first 90 projects, the labelling requirement
discarded **89 correctly-evidenced values from 64 projects** — 60 of them `phase`,
which being NOT NULL silently became the `announced` default, so the stored phase
distribution described the gate rather than the projects.

A value now survives if any *verified* quote actually asserts it, whichever field
the model filed it under. This is strictly stronger than what it replaced:
quantities are compared after normalization (so "1.2GW" evidences `1200.0`), which
means a genuine sentence citing a *different* number can no longer launder an
invented value — something the old label-only check permitted, since a labelled
quote never had to contain the number it was cited for.

Two fields need different treatment and get it. `phase` is a judgement an article
never spells out, so it matches on wording ("broke ground" → `construction`).
`blocker` and `notes` are paraphrases — a correct summary like "grid
interconnection delays" shares no substring with "the project awaits two
345-kilovolt upgrades" — so they keep the label check.

### Deriving county and coordinates

Three of the twelve fields are a lookup, not a research problem, and no amount of
searching fixes them:

* An article writes "Mount Pleasant, Wisconsin" and essentially never adds "Racine
  County", so `county` sat at 44%.
* Articles do not print coordinates at all, so `lat`/`lon` sat at **0%** — the
  evidence gate correctly discarded every value the model ever produced for them,
  because none could be quoted.

`tracker ingest geo` derives all three from two free US Census files (no API key,
no rate limit). On the current database that moved `county` from 49% to 80% and
`lat`/`lon` from 0% to 89%, at zero LLM cost.

Two honesty constraints shape it:

1. **A place centroid is not the site.** "Abilene, TX" resolves to the middle of
   Abilene, kilometres from the campus. Fine for a dot on a national map, wrong for
   anything else, so every derived coordinate says so in its own excerpt: "the
   centre of the place, NOT the project site."
2. **A city spanning several counties has no derivable county.** Houston and Austin
   touch four each. Picking one would invent a fact, so `county` stays NULL and the
   run reports which cities were skipped and why. That is what holds `county` to
   80% rather than 100%, and the remaining 20% is genuinely not derivable from a
   city name — it needs a site address.

A derived row is a real citation (it points at the Census file, and a reviewer can
check the mapping by hand) but it is **not testimony about the project**, so
`confidence` excludes any source whose `extractor` starts with `derived:` before
scoring. Otherwise one press release plus a Census lookup would read as two
independent domains and reach confidence 3 — the score reserved for corroborated
facts. `county`, `lat` and `lon` are all `FILL_ONLY`, so a value an article really
stated is never overwritten by a lookup.

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

### Five schema additions beyond the PRD's three tables

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
- **`risk` table** — the PRD asks which obstacles could stop a project and how that
  reads through to chip, cloud and power companies. `project.blocker` is one
  nullable sentence, and the PRD's own list names seven obstacle kinds that a real
  project has several of at once. See below.

### Risk is a table, because a sentence cannot be cleared or counted

`project.blocker` survives as a **derived** column — the summary of the most severe
open risk — so the twelve tracked fields and the export shape are unchanged. But the
obstacles themselves live in `risk`, one row each, because the single column could
not do four things the PRD asks for:

- **Hold more than one.** Grid capacity *and* local opposition *and* a transformer
  lead time is the normal case, not an edge case. One column has to pick.
- **Ever be cleared.** `upsert._resolve` returns the existing value when a field has
  no claims, so a blocker could be replaced but never set back to NULL. A resolved
  obstacle sat on the row forever.
- **Be counted.** "How much planned capacity is blocked on transmission in ERCOT" is
  the question that carries the read-through, and free text cannot answer it. A
  closed `category` vocabulary can.
- **Be evidenced without a carve-out.** A blocker sentence is a paraphrase, so it
  can never be a verbatim substring of its own evidence — both blockers in the live
  database fail `_stated_in` against their own article text. The gate handles that
  with `_SUMMARY_FIELDS`, which accepts a paraphrase when the model *labels* a real
  quote with the field name. That works, but the label is the weakest link: any
  verified sentence under the right label was enough.

The last point is why `risk` splits the two apart: `summary` may be a paraphrase, and
`quote` holds the verified verbatim sentence beside it — and the quote must contain
wording for the *category it is filed under*, checked against `_RISK_EVIDENCE` the
same way `phase` is checked against `_PHASE_EVIDENCE`. That is strictly stronger than
the label carve-out it replaces, so `blocker` came out of `_SUMMARY_FIELDS`: obstacles
became storable by tightening the check, not by loosening it.

Severity is `watch` / `material` / `blocking`, ordered, and that order is
load-bearing: it decides which risk becomes `project.blocker`. A source that names an
obstacle without stating any effect gets `watch`, the conservative direction, because
`blocker` is the field an operator acts on.

Two rules about clearing, both learned from what the old column got wrong:

- **An article that stops mentioning an obstacle does not clear it.** Silence is not
  evidence. A risk goes away when a source reports the resolution or an operator
  marks it resolved in `tracker review`.
- **Re-reading an edited article updates wording and severity, but never revives a
  risk an operator resolved.** `status` belongs to the operator; the extractor owns
  the description, not the verdict.

`unclassified` is in the category vocabulary but the extractor may not use it — a
risk nothing can aggregate is invisible to every rollup, which is the one thing this
table exists to make possible. It is reachable only from a hand-curated `blocker`
string and from the 0004 backfill, both of which are a human asserting an obstacle
without saying which kind.

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
- The crawl path has been run live against MiniMax, not only fixtures: an initial
  live run surfaced four defects (fixed in `de4821c`), and `tracker enrich` was
  verified live on project #93 (OpenAI Stargate, Abilene).
- `project.country` is a dead column in v1 (`CHECK country='US'`), kept for
  forward compatibility. No CLI filter exposes it.
