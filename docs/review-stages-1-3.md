# Stages 1–3 under review: what is true, what is not, and what to do

Measured against `data/tracker.db` on 2026-08-12, HEAD `33ac124`. Every number
below is reproducible with `scripts/measure_stages123.py` (new, read-only) and
`scripts/measure_extraction.py` (existing). Nothing here has been applied.

---

## 0. The finding that reorders the whole list

**The current extract prompt is `extract-v1@4ea77aad`. Not one stored source was
extracted under it.**

```
current extract prompt: extract-v1@4ea77aad

   2387 sources  extract-v1@678e5557          <- 93%
    116 sources  extract-v1@d3f5dce2
     34 sources  extract-v1@81f38fee
     32 sources  extract-v1@5d479a68
      5 sources  everything else
```

`scripts/measure_extraction.py` states the consequence itself: *"Every defect
sits on a superseded prompt. The gate is working; this is a remediation job, not
a bug."*

This matters because **four of the eight stage-3 problems already have prompt
rules written for them** — generation-is-not-a-block, `operational` requires the
site to be running, county-not-city, one-event-per-article. Those rules have
never touched a stored row. Before writing a new rule, the question is whether
the existing one works, and that costs a re-extraction, not a prompt edit.

Re-extraction is not free: only **250 of 1,928 distinct source URLs (13%)** have
cached text, so a full pass is ~1,678 refetches plus ~1,928 model calls.

---

## 1. Confirm or correct

### Stage 1 — DISCOVER

#### 1(a) Keyword-only relevance — **mechanism confirmed, evidence misattributed**

Confirmed: `discover.FilterSpec.matches` ([discover.py:129](../tracker/ingest/discover.py#L129))
is a two-tier substring test, and the call site passes only
`f"{candidate.title} {path}"` ([discover.py:474](../tracker/ingest/discover.py#L474)).
It never sees the body.

Quantified — this is the strongest number in the review:

```
URLs that reached an LLM call : 4854
produced no project at all    : 2381  (49%)
```

Sample `no_project` titles show the failure exactly: *"expanding our third party
fact checking program in india"*, *"expanding end to end encryption protects
fundamental human rights"*. Both are Meta-newsroom posts that pass because the
feed is `topic_implied` and `expanding` is a signal term.

**Where the brief is wrong.** The Entergy and Everest sources are not off-topic
articles that leaked in. Every one is genuinely about Hyperion:

| src | url | what it is |
|---|---|---|
| 1112 | nola.com/…/meta-facebook-data-center-electricity-louisiana | Meta's campus and its power |
| 1124 | entergy.com/blog/what-theyre-saying-data-center-boom… | the campus |
| 558 | sec.gov/…/etr-20260630.htm | Entergy 10-Q discussing the campus |
| 668 | wwno.org/…/advocates-cite-lack-of-transparency… | the campus |
| 669 | measuredai.substack.com/p/meta-gw-louisiana-data-center | the campus |

They are attached to #10 **correctly**. What went wrong is that the extractor
lifted generation assets and a neighbouring campus name out of them. That is
**3(c)**, not 1(a).

The genuine 1(a) evidence on #10 is aggregator pages:
`itkservices3.com/background/data_centres_usa` (src 1413),
`irecruit.co/projects/data-center-construction-project-tracker` (src 2265),
`networkenvironments.com/weekly-digest…` (src 3423). Src 1413 is where the
phantom 1,500 MW "Phase 1" comes from.

One cheap lever the brief misses: `Candidate.content` already carries the full
body when a feed syndicates it ([discover.py:168](../tracker/ingest/discover.py#L168)),
and the filter does not consult it.

#### 1(b) `published_at` only from feeds — **confirmed, and the causal chain is proven**

Coverage: **6 of 61** on #10; **326 of 2,758 (11.8%)** database-wide.
`upsert._published_at` ([upsert.py:193](../tracker/upsert.py#L193)) reads
`ingest_url.published_at`, which only `discover` fills, and only from a feed.

The $10B chain, verified live by fetching both pages:

| src | value | type | published | fetched |
|---|---|---|---|---|
| 661 | **$50B** | government_doc | **2026-07-13** | 2026-08-09 11:39 |
| 1116 | $10B | government_doc | **2024-12-04** | 2026-08-10 05:35 |

Equal weight, both quote-backed, so the sort falls through to recency
([upsert.py:323](../tracker/upsert.py#L323)). `fetched_at` picks the $10B page by
**eighteen hours of crawl order**. `published_at` picks the $50B page by
**nineteen months**. Exactly as the brief says.

**One correction to scope:** the tiebreak already exists.
`Settings.merge_by_publication_date` ([config.py:283](../tracker/config.py#L283))
and `_Claim.recency` ([upsert.py:224](../tracker/upsert.py#L224)) are built and
tested; the setting is off and starved of data. This is not "build a tiebreak",
it is "feed the one that exists".

**The exposure is far larger than the existing harness can see:**

```
stored values with a same-weight, same-confirmation rival that disagrees : 506
  of those, both sides carry a published_at                              :  67
  => visible to measure_extraction.py section 3                          :  67
  => decided by crawl order, invisible today                             : 439
```

by field: name 140, phase 79, mw_planned 68, company 55, investment_usd 39,
first_announced 38, blocker 31, customer 21, mw_built 18, expected_online 17.

#### 1(c) No cap, no scoring, no supersession — **confirmed**

```
#2    78 sources,  9 decide at least one field  (12%)
#10   61 sources,  8 decide at least one field  (13%)
#1    48 sources,  6 decide at least one field  (12%)
```

Roughly seven in eight citations on the heaviest rows are inert. No supersession
concept exists anywhere in the schema or the merge.

### Stage 2 — FETCH

#### 2(a) No date extraction from content — **confirmed, but the stated remedy does not exist**

Confirmed: no JSON-LD, OpenGraph, `<time>` or byline parsing anywhere in
`fetch.py`.

**The premise that the HTML is already cached is false.** The cache stores
converted text, not HTML:

```
files: 585
  datePublished             0
  article:published_time    0
  <time                     0
  application/ld+json       0
```

The metadata is discarded before `_write_cache` runs. **A backfill over
`.cache/articles/` cannot recover a single date.**

Feasibility of doing it properly — 10 live Hyperion URLs sampled:

```
machine-readable date found on 7/10 pages   (json-ld 5, <time> 2)
```

Both government_doc pages in the reference case resolve, which is what makes the
fix decisive rather than merely tidy.

Free deterministic partial: dates embedded in URL paths.

```
published_at already set  : 326 (11.8%)
recoverable from URL path : 175 ( 6.3%)
=> coverage after URL pass:      18.2%     (#10: 6 -> 15)
```

Worth doing — it costs twenty lines and no network — but note it **does not fix
the reference case**: neither `opportunitylouisiana.gov` URL carries a date.

#### 2(b) Cache never overwrites — **right symptom, wrong mechanism**

`_write_cache` ([crawl.py:2242](../tracker/ingest/crawl.py#L2242)) **does**
overwrite; `write_text` truncates.

The trap is on the read side. `_split_cached`
([crawl.py:2212](../tracker/ingest/crawl.py#L2212)) serves any cached URL and
removes it from the fetch list, so the write never happens. `force=True` only
bypasses `already_done`, not the cache. A poisoned body is therefore served
forever.

```
21 of 585 cached bodies are below the 200-char prose floor
180 ingest_url rows sit at status='thin_content'
```

Those 21 can never recover through `--retry-failed`.

Two notes. The `fetch.py:317` citation is about something else — it is the
`JS_SETTLE_S` comment explaining why the browser rung waits 3 s before reading.
Related in spirit, not the same defect. And the worst case is already mitigated:
`sync`'s refresh phase passes `cache_dir=None` deliberately
([cli.py:5723](../tracker/cli.py#L5723)).

#### 2(c) `MIN_PROSE_CHARS` character-based — **confirmed, and already a recorded decision**

Lives at [crawl.py:285](../tracker/ingest/crawl.py#L285), not in `fetch.py`. The
comment above it already documents the Chinese-language bias, the measurement
behind the number, and the decision to accept it: three Chinese pages in the
corpus, scoring 180/269/282. Lowest-value item on the list.

### Stage 3 — EXTRACT

#### 3(a) One article per call, in isolation — **confirmed**

`extract_one` ([crawl.py:1816](../tracker/ingest/crawl.py#L1816)), one call per
`FetchResult`, `MAX_PROJECTS_PER_ARTICLE = 5`
([crawl.py:82](../tracker/ingest/crawl.py#L82)). No cross-source view. Correct
as stated, and correct as a design — see §5.

#### 3(b) Tense and modality misread — **not reproducible**

Both cited sources now file `phase='construction'`:

| src | quote | claim |
|---|---|---|
| 660 | "Once operational, this data center will support 1,000 operational jobs" | `construction` |
| 236 | "Since breaking ground on our Richland Parish Data Center in December 2024…" | `construction` |

Across #10: **40 construction, 17 announced, 3 permitting, 1 operational** — and
the single `operational` is an Instagram post whose phase claim is already marked
unconfirmed with no quote.

The prompt carries the rule (`extract-v1` ~line 275: *"'operational' REQUIRES the
article to say the data center is running"*, plus the Section 6 phrase list) and
a `modality` evidence axis.

**The brief's reference numbers are stale.** Live values are `mw_planned = 5,000`
(not 14,462) and `phase = 'construction'` (not `operational`). Both were fixed by
`4e19ded` and `7e544b9`. `investment_usd = $10B` is still wrong and is the one
headline defect that remains.

#### 3(c) Generation extracted as data-centre capacity — **confirmed; the live defect on #10**

```
21 of 1127 capacity_block rows name a power asset (2%)
carrying 15,091 MW that is generation, not IT load

  #10   6 blocks, 5,962 MW   Richland Parish Units 1-4, Franklin Farms Gas Plants,
                             Natural gas generating facilities, Franklin Farms 1/2,
                             Franklin Farms Solar
  #13   2 blocks, 4,600 MW
  #2    4 blocks, 1,845 MW
```

The prompt already forbids this in the strongest terms it uses anywhere —
*"THREE THINGS ARE NEVER BLOCKS … GENERATION AND GRID ASSETS"* — and cites
**5,962 MW**, which is exactly #10's number. Every offending row predates that
text. **Remediation job, not a prompt job.**

#### 3(d) Block labels fork into several keys — **phenomenon confirmed, cause wrong, half already solved**

The cause named in the brief is real but is not what forked Hyperion. It lives at
[blocks.py:69](../tracker/blocks.py#L69) (`TYPE_WORDS`) and
[blocks.py:92](../tracker/blocks.py#L92) (`_NOISE`); `blockcheck.py:64-73` is the
*comment describing* it.

Hyperion forked for a different reason — **the parent prefix**:

```python
block_key("Phase 1", None)       -> "phase-1"            generic
block_key("Phase 1", "Hyperion") -> "hyperion.phase-1"   not generic
```
([blocks.py:260](../tracker/blocks.py#L260)). All three pairs are the *same
label*; whether the extractor supplied `parent` decided the key.

Database-wide: **155 such pairs, 76 colliding on MW, 79 safely mergeable.**

And `tracker blocks 10` already finds all three groups with the right verdicts:

```
mergeable #10 phase-1   2,064 MW  vs  1,500 MW 待确认
collides  #10 phase-2   2,000 MW  vs  2,060 MW — both confirmed, so two figures
mergeable #10 phase-3       -     vs  5,000 MW
```

It refuses `phase-2`, exactly as the house rule requires. The `block_alias` table
exists and `blocks.py:780` reads it. **It holds 0 rows and no command writes
one.** The missing piece is an accept path, not a detector.

#### 3(e) Facility load vs IT load — **not found in the data**

No block anywhere in the database carries a label containing `IT load`,
`critical` or `facility load`. The triple quoted in the brief (Phase 1 2,000 /
Initial Phase 2,000 / Phase 1 IT Load 1,500) is not present. #10's actual overlap
is `Phase 1` 2,064 vs 1,500 and `Phase 2` 2,000 vs 2,060 — the 3(d) fork plus
genuine source disagreement, not a load-type distinction.

Adding a load-type axis now would violate the claim-envelope house rule: nothing
in the corpus can verify it.

#### 3(f) Geography at the wrong granularity — **inverted**

The prompt already says the opposite of what the brief describes:

> *"If the article gives only a county or parish, leave city null and put it in
> `county` instead. Do NOT copy a county or parish name into city."*

61 sources supply `county`, 1 supplies `city` — and that one (src 3295) copied
"Richland Parish" into `city`, violating the rule. `looks_like_county`
([crawl.py:1602](../tracker/ingest/crawl.py#L1602)) caught it, so
`project.city IS NULL`, correctly.

The real defect is downstream: `gaps.for_project` returns `city = missing,
is_gap=True`, so an unincorporated site is marked down for a field that cannot
exist. That is a `NOT_APPLICABLE` case in `gaps.py` — **stage 6, outside this
scope.** See §5.

#### 3(g) Events not deduplicated, no materiality — **confirmed, and larger than stated**

```
2825 events in 1194 (project, type) groups
 614 groups hold more than one date
1631 rows are surplus  => 58% of the event table
```

#10: 19 `announced`, 9 `groundbreaking` (2024-10 … 2025-12). Confirmed exactly.

The prompt already forbids intra-article duplication and
`uq_event_project_type_date` collapses exact matches. Neither can touch
cross-source date *disagreement*, which is what this is.

#### 3(h) `risk.severity` is always 'watch' — **wrong**

```
database-wide : watch 447   material 183   blocking 24
project #10   : watch  25   material   3
```

It discriminates across every prompt vintage. The prompt gives an observable
three-way test — *"material: the article says the schedule or the scope moved"* —
and instructs `watch` as the explicit default when the article does not say.

This is **not** the retired `scope` case: `scope` collapsed to 97% on one value,
severity sits at 68/28/4.

The criticism that does survive: severity is a judgement layered on top of the
obstacle's quote rather than a quoted fact itself, and only 235 of 447 `watch`
rows carry any quote. Worth stating as a caveat; not worth retiring the field.

---

## 2. Root cause and the smallest fix

| # | verdict | root cause | smallest fix | kind |
|---|---|---|---|---|
| 1(a) | confirmed | `discover.py:474` passes title+path only; `Candidate.content` unused | feed the syndicated body to `matches` when present; require the topic term within N chars of a signal term | **code** ~30 ln |
| 1(b) | confirmed | `upsert._published_at` can only read what `discover` wrote | fix 2(a); then flip `merge_by_publication_date` | **config**, gated on data |
| 1(c) | confirmed | no supersession concept | out of stage-1 scope — see §5 | — |
| 2(a) | confirmed | `fetch.py` discards HTML before caching | parse `datePublished` / `article:published_time` / `<time>` in the fetcher, carry on `FetchResult`, persist to `source.published_at`; plus a free URL-path pass | **code** ~60 ln + backfill |
| 2(b) | corrected | `_split_cached` short-circuits the fetch (`crawl.py:2220`) | do not serve a cached body below `MIN_PROSE_CHARS`; re-fetch it | **code** ~3 ln |
| 2(c) | confirmed | character floor at `crawl.py:285` | per-script floor, or leave it | **code**, low value |
| 3(a) | confirmed | by design | do not change — see §5 | — |
| 3(b) | not reproducible | — | nothing to do | — |
| 3(c) | confirmed | rows predate the prompt's generation rule | re-extract under `4ea77aad`; or targeted purge of the 21 blocks | **data** |
| 3(d) | confirmed | `parent` present on one source, absent on another (`blocks.py:263`) | `tracker blocks --accept` writing `block_alias`, mergeable groups only | **code** ~60 ln |
| 3(e) | not found | — | nothing to build | — |
| 3(f) | inverted | `gaps.py` has no `NOT_APPLICABLE` for city on a county-only site | one predicate in `gaps.py` | **code**, stage 6 |
| 3(g) | confirmed | cross-source date disagreement | canonical-event selection at rollup | **stage 6** |
| 3(h) | wrong | — | document the caveat | **docs** |

No schema change is required by anything above. `block_alias` and
`source.published_at` both already exist.

---

## 3. Ranking, and the order that is forced

**(rows corrected) / (effort)**

| rank | fix | rows | effort | why here |
|---|---|---|---|---|
| 1 | cache guard for sub-floor bodies | 21 | ~3 lines | free; unblocks 180 `thin_content` retries |
| 2 | URL-path date pass | 175 | ~20 lines, no network | free and deterministic |
| 3 | `tracker blocks --accept` | 79 pairs | ~60 lines | detector and table already exist |
| 4 | fetch-time date capture + refetch backfill | up to 506 | ~60 lines + ~2,400 fetches, **no LLM** | the reference case; the largest correctable population |
| 5 | filter tightening | bites into 2,381 wasted calls | ~30 lines | saves spend, corrects no stored row directly |
| 6 | re-extraction under `4ea77aad` | 21 blocks / 15,091 MW + unknown | ~1,678 fetches + ~1,928 calls | the only thing that fixes 3(c) |
| 7 | per-script prose floor | 3 | low | near-zero value |

**Forced ordering.** Dates before re-extraction, and it is not a preference.
`crawl.run` passes the publication date *into the prompt*
([crawl.py:2143](../tracker/ingest/crawl.py#L2143) →
[crawl.py:2166](../tracker/ingest/crawl.py#L2166)), and it currently sends
`"unknown"` for 55 of #10's 61 sources. Re-extracting before the dates land
spends the whole budget with the model's single best anchor for tense, staleness
and supersession still missing — and re-extraction is the one step too expensive
to run twice.

`blocks --accept` must also follow re-extraction, since re-extraction rewrites
labels and parents.

So: **1 → 2 → 4 → (measure, then flip `merge_by_publication_date`) → 6 → 3 → 5.**

**Kill criterion for the one model stage (step 6), written before it is built.**
Re-extract #10's 61 sources under `4ea77aad` and stop there. PASS requires all
four:

1. generation blocks on #10 fall from 6 to ≤ 1;
2. `mw_planned` stays 5,000 and `phase` stays `construction`;
3. no currently-confirmed value loses its quote;
4. `investment_usd` reaches $50B **or** is refused as a flagged conflict — not
   silently re-decided.

Any failure stops the remaining ~1,867 sources.

---

## 4. What cannot be fixed in stages 1–3

- **1(c) supersession** — "this filing replaces that press release" is a
  statement about a *set* of sources. One article in isolation cannot make it.
  Stage 5, as a claim-level rule.
- **3(a) cross-source judgement** — this is the architecture working. Sources are
  independent observations; reconciliation is stage 5's job. Giving the extractor
  a cross-source view would let one article's framing rewrite another's quote.
- **3(f) the city gap** — the extraction is already correct; the scoring is not.
  `gaps.py`, stage 6.
- **3(g) event materiality** — choosing which of nine groundbreaking dates is
  canonical needs all nine. Stage 6 rollup.
- **3(d) applying a merge** — detection is stage 3's; the decision is stored, not
  applied, per `blockcheck.py`'s own design. Stage 6.
- **1(b) consuming the date** — already built in stage 5. Stages 1–2 only owe it
  data.
