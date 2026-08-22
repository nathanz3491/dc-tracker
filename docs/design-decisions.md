# Design decisions

The decisions that are not obvious from the code, and the places this implementation deliberately diverges from the PRD.

Part of the [dc-tracker documentation](README.md).

---

## ISO interconnection queues do not identify data centers

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

## The JSON contract is enforced in code, not by the provider

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

## Discovery reuses `ingest_url` rather than adding a queue table

A candidate lands in `ingest_url` with status `discovered`. That table already
existed to record per-URL crawl outcomes, and "a URL nothing has read yet" is just
one more state in the same lifecycle — so the queue is a status value plus three
metadata columns (migration `0003`), not a subsystem.

The `title` column earns its place: without it, `tracker queue` could only show a
bare URL, which is not enough to judge whether an article is worth an LLM call.

Discovery never touches a URL already in the table, whether it was crawled
successfully or failed. Re-queueing a processed URL would let discovery quietly
undo the crawl path's bookkeeping.

## Feed filtering is three tiers, and two of them can be implied or absent

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

## Hedged dates resolve to a quarter, not to NULL

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

## The evidence gate, not the prompt, is what prevents fabrication

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

## Deriving county and coordinates

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

## `tracker clean` — one bar, four tiers, and the command that raises a row

Reading one campus by hand found eleven defects across six subsystems. There are
hundreds of rows, so the fixes are worth nothing unless they are a pipeline, and a
pipeline needs a definition of done that is computable.

`tracker clean` composes the detectors that already exist — `logic`, `audit`,
`quality`'s census, `gaps`, `blockcheck`, `Risk.unconfirmed`, the prompt stamp on
`source.extractor` — and reimplements none of them. The whole free sweep runs in
about seven seconds over the database, which is why progress is reconstructed from
the data rather than stored: there is no ledger table and no `clean_tier` column.

```
T0 SOURCED    something real cites it, and it does not contradict itself
T1 SOUND      nothing in a total is a lie          <- the bar worth chasing
T2 COMPLETE   the fields a reader acts on are there, and each is backed
T3 SETTLED    every open question has been answered
```

T1 first, because the numbers this tool publishes are sums. An incomplete row makes
a total *smaller*; a row carrying an implausible figure, an unanswered duplicate, or
a value decided by crawl order makes it **wrong**, and one wrong row discredits the
table.

Two definitional choices are load-bearing. `NOT_APPLICABLE` counts as complete —
`mw_built` on an announced project is correctly null, and a 12-of-12 bar failed 97%
of rows, which is a target nobody can use. And 待确认 counts as *backed*: the gate
declaring it could not confirm a value is the gate **working**, so only
`confirmed_without_quote` fails the condition.

The definition is calibrated rather than asserted: a test says that if a
fully-answered row cannot score T3, the definition is wrong and `clean.py` changes —
not the row.

```bash
tracker clean                        # the scorecard, plus what every value rests on
tracker clean --project 10           # one row, and the exact command that fixes each failure
tracker clean --plan --tier 1        # the worklist, closest rows first
tracker clean --snapshot             # append to data/runs/clean.jsonl
tracker clean --since 1              # diff the last two runs
```

The one thing a column cannot be is a time series, which is what `--snapshot` is
for. Two named console workflows drive it: `clean-free` (no LLM, no network) and
`clean-paid`. Run the free one first — it removes most of the work for nothing.

## The prompts needed to know what a data center is

Reading one campus closely — Meta's Hyperion, the most heavily sourced row in the
database at 59 sources — turned up a cluster of failures that looked unrelated and
were not. `mw_planned` read **15,962 MW**, which would be three times the largest
campus announced anywhere; `investment_usd` held **$10B** against 5 GW, about a
fifth of what that costs; `phase` said **operational** for a site that has never
been powered, evidenced by a sentence about breaking ground.

The prompts were not missing rules. `extract-v1` already said, in as many words,
that `mw_planned` "is NOT the capacity of a power plant, solar farm or substation
built to serve it". The gap was that **nothing told the model what a data center
is dimensionally**, so no rule had anything to bite on: 5,962 MW of gas turbines
and solar panels read as ordinary tranches of a campus, and a superseded
announcement read as a current figure.

So `prompts/_industry.txt` is prepended to every prompt's system message. Eight
sections of durable background — the campus/building/hall hierarchy; IT load
versus generating capacity and the words that mark which is which; the five other
things a dollar figure in these articles is usually about; a plausibility envelope
in orders of magnitude ($8–15M per MW, ~5 GW as the largest campus announced
anywhere); who the operator is as against the utility, the EPC and the financier;
the lifecycle phrases that get misread as "running"; how a project gets restated
upward over years; and what actually obstructs one.

**It is background for judgement, never a source of values**, and that constraint
is the load-bearing part. It sits in tension with extraction's first rule — never
draw on knowledge from outside the article — and the tension is resolved
explicitly in both places, because a model that answered *from* this block would
fill megawatts with plausible industry averages and pass the evidence gate while
doing it. There is a test asserting the block still forbids itself.

Prepended, not appended, so the per-prompt rules come last and win where the two
touch. About 2,700 tokens on every call, which on a stable system prompt is a
cache hit: 0.00005 CNY.

## Crawl4AI fetches, it does not extract

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

## Project fields are recomputed from claims, not merged incrementally

Each `source` row records what *it* asserts in `source.claims`. After the sources
are written, every project field is derived afresh from all of them by a declared
policy in `upsert.FIELD_POLICY`. This buys three properties the PRD asks for and
that incremental merging does not give you:

> **The catch, and `tracker backfill derive`.** The derivation is only *applied*
> when something writes to the row. Improving the merge policy, the evidence gate
> or the block rollup therefore does not improve a project that is already stored
> — and `enrich`, the command usually reached for, only ever *adds* a source; it
> cannot correct a row it did not create. `tracker init` recomputes confidence,
> accelerators and blocks and stops there.
>
> ```bash
> tracker backfill derive --dry-run     # what would move, and which fields
> tracker backfill derive               # move it
> ```
>
> No LLM, no network, no migration. On the live database its first run moves 322
> values across 213 of 300 projects — 205 note blocks, **81 blockers**, 16 phases,
> 9 capacities. The blockers are the finding: `blocker` is derived from the risk
> rows and nothing re-derived it after a risk was resolved, so rows carried
> obstacles that had been cleared, and some whose obstacles were *all* resolved
> still showed one.
>
> **Running it twice changes nothing**, and that is the test: if a second pass
> keeps moving rows then the derivation is not a pure function of what is stored,
> and every number in the database is whichever pass ran last.


- **Idempotence.** Re-ingesting the same input recomputes the same values, so
  `updated_at` genuinely does not move. `test_reingest_is_idempotent` is the
  load-bearing test of the whole design.
- **Order independence.** PJM-then-news equals news-then-PJM.
- **Open question Q2 for free.** Two conflicting `mw_planned` values both survive
  in their own `source` rows, the project field takes the higher-weighted one,
  and the spread is disclosed in `notes` when it exceeds 20%. Nothing is
  destroyed to make the merge work.

## City versus county is a database invariant, not a convention

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

## Provenance is per field, not per source

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

## `defaulted` is not 待确认

A fifth tier, and the distinction is not pedantic. `phase` is NOT NULL, ingest
paths deliberately omit it from `source.fields` when no source states one, and the
column falls back to `announced`. Reporting that as 待确认 asserted that a source
had claimed it and failed to prove it. Nobody had claimed anything. On the live
database 37 values were being mislabelled that way.

## Confidence, and why one source never reaches 3

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

## Five schema additions beyond the PRD's three tables

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

## Risk is a table, because a sentence cannot be cleared or counted

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

## The database is not committed

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

## Other decisions worth recording

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
- **Prompt version identity is filename + SHA-1 of the file bytes**, including the
  shared block's. A filename alone starts lying the moment you edit the file,
  which is exactly what iterating on a prompt means — and a shared partial that
  could change underneath the hash would reintroduce that failure through the back
  door.
- **One industry block is prepended to all eleven prompts** (`prompts/_industry.txt`).
  See "The prompts needed to know what a data center is" below.
- **Prompts template with `string.Template` (`$var`), not `str.format`.** The
  prompt contains a JSON schema block full of literal braces, which `str.format`
  would raise on.
- **`upsert.py` and `dedup.py` are outside the PRD's file layout.** The PRD names
  no home for dedup matching, merge policy or Q2, and putting them in
  `normalize.py` would break that module's side-effect-free contract.

## Coverage needs a list, because absence has no source

Every discovery path here is source-driven: poll the feeds, sweep an archive, ask a
model to brainstorm projects, read a filing. All of them answer *what has been
published*, and none can answer *who are we missing* — an operator nobody wrote
about last month is indistinguishable from an operator that does not exist. The
system had no representation of an expectation, so it could not detect a gap.

Measured before this was fixed: 300 projects, 102 distinct company spellings, and
zero rows for **Nebius** — a top-five AI cloud running a Kansas City campus.
CoreWeave had no row under its own name either, appearing only as a tenant inside
two Core Scientific projects. Both were invisible to `gaps`, `verify` and `stats`,
because all three measure the rows that exist.

`seed/operators.toml` is the expectation, written down. It is deliberately:

- **hand-written and checked in**, not generated. A model asked each run which
  operators exist would answer differently each time, and nothing would tell you
  what it forgot — the failure mode being fixed. A file diffs, reviews, and can be
  argued with.
- **not `edgar-companies.toml`.** That list is scoped by CIK because EDGAR
  full-text search is only precise when filtered by filer, so it structurally
  cannot hold Vantage, STACK, Aligned, Crusoe, Lambda or QTS — all private. The
  rosters overlap on the public names and a test asserts every EDGAR company
  (except the utilities and contractors, which own no campuses) also appears in the
  operator roster, so they cannot drift.
- **operators only.** Utilities and contractors are sources, not owners. "No rows
  for Dominion" is the schema working, not a gap, so putting them here would
  manufacture 14 permanent false positives.

**Why matching is loose, and why it says so.** One operator files under many
spellings — "Nebius Group N.V.", "Aligned DataCenters", "Cipher Stingray LLC",
"RagingWire" — and coverage is worthless if it reports a gap that is really a
spelling. Both sides are normalized through `dedup.company_key`, stripped of the
words every data center company shares (including the plurals `company_key` leaves
behind, which is what makes "Compass Datacenters" find "Compass Data Centers"), and
compared as token subsets.

The subset runs in one direction only: "Cipher Mining" finds "Cipher Mining Inc."
and must not find a bare "Cipher", which could be anybody. That asymmetry is the
same one `tracker point` uses and for the same reason — a wrong fold silently
credits one operator with another's capacity and nothing downstream detects it,
while a wrong miss shows up as an operator you already have appearing in the absent
list, which is annoying and self-correcting. Loose matches print with a `~` so the
rule is auditable, and the reverse gap — companies no entry claims — is printed
every run, because a hand-written list's real failure mode is going stale.

**`tracker prospect` writes nothing it is told.** It turns a rostered name into
leads from three sources — the unread queue and the sitemap archives, both free,
then the paid search — and the model's contribution is at most a list of campus
names used to build those queries. Rows still come only from a fetched article that passes the evidence
gate. This is the same asymmetry `search.py` documents, and `tests/test_prospect.py`
asserts it directly: a campus the model invented produces no project, no source and
not even a queued URL.

**A second, unrelated hole under the same name.** Nebius was in
`edgar-companies.toml` from the start and yielded no filings, because that file
asked every filer for 10-K, 10-Q and 8-K and Nebius Group N.V. is a Dutch foreign
private issuer: it files 20-F and 6-K. The fix is a per-company `forms` override
rather than widening the shared list, which would have doubled the cost of every
domestic filer to reach one foreign one. Worth recording because the two blind spots
were independent — one in what we looked for, one in where we looked — and the
roster is what made either visible.

**The cheapest lead source is the queue, and finding it took writing the roster
first.** `ingest_url` already held candidates naming operators with no rows: the
extract phase is depth-first by design — an LLM call spent on an article about a
tracked project buys a second source, which fills fields one article cannot — so an
article about an operator we have nothing for matches no known project and sorts
last behind a permanent supply of better candidates. Correct ordering, and it has a
starvation case at the exact place coverage is worst. Nothing measured that until
something held an opinion about which operators ought to be there. `sync
--prospect N` therefore moves those URLs to the front of the extract phase rather
than queueing them and hoping.

**Scope stays US-only.** Nebius's largest sites are in Finland and none of them
belong here. `project.state` is a NOT NULL two-letter code, the locality derivation
is Census-backed, and the ISO and EDGAR paths are US-shaped throughout; going
global is a migration and a second locality pipeline, not a filter change. The
roster therefore lists operators by their *US* presence, and an operator with no US
campus wastes one prospecting round rather than being silently right.

## The seed file

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
