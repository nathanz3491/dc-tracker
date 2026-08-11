# dc-tracker

Ingest, normalize and query US data center construction projects, where **every
non-null fact traces back to a source URL with a confidence score**.

SQLite is the source of truth. Ingestion is deterministic and re-runnable. The
CLI is the primary interface; `tracker serve` exposes the same dataset and the
same commands as a live console (see below) for anyone who would rather click
than type.

## What it does

Three ingest paths converge on one normalizer and one write path:

| Path | Command | What it is good for |
|---|---|---|
| Hand-curated JSON | `tracker ingest manual --json seed/sample-projects.json` | Projects you know about that no feed carries |
| ISO queue export | `tracker ingest pjm --csv FILE --iso pjm` | Candidate generation and corroborating citations — **not** a project feed, see below |
| News extraction | `tracker ingest crawl --urls urls.txt` | The 12 tracked fields, pulled from articles by an LLM and gated on quoted evidence |
| SEC filings | `tracker ingest edgar` | Investment, in-service dates and named tenants, from the one publisher that cannot refuse us |

Articles to extract from come from `tracker discover`, which polls news feeds and
queues candidates for triage.

Then query: `tracker list`, `tracker show ID`, `tracker stats`, `tracker capex`,
`tracker duplicates`, `tracker blocks`, `tracker review`, `tracker verify`,
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

Three optional extras, none required:

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
DataCenterDynamics does with Cloudflare bot management, it stays discovery-only;
see "Why DataCenterDynamics is discovery-only" below.

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
TRACKER_DEEPSEEK_API_KEY=your-key
```

`.env` is gitignored, and it is read by **absolute path** — so it applies no
matter which directory you run `tracker` from. Note the `TRACKER_` prefix: every
setting this tool reads carries it. `TRACKER_DEEPSEEK_API_KEY` and
`DEEPSEEK_API_KEY` are not the same variable, and only the first is read.

Keys come from `platform.deepseek.com` and work against the single host
`https://api.deepseek.com`. There is nothing else to configure.

> **Moved off MiniMax.** `TRACKER_MINIMAX_*` variables are no longer read by
> anything. A leftover MiniMax key in `TRACKER_DEEPSEEK_API_KEY` returns HTTP 401,
> and the error says so rather than leaving you to guess.

`tracker ingest crawl --check` verifies the key in one cheap call, before you
spend a run's worth of fetches.

### One command for the whole loop

```bash
tracker sync
```

Four phases: **discover** new candidate articles from the feeds, **extract** them
into the database, **refresh** existing projects by re-reading their sources, then
**list** the result. Needs the API key set as above. When a search key is also
configured (see "Search" below), the discover phase runs LLM-proposed web
searches automatically — `--search 0` skips them for a run.

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
tracker search --from-llm 20            # let the model propose the queries
tracker search --from-llm 20 --print-only   # just show them, search nothing
tracker sync --search 10                # more searches than the default
tracker sync --search 0                 # skip searching this run
```

Needs one search key in `.env` — **this project uses Serper**
(`TRACKER_SEARCH_PROVIDER=serper`). Once any key is configured, `tracker sync`
runs its search phase automatically and `tracker enrich`'s search harvester
stops being skipped; `--search 0` opts a sync out. Four backends are supported,
and whichever key you add is picked up automatically:

| backend | variables | free tier | card to sign up | index |
|---|---|---|---|---|
| **Serper** | `TRACKER_SERPER_API_KEY` | 2500 queries | no | Google |
| Google | `TRACKER_GOOGLE_API_KEY` + `TRACKER_GOOGLE_CSE_ID` | 100/day | no | Google |
| Brave | `TRACKER_BRAVE_API_KEY` | 2000/month | yes | Brave's own |
| Bocha | `TRACKER_BOCHA_API_KEY` | pay-as-you-go | no (CN signup) | Chinese-web-heavy; leads, not citations |

Pin one explicitly with `TRACKER_SEARCH_PROVIDER=serper`. `tracker search` prints
exactly where to get each key if none is set, and `--print-only` works without any
of them so you can run the queries by hand.

**A Wikipedia hit is mined for its references.** The top result for a tracked
campus is routinely its Wikipedia article, and the article's References section
is a curated bibliography of primary sources — measured on Hyperion, 50 external
links including the investor-relations release behind the Blue Owl joint
venture, which no configured feed carries. Search (and `enrich`) queue the
survivors of a keyword pass alongside the article itself, with Wayback wrappers
unwrapped to the URL they archived. The article is also extracted like any page,
under one guard: a wikipedia.org citation never counts as an independent domain
in `confidence`, because Wikipedia summarizes the same coverage the row already
cites — it can supply quotes and leads, never corroboration.

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

**Why search runs by default now, where it used to be optional.** `--deep` still
reaches the configured archives with no key and no quota — but only those. On a
database `sync` has already worked through, `enrich`'s free harvesters (queue,
retry, archive) draw from exactly the corpora sync drained, so search was the
one harvester that could reach new ground and it was silently skipped without a
key. Measured on Hyperion (#10) after configuring Serper: queue 0, retry 0,
search 42 URLs — the official campus page, the state economic-development
release, and 25 Wikipedia references among them.

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
tracker enrich --select 30      # the 30 worth finishing, chosen closest-first
tracker enrich --all            # everything below --target; --budget is the bound
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
| search | API | the configured backend (Serper here), queried against this project's *own* gaps; Wikipedia hits mined for their references |
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

**Without a search key, its reach is capped by the corpus.** Measured on a
94-project database with none: 17 projects had unread archive articles and 77 had
none, because the configured archives simply never covered them — on a database
`sync` has already worked through, the free harvesters draw from exactly the
corpora sync drained, and `enrich` honestly reports that it found nothing to
read. The configured Serper key is what lifts that ceiling: measured on Hyperion
(#10), queue and retry harvested 0 and search harvested 42 URLs, 25 of them
references mined from the campus's Wikipedia article.

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

### The console: the same dataset, live, with the commands as buttons

```bash
tracker serve
```

Opens `http://127.0.0.1:8765/`. Eight views — Projects, Map, Capex, Queue,
Coverage, Commands, Runs, Help — reading the database on every request, so it
reflects what a run just did without re-exporting anything. A run started from the
page refetches the dataset when it finishes, so a crawl that adds a project or a
merge that removes three is visible without a reload.

**Different from `tracker export html`, and both are worth having.** The export is
one self-contained file you can email; it is frozen at the moment it was written
and cannot run anything. The console is a server: live, and able to execute the
commands that change the data.

Hovering any value shows the sentence behind it. That works because the evidence
gate's per-field quotes are now stored (`source.quotes`, migration 0007) rather
than collapsed into one excerpt — see "Provenance is per field, not per source"
below. Citations recorded before that migration fall back to the source excerpt
and the page says so rather than passing a paragraph off as the sentence behind
one number.

**Running commands from the browser.** The Commands view is built by introspecting
the CLI itself, so it cannot fall behind: every flag appears with its real type,
default and help. Output streams into the Runs view and is kept per run under
`data/runs/`.

Flags are rendered for someone who has not used a terminal — a plain-language
label with the real flag beside it, a picker listing the actual projects instead
of an id you have to go and look up, presets around the CLI's own default rather
than an empty number box, and the thirteen flags on `sync` folded down to the ones
you might change. The argv preview stays: it is the honest record of what will
run, it is what you paste into a terminal on a read-only console, and matching it
against the labels is how someone graduates to the CLI.

**Routines** sit above the command list, because for most visits the question
"what do I run to catch up" has one right answer and it is three commands in a
particular order:

| Routine | Steps |
| --- | --- |
| Catch up on the news | `sync` → `ingest geo` → `logic check` |
| Deepen what we already have | `enrich` → `ingest geo` → `gaps` |
| Tidy the database | `duplicates` → `logic check` → `stats` |
| Prepare a report | `stats` → `capex` → `verify` |

The order carries reasons the page now states: geography is a free lookup, so
deriving it *after* the read locates the rows that just arrived; contradictions
come from new values, so checking logic before the read reports problems the run
was about to fix. Each runs as **one job with one log and one entry in the
history** — not chained by the browser, where a closed tab would abandon the
sequence halfway. It stops at the first real failure, except for steps like
`duplicates` that exit non-zero when they *find* something, which is an answer
rather than a breakage. Adding a fifth routine is eight lines in
`webui/workflows.py`; a node editor would have been a builder nobody asked for.

**The AI overview** in each project drawer is the one thing in the console that
is a *reading* of the values rather than one of them. It is a card in the stats
tab's ordinary flow, under the figures it is a reading of — it was briefly pinned
above the tab strip, which made it the one block you could not scroll past. It generates
when you open the row and streams as it is written, and it is cached by content —
so a row is paid for once, and reopening it is free until something about the row
actually changes. It is never stored, never becomes a source, and cannot move
confidence. See `overview.py`.

It is written by `fast_extractor` — `TRACKER_DEEPSEEK_FAST_MODEL` with **reasoning
disabled** — rather than by the reasoning tier, because this is the one call
somebody sits and waits for.

**The latency here is a thinking question, not a model question**, and that took a
provider migration to be able to act on. Measured across MiniMax's whole roster on
this prompt, time to the first *visible* word ranged from 12.4s to 46.6s — and
`MiniMax-M3` was worse than slow, spending the entire completion budget thinking
and returning an empty briefing. Tokens spent inside `<think>` are invisible, so a
model that streams instantly and then deliberates is not fast; only removing the
reasoning moved the number. On MiniMax the only way to remove it was to pick the
one model that could not think (`M2-her`, 2.7s) — `thinking`, `reasoning_effort`
and `enable_thinking` were all accepted by that API and all ignored, and an
assistant prefill of `</think>` did not suppress it either.

That workaround had a measured cost. `M2-her` is built for dialogue and sometimes
read the data wrong: on Fairwater — construction track `nothing reached`, every
other track passed — it wrote *"All tracks complete; construction the last to
finish"*, inverting the most informative field in the row. It also named a utility
and a permit process that appear nowhere in the data.

DeepSeek honours `thinking: {"type": "disabled"}`, so the fast path is now the
same `deepseek-v4-flash` as everything else with reasoning switched off at request
time, and **that accuracy trade is gone** — the briefing is written by the same
model that does the extraction. Any reasoning that does arrive is still stripped as
it streams, so it never reaches the page.

One guard survives the move and earns its place:

* the prompt asks for an `[[END]]` sentinel, and `overview.RUNAWAY` cuts the
  **stream** there, or at the point the model starts a second answer. Cutting the
  stream rather than the finished text is what saves the time: abandoning the
  generator closes the connection, so tokens after the answer are never waited
  for. Left alone, `M2-her` wrote 756–982 words against a 110-word instruction,
  repeated itself under headings like "Final answer (last round)", and narrated
  its own word count. "The model stops when asked" is not a property worth
  assuming of a provider on the strength of not yet having seen it fail.

`MODEL_TOKEN_CAP` also survives, empty. It clamped the budget to the 2048 `M2-her`
accepted — without it every request was an HTTP 400 and there was no briefing at
all. The v4 models take 384K, far above anything asked for here, so today it is a
no-op guarding a hazard that has happened once.

The reply is markdown — one sentence, then two or three bullets — rendered to
React elements by a small parser in `app.js`. Deliberately **not** `innerHTML`:
this text is written by a model out of articles fetched from the open web, which
makes it the least trustworthy string in the product, and turning it into markup
would run a path from someone else's page into a console that executes commands.
Links are flattened to their text for the same reason. Verified by feeding the
panel a briefing containing `<script>` and an `onerror` attribute: both render as
characters, nothing executes.

Three things bound what that can do, and they are the reason it is safe to leave
open:

* **The bind address.** Loopback only. `--host` anything else is refused without
  `--allow-remote`, because anyone who can reach the port can start a run.
* **No shell, ever.** A request names a command and a flag object; the server
  validates both against the catalog and builds an argument *list*. Nothing is
  concatenated into a command line, so `;`, backticks and `&&` are inert. An
  unknown flag is an error rather than something passed through — which is how a
  `--db` or an `--out` would otherwise arrive.
* **Spending is confirmed.** `sync`, `enrich`, `infer`, `search`, `point`,
  `logic check`, `ingest crawl` and `ingest edgar` spend real LLM tokens, and no
  single click can start one — the UI asks a second time and says what it will
  cost. A routine containing any of them is confirmed the same way, so wrapping a
  command in a sequence is not the way around this.
* **Destruction is confirmed too**, on its own axis, and more heavily. `merge`
  spends nothing and is the only command here that cannot be undone, so it still
  takes the command name typed out — proportionate friction in front of an
  irreversible act, where a second click is proportionate to spending money you
  can decide to spend again. The check is on the command name, not its flags, so
  no argument combination talks its way past it.
* **A routine is not a back door.** Its steps are validated against the same
  catalog, so a blocked command — `cloudflare`, which publishes this page to a
  public URL — cannot be reached by putting it in a sequence.

`tracker serve --no-run` drops the runner entirely and serves the views read-only.

**No network requests at all** — React, d3, three.js, Lucide, the Census boundary
file and all three webfonts are vendored under `tracker/webui/static/vendor/`
(3 MB), and the server sends a `default-src 'self'` CSP so a CDN URL creeping back
in fails loudly instead of quietly reintroducing the dependency. The front end has
no build step and the repo has no `package.json`: React is a UMD global, the
Meridian component bundle is already compiled, and `htm` supplies JSX-like
templates from tagged template literals.

**The mark**, beside the wordmark in the header, on the sign-in card and in the
tab, is a citation bracket whose bar stops where the evidence stops — the empty
half of the bracket is this project's one rule drawn literally: a figure nobody
published stays null rather than guessed. It is 168 bytes of inline SVG filled
from `currentColor`, which is what lets a single copy serve both themes (`--primary`
is honey `#a05e1c` on cream and `#dca75f` on espresso) at no request. The favicon
is the same two paths with **no background plate** — a honey tile would sit lit in
dark browser chrome, whereas the bare mark takes whatever the tab strip is. It is
drawn full-height on its 24-unit grid and aligned by baseline rather than centred,
because `dc-tracker` has no descenders and centring the mark on the line box drops
it 3.5px below the wordmark's optical middle.

### Putting the console on the internet

```bash
tracker cloudflare --check     # is everything in place?
tracker cloudflare             # publish it
```

The console still binds loopback. `cloudflared` makes an **outbound** connection
to Cloudflare and relays traffic back down it, so nothing is opened on your
network and no router or firewall changes are involved. `tracker serve --tunnel`
is the same thing in one flag; the command exists because publishing deserves a
readiness check and a second shape.

**Two shapes.** A *named tunnel* is one you created once on your own account: the
hostname is yours and survives a restart, which is what you want if the link is
going to anyone else — re-sending a fresh URL every session is how one ends up
written down somewhere it should not be. A *quick tunnel* is anonymous: a random
`https://<four-words>.trycloudflare.com`, no Cloudflare account, and a different
URL every session. **A quick-tunnel URL cannot be preserved across a restart**;
that is Cloudflare's design, not a missing flag.

Creating the named tunnel is deliberately left to you. It writes credentials into
your home directory and a DNS record into your zone, both of which outlive the
process:

```bash
cloudflared tunnel login
cloudflared tunnel create dc-console
cloudflared tunnel route dns dc-console console.example.com
```

Then record it once, in `.env`, and publishing takes no arguments:

```
TRACKER_TUNNEL_NAME=dc-console
TRACKER_TUNNEL_HOSTNAME=console.example.com
```

```bash
tracker cloudflare          # the configured tunnel; same URL every time
tracker cloudflare --quick  # a throwaway URL instead
```

These are settings rather than flags retyped every session because they describe
the machine, not the run. `--name` and `--hostname` override them **together**:
an explicit `--name` will not inherit the configured hostname, because printing a
real hostname beside a different tunnel produces a URL that looks right and points
at the wrong thing. `serve --tunnel` uses the same pair, so the two ways of
publishing cannot land on different URLs.

Restarting to pick up new code is then just Ctrl-C and the same command. The
console re-reads its static files from disk on every request and stamps every
asset URL with that file's version, so a restart genuinely replaces the front end
— a browser or a CDN edge cannot keep serving the previous one, because a changed
file is a different URL. Only the Python process needs the restart.

`TRACKER_CONSOLE_PASSWORD` is required for either shape and the command refuses to
start without it. A quick-tunnel hostname is random but **not secret** — it goes
over the wire and Cloudflare knows it — so it is obscurity, not access control.
The password, the per-client and global lockouts, and `--no-run` are the access
control. The console never sees the tunnel: cloudflared connects to it over
loopback, which means the "refuse a non-loopback bind" check never fires and the
password is what replaces it.

**`--check` before you need it.** It verifies the password, that `cloudflared` is
present *and actually executes*, that a `--name` tunnel exists on the account, and
that the database and front-end files are there — then exits. Worth running once,
because two of those fail in ways that are otherwise discovered at the worst
moment: a truncated `cloudflared.exe` is a valid PE file that dies with WinError
193 and no output, and npm's `.CMD` shim on Windows swallows both.

**Behind a proxy, the quick tunnel is relayed.** cloudflared builds its own
`http.Transport` for the one request that asks Cloudflare for a quick tunnel, and
a zero-value Transport has no proxy function — so that request ignores
`HTTPS_PROXY` however it is set, while every other tool on the machine honours it.
On a filtered link that is the difference between working and not: measured
against `api.trycloudflare.com` over an hour, direct swung between 3.8s and 28s
while the proxy stayed near 4s, against a fixed client budget of about ten
seconds. `tracker cloudflare` failed with `context deadline exceeded` roughly
three times in four.

So the console starts a loopback relay and points cloudflared's `--quick-service`
at it. The relay forwards that one request through the proxy and hands the JSON
back; the outbound leg is still TLS to Cloudflare and the only plaintext hop is
inside loopback, where cloudflared and the console already talk. It is not a
general proxy — the upstream host is a constant, only the path travels, and an
absolute request URI is refused. The proxy is found in the environment or, on
Windows, in `Internet Settings`, which is where Windows apps look and Go does not.

`--proxy http://host:port` forces one, `--no-proxy` skips the relay entirely, and
`--check` prints which was found. Attempts are retried three times regardless,
because the underlying failure is a latency race rather than a refusal.

Before this, a failed request did something worse than fail: the error text
contains the API's own URL, the hostname pattern matched it, and the console
printed `public: https://api.trycloudflare.com` — a link to Cloudflare's API
presented as your console.

If it still times out, use a named tunnel. It never calls that endpoint.

### Documentation

| Where | What |
| --- | --- |
| [docs/what-we-built.zh-CN.md](docs/what-we-built.zh-CN.md) | **Start here.** 最短的一份：有了什么、怎么跑、报数前要知道的数字。~100 行。 |
| This file | Everything: install, keys, every command, and the reasoning behind each design decision. |
| [docs/architecture.md](docs/architecture.md) | How the CLI, the database and the console fit together — and why the browser never computes a judgement of its own. |
| [docs/guide.zh-CN.md](docs/guide.zh-CN.md) | 中文使用指南 |
| [docs/architecture.zh-CN.md](docs/architecture.zh-CN.md) | 架构说明（中文） |
| The console's **Help** tab | The three ideas — evidence tiers, five tracks, confidence — where you are actually looking. |

### SEC filings: the publisher that cannot lock us out

```bash
tracker ingest edgar                        # every company in the list
tracker ingest edgar --kind utility         # one class of filer
tracker ingest edgar --company Meta         # just one
tracker ingest edgar --per-company 3        # the cost dial: filings per company
tracker ingest edgar --dry-run              # prepare and cache, extract nothing
```

Every other source here is somebody's website, and the good ones increasingly
answer 403 to anything that is not a browser. Filings are different in kind:
publication is a legal obligation, the format is fixed, and they come from a
government host with a documented rate limit rather than a bot filter. They are
also where the fields we cover worst actually live — investment in the cash-flow
statement, in-service dates in MD&A, and the end customer in the lease footnotes.

Needs `TRACKER_USER_AGENT` set to a real contact; SEC blocks the shipped
placeholder, and the command refuses to start rather than collect a run's worth
of 403s.

**Five classes of filer, and `--kind` reads one.** Hyperscalers and neoclouds are
the buyers; landlords hold the lease footnotes that name a tenant. The other two
were added because the buyers alone cannot answer the two questions that matter
most:

* `--kind utility` — the power company is the counterparty that cannot be
  bypassed, and its filings state which large load has actually signed an
  interconnection agreement and when energisation is expected. `power` is the
  track this database is worst at and the one it refuses to infer from
  construction. The fourteen utilities were picked by measured exposure: each
  serves a state holding at least ~1% of tracked capacity. Constellation and
  Talen are there because a nuclear PPA names its counterparty *and* its site,
  which is the customer-attribution fact 60% of the database lacks.
* `--kind contractor` — an E&C filer discloses backlog, and backlog leads
  energisation by a year or two. It is the weakest at naming sites: expect timing
  and corroboration rather than new projects, and judge the class on that.

Each class is searched with **its own phrases**, from `[search.by_kind]`. A
utility does not write "build-to-suit"; it writes "large load" and
"interconnection agreement". Adding a class of filer without adding its
vocabulary is how a new source returns nothing and looks like it had nothing.

Utilities and contractors are sources, never buyers — only `hyperscaler` and
`neocloud` count as end users in `capex.attribute`, so adding forty companies
cannot quietly move anybody's attributed capacity.

Which companies are read is [seed/edgar-companies.toml](seed/edgar-companies.toml),
and that file **is** the precision mechanism. Full-text search scoped by CIK
returns only that company's filings; unscoped, `"data center campus"` returns
1,066 hits led by shell companies. The `sics` parameter is accepted and silently
ignored, so industry filtering is not available. CIKs must be **10-digit
zero-padded** — `1326801` returns nothing where `0001326801` returns 105, with no
error either way.

Two things the module does that the news path does not need:

- **Selects a section rather than truncating.** A Meta 10-Q is 369,000 characters
  against a 24,000-character model budget. `crawl.truncate` keeps the head and
  tail, which is right for an article and wrong here, because a filing's facts sit
  in MD&A and the footnotes. Paragraphs are scored on the density of things the
  evidence gate can verify. Measured over 39 filings: ~6% of the document kept,
  and where the budget is not the binding constraint, 99% of the fact-bearing
  paragraphs survive.
- **Refuses contracts.** An 8-K exhibit is as often a credit agreement as a press
  release, and a credit agreement is dense with exactly what the scorer rewards.
  Legal vocabulary per 10,000 characters separates them cleanly — 0.3 for a 10-Q,
  0.5 for a press release, 20.1 for a financing exhibit — so anything above 5 is
  dropped before it costs a call.

Everything after that is the ordinary path: the selected section is written into
the same article cache the feeds use, and `crawl.run` reads it with the same
prompt, the same evidence gate and the same write path. A filing is held to
exactly the standard an article is.

### Who is actually buying the capacity

```bash
tracker capex
```

The database is keyed on the site — `(operator, locality, state)`. The question an
analyst is paid to answer is on a different axis: how much capacity does each end
customer have in flight, when does it land, and how exposed is it. Those grains do
not coincide, because much hyperscaler capacity is built by wholesale developers
and leased.

Grouping on `project.customer` alone would answer for about a tenth of the
database, so attribution is three rules in order:

1. a **named tenant**, folded so Meta and Facebook are one buyer and a hedge like
   "a Fortune 100 technology company" names nobody;
2. otherwise the **operator, if the operator is an end user** — a Meta campus is
   Meta's, and `customer` being NULL there is correct rather than missing;
3. otherwise **unattributed**, reported as its own row rather than hidden, because
   how much is being built for nobody we can name is itself worth knowing.

Who counts as an end user comes from the `kind` column in
[seed/edgar-companies.toml](seed/edgar-companies.toml), plus a short list of
private companies that file nothing with the SEC — without which OpenAI and xAI,
two of the largest positions in the table, would have been invisible. Only
`hyperscaler` and `neocloud` count; a utility connects capacity and a contractor
builds it, and neither buys it.

`--by-quarter` buckets the pipeline by calendar quarter instead of by year, which
is the grain the question is usually asked at. Read it as a shape rather than a
schedule: 34% of dated projects land on 1 January, because that is where a source
saying only "sometime in 2027" normalises to. `capex.date_precision` measures it
and both the CLI and the page print the number rather than leaving you to guess.

The footer states what fraction of projects the view can speak for. That is not
decoration: a rollup silently covering a third of the data looks authoritative and
is not.

The table defends itself against the two ways it used to be wrong, and both
defences disclose rather than hide:

- **One row per suspected campus.** A campus stored twice is a nuisance in a site
  listing and a wrong number the moment anything groups by buyer — the Abilene
  campus was in the database four times, so 1.2 GW was counted four times against
  OpenAI. The rollup now counts the one row a merge would most likely keep (a
  named tenant first, then the largest capacity) and sets the others aside;
  the footer says how many megawatts and dollars were skipped. Skipped, not
  merged: `tracker merge` remains the repair, and the rows are still there.
- **Implausible dollars are excluded; merely unquoted ones are counted and
  disclosed.** A figure the `$/MW` plausibility ceiling demoted — the signature of
  a programme-wide total like "OpenAI's $500 billion Stargate" quoted in an
  article about one campus — is kept out of the investment column and shown in the
  footer instead. A figure that simply went unquoted is a different thing: very
  likely correct, and nobody sourced it. Excluding those too understated the one
  number the table exists to state, and one 待确认 bit could not tell them apart —
  which is why the gate now records *why* it refused a value and the rollup reads
  that back rather than re-judging the figure.
- **An obstructed project counts even when its obstacle is 待确认.** The
  obstructed column includes projects whose only open obstacles are unquoted, and
  the footer says how many: understating exposure is the worse direction to be
  wrong in, and an obstacle a source reported is information before it is
  evidenced.

The year columns are a continuous range, so a year nothing is dated for shows as
an empty column between years that have capacity — "nothing is *dated* 2029"
rendered honestly, instead of 2029 silently vanishing between 2028 and 2030.

Two things it deliberately does not do. It never infers a tenant — who signed a
lease is a *fact* with a documented answer, and `tracker infer` exists precisely to
keep judgements and facts apart. And where a project names a tracked operator as
its own customer, it flags the row instead of correcting it, for the same reason
`dedup` refuses to auto-merge across granularity: a landlord genuinely can lease
from another landlord.

The console's **Capex** view is this table made openable, because an aggregate a
reader cannot open is one they have to trust. **Click any figure in a row** and it
breaks into the sites that make it up, with the *column* deciding the view: the
site list, planned capacity with a share bar, what is actually running,
investment per site with the never-confirmed ones marked, what is obstructing
which site, what has slipped, or — from a year column — the sites dated into that
year. The panel never sums, so the rows always add to the cell you clicked, and a
site nobody has sized says so rather than showing a zero. Clicking a site opens
its drawer, so the drill-down ends at citations and not at another total.

**Hover a buyer** and a card shows the instant facts with a model-written reading
of the position streaming under them — the capex twin of the project drawer's AI
overview, same fast model, same rules: cached by content, cut at the sentinel,
never stored, never evidence. That prompt asks for **no figures at all**: measured
over four rounds, the fast model wrote fluent analysis and unreliable arithmetic
(subtracting to invent "3,300 MW due mid-year", summing two sites, "only 30%
online"), so every share it was reaching for is now computed server-side *in
words* and the numbers stay where they are correct — on the card above the prose,
and in the table behind it.

The duplicate review also lives on that page rather than under Coverage: the
repair belongs next to the figure it protects. Reviewing a group by eye is also the one thing a browser does
better than the CLI — the candidate rows sit side by side with their capacity,
citation count and dates, a radio picks the survivor, and the merge runs through
the same `/api/run` path as everything else, behind the typed confirmation.

Which id survives decides more than a row number: quantitative fields are
recomputed from the combined citations, but identity fields — name, company,
locality — keep the survivor's values, so pick the row whose identity should win.

#### Saying no: `tracker duplicates park`

```bash
tracker duplicates                       # suspected groups, strongest evidence first
tracker duplicates --no-weak             # only pairs raised by more than a shared word
tracker duplicates park 55 58 --reason "different operators, different buildings"
tracker duplicates parked                # every pair ruled out, and who ruled it out
tracker duplicates unpark 55 58          # put the question back
```

The report proposes and never merges, which left it with exactly one possible
answer. A pair that was simply wrong came back on every run, ahead of the real
ones — and this is not only clutter: `capex` reads the same pairs and holds one row
of every suspected group out of the buyer table, so a false pair takes a real
campus's capacity out of a number somebody quotes. Parking is stored pairwise, so a
third row appearing next week is still asked about.

**Each pair now says what raised it**, because "these look similar" is not
something a reader can check:

| evidence | what it means |
|---|---|
| `same tranche` | both rows hold one derived `block_key` that appears in no other town |
| `shared operator` | one company string names the other's operator — how one campus becomes four rows |
| `name overlap` | a distinctive word in common, the weakest signal, hidden by `--no-weak` |

Two false pairs on the live database prompted the scan's rules to tighten. `Aligned
Data Centers Phoenix` matched `NTT Global Data Centers Americas Phoenix` on the
token "centers" — the generic-word list held the singular and not the plural. And
`Element Critical — Houston One` matched `Switch — Houston Data Center Campus`
because both had a tranche labelled "existing". **A tranche key that turns up in
more than one town is vocabulary, not identity**, and no longer pairs anything;
rarity is measured across localities rather than across rows so the flagship case
survives — the Abilene campus is stored four times and all four hold `building-1`,
in one town.

### Numbers that cannot be true

```bash
tracker audit                # every project
tracker audit check 72 25    # only these
tracker audit resolve        # settle what it found
```

Free, read-only, no LLM. Run it after every sync or backfill; an empty result is
the point. (The ids moved onto `check` when `resolve` arrived: a command group
cannot take a variable number of positional arguments *and* dispatch a
subcommand — `tracker audit resolve` would parse "resolve" as a project id.)

**Why this is not `logic check`.** That command asks whether a row's fields
contradict *each other*, and it cannot help here, because each of these rows was
perfectly self-consistent around a figure wrong by three orders of magnitude.
Project 72 read *Flexential, Englewood expansion, 11,250 MW* — larger than any
campus planned anywhere, for a colocation operator whose entire portfolio is under
500 MW. Nothing contradicted it, so nothing flagged it, and it sat as the largest
number in the database feeding an 8.7-million-H200 estimate and every national
total. **A unit error does not look like a lie. It looks like a big number.**

Six checks, each leaning on a stated piece of physics or economics rather than a
threshold somebody picked:

| check | what it means |
|---|---|
| `same_figure_two_units` | two sources ~1000× or ~100× apart — the same number in kW and MW, not a disagreement |
| `campus_exceeds_worlds_largest` | over 8,000 MW on one site, beyond anything actually planned |
| `block_out_of_scale` | a tranche larger than the campus containing it |
| `giant_capacity_unconfirmed` | a gigawatt claim with no quote anywhere behind it |
| `usd_per_mw_out_of_band` | outside $0.3M–$60M per MW, when real builds run $2M–$30M |
| `h200_disagrees_with_capacity` | a derived figure that drifted from the capacity it came from |

Unit errors sort first, because those are the ones that poison totals.

On the live database it returns 22 findings across 20 of 225 projects: a rate you
can actually work through, which is the difference between a check people run and
a check people mute.

#### Settling them: `tracker audit resolve`

The listing was half a tool. It reported the same 11,250 MW expansion on every run
because the only repair it could offer was a sentence telling somebody to go and
read an article. `resolve` is that somebody, and it climbs only as far as it has
to:

| rung | what it is | cost |
|---|---|---|
| 1 | **Arithmetic** — the answer is not a judgement | free |
| 2 | **You**, with every claim and quote on screen and one-key answers | free |
| 3 | **A reasoning model** on what the row holds | one call |
| 4 | **The open web**, when the model says the row lacks the answer | a search + up to 4 fetches |
| 5 | **The model again**, with those passages | one call |

Each rung runs only because the one above declined, which is the whole cost
control. Rung 1 is deliberately two cases and no more: `h200_equivalent` is a fixed
ratio applied to capacity, and a tranche *labelled* "2.4 MW Lease" carrying 2400 on
a 15 MW campus is that label read as kilowatts — the correct figure is written down
beside the wrong one, so nothing is being guessed. Rung 2 offers a third answer
besides the edits, `?`, which hands the question down; not knowing is the commonest
honest response and it should cost one keystroke.

**A model's output is one key from a list a person wrote**, plus a confidence and a
sentence. It cannot type a capacity. It may answer `m` — "the row does not contain
the answer" — which is what sends the question to rung 4 rather than forcing a
guess. Pages fetched there are read and not stored: they inform one decision, and a
page skimmed for one number is not a citation for anything.

Every edit is written into the row's notes naming who made it — `rule`, `operator`,
`model` or `model after search` — and a finding settled once is not asked again
unless you pass `--again`. `--no-ask`, `--no-llm` and `--no-search` each stop the
ladder at that rung; `--no-llm --no-ask` does the free repairs and nothing else.

### One tranche wearing several names

```bash
tracker blocks                    # every project, strongest evidence first
tracker blocks 1 10                # only these projects
tracker blocks --only mergeable    # one verdict at a time
```

Free, read-only, no LLM — the shape `tracker duplicates` already established,
one level down: a campus read by 25 sources acquires a name per source.
Fairwater held `Building 2`, `Facility 2`, `Second facility` and `Area II` for
one building, because `blocks.block_key` folds ordinals hard ("Phase I" and
"first phase" already converge) but cannot fold *which noun a source chose*.

Three verdicts, and only the first proposes anything:

| verdict | what it means |
|---|---|
| `mergeable` | one tranche under several names; nothing disagrees |
| `collides` | two sources confirm *different* capacities for it — one figure told twice would agree, this doesn't, so it refuses rather than picks |
| `ambiguous` | a bare ordinal that fits two families — "Facility 1" reads as both `Building 1` and `Phase 1` |

It never writes: `block_key` is the write path's identity, and folding it
further to collapse these names would also fold `Phase 1` into `Building 1`.
On the live database: 23 groups across 16 projects, 21 mergeable (44 rows
that are 21 things), 1 collides, 1 ambiguous. The collision is worth more
than the merges — Hyperion holds `Phase 1` at 2,000 MW and `Phase 1 IT Load`
at 1,500, which are facility load and IT load, two measurements of one
phase, not a disagreement to resolve by picking.

### Values that contradict each other

```bash
tracker logic check                    # free: rules and source disagreements
tracker logic check --severity error   # only the impossible ones
tracker logic check --read 20          # also have a model read 20 rows
tracker logic check --audit 20         # audit the evidence behind 20 rows' values
tracker logic resolve                  # work through them, one at a time
tracker logic resolve --code built_exceeds_planned   # one kind at a time
tracker logic resolve --auto           # only the repairs needing no decision
```

Every other check asks whether a value is *supported*. This one asks whether the
supported values *agree*: a row can be perfectly cited and still be impossible. A
campus marked `operational` whose construction track has reached nothing is either
the wrong phase or a missing milestone, and both citations behind it can be sound.

Three layers, and only the last costs anything.

**Rules** are free and state their reasoning, so you can disagree with one without
reading code. On the live database they find 21 impossibilities and 125 warnings
across 221 projects — energised before operational, 100 MW built against 32 MW
planned, an expected-online date 944 days in the past on a project still marked
under construction.

One of them exists because of a quirk worth knowing: `tracks.standing` reads an
event's *type* and never its date, so an `energized` dated next December counts as
reached today and drags the whole power track with it. `milestone_in_the_future`
reports that rather than fixing it, because changing what "reached" means would
move every track strip in the product.

Two of them ask a narrower question than the rest: not whether the row's fields
agree with each other, but whether a stored **number** agrees with the citations
under it.

| check | what it means |
|---|---|
| `value_above_its_evidence` | the row holds more than its claims and blocks can account for |
| `value_without_evidence` | the row holds a figure no source on it claims at all |

Both are restricted to money and megawatts — the fields that feed `tracker capex`
and the national totals, where an unsupported figure misstates a rollup rather
than just one row. On the live database: **5 and 22** findings across 20 projects.

Stargate Abilene is why they exist. The row read `mw_built = 1200` while the only
`mw_built` claim on it was a well-quoted **200**. Both "1.2 GW" quotes had since
been re-extracted as `mw_planned`, correctly — committed capacity is not energised
capacity — but `mw_built` merges by MAX, and MAX counts the value already stored
among its own candidates. **So it can never come back down.** The figure outlived
the claim that produced it by 1,000 MW, against a ~0.4 GW satellite read and the
project's own `phase-1` block of 200 MW serving.

The collision check below could not see it, and that is the point: a collision
needs *two* claims on a field to compare. One claim and a row that disagrees with
it is the cheapest possible version of the error, and it was invisible.

Both consult the block rollup as well as the claims, because `blocks.reconcile`
deliberately raises a campus scalar to the sum of its tranches. The first cut of
the rule did not, and reported 28 rows behaving exactly as designed.

**Collisions** are two sources claiming different values for one field. The winner
is read back from `upsert.resolve_field` — the same function the write path used —
and printed with its reason. That reason is **not always "the better source won"**:

| field | decided by |
| --- | --- |
| `mw_built` | the largest figure; energised capacity only grows |
| `first_announced` | the earliest; that is what "first" means |
| `phase` | furthest along, unless a source says it stopped |
| `name`, `company`, `city`, … | first seen, never overwritten; churn beats staleness |
| everything else | credibility, then recency |

Getting that wrong is not cosmetic. The first version re-derived the winner as
though credibility always decided, and reported **73 of 221 rows** as having
drifted from their sources. None had. After asking the real resolver: **zero** —
which is the correct answer for a write path that is working.

**Judgement** is `--read N`, one LLM call per project, off by default. It catches
what a rule cannot phrase. Every finding must name two fields and quote its
evidence or it is dropped, it is never allowed to pick a collision winner, and
nothing it says is written. Measured honestly: across four rows read during
development it returned **nothing** — the guard rails demonstrably hold, and its
usefulness on this database is still unproven. The rules are carrying the value.

**The evidence audit** is `--audit N`, the same cost, and asks the prior question:
does the sentence recorded as a value's evidence actually state that value *for
this project*? The gate that stored the quote checked mechanically — the figure
appears in the sentence — and what survives that and still goes wrong is
semantic: a programme-wide total quoted as one campus's money, a figure about the
building next door, an aspiration recorded as a schedule. Rows are read costliest
first, because the audit exists to protect the capex sums and the dollars at
stake pick the order. Findings carry one of three verdicts — `unsupported`,
`misattributed`, `hedged` — plus a reason checkable against the quoted sentence
alone, and nothing is written: a person confirms a finding by demoting the value
in `tracker review`, and an unconfirmed investment figure already stays out of
the capex sums, so the repair path existed before the audit did.

**`logic resolve` is the part that makes the other 149 worth finding.** It first
re-runs the merge policy on any row whose stored values its own sources no longer
support — arithmetic, no decision needed. Then it walks you through the rest one
at a time, with the values and the quotes behind them on screen, and the answers
as single keys:

```
#164 Cologix — COL4  Columbus, OH
built_exceeds_planned 50 MW built against 36 MW planned
  mw_built = 50.0
    "50 MW of power will be available across three data halls"
  mw_planned = 36.0
    "adding 36MW of power to its total capacity in the Region"
  u  the plan was revised — raise mw_planned to mw_built
  c  the built figure is wrong — clear it
  v  it is fine as it is — mark the row verified
  s  skip    q  stop here
```

That is the distinction the tiers actually draw. A *model* may not assert
`mw_planned = 100`; an *operator* looking at both quotes may, and until now had
nowhere to put the answer. Each decision is written into the project's notes as
plain prose — the one class of note re-ingesting never regenerates — so the record
of a human overruling the data survives the next `sync`. Committed after every
answer, because somebody triaging forty rows will stop partway.

With no terminal (the console runs commands without a keyboard) it does the
automatic repairs, reports what needs a person, and stops.

`logic check` is in `LLM_COMMANDS` even though its default run is free, because
`--read 50` spends fifty calls and the console's gate gates command names, never
flags. `logic resolve` is gated too: it is the only command that rewrites fields
in bulk.

### What the stored data actually rests on

```bash
python scripts/measure_extraction.py
python scripts/measure_extraction.py --mutants
tracker ingest crawl --stale-prompt --limit 20
```

`gaps` counts empty fields. This counts *full* ones, and asks the harder
question: does the value have a sentence behind it.

Two numbers already existed and neither answers that. The evidence gate's 98.7%
exact-substring rate measures the quotes that exist — a statement about a
population it never looked at. And "66% of claims carry no quote" counts the
model's raw output, most of which the gate correctly demoted to 待确认, so it
reads as a scandal and is mostly the gate working.

The measure that matters splits values three ways, and the third is the only
defect. Measured before and after one full re-extraction:

| | before | after |
|---|---|---|
| quote-backed | 368 (49.2%) | 434 (52.0%) |
| 待确认 — no quote, **and the row says so** | 286 (38.2%) | 385 (46.1%) |
| confirmed, no quote, and nothing says so | **89 (11.9%)** | **11 (1.3%)** |
| total values measured | 748 | 835 |

The second row is the gate doing its job. A measure that lumped it in with the
third would report 286 successes as failures and hide the rows that actually
needed re-reading.

**All 89 came from prompts that no longer exist** — 61 from `extract-v1@8eb51f2a`
and 28 from `extract-v1@cef10fb4`, both predating migration `0007`, the migration
that added the column a per-field quote lives in. There was nowhere to record the
sentence when those rows were written, so they read as established ever since.
None came from the current extractor.

That is a fact about *history*, not about any row, which is why no per-row check
surfaced it: `tracker audit` asks whether a number could be true and `logic check`
asks whether two fields agree, and each of those 89 values passed both.

`--stale-prompt` is the re-read, and `source.extractor` is what makes it
possible: it has recorded which prompt produced every row since `0001` and
nothing had ever compared it to the current stamp. It serves from the article
cache by default, so it is a re-read rather than a re-fetch — the point is to
find out what a better prompt makes of the *same* article, and re-fetching would
confound that with the page having changed.

`--mutants` is the other half: it plants known faults in a throwaway copy and
counts what gets caught. It exists because this README and `HANDOFF.md` both
cited *"16 planted mutants, all caught"* as the evidence for `tracker audit`, and
no script, test or commit contained it — the run was manual, against a copy of a
live database nobody kept. A claim about detection that cannot be re-run is not
evidence, so now it can be.

**Why 14 remain, and why re-reading cannot clear them.** The write path is keyed
on `(project_id, url)`, but which project an article describes is re-derived from
the article on every read. So when a re-read routes to a different project — or
to none — the original row keeps its old source at its old vintage, orphaned. Of
61 URLs still stale after the run, 28 now return **no project at all**, 27 route
elsewhere, 2 are refused by the prose floor and 4 are 403s with no cached copy.
The Switch/Data Foundry acquisition story is the clearest case: the old prompt
built two campuses out of it, and the current one declines it entirely. Whether
that is the gate getting stricter or a regression is a judgement, so the rows are
reported and left alone rather than deleted.

### What a value is a value of

Hyperion (#10) carries three investment figures, and they are not a disagreement:

| figure | the sentence behind it | what it measures |
|---|---|---|
| $10B | *"the buildout of the infrastructure itself"* | this site |
| $27B | the Blue Owl campus joint venture | this site, later |
| $50B | *"more than $50B of investment to the region"* | roads, water, sewage, jobs |

Every merge policy in this schema exists to pick a winner among rival claims about
one quantity. These are three measurements of three different things, so picking a
winner is the wrong operation — and the row held the oldest and smallest of them
while its own notes read *"expanded to up to $50 billion"*.

Four qualifiers now travel with each claim, in `source.claim_meta`:

- **`scope`** — this site, a named tranche (`block:Phase 1`), the programme, the
  region, the operator's portfolio, or `unnamed`. `unnamed` is a correct and
  common answer; guessing `this_site` to be helpful is the error the axis exists
  to prevent.
- **`bound`** — `exact`, `approximate`, `at_least`, `at_most`. Prompt RULE 4 used
  to say *"500-700 MW → 500 (the LOWER bound; say so in notes)"*, destroying the
  range on purpose and routing it to prose nothing could read back.
- **`modality`** — `speculated` → `targeted` → `planned` → `contracted` →
  `achieved`.
- **`as_of`** — the date the claim was true, when it differs from the article's.

**One of the four axes failed its own test, and was not promoted.** The kill
criteria were written down before the corpus was re-read: an axis whose modal
value exceeds 95% is reporting a default rather than the article.

| axis | coverage | modal value | verdict |
|---|---|---|---|
| `bound` | 32.0% | `exact` 87.4% | passes |
| `modality` | 32.0% | `planned` 84.3% | passes, weakly |
| `scope` | 32.0% | `this_site` **96.9%** | **failed** |

`scope` failed the decisive test too. Across every `investment_usd` claim in the
database it returned 44 `this_site`, 4 `unnamed`, 1 `block:VA13` — and **zero
`region`, zero `programme`**. The two values that would have separated Hyperion's
$10B buildout from its $50B regional figure never fired once. So it stays
captured and measured, and nothing is built on it.

`bound` earned its place on the same row: *"roughly $27 billion"* reads
`approximate`, *"more than $50 billion"* reads `at_least`.

**Every axis is checked against the quote.** That is the whole design, and the
reason to expect these to carry information where `risk.severity` does not —
severity is a judgement no article states, so it reads `watch` on every risk in
the database, fully populated and carrying nothing. A bound is not a judgement:
the article either hedged the number or it did not, and the hedge is a word in the
sentence. `bound: at_least` needs one. `scope: block:<label>` must resolve to a
tranche on the record. `modality: achieved` is demoted to `targeted` outright when
the date is in the future — which is Hyperion's live defect, where *"an interim
milestone of 1.5 GW is being targeted by the end of 2027"* was stored as
`announced`, dated 2027-12-31, and counted as **reached** on the track strip.

The check runs against the *stored* quote, never the model's offered text.
`_verbatim_run` may have repaired the quote to the article's own words, and
checking the model's version would let it license a hedge by writing one into a
sentence nobody published — the fabrication route the evidence gate closed,
reopened one level up.

**A refused axis never costs the value.** `axis_gate` returns labels and is never
given the figure, so the worst case is a claim described no better than it was
yesterday, and a model labelling everything `at_least` to sound careful gains
nothing. Nothing reads the axes to choose a value yet, deliberately: `source_type`
became load-bearing before anyone measured it, and `confidence.find_conflicts` is
now documented as too risky to correct.

On screen they cost almost nothing. `bound` is one glyph on the number (`~5,000`,
`≥$50B`) rather than a column that would be empty on most rows and would break the
numeric alignment on the rest. `scope` and `modality` appear as chips in the
citation popover **only when they say something** — no chip reading "this site" on
a row where nothing is in dispute. That is what the previous round of added fields
got wrong: they rendered unconditionally, so they were noise everywhere.

And dates now print at the precision the source gave. `normalize.parse_date` has
always returned `day|month|quarter|half|year`, and its docstring has always said
why it matters; nothing outside that module had ever read it, so a year-only
"in 2024" rendered as `2024-01-01`. It now renders as `2024` — shorter, and
claiming less.

### Crawl order is not publication order

The measurement reports one more thing, and it is not fixed by re-reading.

`upsert.claims_by_field` ranks claims by `(confirmed, weight, fetched_at, url)`.
Most of this corpus is trade press, so ties on credibility are the common case,
and `fetched_at` is the moment the crawler happened to visit the page. Six stored
values are decided that way against publication order — Aligned Phoenix holds
65 MW from a 2017 article over 400 MW from a 2022 one, and Hyperion holds Meta's
superseded $10B over the $27B that replaced it, on a row whose own notes read
*"expanded to up to $50 billion"*.

Re-extraction made that sharper rather than better: it gave all three Hyperion
figures quotes, so they now tie three ways and crawl order still picks.

The date was already being collected — `discover` writes it to
`ingest_url.published_at` from the feed — and nothing downstream had ever read
it, because there was no column on `source` to carry it to. Migration `0014` adds
one and backfills it by URL (241 of 553 sources).

```bash
TRACKER_MERGE_BY_PUBLICATION_DATE=1 python scripts/measure_extraction.py
```

**The tiebreak is off by default**, and the reason is worth stating rather than
treating as caution. Turning it on takes the six inversions to zero, but they are
not uniformly improvements: #116 would move from 120 MW to 40 MW, because the
smaller figure was published a day later. Publication order is the more
*defensible* rule; it is not the rule that always yields the larger number. Run
the line above to see what it would change before deciding, and note that the
flag changes the policy — stored values move as each row is next written.

The same fact fixes a second bug. The prompt's `ARTICLE_DATE` was always
`unknown`: `extract_one`'s `published_date` parameter existed, the prompt
interpolated it, and no caller ever passed it. RULE 5 resolves relative timing
against it, so with the date unknown every "next year" was correctly forced to
null. Nothing was fabricated — the cost was silent, in schedule fields never
extracted for want of a value already in the database.

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

### Filling in capacity blocks on projects ingested before they existed

```bash
tracker backfill blocks --limit 25
```

A campus is rarely one thing. Lake Mariner is 378 MW under construction for
Fluidstack, 60 MW already serving Core42, and 145 MW of legacy capacity energised —
and until blocks existed the row could only say one number and one customer. Every
project ingested before migration `0009` still has no blocks, because turning an
article into blocks needs the article text rather than the schema.

This re-reads the stored articles for that one purpose. It is deliberately **not**
`ingest crawl --force`: a plain re-crawl re-extracts every scalar with a model that
behaves differently today than it did at ingest time, churning 227 rows and every
`updated_at` inside what is supposed to be a backfill. This writes one column,
`source.blocks`, and lets the ordinary rollup do the rest.

Costs one LLM call per article, keyed on URL rather than source row — 373 crawled
source rows are only 229 distinct articles, because 62 feed more than one project.
Most are already cached, so `--refetch` is only needed for the remaining 36. It is
resumable (an article whose blocks are stored is skipped) and idempotent (blocks are
rebuilt wholesale, keyed on `(project_id, block_key)`), so the sensible way to run it
is in tranches: `--limit 25`, look at what came back, then more. Filings sort first,
because per-phase tables are where blocks actually live.

**The part worth understanding before running it.** One article routinely describes
several campuses, and one URL is often already cited by several rows, so deciding
*whose* blocks these are is the whole job — and getting it wrong does not mislabel a
value, it moves megawatts into another campus's total. Two guards, both added after
the unguarded version wrote real wrong numbers into a copy of the database:

- **A row is matched on locality, never on the operator.** An earlier version wrote
  an 80 MW "Portland Expansion" onto eight STACK rows, every one of which matched on
  "STACK Infrastructure". A stated city that *disagrees* is now a veto, not merely a
  low score.
- **A portfolio article is split, but only when it is one.** A Core Scientific filing
  covering five campuses gave all six of its blocks to both the Denton row and the
  Dalton row, recording 588 MW twice. So each block is now routed by its own label —
  but only once some block in the article is found to name one row and not another.
  Demanding it unconditionally was tried and was worse: it emptied Lake Mariner,
  whose blocks are called "Akela" and "La Lupa", because a building is usually named
  after nothing in particular.

Both guards skip rather than guess. A missed block is a gap somebody can see; a
misrouted one is a wrong number nobody can.

**What the blocks then change.** Once a project has them, four `logic` rules stop
calling a partly-live campus contradictory — a campus with one tranche energised and
another going up is a campus, not a defect, and that was all 18
`energized_but_not_operational` findings. `tracker show` grows a block table above
the sources, the console drawer grows a Blocks tab, and Capex splits capacity between
the buyers the tranches name rather than giving a whole campus to one of them. A
project with no blocks behaves exactly as it did before, because no blocks means
nothing has been read rather than that the row agrees with itself.

**A block's capacity counts only if a quote named it.** A block whose `mw` came
through as 待确认 is still recorded and still shown — that is what the tier is for —
but it is left out of the campus total, and `reconcile` says which blocks it left
out. Summing an unconfirmed figure launders it into a total that then reads as
cited, and the first live tranche proved the cost: it raised one campus from 7 MW to
7,500 MW off a single unquoted block. Read the run's notes; `mw_planned` moving by
orders of magnitude is the thing to look for.

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
tracker queue                     # newest first, whole URLs, one id per row
tracker queue --feed ntt-newsroom # one source at a time
tracker queue --drop --id 1904    # the id is the handle; the URL is a link
tracker queue --drop --feed ntt-newsroom
tracker queue check               # ask every queued URL whether it is still there
tracker queue prune               # re-apply the filter to what is already queued
```

**The URL column is the whole URL.** It used to be `url[:60]`, which looked tidy
and was the most damaging thing in this output: the string on screen was a *prefix*
of a real link, so opening it gave "404 not found" and pasting it into `--drop
--url` matched nothing. A queue whose links all 404 is a queue nobody trusts. Every
row now carries its id, which is a short handle that cannot be mistaken for a link.
The order is newest-first, because a queue is read by a person deciding what is
worth a crawl while the crawl itself drains oldest-first.

The two maintenance commands exist because a queue is a promise that everything in
it is worth an LLM call, and two things break that promise quietly:

* **`check`** fetches every queued URL and reports which are gone. Conservative
  about what "gone" means: 404 and 410 are dead, **403 and 429 are not** — a
  newsroom answering 403 to a non-browser is what `ingest crawl --browser` is for,
  and on the live queue that was 55 URLs across seven publishers, which is to say
  the best-defended sources. `--drop` removes the dead ones and nothing else.
* **`prune`** re-applies the filter in `seed/feeds.toml` to rows that were queued
  under an earlier version of it. The filter is data and data gets edited; nothing
  ever re-applied it, so the queue accumulated everything that passed any *past*
  filter. On the live database that was 417 of 1,241 queued candidates — NTT
  marketing articles, DataBank compliance blogs, sponsored posts, and Meta's
  announcement of the winners of an AR effects contest. Report-only until `--drop`,
  and a row from a feed no longer in the config is left alone: commenting out a
  feed should not delete a queue.

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
tracker risks --uncited                # only the 待确认 ones
tracker risks --summary                # the kinds table alone
tracker risks confirm                  # read the articles behind the uncited ones
tracker list --risk transmission       # projects waiting on grid work
tracker stats                          # includes MW at risk per category
tracker exposure --by company          # capacity behind an obstacle, rolled up
```

```text
obstacles by kind
+----------------------+----------+----------+----------+----------+-------+--------+
| kind                 | projects | capacity | blocking | material | watch | quoted |
+----------------------+----------+----------+----------+----------+-------+--------+
| community_opposition |       21 |   18,836 |        3 |        4 |    14 |   9/21 |
| transmission         |       18 |   23,806 |        — |        2 |    16 |  16/18 |
| grid_capacity        |       15 |   22,176 |        — |        2 |    13 |   4/15 |
+----------------------+----------+----------+----------+----------+-------+--------+

transmission  18 project(s), 23,806 MW  (+4 with no cited capacity)
  material #1 Microsoft — Fairwater (Mount Pleasant, WI · 900 MW)
      Two 345-kilovolt upgrades outstanding before full load.
      "must complete two 345-kilovolt upgrades"
  watch    #29 NTT — Hillsboro (Hillsboro, OR · no capacity)
      Cooling draw questioned by the county board.
      待确认 uncited — the quoted sentence does not state this category
```

**Whether an obstacle is quoted is a column, not a footnote.** The old layout
printed every obstacle at the same weight and then admitted at the bottom that a
third of them rested on nothing quotable, which is the wrong way round: that is the
first thing a reader needs and it was the last thing they were told. The kinds
table carries it per category — `4/15` on `grid_capacity` is a different statement
about that number than `16/18` on `transmission`.

Categories map onto the PRD's obstacle list — `grid_capacity`, `transmission`,
`permitting`, `environmental`, `equipment_supply`, `chip_supply`, `financing`,
`offtake`, `community_opposition`, `water` — which is what makes the read-through
countable: MW blocked on `transmission` is a power and utility signal, MW blocked on
`offtake` or `chip_supply` is a cloud and semiconductor one.

Each obstacle shows the verbatim quote behind it, and one that has none says
`uncited` rather than sitting silently beside the evidenced ones — and says
*which* kind of uncited, because the answers differ. "Quoted nothing for it"
means go and find a source. "The quoted sentence does not state this category"
means the sentence is real and filed under the wrong heading, which is a
correction to a source you already have.

**An obstacle whose quote fails is kept, not deleted.** Until migration 0012 it
was the one thing in the ingest path that still went on the floor, and that fell
hardest on the field this database is worst at: no press release names its own
blocker, so an adversarial second source is the only thing that ever records one.
The failed quote is still never stored beside it. Unconfirmed obstacles count
toward the MW sums, with the count disclosed in the footer — but they cannot
quietly become `project.blocker`, which is one of the twelve tracked fields: a
confirmed obstacle always outranks an unconfirmed one, and if an unconfirmed one
does fill the column it is marked 待确认 there too, so confidence and the 9-of-12
count are never told it was cited.

#### Settling them: `tracker risks confirm`

Counting an unevidenced obstacle and marking it 待确认 are both honest, and together
they were unsatisfying: nobody had ever gone back to check which reading was right.
This does, one model call per obstacle, worst first.

**With the whole article, not the excerpt.** The excerpt is the fragment chosen by
the extraction that already failed to find the sentence, so re-reading it would
mostly reproduce the first answer; the article comes from the crawl cache. The
project, the obstacle, and *every other obstacle on the row* go with it — half
these rows are `quote_off_target`, a real sentence filed under the wrong category,
and that is invisible without seeing what the other categories already claim.

**What makes the answer trustworthy is not the model.** A returned quote is
accepted only if it is verbatim in the article, checked with the same matcher the
extraction path's evidence gate uses, and only if the sentence carries wording for
the category it is filed under. A paraphrase is refused. A real sentence about the
wrong thing is refused. The obstacle then stays exactly as it was — so the worst
case of running this is that it cost a call and changed nothing.

Three outcomes: **confirmed** attaches the quote and clears the 待确认 mark;
**refuted** marks the obstacle `superseded`, dropping it out of the open counts
without deleting the record of having believed it; **unclear** writes nothing and
is the honest majority answer. `--dry-run` judges at full cost and writes nothing.

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

### What no article says: `tracker infer`

The PRD asks for two things no document contains — 你还需要分析项目可能遇到的困难,
and 接下来出现什么信号，才可以证明项目正在继续推进. Those are analysis, not
extraction, and this is the command that does them.

```bash
tracker infer 2 3        # one call per project
tracker --json infer 2   # the same, as structure
```

```text
#2 xAI — Colossus  Memphis, TN ─────────────────────────────────────────────

What could obstruct this — 可能遇到的困难
  ████· 0.75  financing material
        Only $6B is announced against a planned 2,000 MW buildout; hyperscale
        capex typically runs $10–15M per MW…

What would show it is still moving — 推进的信号
  ████· 0.75  Announcement of a new xAI debt facility earmarked for Colossus
        Would close the financing gap that gates the full buildout.

Inferred by deepseek-v4-flash from this row's 5 recorded obstacle(s), its milestones and
its gaps. Not stored, not evidence — a judgement drawn from the facts.
```

Two questions, two headings, ranked by the model's own confidence with the figure
beside the bar. The previous layout was a four-column table whose widest column was
free prose, which wrapped to two characters a line on a narrow terminal and made
the two questions indistinguishable.

**It cannot write a fact.** Only obstacles and next-signals are accepted; anything
quantitative the model volunteers is dropped and reported, because 关键数字 must come
from a document. That is enforced in code — `infer.INFERABLE` — not by asking the
model nicely.

In the console, every project's drawer has an **Inferred analysis** panel with a
**Run analysis** button. It is a button and not an automatic panel, which is a
deliberate difference from the AI overview above it: the briefing is cached by
content, so a row is paid for once and reopening is free, while an inference is
never cached or stored and its value is that somebody asked for it against the row
as it stands. One click, one call, one answer.

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

1685 tests, about two minutes. **A fresh clone with no API key and no network access
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

### The JSON contract is enforced in code, not by the provider

This started as a MiniMax constraint and survived the move to DeepSeek as a
choice. On MiniMax, `response_format` — both `json_object` and `json_schema` — was
**silently ignored**: no error, no warning, just prose-wrapped JSON as though you
had never asked. Anything claiming to enforce a schema against those models
(including Crawl4AI's `LLMExtractionStrategy` `schema=` parameter) was promising
something the provider did not do.

DeepSeek genuinely supports `response_format={"type": "json_object"}`, so the
constraint is gone — but its own docs attach two conditions that make it the wrong
foundation here: the literal word `json` must appear in the prompt (ours say
`JSON`), and the endpoint *"has a probability of returning empty content"* in that
mode. An extraction run that occasionally returns **nothing** is worse than one
that returns prose-wrapped JSON, because the reader below recovers from the second
and cannot recover from the first. `TRACKER_DEEPSEEK_JSON_MODE=true` turns it on
for anyone who wants to measure it; it is off by default.

So `tracker/llm.py` owns the contract: strip `<think>` blocks and code fences,
brace-scan for the outermost object, repair the malformations these models
actually emit (trailing commas, smart quotes, a dropped `{` on the first object
inside an array), validate, and allow **exactly one** corrective retry. Cost per
URL is bounded at two calls.

Three DeepSeek details, two of them the reverse of what this code used to do:
`max_tokens`, not the `max_completion_tokens` MiniMax wanted; thinking is a
**request flag** (`thinking={"type": "enabled"|"disabled"}` with
`reasoning_effort`) and is actually honoured, which is why there is no longer a
separate no-think model in the roster; and reasoning may return either in its own
`reasoning_content` field or inline in `<think>` tags, so both shapes are handled.
One platform, one host — `tracker ingest crawl --check` confirms the key in one
call.

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

**A quote the model edited is repaired, not discarded.** Exact containment was
throwing away real figures. Measured over 131 evidence quotes from 8 cached
articles, 33 failed the substring test, and the dominant cause was not
fabrication: the model *resolves references* while quoting. The article says "The
campus is a single building comprising two data halls that serve as a 16.5 MW data
center"; the model writes "The **Austin** campus is a single building…",
substituting the site name for the definite article. Helpful for a reader, fatal
for a substring test, and it was costing capacity and capex figures that were
genuinely published.

So when containment fails, `_verbatim_run` finds the longest stretch of the quote
that really is in the article, and the **article's own words for that stretch are
what get stored** — never the model's edit. Two floors keep it honest: the run
must be at least 40 characters and at least half the quote, tuned against a
negative control in which every sampled quote was also tested against an unrelated
article. Nothing crossed. Acceptance went from **75% to 95%** with zero false
positives.

The span is then widened to its sentence boundaries, because a recovery that drops
the number is worthless — one observed run ended at "…the offering was $", the
article having wrapped the line inside the figure, so the quote was real but no
longer evidenced the value and `_stated_in` discarded it anyway. Widening only
ever adds the article's own text, so it cannot introduce a word nobody published.

This is why the anti-fabrication guarantee survives the change: `_stated_in` runs
against the *stored* text, so a value still has to be asserted by a sentence
somebody actually wrote. Risk quotes take the same path, and their category check
then runs against the recovered text — strictly harder than checking the model's
own phrasing, which could otherwise carry the category's keyword in a word the
article never used.

**A page with nothing to quote is refused before the call.** A fetch can return
200 and 600 characters of navigation furniture, and nothing checked. The model
got a teaser card, invented a plausible project from the title, and then *every*
quote failed together — which is what a wall of `company / city / county / state
/ phase` rejections in one run actually means. The row was written anyway,
because identity fields are restored from the ungated values.

The check is on **prose**, not raw length, because raw length cannot draw the
line: a real Meta 8-K excerpt is 590 characters and an Applied Digital teaser
card is 598, and the shorter one is genuine. Counting only characters in lines
long enough to be sentences separates them cleanly — that 8-K scores 553, and all
fifteen cached teaser cards score 74, being one site-wide banner line about a
different campus. Measured over 544 cached articles, a floor of 200 refuses 20 of
them and cannot fire on the corpus that matters: the thinnest of 246 trade-press
articles scores 3,025, and only one of 115 SEC filings falls below.

Refused pages get their own `ingest_url` status, `thin_content`, rather than
`no_project` — that one means a model read the page and found nothing, and
discovery never retries it. Nothing read this page, so `tracker queue --failed`
lists it (grouped by host, which is how eight identical teasers read as one
pattern instead of eight silent charges) and `--retry-failed` picks it up if the
site later serves the body.

**When it refuses, it says what it refused.** The warning used to name the field
and nothing else, which is not enough to judge whether the gate is too strict:

```
evidence quote for 'mw_planned' is not in the article (best run 34 of 200 chars, 17%);
  offered: 'Vantage has begun construction on a data center campus in Port Washington…'
```

**Checking these numbers rather than trusting them:**

```bash
python scripts/measure_evidence_gate.py
```

Re-runs all three measurements — the prose floor, how often recovery is needed at
all, and the negative control — against whatever corpus you have. On the current
one: **98.7% of 1,250 stored quotes are exact substrings of their own article**,
recovery is needed for 0.5%, and **0 of 3,064 quotes crossed into an unrelated
publisher's article**. The first number is the one that matters before loosening
anything: the matching is not what is refusing values.

The control taught one thing worth knowing. "Unrelated" has to mean a different
*publisher* — pairing naively reported three crossings, all of them a single
company's boilerplate recurring in its own filings or its own site. Those are
reported separately now, because they name the gate's genuine blind spot rather
than a threshold that needs raising: boilerplate is verbatim everywhere a company
publishes, so quoting it proves the sentence was published and nothing about
which site it describes. That is what `tracker logic check --audit` is for.

**What the gate refuses, it keeps and flags.** A value with no quote behind it is
待确认, not deleted (migration 0006), and since migration 0012 so is a *risk* —
which was the last thing in the ingest path that still went on the floor. The
quote that failed is never stored beside it, but the reason is
(`vocab.UNCONFIRMED_REASONS`), because the tier covers cases that ask for
opposite work: `no_quote` sends you to find a source, while `out_of_scale` — a
programme-wide total quoted in an article about one campus — sends you to correct
a figure you already have. Going looking for a citation for that one would find
one, and it would still be the wrong number.

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
- The JSON contract is enforced in code rather than by the provider (above), so
  the parse/repair/retry loop has to live somewhere testable.
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

### Provenance is per field, not per source

`evidence_gate` has always known the verbatim sentence behind each individual
value — that is what lets a value through. But `_excerpt()` then concatenated the
best three into one ≤500-character `source.excerpt` and the field association was
thrown away. Nothing downstream could answer "which sentence says this project is
900 MW?", so `tracker show` printed the same paragraph under all twelve fields, as
though one excerpt evidenced every one of them.

Migration 0007 adds `source.quotes`, a JSON object keyed the same way as
`source.claims`, and `gaps.provenance()` reads the pair. Three details are
load-bearing:

* **The quote comes from the source whose value won the merge**, not from the
  strongest source that mentions the field. For a field two sources disagree on
  those are different rows, and quoting the loser would print a sentence stating a
  figure the project does not have. `provenance()` therefore asks
  `upsert.claims_by_field()` for the same ordering the write path used rather than
  re-deriving it.
* **A fallback is labelled as one.** The 264 citations recorded before the
  migration have no per-field quote and never will — those words were not written
  down. They fall back to the source excerpt with `quote_is_exact: false`, and
  every surface says which it is showing.
* **The gate now prefers the quote that states the value** over the one the model
  filed the field under. It already accepted a value evidenced under any label,
  because models are unreliable bookkeepers; the same unreliability means a
  labelled quote need not contain the number it was filed against. Harmless while
  these only fed a three-quote blend, not harmless once one sentence sits beneath
  one value.

### `defaulted` is not 待确认

A fifth tier, and the distinction is not pedantic. `phase` is NOT NULL, ingest
paths deliberately omit it from `source.fields` when no source states one, and the
column falls back to `announced`. Reporting that as 待确认 asserted that a source
had claimed it and failed to prove it. Nobody had claimed anything. On the live
database 37 values were being mislabelled that way.

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

The same reasoning extends to **tertiary domains** (`TERTIARY_DOMAINS`, today
just wikipedia.org): a Wikipedia citation is kept, quotable and worth its floor
of 1, but it never counts toward domain independence, agreement, or conflict.
Its paragraph on a campus is the trade-press coverage one step removed, so
letting it corroborate would launder aggregation into independence — and letting
it *conflict* would dock a row for Wikipedia's staleness rather than for a real
disagreement between reporters.

`updated_at` means "a field changed". `last_verified_at` means "an operator says
this row is right" (PRD open question Q4), and it is the only path from a single
source to 3.

### Five schema additions beyond the PRD's three tables

Each unblocks a stated PRD requirement that the three tables cannot hold:

- **`source.claims`** (JSON) — without it, Q2's "keep both conflicting values" has
  nowhere to keep them and the confidence agreement rule has nothing to compare.
  `source.fields` is *derived* from it, so the two can never disagree.
- **`source.extractor`** — which extractor and which prompt version produced this
  row (`crawl:extract-v1@3f2a91c4:deepseek-v4-flash:httpx`). Without it, "which prompt
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
- The crawl path has been run live against a provider, not only fixtures: an initial
  live run surfaced four defects (fixed in `de4821c`), and `tracker enrich` was
  verified live on project #93 (OpenAI Stargate, Abilene).
- `project.country` is a dead column in v1 (`CHECK country='US'`), kept for
  forward compatibility. No CLI filter exposes it.
