# Ingesting

Getting articles in: the API key or a local model, the one-command loop, the operators we are missing, depth versus breadth, operator press releases, optional search, and SEC filings.

Part of the [dc-tracker documentation](README.md).

---

## Setting the API key

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

## Or a local model, on any command that spends LLM calls

Every command that costs LLM calls takes the same flag:

```bash
tracker sync --llm-provider ollama          # this run, free and local
tracker enrich 93 --llm-provider deepseek   # this run, the API — the default
tracker ingest crawl --check --llm-provider ollama   # verify the local side
```

The local provider is an [Ollama](https://ollama.com) server —
`TRACKER_OLLAMA_BASE_URL`, loopback by default — serving the model named by
`TRACKER_OLLAMA_MODEL`. Setting `TRACKER_LLM_PROVIDER=ollama` in `.env` makes it
the default for every run on that machine, including the console's briefing
panels, which have no flag of their own.

**DeepSeek stays the default deliberately.** Stored values carry no record of
which model produced them, so a silent switch of model is a quality change
nothing downstream can detect. Going local is a decision — per run with the
flag, per machine with the setting — never an accident.

Three things behave identically whichever provider answers, because they live
outside the provider: the JSON contract (parse → repair → validate → one
corrective retry), the thinking filter (a local model's `<think>` deliberation
is stripped the same way DeepSeek's `reasoning_content` is), and the tier
policy (extraction and `infer` reason, the drawer's briefing does not — on
Ollama the effort dial collapses to think-or-not).

Two Ollama-specific settings are worth knowing about, and one is load-bearing.
`TRACKER_OLLAMA_NUM_CTX` (default 32768) rides on every request because
Ollama's own default context is a few thousand tokens and **input beyond it is
truncated silently** — an extraction would quietly read half the article and
the evidence gate would reject quotes the model never saw.
`TRACKER_OLLAMA_TIMEOUT_S` (default 600) is far above the API's, because a
local model generates at tens of tokens a second and a timeout that fires
mid-generation wastes everything already computed.

The flag is named `--llm-provider`, not `--llm`, because `--llm/--no-llm`
already means "let a model decide at all" on the three resolve commands — the
name predates there being more than one model to choose.

One caveat worth saying out loud: the tracked-field prompts were tuned against
DeepSeek. A local 27B is capable of them, but nobody has measured its
extraction accuracy here — run a few articles with `--llm-provider ollama
--dry-run` and read what comes back before pointing a whole `sync --full` at it.

## One command for the whole loop

```bash
tracker sync         # keep current
tracker sync --full  # everything, including the operators we have no rows for
```

Up to seven phases. A bare `tracker sync` runs five of them:

| | phase | what it does | on by default |
|---|---|---|---|
| 1 | discover | poll the feeds, sweep archives, search -> queued candidates | yes |
| 2 | prospect | chase operators the roster says we have no rows for | `--prospect N` |
| 3 | extract | crawl the queue -> new projects | yes |
| 4 | refresh | re-read existing projects' sources -> updated fields | yes |
| 5 | enrich | throw every retrieval method at the thinnest rows we hold | `--enrich N` |
| 6 | settle | re-derive what the citations imply, rescore confidence | yes |
| 7 | list | show the result | yes |

Needs the API key set as above. When a search key is also configured (see "Search"
below), the discover phase runs LLM-proposed web searches automatically —
`--search 0` skips them for a run.

Phases are numbered against the plan the run actually chose, so a default run says
`1/5 … 5/5` and `--full` says `1/7 … 7/7`. A phase that was not asked for is absent
from the count rather than printed as skipped — "2/7 prospect — skipped" on every
ordinary run only trains you to ignore the labels.

**Why the expensive two are off by default.** `prospect` and `enrich` are the phases
that can spend without a ceiling in sight, and the run people do daily is the cheap
one. `--full` is the deliberate version:

```bash
tracker sync --full                 # prospect 5, enrich 10, --deep, --retry-failed
tracker sync --prospect 3           # keep current, and chase three missing operators
tracker sync --enrich 20            # keep current, and complete twenty thin rows
tracker sync --full --prospect 1    # --full, but one operator: a number given beside
                                    # it is never overruled
```

Every phase that costs LLM calls is capped separately — `--limit`,
`--refresh-limit`, `--prospect`, `--enrich`/`--enrich-budget` — because they buy
different things. `--limit` buys breadth, `--refresh-limit` buys currency,
`--prospect` buys coverage of operators we are blind to, and `--enrich` buys depth
on rows already here. One budget spread across all four would silently favour
whichever phase ran first.

```bash
tracker sync --limit 25 --refresh-limit 25 --refresh-days 14
tracker sync --dry-run          # write nothing; see the note below on cost
tracker sync --browser          # escalate blocked pages, needs the 'crawl' extra
tracker sync --skip-discover    # work the existing queue only
tracker sync --skip-refresh     # new projects only
tracker sync --skip-derive      # leave the derived values alone
```

**`--dry-run` writes nothing, and that is all it promises.** It is not a free
preview. The discover phase still polls the feeds and still runs its searches; the
prospect phase still searches and still asks the model for campus names; the
refresh and enrich phases still fetch articles and still put them through the
extractor, because "what would this run change" cannot be answered without doing
the reading. What it guarantees is the transaction: every write is rolled back.

The one phase a dry run genuinely does not spend on is **extract**, and for a
mechanical reason rather than a designed one — the candidates phase 1 found were
rolled back with everything else, so there is nothing queued to read. The run says
so rather than reporting an empty queue as good news.

The refresh phase is what keeps data current rather than merely growing: articles
get edited, and a campus that was "announced" last quarter is under construction
now. Re-reading a known citation updates every field it supports. It deliberately
bypasses the article cache — serving a cached copy would guarantee the answer is
"nothing changed".

The settle phase is free and deterministic. Every derived value — county,
coordinates, capacity block rollups — is a function of the row's citations and is
recomputed only when something writes to the row, so a run that added sources and
stopped there would leave them stale. Confidence is the same shape of cache.
Neither spends a call or a request.

## The operators we do not have

Discovery is source-driven: it finds what was published. That cannot answer "who are
we missing", because an operator nobody wrote about last month looks exactly like an
operator that does not exist. Measured on the live database: 300 projects, 102
distinct company spellings, and **no Nebius row at all** — a top-five AI cloud with
a Kansas City campus. CoreWeave had none under its own name either, appearing only
as a tenant inside two Core Scientific projects.

[`seed/operators.toml`](../seed/operators.toml) is the missing half of that
comparison: the operators this database is supposed to know about, hand-written and
checked in, including the private ones that can never appear in
`edgar-companies.toml` because they have no CIK.

```bash
tracker coverage                    # who is absent, thin, covered
tracker coverage --kind neocloud    # one class
tracker coverage --covered          # list the ones we do have, too
```

A read: it spends nothing and runs anywhere. Three answers per operator — `absent`
(no rows), `thin` (one row, or rows nobody has sized), `covered`. It also prints the
reverse, companies with projects that no roster entry claims, which is how the file
grows: each is either an operator to add or a spelling to alias.

Matching folds the spellings one operator files under. Both sides are normalized
through `dedup.company_key`, stripped of the words every data center company shares,
then compared as token subsets — so "Nebius" finds a row stored as "Nebius Group
N.V." and "Aligned" finds "Aligned DataCenters" with no alias needed. The aliases in
the file are for what no string rule can reach: renames (RagingWire became NTT),
acquisitions (DuPont Fabros is Digital Realty), and single-site LLCs filed under
their own names. A match made by the loose rule alone prints with a `~`, so a wrong
fold is visible rather than silent.

Then go and get them:

```bash
tracker prospect                    # the five worst gaps
tracker prospect Nebius CoreWeave   # these two, now
tracker prospect --dry-run          # the queries and the archive hits, nothing queued
tracker prospect --extract 0        # queue only; let the next sync read them
```

Three lead sources per operator, cheapest first — the same ordering `enrich` uses:

| source | cost | what it contributes |
|---|---|---|
| queue | free | URLs already in `ingest_url` that name the operator and were never read |
| archive | fetch | sitemap URLs whose slug or headline names the operator. No key, and reaches back years |
| search | search + one LLM call | four templated queries, plus one per US campus a model proposes |

**The queue source is not redundant, and it is the one that surprised us.** The
extract phase is depth-first on purpose: it spends each LLM call on an article
covering a project already tracked, because a second source fills fields one
article cannot. An article about an operator we have *no* rows for matches no known
project, so it sorts last behind a permanent supply of better candidates and can
wait indefinitely. That is one way a database ends up holding a Nebius URL and no
Nebius row. Inside `tracker sync --prospect N`, these URLs are moved to the front
of the extract phase's list for exactly that reason.

None of the three is required. Without a search key it reads the queue and the
archives; without an API key it runs the templated queries only. Both are
configurations rather than failures, and the command says which one it is in.

**The model's campus names are leads, never facts** — the same asymmetry the search
path rests on. They are only ever used to build a query. If it invents a site, the
search finds nothing and the run moves on; a row appears only where an article was
fetched and the evidence gate found a verbatim quote, exactly as on every other
path. `tests/test_prospect.py` asserts that directly.

The run ends by re-measuring coverage and printing `Nebius 0 -> 2 row(s)`, because
"queued 40 URLs" says what it spent and only the second number says what it got. An
operator that gained nothing is not necessarily a failure: the articles may exist
and say nothing quotable, which is what the evidence gate is for.

**Nebius also had a second, unrelated hole.** It was in `edgar-companies.toml` from
the start and produced no filings at all, because that file asked every filer for
10-K, 10-Q and 8-K — and Nebius Group N.V. is a Dutch foreign private issuer, which
files 20-F and 6-K. A `[[company]]` entry may now carry its own `forms = [...]`, and
Nebius does. Two independent blind spots, one missing operator, and the roster is
what made either of them visible.

## Depth versus breadth

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

## How long a run takes, and the one knob that changes it

The model is most of the elapsed time. A fetch is seconds and an insert is
milliseconds; an extraction with reasoning on is tens of seconds, and until
recently they went out strictly one at a time — while the fetches ahead of them
already ran four abreast.

`TRACKER_LLM_CONCURRENCY` (default 6) is how many extractions are in flight at
once. One article's extraction knows nothing about another's, so this changes how
long a run takes and **nothing about what it stores**:

- The prompts are untouched, so every quote is still checked against the article it
  came from.
- The token spend is identical. These are the same calls overlapped, not a bigger
  batch.
- Results are consumed in input order, so the run's log reads as it always did.
- Writes stay on one thread, keeping the commit-per-article checkpoint — so an
  interrupted run still resumes where it stopped.

**Putting several articles in one prompt would be cheaper and is not on offer.**
The evidence gate verifies a quoted sentence against the source it is filed under;
with five articles in one call the model can attribute article 3's sentence to
article 1's project, producing a quote that checks out against the batch and not
against the citation. That is a silent failure in the one mechanism the whole
dataset rests on.

Two things to know before raising it:

- **The ceiling is the provider's rate limit**, not this process. Every worker that
  meets a 429 pays the backoff, so past some point more workers is slower. Measure
  with `scripts/measure_extraction.py` rather than guessing.
- **A local model wants 1**, which is what `TRACKER_OLLAMA_CONCURRENCY` defaults
  to. Local inference is compute-bound: a second concurrent request queues behind
  the first and competes for the same VRAM. At 1 the code takes its original serial
  path, with no thread pool at all.

## Reaching back for older projects — no API key

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
[seed/feeds.toml](../seed/feeds.toml).

This is the recommended way to fill the database. Search (below) is optional.

## Operator press releases, and why they matter most

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
[seed/feeds.toml](../seed/feeds.toml) rather than failing every run.

## Search: an optional alternative that needs keys

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

## Why DataCenterDynamics is discovery-only

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

## Going and getting one named campus

```bash
tracker point "Monarch Compute Campus"                 # search for it, read what turns up
tracker point "Monarch Compute Campus" --url URL ...   # read links you already have
tracker point "Monarch Compute Campus" --dry-run       # say what it would do; costs one call
```

Matched to a row we already hold, it runs `enrich` on that row. Not matched, it
searches for that name specifically and the ordinary write path builds the row
with its citations.

**Identification happens after reading, not before.** With `--url` there is no
point paying to ask "which row does this typed string look like" when the article
is about to state the operator and the town outright — so each article is
identified from *its own extracted identity*, once it has been read, and the
answer is acted on rather than printed.

That reversal fixes a real failure. A dedup key cannot express *this town is in
that county*, so an article naming Point Pleasant produced a different key from
the row already stored under Mason County, and one campus became two rows —
`nscale|city:point pleasant|WV` beside `nscale|county:mason|WV`. Asking the
question from a name could not have caught it; asking it from the article, which
names both the operator and the place, can.

The rails did not move. The model still chooses from a deterministic shortlist,
still cannot answer with an id it was not offered, and anything below the
confidence floor still means a new row — because a wrong "no" makes a duplicate
that `tracker duplicates` finds and `tracker merge` fixes, while a wrong "yes"
attaches an article to another campus's history and nothing detects that.

Routing is **not** a merge. It attaches this article's claims to that row and says
so in the notes; the other identity stays unclaimed, and `tracker merge` is still
what makes the decision permanent. Identity fields are `FILL_ONLY`, so a routed
article cannot rename a row or move it — though it can fill a field that was
empty, which is how the county row learns which town it is in.

Batch crawls are unchanged: `ingest crawl` and `sync` have nobody asking about a
particular campus, so the dedup key still decides alone and a cross-granularity
split is still detected and disclosed rather than resolved.

## Completing one project, cost no object

```bash
tracker enrich 93 --dry-run     # what it would read, without writing
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

**A last stage settles what harvesting just put into dispute.** Adding sources is
what *creates* contested fields, and until now the run ended by handing that to a
sort — quote-backed first, then source weight, then date — the same default
`tracker logic conflicts` exists to override. So the run's final step sends every
still-contested field (not only what this run added) to the reasoning model:
`--skip-settle` turns it off, `--dry-run` covers it like everything else, and a
missing reasoning-model API key degrades to a skip rather than losing the articles
this run already paid to read. Settlements and refusals both print, because a
refusal is the answer on a field two publishers genuinely disagree about.

## SEC filings: the publisher that cannot lock us out

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

Which companies are read is [seed/edgar-companies.toml](../seed/edgar-companies.toml),
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
