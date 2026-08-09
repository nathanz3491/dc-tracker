# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

First working version. Nothing has been released yet, so everything below is the
initial build of the v1 PRD.

### Added

- **Milestones now carry evidence, and say so when they carry none** (migration
  `0017`, `tracker/ingest/crawl.py`, `tracker/upsert.py`, the console's
  milestones card). `events[]` was the last extracted structure with no gate
  behind it at all: the prompt has said "Only milestones whose date you can
  quote" since v1 — a request with no mechanism — and every milestone fed the
  track strip on the model's say-so. Observed live on Fairwater (#1): a
  `groundbreaking` dated 2026-06-23 whose own description reads "Open house
  event held to announce opening", counted as breaking ground two years after
  the site actually did.

  Events get exactly what risks got in 0004+0012: a `quote` column verified
  verbatim against the fetched article (same recovery path), an `unconfirmed`
  reason when the gate cannot confirm it, demotion instead of deletion, and the
  article's own words stored rather than the model's edit. The description stays
  the model's one-line wording, for the same reason risk summaries do — demanding
  it be verbatim is what took the old `blocker` field to zero coverage.

  **The backfill deliberately differs from 0012.** Risks left pre-gate rows NULL
  because the old gate deleted whatever it refused, so survivors really were
  confirmed. No gate ever ran on an event, so NULL-means-confirmed would be a
  false statement about every existing row: all 843 are marked `no_quote`. A
  re-read under the new prompt upgrades them — a verified sentence replaces the
  flag, and never the reverse, because a later article that merely fails to
  quote a milestone is not evidence against the sentence one already did.

  On screen, an unverified milestone carries a `not cited` chip and a verified
  one carries the article's sentence on hover. Today that chip is on every
  event, which is the truth: none was ever checked. The prompt change moves the
  stamp, so `--stale-prompt` already knows which sources to re-read to start
  confirming them.

- **A live campus whose running tranches can cite no capacity is now a finding**
  (`tracker/logic.py`, `built_capacity_uncited_in_blocks`). Observed on Fairwater
  (#1): the console read "0 MW delivering of 350 MW" directly above an energized
  350, while the phase said operational and the events said the site came online
  in April. Nothing was wrong inside any one of those statements — the zero is
  honest, because only cited capacity is summed and the 350 is 待确认 — but the
  contradiction between the scalar, the blocks and the events had no rule naming
  it. Its sibling `live_block_without_cited_capacity` guards on `mw_built is
  None`, so a row with the scalar *set* was exactly the shape it could not see.
  Report-only like every block rule, because the fix is a citation for a running
  tranche and no automatic edit can find one. Live: 19 findings on 19 projects,
  including a 46,500 MW `mw_built` on #62 that is plainly a unit error.

  The console's blocks headline says why the zero is a zero instead of leaving
  the page to disagree with itself: "3 tranches are running with no cited MW —
  350 MW stated, 待确认". The number itself stays 0 — summing an unconfirmed
  figure to make a zero go away is the same mistake that once reported 36,000
  unconfirmed megawatts as running, in reverse.

- **`tracker enrich --all`: every project still below the target**
  (`tracker/cli.py`, `tracker/ingest/enrich.py`). `--select N` existed for "the N
  worth finishing"; there was no way to say "all of them" without first counting
  them. `--all` is `--select` with no cap — same query, same closest-first
  ordering, and projects already at the target are still excluded, so it never
  spends on finished rows. It is not unbounded spend: `--budget` remains the real
  ceiling, and the ordering is what decides who gets the articles before it runs
  dry. Exactly one of ids, `--select` or `--all` may be given — silently unioning
  them would let `enrich 90 --all` spend the whole budget while reading as though
  it targeted one project.
- **Wikipedia is a source now — cited under guard, and mined for its references**
  (`tracker/ingest/wiki.py`, `tracker/ingest/search.py`, `tracker/confidence.py`).
  Google's first result for a tracked campus is routinely its Wikipedia article,
  and the blocklist threw it away. It is now kept, with the two halves handled
  differently because they are different things:

  - **The article** is extracted like any page — the evidence gate applies
    unchanged — but `confidence.TERTIARY_DOMAINS` keeps a wikipedia.org citation
    out of domain independence, key-field agreement, and conflicts. Wikipedia's
    capacity figure IS the trade-press figure one step removed, so letting it
    corroborate would launder aggregation into independence; letting it *conflict*
    would dock rows for Wikipedia's staleness. It still floors the score at 1.
  - **Its references** are mined through the MediaWiki API (`action=parse`,
    `prop=externallinks`) — the cached article text has its hrefs stripped, so the
    API is the only honest way to read them. Measured on Hyperion: 50 external
    links, among them the investor.atmeta.com release behind the Blue Owl joint
    venture — the primary source for the $27B figure this database has held the
    superseded $10B against. Wayback wrappers are unwrapped to the URL they
    archived, the wikimedia tool family is dropped, and a keyword pass over the
    slug keeps a link when *any* tier fires (a reference cited by a data-center
    article has already had its relevance judged by an editor). Capped per page.
    Mining runs in `tracker search`, `sync`'s search phase, and `enrich`'s search
    harvester — from the raw hits, so an opaque wiki snippet failing the keyword
    filter does not cost the references.

- **Searching is on by default once a key is configured** (`tracker/cli.py`,
  `tracker/ingest/enrich.py`). `tracker sync` now runs its search phase
  automatically when any backend holds a key (`--search 0` still disables it;
  the count defaults to `TRACKER_SEARCH_MAX_QUERIES`). This closes the measured
  gap that prompted the change: after a sync, `enrich`'s queue/retry/archive
  harvesters draw from corpora sync already drained, so on a well-synced database
  the search harvester was the only one that could reach new ground — and it was
  silently skipped without a key. Verified live on Hyperion (#10) with Serper:
  queue 0, retry 0, **search 42** URLs, 25 of them Wikipedia references.
  The project's configured backend is Serper (`TRACKER_SERPER_API_KEY`).

- **Every harvest now says which search backend answered** (`tracker/cli.py`,
  `tracker/ingest/enrich.py`, `tracker/ingest/search.py`). `enrich` had been
  running its searches against **Bocha** — pinned by `TRACKER_SEARCH_PROVIDER`,
  and by this repo's own measurement an index that "does not index US data center
  trade press at article depth" — so the harvester was working exactly as built
  and returning almost nothing citable. Nothing on screen said which engine it
  asked, because `_render_enrich` assembled each harvester's `note` and then
  never printed it: the run reported a method that found nothing rather than one
  pointed at the wrong index.

  Providers now carry a `NAME`, the search harvest reads `3 quer(ies) run via
  serper; 24 wikipedia reference(s)`, every harvester's note is rendered under
  the round table, and `sync` names the backend when it turns searching on. A
  misconfigured index is now a visible fact rather than something you have to
  remember. The pin is `serper`.

- **`tracker/ingest/wiki.py` tests and live fixtures** (`tests/test_wiki.py`):
  the miner's fixtures are the Hyperion article's real external-links list as
  returned on 2026-08-08, so the branches the live data exercises stay pinned.

- **`tracker audit resolve`: the ladder that settles an impossible figure**
  (`tracker/audit.py`, `tracker/prompts/audit-resolve-v1.txt`, `tracker/cli.py`).
  `tracker audit` has reported the same 11,250 MW colocation expansion on every run
  since it was written, because the only repair it could offer was a sentence
  telling somebody to go and read an article. A report nobody can answer stops
  being read.

  Five rungs, each running only because the one above declined: **arithmetic**
  (free), **the person at the keyboard** (free), **a reasoning model** on what the
  row holds, **the open web** when the model says the row lacks the answer, and
  **the model again** with what the search found. That ordering is the cost
  control — most findings never reach a paid rung.

  Rung one is deliberately two cases and no more. `h200_equivalent` is a fixed
  ratio applied to capacity, so re-deriving it is not an opinion; and a tranche
  *labelled* "2.4 MW Lease" carrying 2400 on a 15 MW campus is that label read as
  kilowatts, where the correct figure is written down beside the wrong one. Rung
  two offers `?` beside the edits, which hands the question down: not knowing is
  the commonest honest answer and should cost one keystroke.

  A model's entire output is one key from a list a person wrote, a confidence and a
  sentence — it cannot type a capacity. Its fourth answer, `m`, means "the row does
  not contain the answer" and is what makes this a ladder rather than one call.
  Pages fetched at rung four are read and never stored: they inform one decision,
  and a page skimmed for one number is not a citation for anything. Every edit
  lands in the row's notes naming who made it, and a finding settled once is not
  asked again.

  `block_out_of_scale` gained two repairs in the process. It offered nothing, on
  the reasoning that an excluded tranche only needs a re-crawl — but five of the
  twenty-two live findings are one label stating its own capacity beside a stored
  value a thousand times larger, which is the same arithmetic the campus-level
  check already offered, applied one grain finer.

- **`tracker duplicates park`: recording that two rows are *not* one campus**
  (migration `0016`, `tracker/pairs.py`, `tracker/models.py`, `tracker/capex.py`).
  The duplicates report proposes and never merges, which left it with exactly one
  possible answer. A false pair came back on every run, ahead of the real ones —
  and not merely as clutter: `capex.rollup` reads the same pairs and holds one row
  of every suspected group out of the buyer table, so a wrong pair takes a real
  campus's capacity out of a number somebody quotes.

  Stored pairwise rather than per group, because a group is a closure computed at
  read time and "these three are separate" says nothing about a fourth row that
  appears next week. `decided_by` mirrors `project_alias`: a model may park a pair
  and a reader must be able to tell that one did.

- **`tracker risks confirm`: reading the article behind an unquoted obstacle**
  (`tracker/riskcheck.py`, `tracker/prompts/risk-confirm-v1.txt`). A third of the
  open obstacles are 待确认 — a source named them and the evidence gate could not
  find a sentence that says so. They are counted anyway, and nobody had ever gone
  back to check which reading was right.

  One model call per obstacle, with the **whole article** from the crawl cache
  rather than the excerpt the failed extraction chose, and with every other
  obstacle on the row beside it — half these rows are `quote_off_target`, a real
  sentence filed under the wrong category, which is invisible without seeing what
  the other categories already claim.

  What makes the answer trustworthy is not the model: a returned quote is accepted
  only if it is verbatim in the article, checked with `crawl._verbatim_run`, and
  only if the sentence carries wording for its category, checked with
  `crawl._risk_quote_supports`. A confirmation looser than the gate it overturns
  would be a way to launder a paraphrase into a citation. Refused quotes leave the
  obstacle exactly as it was, so the worst case is a call that changed nothing.

- **`tracker queue check` and `tracker queue prune`** (`tracker/ingest/discover.py`,
  `tracker/cli.py`). A queue is a promise that everything in it is worth an LLM
  call. `check` asks every queued URL whether it is still there — 404 and 410 are
  dead, **403 and 429 are not**, because a newsroom answering 403 to a non-browser
  is what `--browser` exists for and on the live queue that was 55 URLs across the
  seven best-defended publishers. `prune` re-applies the filter in
  `seed/feeds.toml` to rows queued under an earlier version of it, which nothing
  had ever done: 417 of 1,241 live candidates no longer qualify.

- **An inferred-analysis panel on every project in the console** (`/api/infer`,
  `tracker/webui/static/app.js`). `tracker infer` answers the PRD's two analytical
  questions and reaching it meant leaving the page, finding the id and typing a
  command, which is why almost nobody ran it. A button, not an automatic panel:
  the AI overview generates on open because it is cached by content, while an
  inference is never cached or stored, so running it on open would spend a call
  every time a drawer opened.

- **The claim envelope: what a value is a value *of*** (migration `0015`,
  `tracker/vocab.py`, `tracker/ingest/crawl.py`, `tracker/prompts/extract-v1.txt`).
  Hyperion (#10) carries three investment figures — $10B for "the buildout of the
  infrastructure itself", $27B for the Blue Owl campus JV, $50B "of investment to
  the region" — and they are not a disagreement. They are three measurements of
  three different things, collapsed into one scalar because that is all the schema
  had, so every merge policy was picking a winner among claims that were not
  competing. The row held the oldest and smallest while its own notes read
  "expanded to up to $50 billion".

  Four axes now travel with each claim in `source.claim_meta`: `scope` (this site,
  a named tranche, the programme, the region, the operator's portfolio, or
  unnamed), `bound` (exact, approximate, at\_least, at\_most), `modality`
  (speculated → targeted → planned → contracted → achieved) and `as_of`. They
  extend the existing `evidence` array in the prompt, which already paired a field
  with a quote and was the right home.

  **Every axis is verified against the quote, and this is the whole design.**
  `evidence_gate` asks whether the article states the value; the new `axis_gate`
  asks whether it states the qualifier. `bound: at_least` needs a hedge word in
  the sentence; `scope: block:<label>` must resolve to a tranche on the record;
  `modality: achieved` needs evidence of something having happened, and is
  demoted to `targeted` outright when the date is in the future — which is exactly
  Hyperion's live defect, where "an interim milestone of 1.5 GW is being targeted
  by the end of 2027" was stored as `announced`, dated 2027-12-31, and counted as
  *reached* on the track strip.

  The check runs against the *stored* quote, not the model's offered text, because
  `_verbatim_run` may have repaired the quote to the article's own words —
  checking the model's version would let it license a hedge by writing one into a
  sentence nobody published, reopening one level up the fabrication route the
  evidence gate closed.

  **A refused axis never costs the value.** `axis_gate` returns labels and is
  never given the figure, so the worst case is a claim described no better than it
  was yesterday. A model labelling everything `at_least` to sound careful gains
  nothing, which is the property that stops the axis drifting into decoration —
  `risk.severity` being the cautionary case, a judgement no article states, `watch`
  on every risk in the database, fully populated and carrying nothing. Nothing
  reads the axes to choose a value yet, deliberately, for the reason `source_type`
  demonstrates: it became load-bearing before it was measured and
  `confidence.find_conflicts` is now documented as too risky to correct.

  **Measured over a full re-extraction, and one axis failed its pre-registered
  kill criterion.** `bound` passes (32.0% coverage, 87.4% `exact`) and works on
  the case it was built for — "roughly $27 billion" reads `approximate`, "more
  than $50 billion" reads `at_least`. `modality` passes weakly (84.3% `planned`).
  **`scope` failed at 96.9% `this_site`**, and worse, the decisive test failed:
  across every `investment_usd` claim in the database it returned 44 `this_site`,
  4 `unnamed`, 1 `block:VA13` and **zero `region`, zero `programme`** — the two
  values that would have separated Hyperion's $10B buildout from its $50B
  regional figure never fired once. An axis whose informative values are never
  produced is decoration however carefully it is verified, so `scope` must not
  become load-bearing and that distinction needs a different mechanism.

  One weakness found in `bound` and not yet fixed: the predicate asks whether a
  hedge word is in the sentence, not whether it attaches to *this* number. A quote
  reading "more than $50 billion… up from the roughly $27 billion plan" licensed
  `approximate` from a "roughly" belonging to the other figure. The check needs to
  be positional.

- **Date precision is stored instead of discarded** (migration `0015`,
  `tracker/upsert.py`). `normalize.parse_date` has always returned
  `day|month|quarter|half|year` and its own docstring says why it matters — "Q3
  2025" and "2025-07-01" are stored identically and mean very different things.
  **Nothing outside that module had ever read it.** The precision went into a
  prose note and the row rendered a day-precision date the article never gave.
  Now cached on the project like `confidence` and `h200_equivalent`, taken from
  the claim that actually won rather than from the best precision anybody offered,
  and rendered at the precision the source gave: `2024`, not `2024-01-01`. The
  rare display change that is both shorter and more honest.

- **Bug: any host starting `news.` was treated as a company filing**
  (`tracker/ingest/crawl.py`). `^(news|about|blog|ir|investor|newsroom|press)\.`
  returned `company_filing` — weight 3, the heaviest in the system — with no check
  on whose domain it was. Live that meant `news.17173.com`, a Chinese gaming
  portal, and `news.futunn.com`, a stock brokerage. On Fairwater (#1) the gaming
  site was the *only* `company_filing` on the row: it decided the stored $3.3B and
  supplied the "strongest source is company_filing" line in the confidence
  rationale. A newsroom subdomain is now only evidence together with a domain
  already known to belong to an operator, which is what `classify_source_type`'s
  own docstring always said it did — "never returns `company_filing` on a guess
  about a general domain". The cost is that a genuine newsroom absent from
  `feeds.toml` drops to `general_media`; adding it there is the designed fix.

- **The planted-mutant harness now exists** (`scripts/measure_extraction.py
  --mutants`). `HANDOFF.md` and `CHANGELOG.md` both cited "16 planted mutants, all
  caught" as the evidence for `tracker audit`, and no script, test or commit
  contained it — the run was manual, against a copy of a live database nobody
  kept. It plants faults in a throwaway copy and reports the catch rate.

- **`source.published_at`, and a merge tiebreak that can read it** (migration
  `0014`, `tracker/models.py`, `tracker/upsert.py`, `tracker/config.py`).
  `upsert.claims_by_field` ranks claims by `(confirmed, weight, fetched_at, url)`,
  and `fetched_at` is the moment the crawler happened to visit the page. Most of
  this corpus is trade press, so ties on credibility are the common case and the
  tiebreak decides them — on crawl order, which is arbitrary with respect to the
  truth.

  The date was already being collected. `discover` writes it to
  `ingest_url.published_at` from the feed, where it was 78% populated and had
  never been read by anything downstream, because there was no column on `source`
  to carry it to and the merge is the only place it matters. The migration adds
  that column and backfills it by URL — unlike `unconfirmed_reasons` in `0013`,
  which was deliberately *not* backfilled, and the difference is the point: that
  column recorded a decision some past extraction made, so inferring it later
  would have invented a refusal no gate ever issued, whereas this one records when
  a publisher published something, which is a fact about the URL and not about our
  reading of it. 241 of 553 sources filled; NULL stays meaningful, because a
  hand-supplied URL has no date anywhere and must fall back to `fetched_at`
  rather than sort as infinitely old.

  **`merge_by_publication_date` is off by default.** With it on, the six live
  inversions go to zero. It stays off because it moves stored values and they are
  not uniformly improvements: Hyperion correctly stops holding Meta's superseded
  $10B, but #116 would move from 120 MW to 40 MW because the smaller figure was
  published a day later. Publication order is the more *defensible* rule, not the
  one that always yields the larger number, and that is a judgement for somebody
  with the report in front of them. Note for whoever flips it: the flag changes
  the policy, and stored values only move as each row is next written.

- **Bug: `ARTICLE_DATE` was always `unknown`** (`tracker/ingest/crawl.py`).
  `extract_one`'s `published_date` parameter has existed since the prompt did,
  the USER section interpolates it, and **no caller ever passed it**. Prompt
  RULE 5 resolves relative timing against it — "construction begins next year" is
  only a date if you know when "next" was written — so with the date unknown the
  rule correctly forced every such phrase to null. Nothing was fabricated; the
  cost was silent, in schedule fields never extracted, for want of a value that
  was already in the database one join away.

- **A measurement of what the stored data actually rests on** (`tracker/quality.py`,
  `scripts/measure_extraction.py`, `tests/test_quality.py`). Two numbers already
  existed and neither answered the question. "98.7% of stored quotes are exact
  substrings of their own article" measures the quotes that *exist*, so it reads
  as reassurance about a population it never looked at; "66% of claims carry no
  quote" counts the model's raw output, most of which the evidence gate correctly
  demoted, so it reads as a scandal and is mostly the gate working.

  The number that matters is the share of **stored** values whose winning source
  recorded a sentence for them. Measured over 748 values on the live database:
  49.2% quote-backed, 38.2% correctly flagged 待确认, and **11.9% — 89 values —
  confirmed with no quote at all**. That last bucket is the defect, because
  nothing on the row says the value is unsupported, so every reader and every
  rollup treats it as a fact.

  All 89 come from two prompt vintages that predate migration `0007`, the
  migration that added the column a quote lives in — 61 from `extract-v1@8eb51f2a`
  and 28 from `extract-v1@cef10fb4`. **None come from the current extractor.** So
  the gate works and the damage is stratigraphy: a fact about history, invisible
  to every per-row check, findable only by counting.

  Nothing is re-derived. The winning source comes from `gaps.provenance`, the
  merge order from `upsert.claims_by_field` — the same definitions the write path
  and both display surfaces use. That is not decoration: a hand-rolled version of
  this measurement returned 83 rather than 89, differing on exactly the fields
  whose merge policy is MAX/MIN/PHASE rather than PREFER_WEIGHT, because a
  re-derived sort picks a different winning source than the write path did.

  The script also reports two things no per-row check can see: which prompt
  vintage produced each source (348 of 368 extracted sources are on a superseded
  prompt), and 6 field values kept an older article over a newer one of equal
  credibility because the merge tiebreak is `fetched_at` — when the crawler
  visited, not when anybody published. Worst of them: Aligned Phoenix at 65 MW
  from a 2017 article over 400 MW from a 2022 one.

- **`tracker ingest crawl --stale-prompt` re-reads what an older prompt wrote**
  (`crawl.stale_by_prompt`). `stale_sources` asks whether the *article* may have
  changed; nothing asked whether *we* had. Every tightening of the gate —
  placeholder demotion, the prose floor, per-field quotes, `unconfirmed_reasons` —
  applied only to rows written after it landed, and `source.extractor` has
  recorded which prompt produced every row since `0001` without anything ever
  comparing it to the current stamp.

  A fourth URL selector on the existing command rather than a new one, because
  choosing URLs is all that differs: `--from-queue` already establishes the shape,
  and the extractor setup, dry-run, cache and reporting are then shared rather
  than duplicated. Served from the article cache by default, which makes it a
  re-read rather than a re-fetch — `--no-cache` would confound "the prompt
  improved" with "the page changed". Live, it selects 228 URLs of which 224 are
  already cached.

  Deliberately unlike `backfill`, which writes `source.blocks` and never updates
  the stamp beside it, so a backfilled row still advertises the prompt that wrote
  its scalars. `test_remediation_leaves_nothing_on_a_superseded_prompt` pins the
  postcondition: after a re-read, the selector returns nothing.

  **Run live against all 228 stale URLs**: 230 LLM calls, 15 projects inserted,
  248 updated, 4 fetch errors and 2 pages refused by the prose floor. Silent
  defects fell **89 → 14** and quote-backed values rose 368 → 435 of a total that
  grew 748 → 808. Before and after reports are in `data/runs/`.

  Two things the run established that measurement alone could not. First, **it
  does not fix the recency inversions** — Hyperion still holds Meta's superseded
  $10B over the $27B that replaced it, because re-extraction gave all three
  claims quotes and left them tied, which is a tiebreak problem and not an
  evidence problem. Second, **re-reading a URL does not always refresh the row it
  wrote.** The write path is keyed on `(project_id, url)` and project identity is
  re-derived from the article every time, so a re-read that routes to a different
  project — or to none — orphans the original source at its old vintage. Of the
  61 URLs still stale afterwards, 28 now yield no project at all: the
  Switch/Data Foundry acquisition story built two campuses under the old prompt
  and is declined outright by the current one. That is either the gate getting
  stricter or a regression, so those rows are reported and left alone.

- **A page with nothing to quote is refused before it costs an LLM call**
  (`tracker/ingest/crawl.py`, migration `0011`). A fetch that returns 200 and 600
  characters of navigation furniture is not an article, and nothing checked. The
  model was handed a teaser card, invented a plausible project from the title,
  and then *every* quote failed — `company / city / county / state / phase`
  refused together, which is the log signature that prompted this work.
  `build_records` then restored the identity fields from the ungated values and
  wrote the row anyway, so the call was paid for and a phantom project outlived
  it.

  The refusal is on **prose**, not raw length, because raw length cannot draw the
  line: a real Meta 8-K excerpt is 590 characters and an Applied Digital teaser
  card is 598, and the shorter one is the genuine one. Counting only characters
  in lines long enough to be sentences separates them — the 8-K scores 553, all
  15 cached teaser cards score 74, being one site-wide banner line ("Applied
  Digital has signed a 210 MW lease at Delta Forge 2… Read More >>"), which is
  exactly the sentence the model was inventing projects out of.

  Measured over the 544 cached articles that could be matched to a URL: the floor
  of 200 refuses 20 of them (3.7%), every one read and confirmed to be nav
  furniture, an 8-character stub, a bare revenue table or one Chinese wire
  newsflash. It cannot fire on the main corpus — 246 trade-press articles, the
  thinnest at 3,025 — nor on the real filings that were the stated risk, since
  only 1 of 115 cached SEC filings falls below it. Line length is measured in
  characters rather than words on purpose: a word count scores every
  Chinese-language page at zero, and recording `thin_content` against a real
  4,392-character article would be a false reason in the database.

  `thin_content` is its own `ingest_url` status, deliberately not `no_project`:
  that status means a model read the page and found nothing, and discovery never
  retries it. Nothing read this page, so it stays in `tracker queue --failed`
  (grouped by host, which is how eight identical teasers become one visible
  pattern) and `tracker sync --retry-failed` picks it up if the site later serves
  the body.

- **`scripts/measure_evidence_gate.py`** re-runs the three measurements the
  gate's thresholds rest on, against whatever corpus you actually have. Corpora
  change and docstrings do not, so this is how you check the numbers rather than
  trusting a comment. It reads the cache and the database, writes nothing, and
  needs no API key.

  Its negative control is the experiment `MIN_RUN_CHARS` was tuned against and
  the one `docs/plan-evidence-gate.md` makes mandatory for any change to the
  matching. Getting it right needed one correction worth recording: "unrelated"
  has to mean a different *publisher*. Pairing naively across the whole cache
  reported three crossings, and all three were one company's own boilerplate
  recurring in its own documents — two filings under one SEC CIK, two pages on
  one domain. Those are reported on their own line now, because they name the
  gate's real blind spot rather than a threshold that needs raising: boilerplate
  is verbatim everywhere a company publishes, so quoting it proves the sentence
  was published and proves nothing about which site it describes.

- **`tracker audit`: numbers that cannot be true** (`tracker/audit.py`). `logic
  check` asks whether a row's fields contradict each other, and could not help
  here: each of these rows was perfectly self-consistent around a figure wrong by
  three orders of magnitude.

  **Project 72** is why it exists. The row read *Flexential, Englewood expansion,
  11,250 MW* — larger than any campus planned anywhere, for a colocation operator
  whose whole portfolio is under 500 MW, unquoted, and implying $187k per MW against
  the $2-30M a real build costs. Three independent smells, no contradiction, so
  nothing flagged it. It was the largest number in the database, feeding an
  8.7-million-H200 estimate and every national total. A unit error does not look
  like a lie; it looks like a big number.

  Six checks, each leaning on stated physics or economics rather than a picked
  threshold: `same_figure_two_units` (two sources ~1000x or ~100x apart),
  `campus_exceeds_worlds_largest`, `block_out_of_scale`,
  `giant_capacity_unconfirmed`, `usd_per_mw_out_of_band`, and
  `h200_disagrees_with_capacity`. Free, read-only, no LLM, scopeable to given ids.

  **Nothing is ever changed.** Which of two figures is wrong is a judgement about a
  sentence, so every remedy is a read or a re-read. Unit errors sort first, because
  those are the ones that poison totals. On the live database: 21 findings across 19
  of 206 projects — a rate you can work through, which is the difference between a
  check people run and one they mute. 16 mutants, all caught — measured by hand
  against a copy of the live database, which is why
  `scripts/measure_extraction.py --mutants` now exists to re-run it.

- **Every number in the capex table opens** (`tracker/capex.py`,
  `tracker/webui/dataset.py`, `server.py`, `app.js`,
  `tracker/prompts/capex-overview-v1.txt`). An aggregate a reader cannot open is
  an aggregate they have to trust, and this table asks to be trusted about
  billions.

  - **Click any figure in a row** and it breaks into the sites that make it up,
    and *which* figure decides the view — because "8 sites", "$185B" and "MW at
    risk" are three different questions and one generic list answers none of them
    well. Seven views: every counted site; planned capacity with a share bar and
    the sites contributing nothing; capacity actually running; investment per
    site with the never-confirmed ones marked and their ingest reason on hover;
    what is obstructing which site, with each obstacle's own summary; which sites
    have slipped; and per year column, which sites are dated into it. Clicking a
    site opens its drawer, so the drill-down bottoms out at citations rather than
    at another aggregate. **The panel never sums** — every figure is a stored
    per-project value, so a reader adding the rows up arrives at the cell they
    clicked (verified live: Meta's twelve rows add to 8,250 MW and $27.3B
    exactly), and a site with no cited capacity says so instead of showing a
    zero that would balance the arithmetic and misstate the world. `Position`
    carries `project_ids` / `duplicate_skipped_ids`, ids only, looked up in the
    projects payload the page already has.
  - **Hover a buyer** and a card shows the instant facts with a model-written
    reading of the position streaming underneath — the capex twin of the project
    drawer's AI overview, same fast model (`M2-her`), via a new
    `/api/capex/overview/stream` endpoint with the same contract: POST plus a
    confirm token because it spends, cut at the sentinel, cached by a fingerprint
    over the position's figures and every underlying row's `updated_at`, never
    stored, never evidence. Measured live: first word in 1.7s, whole card in
    ~2.5s, and re-hovering an unchanged buyer is free. The hover waits 450ms so
    mousing down the table does not fire a request per row.

    **The prompt asks for no figures at all, and that is the finding.** Written as
    an analyst on the reader's desk — one characterising sentence, one bullet,
    one open question — the fast model produced good copy and unreliable numbers:
    across four measured rounds it subtracted running from planned and called the
    remainder "3,300 MW due mid-year", summed two sites into "4,200 MW in 2028",
    wrote "only 30% online", spelled out "nine thousand seven hundred and
    thirty-nine megawatts", and invented a source ("likely the January
    statement"). Three rounds of tightening rules did not fix it; removing the
    job did. Every derivation it was reaching for is now computed server-side and
    labelled — each site's share **in words** (`_SHARE_WORDS`, because it also
    got "about a quarter" wrong as "more than a third"), and the unbuilt
    remainder marked `NOTHING DATES IT` — and the prompt's rule is that the
    answer contains no digits, since the figures sit on the card directly above
    it. Measured after: 5 of 7 briefings digit-free, and both leaks were figures
    copied correctly from the context. Names, places and the closing question are
    what this model is good at, so that is all it is asked for now.

- **`tracker logic check --audit N`: does each value's evidence actually say it?**
  (`tracker/logic.py`, `tracker/prompts/evidence-v1.txt`). Every existing check
  asks whether values are supported or agree with each other; this asks the prior
  question — whether the sentence recorded as a value's evidence actually states
  that value *for this project*. The gate that stored the quote checked
  mechanically (the figure appears in the sentence); what survives that and still
  goes wrong is semantic: a programme-wide total quoted as one campus's money
  (`misattributed`), a sentence about a different building (`unsupported`), an
  aspiration recorded as a schedule (`hedged`). One LLM call per row, costliest
  rows first, off by default. Same guard rails as `--read`: a finding must name a
  field the model was shown, use one of the three defined verdicts, give a
  checkable reason and clear the confidence floor, or it is dropped — and nothing
  is ever written. A confirmed finding is answered by demoting the value in
  `tracker review`; an unconfirmed investment figure already stays out of the
  capex sums, so the repair path existed before the audit did.

- **Placeholders are rejected in more shapes, and flagged if they ever reach
  storage** (`tracker/normalize.py`, `tracker/logic.py`). `is_blank` already
  nulled `TBD` / `N/A` / `—`; it now also catches decorated and spelled-out forms
  — `$TBD`, `TBD (est.)`, `to be determined`, `not yet announced`, `N.D.`,
  `...`, `??`, `xx` — via a start-anchored pattern, so a sentence merely
  containing the letters survives and `ND` still reaches the state normalizer.
  Two new free `logic check` rules watch the stored data itself:
  `placeholder_value` (ERROR — a non-answer stored as a fact, meaning some path
  bypassed the normalizer) and `placeholder_quote` (WARNING — a value whose
  recorded evidence is empty or itself a placeholder, so the console would show
  "TBD" as the sentence behind a number). Measured on the live database: both
  return zero today, which is the normalizer earning its keep — the rules exist
  so a regression is caught rather than trusted.

- **The capex table now defends its own numbers** (`tracker/capex.py`,
  `tracker/cli.py`, the console's Capex view). Three changes, each answering one
  way the table was wrong, all disclosing rather than hiding:

  - **One row per suspected campus.** The Abilene Stargate campus was stored four
    times — once per company a source attached to it — and `rollup()` counted
    1.2 GW four times against OpenAI. The rollup now counts one representative per
    `suspected_duplicates` group (a named tenant first, then the largest capacity,
    then the oldest id — the row a merge would most likely keep) and sets the
    others aside in per-buyer `*_skipped` disclosure fields. Skipped, never
    merged: `tracker merge` remains the only real repair. Measured live: 10,293 MW
    and $707.9B set aside.
  - **Only confirmed dollars are summed.** `investment_usd` asserted by a source
    but confirmed by none — the signature of a programme-wide total ("OpenAI's
    $500 billion Stargate") quoted in an article about one campus and demoted at
    ingest by the `MAX_USD_PER_MW` ceiling — is excluded from the sum and
    disclosed as `investment_excluded_usd`. The rollup reads back what ingest
    decided (`Source.unconfirmed_fields`) rather than re-judging the figure, per
    the same rule `webui.dataset._unconfirmed_because` follows. Measured live:
    OpenAI's investment column went from $3,215B to $635B counted, with $2,012.9B
    disclosed beside it — the numbers sum back, nothing is hidden.
  - **The year grid is a continuous range** (`capex.year_columns`), so a year
    nothing is dated for renders as an empty column instead of vanishing — 2029
    now appears, empty, between a dated 2028 and 2030. Quarters stay data-only
    (`capex.quarter_columns`): the quarter view is a shape, and a shape survives
    gaps. Both windows are computed server-side and shipped in the dataset
    payload; the browser's own copy of the window logic was deleted per the
    "browser never re-implements a judgement" rule.

- **`project_alias`: merges that outlive the next crawl** (migration `0010`,
  `tracker/merge.py`, `tracker/upsert.py`). `upsert_record` matches on exact
  `dedup_key`, so a merge used to last exactly until the next crawl: fold the
  OpenAI-angle Abilene row into the Crusoe one, and the next OpenAI-angle article
  quietly re-created it. `tracker merge` now records each folded identity in a
  `project_alias` table (the project-level sibling of `block_alias`, and for the
  same stated reason: a hand-merge with nowhere durable to live evaporates), and
  the upsert consults it after an exact-key miss, routing the record to the
  survivor. Aliases point at project ids and are repointed when their target is
  itself merged, so chains stay flat and resolution is one lookup. `--force-new`
  still bypasses it — the escape hatch for two genuinely separate campuses —
  and deleting a survivor cascades its aliases away, so a stale redirect cannot
  outlive its target.

- **`capacity_block`: what an AI data center actually is** (migration `0009`,
  `tracker/blocks.py`, `tracker/vocab.py`, `tracker/ingest/crawl.py`).

  `project` carried one `phase`, one `mw_planned`, one `mw_built`, one `customer`.
  That was adequate when a campus was either built and serving customers or not
  built. A modern AI campus is several of those at once — 150 MW energised and
  serving one buyer, 150 MW under construction pre-leased to another, 300 MW planned
  with nobody named — and the row could not say it. Measured on the live database:
  **28 projects partly built, 15 in `construction` with megawatts already live, 12
  with power energised while construction was mid-track, 49 with a customer named
  and nothing built, and 52 whose sources described a phase with its own capacity
  that was being discarded into a single `mw_planned`.**

  A block carries its own label, MW, status, customer and dates, each with its own
  citation. The status ladder makes two distinctions the project enum cannot:
  **`shell_complete`** (built, no power — which `tracks.py` calls the most
  informative signal in the dataset and which previously had nowhere to live) and
  **`energized` vs `serving`** (power on, versus a customer running on it).

  Verified end to end against Iron Mountain's Q1 2026 filing. **Project 39 — the row
  that read "AZP-2, 48 MW, $47.4M, online Q3 2026" with the capacity from one
  facility and the money and date from another — now carries `azp-3.phase-3` as its
  own 8 MW block with its own Q3 2026 date.** VA-9 came in as two tranches with
  *different* dates, Q4 2026 and Q1 2027, where before that was one row with one.

  Four design points, each with tests:

  - **Identity is a derived key, never a name a source chose.** `block_key` folds
    "Phase 1" / "Phase I" / "phase one" / "first phase" onto `phase-1`, and makes a
    filing's "AZP-3 Phase 3" meet an article's "Phase 3" *of AZP-3* — the convergence
    project 39 needed. It never decides `phase-1` and `azp-2` are one block on a
    similarity score; that is an operator's call, recorded in a new `block_alias`
    table. Same asymmetry `dedup.py` argues for projects.
  - **A project row is not one campus.** `dedup_key` is `company|city|state`, so one
    row holds every facility an operator has in one city. The likeliest corruption
    this could cause is a generic "Phase 1" from two different campuses colliding and
    summing their megawatts — so blocks carry `parent` and a `generic` flag, and an
    unplaceable block's capacity is **excluded** from the rollup rather than guessed.
  - **The rollup only ever raises.** `reconcile` may lift a scalar or fill a null,
    never lower one or blank one. A block sum is a *floor* on the campus; a cited
    campus total is a different, also-valid figure. That is what let migration 0009
    land on 227 live rows with every value provably unchanged, and it means the
    "9 of 12" count can only go up.
  - **Each block's evidence pool is sealed.** `evidence_gate` deliberately ignores
    field labels and lets any verified quote support any value — earned behaviour
    that recovered 89 values across 64 projects. At block granularity that same
    tolerance *is* the project-39 bug, since block megawatts get summed. So the gate
    runs once per block over that block's own evidence, and a second check requires
    the containing sentence to actually name the block: the segment's head **and**,
    where it has one, its ordinal — because `AZP-2` and `AZP-3` share a stem and it
    is the number that distinguishes them.

  Blocks are a cache rebuilt wholesale from a new `source.blocks` column, the same
  status `confidence` and `h200_equivalent` have, so re-ingest is idempotent and
  `merge` and `recompute_from_sources` get the behaviour without a second
  implementation. `tracker init` recomputes them. `upsert.resolve` was extracted so
  block fields go through the field engine — including the 待确认 rule — rather than
  a copy of it.

  Not yet done, and honest about it: the `logic` rules are not yet re-expressed per
  block and the read surfaces do not show blocks. Deliberately **not** a thirteenth
  tracked field.

- **The rules ask the blocks before calling a campus contradictory**
  (`tracker/logic.py`). Four rules were written when a campus was either built and
  serving customers or not built. On a modern AI campus — part energised for one
  buyer, part still going up — they fired on the ordinary shape of the thing and told
  an operator to fix data that was correct. **That was all 18
  `energized_but_not_operational` findings and a share of another 45
  `past_its_own_date`**, out of 144 total.

  A campus with no blocks keeps exactly the previous behaviour, because no blocks
  means nothing has been read rather than that the row is coherent.

  `past_its_own_date` becomes per tranche: a campus whose phase-1 date passed while
  phase 2 runs to a later schedule is a campus, not a defect.
  `operational_without_built_capacity` splits in two — a *running* tranche whose
  capacity no quote confirms is a missing citation, not a phase to step back.

  Two new rules are the design's own instrumentation: `block_label_ambiguous` for a
  tranche that cannot be placed, and `blocks_may_double_count` for labels nested
  inside one another. Riot Rockdale came back as "AMD Lease", "AMD Lease Initial
  Deployment" and "AMD Lease Expansion" — one lease at three grains, whose megawatts
  sum three times. All four block rules are report-only: each names something only a
  person can settle, and an automatic edit would be guessing at exactly the grain
  this design exists to stop guessing at.

- **Blocks are visible** (`tracker show`, `to_json_object`, the console drawer). The
  block table had been written to since migration `0009` and read by nothing, so the
  richer state existed in the database and was invisible in the product. `show` gets
  a table above the sources, because on a partly-built campus it is the answer to
  "what state is it in". The JSON carries an `mw_counted` flag per block — without
  it the tranche figures appear not to add up to `mw_planned`, since a 待确认
  capacity is shown and deliberately not summed. The drawer's Blocks tab is offered
  only when there are blocks: an empty tab on 88% of the database would read as
  "this campus has one tranche", the opposite of what a missing backfill means.

- **Capex attributes capacity per tranche** (`capex.block_shares`). Lake Mariner is
  378 MW being built for Fluidstack beside 60 MW already serving Core42, and all 750
  went to whichever name reached `project.customer` first. Capacity a tranche assigns
  to a named buyer now goes to that buyer; the rest of the campus stays where it was,
  so the total is **conserved rather than replaced**. A tranche books on its own
  date, so a buyer's capacity appears when it first arrives instead of when the last
  building finishes.

  Megawatts are split and money is not: a tranche states its capacity often and its
  share of the investment almost never, so splitting the money would mean inventing a
  ratio. One consequence stated plainly — the `projects` column can now exceed the
  row count, because two buyers at one campus really are two positions.

- **A shared tranche is evidence of a duplicate row** (`capex.suspected_duplicates`).
  Three rows in Andrews, TX each hold the same 70 MW AWS block, so an unmerged pair
  now double-counts at the tranche grain as well as the campus one. A derived
  `block_key` on both rows is much harder evidence than a name resemblance, so it
  raises pairs the name test misses and is reported as the argument for the ones it
  finds. Generic keys are excluded — half the database has a `phase-1`, and pairing
  on it would pair everything.

- **`tracker backfill blocks`** (`tracker/backfill.py`). The 227 rows ingested
  before migration `0009` have no blocks, because turning an article into blocks
  needs the article text rather than the schema. This re-reads the stored articles
  for that one purpose.

  Deliberately not `ingest crawl --force`, which would re-extract every scalar with
  a model that behaves differently today than it did at ingest time — churning 227
  rows and every `updated_at` inside what is meant to be a backfill. This writes one
  column, `source.blocks`, and lets the rollup do the rest. Keyed on URL, not source
  row: **373 crawled sources are only 229 distinct articles**, since 62 feed more
  than one project, and 193 are already cached. Resumable, idempotent, filings first.

  **The hard part is deciding whose blocks these are**, and both guards exist because
  the unguarded version wrote real wrong numbers into a copy of the live database:

  - **Locality, never the operator.** An 80 MW "Portland Expansion" landed on eight
    STACK rows — San Jose, Prince William, Chicago, Avondale, Fort Worth, New Albany
    and two Portlands — because the only thing being matched on was a company name
    all eight share. Matching now runs over name and city, and a *disagreeing* city
    is a veto rather than a low score: "STACK Infrastructure"/Chicago against "STACK
    Infrastructure"/Portland still scores 0.67 on the operator's two words alone.
  - **A portfolio article is split, but only when it is one.** A Core Scientific
    filing describing Denton, Dalton, Austin, Marble and Muskogee gave all six of its
    blocks to both the Denton row and the Dalton row, recording 588 MW twice. Each
    block is now routed by its own label — against what *distinguishes* the sibling
    rows, since the tokens they share are exactly what makes them indistinguishable.
    Demanding this unconditionally was tried first and was worse: it emptied Lake
    Mariner, whose blocks are "Akela" and "La Lupa", because a building is usually
    named after nothing in particular. So portfolio-ness is detected from the
    article rather than assumed, and only then must every block earn its place.

  **A 待确认 capacity is shown and not summed** (`blocks.mw_is_confirmed`). Found by
  reading the first live tranche rather than by a test: `rollup` summed every
  placeable block's megawatts regardless of whether a quote named them, and since
  `reconcile` records no tier, the campus then asserted the total as though it were
  cited. **It raised Applied Digital Jamestown from 7 MW to 7,500 MW off one
  unconfirmed block**, and CHI-1 from 12 to 36. Keeping an unconfirmed figure is the
  whole point of the 待确认 tier; *summing* it launders it. Blocks still show the
  number — `reconcile` now discloses which ones are excluded and why. The three
  contaminated scalars were repaired by re-deriving them from their citations, which
  `Policy.MAX` cannot do on its own because it counts the existing value.

  Both guards skip rather than guess, which is the recoverable direction. Verified on
  a nine-campus article that routed Colossus to Memphis, Stargate to Abilene and
  Prometheus to New Albany, and dropped an unattributable "Planned 600 MW Expansion"
  entirely. **Lake Mariner now reads the way the schema was rebuilt to allow: 378 MW
  under construction for Fluidstack beside 60 MW already serving Core42.**

- **`h200_equivalent`: capacity restated as accelerators** (migration `0008`,
  `tracker/compute.py`). Megawatts is what gets reported and is not what anybody
  is asking; the question behind these rows is how much training capacity a site
  represents.

  Derived from capacity by default and tiered `derived`, like `county` and the
  coordinates — a unit conversion, not a new claim. An article that states a chip
  count outright beats it and comes through the evidence gate with its own quote.
  A site nobody has sized stays **null rather than zero**, because a zero would be
  summed and every total would be wrong.

  The ratio is **1.3 kW per H200 at the meter** (~770 per MW), built from
  published figures rather than picked: the H200 SXM board is 700 W, a DGX H200
  draws 8.5 kW for eight of them — 1.06 kW per GPU of node-level IT load, which is
  what a data center actually powers — and a liquid-cooled AI hall is underwritten
  at PUE 1.15–1.25 for 2026. Every input to that ages, so it is
  `TRACKER_KW_PER_H200` and the column is recomputed rather than migrated:
  `tracker init` re-bases the whole table. Output is rounded to two significant
  figures, because the megawatt input was rounded before it was published.

  Deliberately **not** a thirteenth tracked field. Those twelve are the PRD's
  definition of done and "9 of 12" is quoted in the docs, the console header and
  the export.

- **A command box on the Commands page**, for what the forms cannot say —
  `merge 4 7 9 --into 2`, `ingest crawl --url a --url b`, several positionals.

  It is not a shell and does not become one. The line is parsed server-side by
  `catalog.parse_command_line` into exactly the `(cmd, flags)` a form produces, and
  `build_argv` turns that into the same validated argument list. `cd`, `rm`, `;`,
  `|` and backticks are words the catalog has never heard of and are refused by
  name, with a suggestion when it is a near miss. A blocked command is still
  blocked; confirmation is still required and is a second Enter. Tab completes, ↑
  recalls. Running from the box does not jump to the Runs view — that would
  unmount the box and take its history with it — so it prints a "view output" link
  instead.

- **`TRACKER_TUNNEL_NAME` / `TRACKER_TUNNEL_HOSTNAME`**, so `tracker cloudflare`
  publishes to a permanent hostname with no arguments. They are settings rather
  than flags retyped every session because they describe the machine, not the run:
  the tunnel credentials are already in the home directory and the DNS record is
  already in the zone.

  The pair is taken **together, and only when neither flag was given**. An
  explicit `--name` does not inherit the configured hostname — printing a real
  hostname beside a different tunnel produces a URL that looks right and points at
  the wrong thing, which is worse than printing none. `--quick` ignores the
  configuration and gets a throwaway URL; it and `--name` are mutually exclusive.
  `serve --tunnel` reads the same pair, so the two ways of publishing cannot land
  on different URLs. A hostname configured without a name is refused by name.

  `--check` now reports where it will publish, and says the DNS route is *not*
  verified from here — `cloudflared tunnel route dns` writes a CNAME this command
  cannot see, so a hostname pointing at a deleted tunnel reports clean and then
  fails at the edge with a 1033.

- **Routines in the console** (`webui/workflows.py`): four named sequences —
  *Catch up on the news* (`sync → ingest geo → logic check`), *Deepen what we
  already have*, *Tidy the database*, *Prepare a report* — run as **one job with
  one log and one entry in the run history**, not chained by the browser.

  The console could always run any single command, which left the operator doing
  the sequencing by hand and guessing at the order. The order matters: deriving
  geography before reading articles wastes the derivation. Each step states why it
  follows the last, in the UI and in the log.

  Every guarantee the single-command path enforces holds here — a sequence cannot
  reach a blocked command, cannot spend tokens without confirmation, and reports
  the cost of its most expensive step. Every step's argv is built before anything
  executes, so a typo in step three is not discovered after steps one and two have
  spent money. Stops at the first real failure; `duplicates` and `logic check` exit
  non-zero when they *find* something, which is a finding, so those are marked
  `tolerate_failure` and the run continues.

- **`tracker point --url URL`** (repeatable): read a specific link instead of
  searching for one, for a press release or a filing that search will not surface.
  Works whether or not the campus is already tracked — the links go through
  `crawl.run` like any other article, so the evidence gate, the dedup key and the
  merge policies all apply unchanged. The identification is advisory on this path:
  it says which row to expect, and the dedup key decides. Overriding it with the
  matched id would let a mistyped name file a filing under the wrong campus, which
  is the one error nothing downstream detects.

- **Streaming for the written briefing** (`llm.MiniMaxExtractor.stream`,
  `overview.stream`, `POST /api/overview/stream`). Reasoning is filtered out *as it
  arrives* — these models put `<think>` in the content field, and `<think>` shows
  up split across frames as `<th` then `ink>`, so a naive passthrough types the
  model's private deliberation into the drawer and then has to erase it.

  Deliberately not retried: `_post` can replay a failed request because nothing has
  been shown yet, but once the first token has reached the reader a retry restarts
  the paragraph mid-sentence. A stream that dies partway is not cached either —
  half a briefing that stops mid-sentence would otherwise be served as that row's
  reading forever, since the content it is keyed on never changes.

- **A survey of government data sources, and the decision not to build one**
  (`docs/government-sources.md`, `scripts/probe_government_sources.py`). Four
  uniform machine-readable routes were tested against the markets holding the
  capacity, and all four failed:

  Municipal permit portals (Socrata) have one API across ~460 jurisdictions and
  none of the ones that matter — Loudoun, Prince William, all of Virginia and
  Arizona return zero datasets; the hits that exist are $500k server-room
  retrofits. FERC and the state PUCs hold the right content behind ASP.NET forms
  with no API. County news feeds produced **0 candidates from 177 entries** scored
  by this project's own discovery filter, with Abilene's city feed silent on
  Stargate Abilene while it is built there. Legistar's agenda API does not have
  the data-center counties as clients, and its "data center" matters are municipal
  IT procurement from 2005.

  Shipping any of them would have added a source class that yields nothing while
  making the source mix look like government coverage exists. The probe script
  exists so the finding can be rechecked rather than believed — portals change.

  What does work is recorded alongside: utilities file large-load and
  interconnection commitments **with the SEC**, which is the already-built
  `ingest edgar --kind utility` path, and a specific `.gov` document can already
  be read with `ingest crawl --url` and is filed as `government_doc`
  automatically. Bulk discovery is the part that does not exist.

- **`tracker logic check` and `tracker logic resolve`**: find values that
  contradict each other, and settle the ones that can be. Every other check
  asks whether a value is supported; this asks whether the supported values agree.
  A row can be perfectly cited and still be impossible.

  Three layers, cost-ordered. **Rules** are free and deterministic — paying a model
  to notice `mw_built > mw_planned` is paying for arithmetic, and a rule states
  its reasoning where anybody can argue with it. On the live database: 21
  impossibilities and 125 warnings across 221 projects.

  **Collisions** report which of two conflicting sources the database keeps, and
  why. The winner is asked for, never re-derived: `upsert.resolve_field` is the
  function the write path uses. That mattered more than expected — the first
  version assumed credibility always decides and reported **73 of 221 rows** as
  drifted from their sources. Each field has a declared policy: built capacity
  takes the largest, `first_announced` the earliest, `phase` the furthest along
  unless something says it stopped, identity fields are never overwritten. Only
  the rest go by credibility then recency. After asking the real resolver, drift is
  **zero**, which is the correct answer for a working write path. `resolve_field`
  is now public for exactly this reason.

  **Judgement** is `--read N`, one call per project, off by default. Findings must
  name two fields and quote their evidence or they are dropped; it may not pick a
  collision winner, because which of two cited numbers is right is a question
  about sources and sources have weights and dates so nobody has to guess. Nothing
  it says is written. Across four rows read during development it returned
  nothing — the guards hold; its usefulness here is unproven and the rules are
  carrying the value.

  Also surfaces a quirk rather than silently working around it:
  `tracks.standing` reads an event's type and never its date, so a milestone dated
  next December counts as reached today. `milestone_in_the_future` reports it,
  because redefining "reached" would move every track strip in the product and
  that is not this command's call to make.

  **`logic resolve` settles what can be settled.** The first cut of this shipped
  as reporting only, which under-read the ask: finding a collision and applying
  its winner are two halves of one job. A row drifts when its stored value is no
  longer what its own citations support — after a hand edit, or a source attached
  by a path that did not re-derive — and `upsert.recompute_from_sources` was
  reachable only through `merge`, so there was no way to repair one. Now there is,
  and it previews unless given `--apply`.

  It settles that and nothing else, which is the honest scope. Measured on the
  live database: **0 of 149 findings** were mechanically resolvable. Whether
  100 MW built against 32 MW planned means a revised plan or two figures about
  different phases of one campus is not in the row, and a tool that picked one
  would be inventing a fact.

  Worth stating plainly because it is the part most likely to surprise: **five
  fields are not decided by credibility.** Built capacity takes the largest cited
  figure, `first_announced` the earliest, `phase` the furthest along unless a
  source says it stopped, and the identity fields are never overwritten once set.
  Only the rest go by source weight and then recency. `logic check` prints which
  rule settled each collision so the reason is always on screen.

- **Utilities and contractors as SEC filers** — 20 companies added to
  `seed/edgar-companies.toml`, taking it from 20 to 40. The power company is the
  counterparty that cannot be bypassed, and its filings say what no operator
  press release does: which large load has actually signed an interconnection
  agreement, and when energisation is expected. `power` is the track this
  database is worst at and the one it refuses to infer from construction, so this
  aims at the gap rather than at more of what already works.

  **Chosen by measured exposure, not by size.** Each utility serves a state
  holding ≥1% of tracked capacity — TX 33.8%, CO 11.7%, NM 8.5%, OH 8.4%,
  GA 7.8%, VA 7.8% — because a utility for a state with no projects is a
  subscription to noise. Constellation and Talen are in for one specific reason:
  a nuclear PPA names its counterparty and its site, which is exactly the
  customer-attribution fact 60% of the database is missing. Contractors are in
  for backlog, which leads energisation by a year or two.

  **Each class is searched with its own phrases** (`[search.by_kind]`). A utility
  does not write "build-to-suit"; it writes "large load" and "interconnection
  agreement". Adding a class of filer without adding its vocabulary is how a new
  source comes back empty and looks like it had nothing to say. Verified against
  the live API: `--kind utility --per-company 1` found 19 filings and prepared 17.

  `--kind` filters a run to one class, which is also the cost dial now that the
  list covers five kinds — reading the utilities is a different question from
  reading the hyperscalers and is worth being able to ask on its own. Utilities
  and contractors are deliberately *not* end users in `capex.attribute`, so
  adding them cannot move anybody's attributed capacity; there is a test.

- **Quarterly buckets on the pipeline.** "Whose capacity lands next quarter" is a
  question a year column cannot answer, and it was the one thing the Capex view
  was asked for and did not have. `tracker capex --by-quarter` and a year/quarter
  toggle on the page.

  Shipped with its own caveat measured rather than assumed: `capex.date_precision`
  counts how many dated projects land on 1 January, which is where a source that
  said only "2027" normalises to. On the live database that is **34%** — so the
  quarters are a shape and the years are the number, and both surfaces say so.

- **`tracker cloudflare`**: publish the console through cloudflared as a
  first-class command rather than a flag on `serve`. Same loopback bind, same
  password requirement, same refusal to publish without one — `serve --tunnel`
  still works and both now run one shared implementation, because the interesting
  part is the refusals and a second copy of those is a second place for one to go
  quietly missing.

  Two things it adds. `--check` verifies the password, that cloudflared is present
  *and executes*, that a named tunnel exists, and that the database and front end
  are there, then exits — worth running once, since a truncated `cloudflared.exe`
  is a valid PE file that dies with WinError 193 and no output. And `--name`
  /`--hostname` run a named tunnel on your own account, so the hostname is yours
  and survives a restart. Creating that tunnel is deliberately not done for you:
  `cloudflared tunnel create` and `route dns` write credentials into your home
  directory and a record into your DNS zone, both of which outlive the process, so
  the command prints the three lines to run and stops.

  `cloudflare` is blocked in the console's own command palette. The stated reason
  is not that it would hold the run slot — it would — but that putting this page
  on the public internet is a decision for somebody at a terminal.

### Changed

- **`tracker risks` leads with whether an obstacle is quoted** (`tracker/cli.py`).
  The old layout printed every obstacle at the same weight and admitted at the
  bottom that a third of them rested on nothing quotable. That is the wrong way
  round: it is the first thing a reader needs and it was the last thing they were
  told. A kinds table now carries it per category — `4/15` on `grid_capacity` says
  something different about that number than `16/18` on `transmission` — each
  obstacle is marked in place, `--uncited` shows only those, and the footer breaks
  the 待确认 count down by *why* rather than reporting one total. Duplicate rows
  (same project, same category, same sentence, different `first_seen`, which the
  unique constraint permits) are collapsed to one line.

- **`tracker infer` answers two questions, laid out as two questions**
  (`tracker/cli.py`). It was a four-column table whose widest column was free
  prose, so reasoning wrapped to two characters a line on a narrow terminal, plus
  a run of unaligned "watch for" lines underneath. Now a heading per question, the
  judgement on its own line with a confidence bar and the figure beside it, the
  reasoning indented under it, and the provenance stated plainly at the end.
  `--json` emits the same structure, which is what the console's new panel reads.

- **`tracker duplicates` says what raised each pair, and raises fewer of them**
  (`tracker/dedup.py`, `tracker/capex.py`). "These two look similar" is not
  something a reader can check. Pairs are now sorted by evidence — a shared tranche
  key, a shared operator, a shared name word — and each prints the evidence beside
  it, with `--no-weak` to hide the last kind.

  Two live false positives forced the rules to tighten. `Aligned Data Centers
  Phoenix` matched `NTT Global Data Centers Americas Phoenix` on the token
  "centers", because the generic-word list held the singular and not the plural.
  `Element Critical — Houston One` matched `Switch — Houston Data Center Campus`
  because both had a tranche labelled "existing", a real word naming a kind of
  tranche and no particular one, which the `generic` flag cannot catch because it
  reads the label's own words. **A tranche key that turns up in more than one town
  is vocabulary, not identity.** Rarity is measured across localities rather than
  across rows, so the flagship case survives: the Abilene campus is stored four
  times and all four hold `building-1`, in one town.

- **A risk whose quote fails is kept as 待确认 instead of deleted** (migration
  `0012`, `tracker/ingest/crawl.py`, `upsert.py`, `capex.py`, `cli.py`).
  Migration 0006 gave every *field* a third outcome — retained, flagged, never
  treated as fact. A risk was the one thing in the ingest path that still went on
  the floor: fail the verbatim check or the category check and the obstacle, its
  severity and the model's summary were all discarded. That fell hardest on the
  field this database is worst at, because no press release names its own
  blocker, so an adversarial second source is the only thing that ever records
  one.

  The quote that failed is still not stored — a sentence is never kept beside the
  thing it failed to support, the same rule `evidence_gate` applies to fields —
  but `risk.unconfirmed` records *why*, and `tracker risks` prints the reason
  rather than a bare "uncited": "quoted nothing for it" sends you to find a
  source, "the quoted sentence does not state this category" sends you to correct
  one you already have. Two things still drop, because there is no 待确认 version
  of them: a category outside the vocabulary, and a missing summary.

  Unconfirmed risks **count** toward `tracker risks`' MW sums and the capex
  obstructed column, with the count disclosed in both footers — understating
  exposure is the worse error, and a total whose composition a reader cannot see
  is what this database exists not to produce. They may not, however, quietly
  become `project.blocker`: that is one of the twelve tracked fields, so
  `vocab.risk_precedence` ranks a confirmed obstacle above an unconfirmed one
  everywhere the "most severe open risk" is chosen, and an unconfirmed winner
  marks `blocker` unconfirmed on the source too. Without that, an obstacle the
  gate refused would arrive in `source.fields` reading as cited and be counted by
  confidence and by the 9-of-12 measure.

- **The 待确认 tier records why, not just that** (migration `0013`,
  `tracker/vocab.py`, `crawl.py`, `capex.py`, `webui/dataset.py`). One bit could
  not be acted on, because the tier covers situations that ask for opposite work:
  a figure nothing quotable backs wants another source, while a programme-wide
  total lifted from an article about one campus wants correcting — going looking
  for a citation would find one, and it would still be the wrong number.

  `evidence_gate` now returns a reason per refused field from a shared vocabulary
  (`no_quote`, `quote_unverified`, `quote_off_target`, `out_of_scale`), stored in
  `source.unconfirmed_reasons`. The consequence that matters is in `capex`: the
  investment column excluded *every* unconfirmed figure, so it dropped the
  programme total it meant to drop and also every campus figure nobody happened
  to quote — understating the one number the table exists to state. It now
  excludes only `out_of_scale`, counts the merely-unquoted, and discloses that
  sum separately. Sources written before this migration have no reason recorded
  and are read as excluded, which is exactly the previous behaviour, so no number
  already being reported moves.

  This also replaces the seam it was named after: `webui.dataset
  ._unconfirmed_because` reconstructed the one reason it could by string-matching
  a marker in the project's notes, and so could only ever speak about the scale
  demotion. It reads the column now, and every other field can say something
  about itself.

- **A rejected evidence quote says what was rejected** (`tracker/ingest/crawl
  .py`). The warning named the field and nothing else, so answering "are we
  losing a lot of data?" needed an instrumented replay of the extraction path. It
  now carries the offered quote and the longest run that really matched:

  ```
  evidence quote for 'mw_planned' is not in the article (best run 34 of 200 chars, 17%);
    offered: 'Vantage has begun construction on a data center campus in Port Washington…'
  ```

  Two lines, and every ordinary run now produces the dataset that question
  deserved. `_verbatim_run` returns the run statistics alongside the recovery so
  the failing path does not pay for the expensive match twice.

- **The console's Capex view now explains itself at first glance** (`app.js`).
  It opened with thirteen columns of raw numbers; a first-time reader — the
  person this page exists for — had to assemble the story from footnotes. Now:
  a five-number headline strip (GW planned, GW running, confirmed spend,
  claimed-but-not-counted, share with a named buyer) sits above the table; the
  columns carry plain-language names with a group row that says what the year
  columns *are* ("MW arriving, by year"); a buyer whose every row was set aside
  as a duplicate says so in words instead of rendering as a rank of dashes; and
  a buyer's excluded money shows inline beside the confirmed figure
  ("$185B +$465B claimed"), so "where is the $500B headline?" is answered on
  the row itself. The coverage tiles moved below the table — the headline strip
  now carries the essential caveat (share attributed) above the fold. Also fixed
  the merge-review card's claim that survivor choice "does not matter": identity
  fields keep the survivor's values, and the card now says so.

- **Project pickers search; so does the projects table.** The command form's
  pickers were `<select>`s holding all 224 rows, which is not a picker but a wall
  — and `merge`, which takes several ids, therefore looked like it took one. They
  are now type-to-filter with the results as removable chips. The table's search
  box gained the same matcher, so **`#42` finds project 42** and `meta ohio`
  narrows on both words. One definition of "matches", used in both places, because
  they had drifted: the table searched six fields and not the id, and the pickers
  did not search at all.

- **The AI overview is a card in the stats flow again**, under the figures it is a
  reading of, matching `.mrd-card`'s radius and shadow. Pinning it above the tab
  strip made it the one block in the drawer that could not be scrolled past.

- **The streamed reveal is smoother.** The animation loop was being torn down and
  rebuilt on every SSE frame, because the text sat in its effect dependencies — the
  cursor restarted several times a second, which was most of the stutter. It is
  created once now, paced by elapsed time rather than by frames, and reveals whole
  words: simulated against the old algorithm on identical arrivals, **23 renders
  with a half-drawn word became 0**. A word growing letter by letter re-wraps the
  line under it, which reads as twitching rather than typing.

- **The briefing is written by `M2-her` and is a third as long.** New
  `TRACKER_MINIMAX_FAST_MODEL`, a third model setting beside extraction and
  reasoning, because this call's constraint is neither volume nor depth but
  *latency*: the panel generates when a row is opened, so the model's speed is the
  page's speed. **46.6s → 2.7s to the first word, 231 → ~65 words.**
  `tracker infer` keeps the reasoning model, where nobody is waiting.

  Chosen by measuring every model on this prompt, and the ranking is not the one
  the model list implies. Time to the first *visible* word — tokens inside
  `<think>` are invisible, so a model that streams instantly and then deliberates
  is not fast:

  | model | thinks | first visible word |
  | --- | --- | --- |
  | `MiniMax-M3` (previous default) | yes | 46.6s, **and returned nothing at all** |
  | `MiniMax-M2.5-highspeed` | yes | 17.9s |
  | `MiniMax-M2.7` | yes | 16.0s |
  | `MiniMax-M2.7-highspeed` | yes | 15.5s |
  | `MiniMax-M2.1-highspeed` | yes | 12.5s |
  | `MiniMax-M2` | yes | 12.4s |
  | **`M2-her`** | **no** | **2.7s** |

  Note that plain `MiniMax-M2` beats every `-highspeed` variant. "Highspeed" is
  output tokens per second, and this job emits ~70 words after a fixed slab of
  reasoning, so throughput is the one thing that barely matters. `MiniMax-M3` is
  worse than slow: it spent the entire completion budget thinking and returned an
  **empty** briefing. Only removing the reasoning moves the number.

  **`M2-her` is the only MiniMax model that does not think**, which is why it is
  the default: **2.7s to the first word against 12.4s**, 65 words on average,
  nothing leaking. Three things were needed, and all three benefit every model:

  - the prompt asks for an `[[END]]` sentinel, because the API's own `stop`
    parameter is accepted and **ignored**;
  - `overview.RUNAWAY` cuts the *stream* at the sentinel, or where the model
    starts a second answer. Cutting the stream rather than the text is where the
    time is saved — abandoning the generator closes the connection, so the tokens
    after the answer are never waited for. Unchecked, `M2-her` writes 756–982 words
    against a 110-word instruction, repeats itself under "Final answer (last
    round)", and narrates its own word count;
  - `MODEL_TOKEN_CAP` clamps the budget to the 2048 it accepts. Without it every
    request was an HTTP 400 and the model was not selectable at all.

  **The known cost.** The shape was never the real problem. On a row whose
  construction track read `nothing reached` and whose other four tracks had
  passed, it wrote *"All tracks complete; construction the last to finish"* — the
  most informative field in the row, inverted. It has also named a utility and a
  permit process appearing nowhere in the data, and written phrases that mean
  nothing ("Major capex is confirmed via gas"). `MiniMax-M2` read the same row
  correctly. The behaviour is variable — four later runs on that row were clean.

  Taken deliberately, with the failure measured rather than assumed, and bounded
  by what the panel already is: labelled as a model's reading, never stored, never
  a source, unable to move confidence. A wrong briefing is a wrong *opinion* beside
  correctly cited values, not a wrong value. `TRACKER_MINIMAX_FAST_MODEL` =
  `MiniMax-M2` buys the accuracy back for about ten seconds a row.

  Thinking cannot be switched off on the rest: `thinking: {type: disabled}`,
  `reasoning_effort: none`, `reasoning_effort: minimal` and
  `enable_thinking: false` are all accepted by the API and all ignored, and an
  assistant prefill of `</think>` does not suppress it. Shrinking the prompt does
  not help either — 1300 fewer characters of provenance quotes moved the first word
  by less than the run-to-run noise. The reasoning is a fixed cost; the model is
  the only lever.

  `overview-v2` asks for one opening sentence and two or three bullets in
  markdown, 110 words maximum. It also carries a rule the fast model needed: read
  THE FIVE TRACKS one line at a time, and call a track blocked only if its own line
  says `nothing reached`. Without it the model merged two tracks into one claim —
  "permits and power both show nothing reached" for a project whose permits line
  read `permit_filed, permit_approved`. That is a false statement about data on the
  screen beside it, which is the worst thing this panel can do. Measured over 8
  projects: **2 misread a track before the rule, 0 after.**

- **The briefing panel generates on open and streams.** All three were reversals of the original design, which put it last
  behind a *Write a briefing* button on the reasoning that a drawer spending money
  when you click a row is a drawer nobody dares click. In use almost nobody clicked
  the button, so the panel cost nothing and did nothing. Cost control moved to
  where it belongs — the briefing is cached by content, so a row is paid for once
  and reopening is free — and generating on open then made the wait the first thing
  that happens when you open a row, which is what streaming is for.

  It is a card in the stats tab's ordinary flow, under the figures it reads. It
  was briefly pinned above the tab strip; that made it the one block in the drawer
  you could not scroll past, and put a model's reading in front of the numbers it
  is a reading *of*. It matches `.mrd-card`'s radius and shadow so it belongs to
  the page — the tint and the `inferred` hue are what mark it out, not a different
  shape — and it keeps its header saying a model wrote it.

  **Smoother streaming.** The reveal loop was being torn down and rebuilt on every
  SSE frame, because the text was in its effect dependencies — the cursor
  restarted several times a second, which was most of the stutter. It is now
  created once and reads the text from a ref, paced by elapsed *time* rather than
  by frames (so it looks the same on any refresh rate), and it reveals whole words
  rather than characters. Simulated against the old algorithm on the same clumped
  arrivals: **23 renders with a half-drawn word, down to 0** — a growing word
  re-wraps the line under it, which reads as twitching rather than typing.

- **The command form is built for someone who has not used a terminal.**
  Plain-language labels with the real flag beside them; a **project picker** instead
  of an id you had to go and look up in another tab; presets around the CLI's own
  default instead of an empty number box; required fields first with the rest folded
  behind *N more options* (`sync` has thirteen flags and all thirteen have defaults
  that work); and missing required values named before the click rather than by the
  server after it.

  Confirmation now distinguishes the two losses it was conflating. `merge` cannot be
  undone, so it keeps the typed command name — deliberate friction in front of an
  irreversible act. Spending tokens is recoverable, so it takes an explicit second
  click instead of homework. The server-side rule is unchanged; the UI supplies the
  string only after the operator has explicitly confirmed, which is exactly what
  that rule checks for.

  The argv preview stays. It is the honest record of what will run, it is what you
  paste into a terminal on a read-only console, and it is how the friendly labels
  teach the flags they stand for.

### Fixed

- **The search blocklist matched substrings, and `"x.com"` blocked every
  `equinix.com` URL** (`tracker/ingest/search.py`). `is_useful_host` tested
  `skip in host+path`, so a top-five operator whose newsroom already answers 403
  — search was the one path to its coverage — was silently unreachable, along
  with xilinx.com, spacex.com and anything else ending in those letters. Domains
  now match on label boundaries (`host == entry` or `host.endswith("." + entry)`),
  with `bloomberg.com/profile` carried as the one host+path prefix rule.
  Regression-tested against equinix.com, spacex.com, and a Bloomberg article URL.

- **`datacenters.atmeta.com` classified as `general_media`** (`seed/feeds.toml`).
  Meta's dedicated data-center site — the top search result for a Meta campus,
  one page per campus plus a news archive — was weight 1, ranking the operator's
  own page below the trade press that rewrote it. Added as a `company = "Meta"`
  newsroom sitemap (robots.txt allows crawling and advertises the sitemap,
  verified live), which makes it `company_filing` weight 3, matchable on
  locality alone, and sweepable by `sync --deep`.

- **Every URL `tracker queue` printed was a prefix of a real one** (`tracker/cli.py`).
  The column was `url[:60]`, which looked tidy and was the most damaging thing in
  the output: the link on screen 404'd in a browser and matched nothing in `--drop
  --url`, which is the only handle the command offered. A queue whose links all
  fail is a queue nobody trusts. Whole URLs now, with a row id beside them that
  cannot be mistaken for a link and that `--drop --id` takes, plus `--feed` to work
  one source at a time and newest-first ordering, because a queue is read by a
  person deciding what is worth a crawl while the crawl drains oldest-first.

- **`tracker logic resolve --llm` spent most of its budget on questions no edit
  could answer** (`tracker/logic.py`, `tracker/cli.py`). A live run read as twelve
  refusals before a single decision, and both causes were ours.

  Eleven of the sixteen rules offer no action on purpose — a phase enum arguing
  with a campus that is half energised is a contradiction in the schema, not in
  the data — and `decide` can only ever answer "nothing to choose between" for
  them. That was 174 of 283 findings, and with the queue ordered by project id the
  first `--limit 30` was almost entirely made of them. They are now counted and
  reported in one line, and the limit is a budget for calls that can act.

  The rest were duplicates: two `risk` rows for one obstacle produce two identical
  findings, and the action offered for one closes both.

- **The triage prompt was shown the wrong page, and the model correctly said so**
  (`tracker/logic.py`). A finding about an open `grid_capacity` obstacle on a
  finished power track declares `fields=("blocker",)`, so the evidence assembled
  for the model was one provenance quote behind a derived string — a sentence
  about something else. The model read it and answered "the provided quote
  addresses grid service approval, not the turbines", which looked like a stubborn
  model and was a prompt with the wrong page open. Findings now carry `subjects`
  (`risk:water`, `track:permits`, `event:energized:2027-06-01`) and the context is
  assembled from them: the obstacle with its own quote and source, the milestone
  with its date and the sentence that reported it. The declines that follow are
  now substantive — "the noise complaint was first seen after the permit was
  approved" — so they are printed in full rather than clipped at 90 characters.

- **The console served a stylesheet whose imported layers were unversioned**
  (`tracker/webui/assets.py`, `server.py`). `stamp` puts a version token on every
  URL the *page* references and did nothing for the twelve files `styles.css`
  itself pulls in with `@import`, so a browser or an edge cache could hold one
  layer from last month behind a parent that looked current. The visible symptom
  was the form layer missing: every dropdown fell back to a native control with
  the custom chevron still drawn beside it, and the switches rendered as bare
  buttons. Stylesheets are now served with their imports inlined and relative
  `url(...)` references re-anchored, and a parent's token covers every file it
  imports — so editing a layer changes the parent's URL, which is the whole
  mechanism. It also removes twelve serial round-trips, since an `@import` is
  discovered only after its parent has been parsed.

- **`_walk` dropped a command the moment it grew a subcommand**
  (`tracker/webui/catalog.py`). The catalog skipped any group and recursed into
  it, which was right while every group was only a namespace. `duplicates`,
  `audit` and `risks` all run on their own, and all three vanished from the
  console's palette the day they gained a subcommand.

- **Becoming a group cost four commands their `--help`** (`tracker/cli.py`).
  Typer takes a group's help text from `Typer(help=...)` when that is given and
  from the callback's docstring when it is not, so passing a one-line summary
  replaced the full explanation every other command in this CLI prints.
  `tracker duplicates --help` said eleven words and stopped. The four invokable
  groups no longer pass `help=`, which also restores the top-level listing to
  each docstring's own first line.

  Each group's prose now opens by saying what the *bare* form does, because the
  usage line reads `[OPTIONS] COMMAND [ARGS]...` and implies a subcommand is
  required — for these four it is not. Three tests pin all of it: the
  explanation survives, the bare-form sentence is present, and every subcommand's
  help is more than a restatement of its own name.

- **A stored number could outlive the claim that produced it, unseen**
  (`tracker/logic.py`). Two new free rules, `value_above_its_evidence` and
  `value_without_evidence`, ask whether a stored scalar agrees with the citations
  under it — as opposed to `logic check`'s usual question, whether a row's fields
  agree with each other.

  **Stargate Abilene is why.** The row read `mw_built = 1200` while the only
  `mw_built` claim on it was a well-quoted **200**. Both "1.2 GW" quotes had since
  been re-extracted as `mw_planned`, correctly — committed capacity is not
  energised capacity — but `mw_built` merges by MAX and `_resolve` counts the
  stored value among its own candidates, **so MAX can never come back down**. The
  figure outlived its claim by 1,000 MW, against a ~0.4 GW satellite read and the
  project's own `phase-1` block of 200 MW serving.

  `check_collisions` already reports `stored_disagrees` — the row drifted from its
  sources — but only *inside a collision*, and a collision needs two claims on one
  field to compare. **One claim and a row that disagrees with it was invisible**,
  which is the cheapest shape the error can take. Measured live: 226 collisions, 4
  flagged, and Abilene's 1,000 MW not among them.

  Both rules consult the **block rollup** as well as the claims, because
  `blocks.reconcile` deliberately raises a campus scalar to the sum of its
  tranches; the first cut ignored it and reported 28 rows behaving exactly as
  designed. Restricted to money and megawatts, where an unsupported figure
  misstates `tracker capex` and the national totals rather than one row. WARNING,
  not ERROR — nothing here is arithmetically impossible, so it is a question for a
  person. On the live database: **5 and 22 findings across 20 of 207 projects.**

- **A row's disclosures could outlive the claims they describe**
  (`tracker/upsert.py`). `recompute_from_sources` computed the derived note lines
  and discarded them — `_derived` was deliberately unused. It is the shared
  re-derive path for `tracker merge` and for `logic resolve` repairs, so after
  either one a row's values moved and the prose explaining them did not: a
  conflict disclosure naming two sources could outlive the disagreement, or the
  merge that folded it.

  Wiring it to `_merge_notes` took one piece of care. Derived lines are rebuilt
  wholesale, and two of them describe the *ingest* rather than the claims — which
  identity was routed here, and which existing row this one might duplicate.
  Neither is recoverable from the row (`duplicate_of` is not a column, so that
  note is the **only** record that the identity question is open) and a recompute
  has no ingest record to regenerate them from, so rebuilding wholesale would have
  deleted both. `_INGEST_ONLY_NOTES` names them and `preserve_derived` carries
  them across. Live database: 39 projects hold a duplicate proposal and one holds
  a routed-here note, so the path is load-bearing rather than defensive.

- **A citation that does not exist could set values** (`tracker/upsert.py`,
  `tracker/blocks.py`). `confidence.compute` already dropped placeholder URLs
  before weighting, and the comment there names the row that motivated it. That
  fix stopped at the **score** and left the **values** alone — the more dangerous
  half. A placeholder carries whatever `source_type` the seed file gave it, and
  `sample-projects.json` types them `company_filing`: **weight 3, the heaviest in
  the system, on a URL that does not exist.**

  Observed live on Fairwater (#1), where the placeholder was source 1, created in
  the same second as the project, and took every identity field on FILL_ONLY's
  first-seen-never-overwritten rule.

  **Demoted, not dropped.** `--allow-placeholders` exists so the shipped seed file
  can smoke-test the pipeline, and a claim-less source makes that produce empty
  rows. Demotion instead marks the claim 待确认, which routes it through a rule
  `resolve` already applies to every policy at once: unconfirmed claims are
  discarded outright the moment any confirmed claim exists. That covers the
  `phase` ladder, which ranks by progression and ignores weight — so zeroing the
  weight alone would not have saved it. A placeholder's "quote" is literally the
  instruction to go and paste one, so `confirmed=False` is not a special case, it
  is the truth about it.

  Three places, not one. `claims_by_field` for field claims;  `blocks_by_key` for
  the same hole at block level, where the **sort** is demoted too and not just the
  claim weight, because `label`/`parent` are never resolved by weight —
  `labels.setdefault` gives them to whichever source is seen first; and
  `_conflict_notes`, which did not apply the 待确认 rule `resolve` does, so a claim
  the engine had already discarded was still disclosed as a rival. #1's note read
  `conflict phase: 'construction' (company_filing) vs 'operational'
  (general_media); kept higher-weighted value` — crediting a URL that does not
  exist, and describing a contest that never happened on either count.

  Seven regression tests, all failing at the previous commit except the two
  pinning the contract that must not move: a placeholder on its own still
  populates its row. Values already written are untouched — identity fields are
  FILL_ONLY and stay put; purging the three stored placeholder citations is step 2
  of `docs/placeholder-remediation-plan.md`.

- **A tunnel that had not reconnected was reported as a database failure**
  (`app.js`). Restarting a published console briefly takes the tunnel down; a tab
  left open then fetched `/api/dataset`, got **Cloudflare's own 502 HTML page**,
  and the console rendered it under the heading "The console could not read the
  database" with the provider's entire error document dumped into the message
  area. Two false statements at once — the database was fine and the console had
  never answered at all. A non-JSON body is no longer taken as an error message,
  502/503/504 and an outright network failure now say *"The console is not
  answering — nothing is wrong with the data, this page just cannot reach the
  server"* with a retry button, and only a real server-side error keeps the
  database wording. Observed live on a restart of the published console.

- **`logic.review` could spend without a number anybody chose.** `read_limit=None`
  meant "unlimited", tolerable only while the extractor was set exclusively by
  `--read N`. The new `--audit` flag broke that invariant: an `--audit 20` run
  silently started contradiction-reading *every* row in the database on its way
  to the audit — caught live after ~10 wasted calls. `None` now means **none**:
  every model call needs an explicit limit, and a regression test holds the line.

- **`tracker capex --json` crashed.** The payload read `p.h200_equivalent` off
  `capex.Position`, which has no such field — every JSON invocation died with an
  `AttributeError`. The column is now derived from the position's planned MW via
  `compute.h200_equivalent`, the same unit conversion the per-project column
  uses, and a regression test parses the output.

- **`logic resolve` could destroy correct data to satisfy a coarse `phase` enum.**
  Three of its operator edits — `_drop_energized`, `_set_phase_operational` and
  `_built_equals_planned` — answered a contradiction by rewriting the row, and
  `--llm` could apply any of them unattended.

  The contradictions they hung off are largely artefacts of the schema rather than
  faults in the data. A modern campus is several states at once: measured on the
  live database, **28 projects are partly built, 15 are `construction` with
  megawatts already live, and 12 have power energised while construction is
  mid-track.** One `phase` enum cannot describe that, so
  `energized_but_not_operational` (19 findings) and
  `operational_without_built_capacity` (7) were mostly the schema complaining about
  itself — and the remedies were severe. `_drop_energized` deleted a real, cited
  energisation milestone, the most informative event type in the dataset.
  `_built_equals_planned` asserted a whole campus was energised because one phase
  was. `_set_phase_operational` marked a campus finished while most of it was still
  being built.

  Those two findings now offer no edit at all — accept or skip, which is the honest
  set of choices — and `past_its_own_date` keeps only the defensible half (clear a
  stale date; do not declare the campus operational). **73 of the 148 findings can
  no longer be auto-edited**; the 75 that remain are ones where an edit really does
  answer the question. Verified against a copy of the live database: a full
  `logic resolve --auto --apply` now leaves all 67 energisation milestones intact.

  A test locks it in, because the removed one-liners are convenient enough to come
  back by accident. The real repair — representing a campus as capacity blocks so
  these states can be *recorded* rather than flagged — is the work this is stage 0
  of.

- **A restart could not dislodge a stale front end.** Static files were served at
  bare URLs — `/static/app.js`, no version — with `Cache-Control: no-cache`. That
  is correct and it is not sufficient: a browser holding the file, or a CDN edge in
  front of a published console, goes on serving last week's page however many times
  the server restarts. The failure is silent, because the page still loads. It cost
  a round trip to establish that a rebuilt panel and a rewritten animation had in
  fact shipped and were simply not being fetched.

  `assets.stamp` now rewrites every `/static/...` reference in `index.html` to
  carry a token derived from the file's mtime and size, so **a changed file is a
  different URL** and nothing anywhere can serve the old bytes. The page itself is
  `no-store`, so new tokens always arrive.

  That also fixed the opposite problem. `no-cache` with no `ETag` meant every page
  load re-downloaded roughly three megabytes of vendored JavaScript, because there
  was no validator to make a conditional request with. A request carrying the
  current token is now answered `immutable, max-age=1y`; a bare or stale token
  still gets `no-cache`. `immutable` is only safe *because* the URL changes — which
  is exactly the objection the previous comment raised against it.

- **The evidence gate was discarding correct values over cosmetic edits to the
  quote.** `enrich` logged `evidence quote for 'mw_planned' is not in the article;
  ignoring` on real, well-sourced figures.

  Measured over 131 evidence quotes from 8 cached articles: 33 failed exact
  containment, and the dominant cause was not fabrication but the model resolving
  references while it quotes — the article says "The campus is a single building
  comprising two data halls that serve as a 16.5 MW data center" and the model
  writes "The *Austin* campus is a single building…". One word substituted, the
  whole citation rejected, the capacity lost.

  `_verbatim_run` now finds the longest stretch of the quote genuinely present in
  the article and stores **the article's own words for that stretch**, never the
  model's edit — then widens it to the enclosing sentence, because one observed run
  stopped at "…the offering was $" where the source wrapped a line inside the
  figure, leaving a real quote that no longer evidenced the value it was cited for.
  Floors of 40 characters and 50% of the quote were tuned against a negative
  control testing every sampled quote against an unrelated article; nothing
  crossed. Acceptance **75% → 95%, zero false positives**.

  The guarantee is unchanged: `_stated_in` runs against the stored text, so a value
  must still be asserted by a sentence somebody published. Risk quotes take the
  same path, with the category check applied to the recovered text.

- **Closing a tab mid-run printed two tracebacks**, and over a Cloudflare tunnel
  that is ordinary rather than rare: the edge drops idle connections and the
  browser's EventSource silently reconnects, so one crawl produces several.

  The cause was platform. Windows raises `ConnectionAbortedError` (WinError 10053)
  where POSIX raises `BrokenPipeError` or `ConnectionResetError`, and the SSE
  handler caught only the latter two — so on the platform this runs on it caught
  nothing. All four are subclasses of `ConnectionError`; that is what is caught
  now, in `_stream`, `do_GET` and `do_POST`.

  The second traceback was the apology failing: the catch-all handler tried to
  send a 500 down the same dead socket, raised again from inside an `except`
  block, and escaped to socketserver. `_error` now tolerates a peer that has
  already gone, and the `ConnectionError` clause sits ahead of the catch-all so it
  never gets that far.

  Verified over a live tunnel — attach to a run's stream, hang up mid-flight: the
  run completes, the console stays healthy, the log stays silent.

- **A Census geocode was rendered as a green, weight-3 official citation.** The
  place-code lookup `ingest geo` uses is served from a `.gov` host, so
  `classify_source_type` files it as `government_doc` — and the drawer then shows
  "a government document supports this project" for a county and a pair of
  coordinates nobody wrote about the campus. 158 of 509 citations are this.

  The scores were never affected, which is worth stating because it was the first
  thing checked: corroboration is counted over `KEY_FIELDS` and a geocode claims
  none of them, so re-scoring every project with the Census sources removed moves
  **0 of 221**. It was only ever a label, so it is fixed as a label — the chip
  reads "reference data — corroborates nothing" for any `derived:` extractor. A
  migration to re-tag rows would have been a lot of moving parts for a word.

- **A wide table in the run log came out shredded.** Rich lays its tables out at
  a fixed `COLUMNS` (160) and draws them with `+-|` characters, so the column
  positions are baked into the text. The log pane was `white-space: pre-wrap`,
  and at a measured 815px — about 113 characters — every 132-character row of
  `tracker list` folded onto a second line and the borders stopped lining up with
  the cells. There is no pane width but 160 characters at which wrapping works,
  so the pane no longer wraps: it scrolls sideways, which is what a terminal
  emulator does, and the scroll is bounded because Rich never emits a line wider
  than `COLUMNS`. Each line is `width: max-content` so a reversed or
  background-coloured run paints its whole width instead of being clipped at the
  pane edge. The page still never scrolls sideways, at 1280px or at 390px.

  A `wrap` toggle sits in the log's corner for the other half of the output —
  `gaps` notes and refusal messages are prose Rich has already wrapped, and
  reading those by scrolling is worse than reading them reflowed. Off by default:
  the tables are the case that breaks rather than merely inconveniences.

- **`tracker cloudflare` failed behind a proxy**, which on the machine it was
  built for meant three times in four. cloudflared builds its own
  `http.Transport` for the one request that asks Cloudflare for a quick tunnel,
  and a zero-value Transport has no proxy function — so that request ignores
  `HTTPS_PROXY` however it is set, while curl, pip and the browser all work.
  Measured against `api.trycloudflare.com` over an hour: 3.8s to 28s direct
  depending on the minute, a steady ~4s through the proxy, against a fixed client
  budget of about ten seconds. The result was `context deadline exceeded` and no
  way to act on it.

  The console now starts a loopback relay, points cloudflared's `--quick-service`
  at it, and forwards that request through the proxy — 4.1s, measured, where the
  direct attempt had just timed out. The proxy is taken from the environment or,
  on Windows, from `Internet Settings`, which is where Windows applications look
  and Go does not. `--proxy` forces one, `--no-proxy` opts out, `--check` reports
  which was found. The relay is not a general proxy: the upstream host is a
  constant, only the path travels, an absolute request URI is refused, and it
  closes with the tunnel.

  Attempts are also retried three times, because the underlying failure is a
  latency race rather than a refusal — the same request measured 3.8s and 28s an
  hour apart. And when it does give up, the message now names the two things that
  help instead of dumping the log: a proxy, or a named tunnel, which never calls
  that endpoint at all.

- **A failed quick tunnel was reported as a working public URL.** cloudflared's
  timeout message contains `Post "https://api.trycloudflare.com/tunnel": context
  deadline exceeded`, the hostname pattern matched the API host inside it, and the
  console printed `public: https://api.trycloudflare.com` — a link to Cloudflare's
  API, handed over as the operator's console. The pattern now excludes `api.`, and
  a "failed to request quick Tunnel" line ends the wait immediately with the real
  reason instead of timing out sixty seconds later with a vague one. Observed
  live; the request itself is transient and succeeded on the next attempt.

### Added

- **A Capex view in the console**, and the duplicate review that belongs beside
  it. `tracker capex` answers the question the site-keyed database cannot — how
  much capacity each end customer has in flight — and the console had no
  equivalent. The view carries the whole thing: the coverage banner saying what
  fraction of projects it can speak for, the buyer table with MW-by-year, at-risk
  and slipped columns, the `*` marker wherever attribution came from ownership
  rather than a cited tenant, the suspect-attribution list, and both honesty
  footers (how much is attributed; every figure is a floor).

  The duplicate review sits on that page rather than under Coverage because
  `capex.py` makes the argument itself: a row stored twice is a nuisance in a site
  listing and a wrong number the moment anything groups by buyer — Abilene was in
  the database four times, so 1.2 GW was counted four times against OpenAI. The
  repair belongs next to the figure it corrupts. Groups appear with their
  candidate rows side by side (capacity, citation count, dates), a radio picks the
  survivor, and the merge runs through the ordinary `/api/run` path rather than a
  bespoke route — one execution path, one lock, one audit log. Deciding which of
  four rows survives by eye is the one thing a browser genuinely does better than
  the CLI.

- **A `DESTRUCTIVE` gate in the console catalog, on its own axis from cost.**
  `tracker merge` permanently deletes project rows and was in neither
  `LLM_COMMANDS` nor `BLOCKED`, so a misplaced click ran it with no confirmation
  at all. It now needs its name typed back, the same ritual `sync` uses for a
  different loss — reporting a merge as "spends LLM tokens" would simply be false,
  which is why `cost` and `destroys` are separate fields rather than one enum. The
  check is on the command *name* and never on its flags: a gate that reads
  arguments is a gate with a bypass in it, and `--dry-run` must not be the thing
  standing between a click and a deletion.

- **`ingest edgar` priced.** It spends one LLM call per filing and shipped outside
  `LLM_COMMANDS`, so it ran from the console without the confirmation
  `ingest crawl` has. Same mechanism, cheaper failure, one line.

- **A distinct label for the second cause of 待确认.** Two very different things
  reach that tier and they need opposite work: nothing quotable backs the value
  (find another source), or the quote is real and the figure belongs to a
  programme rather than this site (correct it — searching would find a citation
  and it would still be wrong). The console reads the disclosure the ingest path
  already wrote into `project.notes`, keyed on `crawl.SCALE_NOTE_MARKER`, and
  shows it as a red "not this site's figure" chip with the recorded sentence. Read
  back rather than recomputed in the browser, which would sometimes accuse a
  figure no gate ever demoted. Six projects in the live database carry one.

- **Variadic positionals in the console catalog.** `merge` takes any number of ids
  as bare arguments, which click models as `nargs=-1` rather than as a `multiple`
  option; reading only `multiple` refused the list, so the Duplicates card could
  never have folded more than one row.

- **A plausibility ceiling on `investment_usd` against `mw_planned`**
  (`crawl._implausible_investment`, $50M/MW). The evidence gate can only confirm
  that a figure was quoted in the article, not that it is *this project's*
  figure — an article about one 1,167 MW campus quoting "OpenAI's $500 billion
  Stargate" verifies fine and would otherwise store the programme total as the
  site's own investment. The threshold was read off the live distribution rather
  than assumed: 41 projects citing both figures cluster under $23.3M/MW with
  nothing until $83.3M+, so it sits in a real gap. A figure over the ceiling is
  demoted to 待确认 rather than dropped, the same treatment migration 0006 gives
  any value the gate can't otherwise verify.

- **`dedup.looks_like_the_same_site`, `company_parts`, `shares_a_party`,
  `distinctive_name_tokens`**: recognise when two rows in one locality are
  probably one campus stored under different company names — a builder, a
  landlord and a tenant each producing their own `dedup_key`. Measured: the
  Abilene Stargate campus existed four times (Crusoe, Oracle, OpenAI,
  "OpenAI/Oracle"), each correctly keyed and each carrying the full 1.2 GW, which
  is exactly what made `tracker capex` overcount before this. Locality alone is
  not the test — Ashburn alone holds fourteen genuinely distinct projects — so a
  match requires either a shared company token or a distinctive shared name
  token once generic industry words and the locality itself are discounted.
  `upsert._find_duplicate_candidate` now also scans same-locality neighbours
  through this check; like every other dedup signal here, a match proposes a
  review candidate and never merges automatically.

- **`tracker ingest edgar`**: SEC filings as a source (`tracker/ingest/edgar.py`).
  Every other source here is somebody's website, and the good ones increasingly
  answer 403 to anything that is not a browser; a filing is a legal obligation to
  publish, served from a government host with a documented rate limit instead of a
  bot filter. Filings are also where the fields this project covers worst actually
  live — investment in the cash-flow statement, in-service dates in MD&A, the end
  customer in the lease footnotes. Precision comes from scoping EDGAR full-text
  search by CIK rather than by phrase (unscoped, "data center campus" returns
  1,066 hits led by shell companies); a 369,000-character 10-Q is reduced by
  scoring paragraphs on evidence density and keeping the best ~6% with their
  neighbours, rather than truncated head-and-tail as a news article would be; and
  an 8-K exhibit that is actually a credit agreement is dropped before it costs a
  call, by legal-vocabulary density (0.3 for a 10-Q, 20.1 for a financing
  exhibit). The module only ever produces article text — the selected section is
  written into the same cache `discover.cache_feed_text` writes to, and `crawl.run`
  reads and extracts it exactly as it would a news article. New
  `seed/edgar-companies.toml` (company name, CIK, `kind`) drives which filings are
  read and doubles as the end-user classification `tracker capex` uses. Needs
  `TRACKER_USER_AGENT` set to a real contact; the command refuses to start on the
  shipped placeholder rather than collect a run's worth of 403s.

- **`tracker capex`**: capacity and spend rolled up by the company actually buying
  it, not the site building it (`tracker/capex.py`). The database is keyed on
  `(operator, locality, state)`; much hyperscaler capacity is built by wholesale
  developers and leased, so the operator on a building is often not the tenant.
  Attribution is a named tenant where a source gives one (folded through
  `dedup.customer_key` so Meta/Facebook are one buyer), otherwise the operator
  itself where the operator is a known end user (from `edgar-companies.toml`'s
  `kind`, plus a short hard-coded list of private companies — OpenAI, xAI,
  Anthropic and others — that file nothing with the SEC and would otherwise be
  invisible), otherwise an explicit unattributed row rather than a silent drop.
  Every figure is a floor: a project whose capacity nobody has cited contributes
  zero. The footer states what fraction of projects the rollup can actually speak
  for, flags projects that name a tracked operator as their own customer
  (usually an extraction error, not a lease — never auto-corrected, only flagged,
  for the same reason `dedup` refuses to auto-merge across granularity), and
  flags likely duplicate rows for one physical site stored under a builder's name
  and a tenant's name both. Never infers a tenant: who signed a lease is a fact
  with a documented answer, and `tracker infer` exists to keep judgement and fact
  apart.

- **`dedup.is_undisclosed`**: recognises when a `customer` value hedges rather
  than names anybody — "a Fortune 100 technology company", "publicly-traded
  global enterprise" — so `tracker capex` doesn't count four hedges as four
  distinct one-project tenants. Matched on live data: 4 of 12 populated `customer`
  values were this shape.

- **Five feeds added to break a single-outlet dependency**
  (`bisnow-datacenter`, `constructiondive`, `semianalysis`, `qts-newsroom`,
  `coreweave-blog`, and others in `seed/feeds.toml`). Measured before adding: 130
  of ~180 editorial citations came from one domain, capping 109 of 124 projects
  below confidence 3 (`confidence.compute` requires two independent registrable
  domains). Each entry's match count is what `discover` actually saw on a real
  poll, not an estimate; `coreweave-blog` and `openai-blog` are deliberately not
  `topic_implied`, since their feeds are mostly product news that would otherwise
  queue GPU benchmarks as data center articles.

- **`discover.cache_feed_text`**: when a feed syndicates the full article body
  (RSS `content:encoded`, Atom `<content>`) rather than a teaser, the body is
  written straight into the article cache and the crawl phase never has to
  request the page. Several outlets — the state nonprofit newsrooms among them —
  serve their feed freely and then answer 403 to any non-browser request for the
  article itself; every one of those articles was previously unreadable. Not a
  bypass: nothing is fetched that the publisher did not hand over, and it's the
  syndication feed used for the purpose a syndication feed exists for. A short-body
  guard (`MIN_USEFUL_CHARS`, the same floor the fetcher itself uses) stops a
  summary-only feed from caching a teaser as though it were the article, and an
  existing cache entry is never overwritten, since a real fetch is more complete
  than a feed excerpt. `tracker discover` and `tracker sync` both report `bodies
  from feed` in their counts now.

- **The console on a phone.** It worked and was miserable: at 490px the sticky
  header took 15% of the screen, the filter card was 558px tall, and the first
  data row sat at y=1137 behind a table showing 321px of a 2112px grid. Below
  720px the table is now a card list — thirteen columns has no good phone form,
  and shrinking the type to force one is worse than changing shape. The header
  drops to a logo and a scrollable view strip, filters collapse behind a count,
  the map takes 60vh and the `compact` mode `dc-map` already had, and the intro
  prose, coverage strip and tier legend step aside (all three are in Help). 940px
  of chrome above the first card became 355px. Coarse pointers get 36px targets
  regardless of width.

- **A Help view.** The three ideas that make the rest legible — evidence tiers
  with the live swatches rather than a picture of them, the five tracks and why
  power is never inferred, confidence 0–3 — plus what each view is for, what
  costs money, and the keyboard. Deliberately does not repeat the README, so the
  two cannot disagree.

- **Colour in the run log.** `runner.py` set `NO_COLOR=1` and `TERM=dumb`, which
  threw away the CLI's own signalling at the one moment someone is reading the
  output. Now forced on and parsed back out of the ANSI stream into styled spans
  (`static/ansi.js`), on a fixed dark terminal surface in both themes — amber on
  cream is a different colour and barely legible, so this is the one surface that
  does not follow the page.

  `FORCE_COLOR=1` alone was not enough, and failed silently: Rich honours it,
  then takes the Windows branch of `_detect_color_system`, finds `legacy_windows`
  and picks `ColorSystem.WINDOWS`, which paints by calling the console API rather
  than writing escapes — down a pipe that API does nothing and the markup is
  simply stripped. `cli._forced_colour` names an ANSI dialect and turns legacy
  mode off, which fixes it for anyone piping this on Windows, not only for the
  console. `NO_COLOR` still wins, per no-color.org.

- **Zoom and pan on the map.** Wheel, drag, and +/−/reset. Geography scales;
  **the marks deliberately do not**. Scaling the bubbles too is the obvious
  implementation and the wrong one — the reason to zoom this map is that a dozen
  Northern Virginia projects sit on top of each other, and bubbles that grow with
  the map stay exactly as overlapped. Button-driven zoom eases in CSS; wheel and
  drag stay instant, because a transition on a continuous gesture reads as lag.

- **`tracker ingest crawl --url URL`**, repeatable, beside the existing
  `--urls FILE`. Reading one link no longer means writing a file first. The
  console's Queue view uses it for a per-article Crawl button, two-step rather
  than typing the command name: that ceremony is proportionate to 25 LLM calls
  and absurd for one. The server-side rule is unchanged — the UI supplies the
  confirmation only after the operator has confirmed. `catalog.build_argv` learned
  repeatable flags, reading click's own `multiple` rather than keeping a list, and
  refuses a list anywhere the CLI takes a single value.

- **`docs/`** — `architecture.md` (how the CLI, database and console fit
  together; no English equivalent existed), `README.md` as an index that says
  which documents you can skip, and the two Chinese documents `guide.zh-CN.md`
  and `architecture.zh-CN.md`. No English `guide.md`: the root README already is
  one, and two documents over the same ground is how one of them becomes wrong.

- **`tracker serve --tunnel`** publishes the console through a Cloudflare quick
  tunnel, and **refuses to start without `TRACKER_CONSOLE_PASSWORD`**. Refusing
  rather than warning is the point: a warning scrolls past, and there is no safe
  reading of "published and open" in front of a process that runs commands.

  The gate (`webui/auth.py`) is not localhost-grade. Everything is behind it —
  before signing in the whole site is one self-contained login page and every API
  route is 401, so not even the frontend is served. Constant-time comparison.
  Session tokens are random, server-side and revocable; the cookie is `HttpOnly`
  and `SameSite=Lax`, which is what actually stops another site POSTing to
  `/api/run`, with an Origin check behind it. Eight failures locks one client for
  15 minutes and **forty across all clients locks the gate** — per-IP limiting
  alone is the wrong shape against a published URL, where an attacker rotates
  addresses. That global limit is what makes a short password safe: 40 attempts
  per 15 minutes is ~3,800 a day against a 36⁷ keyspace, or about 57 million
  years.

  `find_cloudflared` prefers a real executable over npm's `.CMD` shim, which
  swallowed both stdout and the exit status, and checks the binary actually runs
  before starting a tunnel. Found the hard way: npm's postinstall had left a
  7.9 MB `cloudflared.exe` where the real one is 54 MB, and every launch died with
  WinError 193 and no output at all.

- **Honest density in the projects table.** It read as empty — 653 of the cells
  were dashes — because the default columns included fields that are legitimately
  null on most rows. Columns are now **ordered by their measured coverage** from
  `gaps.measure()`, defaulting to those above 50% with the rest one switch away
  and the switch saying how many it is hiding. Self-maintaining: as coverage
  improves, columns promote themselves. Visible cells went from ~46% populated to
  95%, with nothing hidden and no figure massaged.

  Alongside it: a real per-row `9/12`, a coverage strip carrying the three best
  *and* three worst fields with true numerators and denominators, and a distinct
  style for a null that is empty *correctly* (`mw_built` before ground is broken)
  versus one that is simply unknown — `gaps.for_project` already drew that line
  and nothing was reading it. 61 of 653 empty cells turn out to be the former.

- **Motion**, on Meridian's own tokens rather than hand-written curves: staggered
  row entrance capped at twelve, drawer scrim fade, track segments filling left to
  right, coverage bars growing from zero, log lines fading in, and header totals
  counting up on first arrival only — a number that re-animates on every keystroke
  is noise. Everything is a keyframe or a transition so `base.css`'s global
  `prefers-reduced-motion` rule reaches all of it; the count-up is the one timer
  and checks the query itself.

- **`tracker serve`** and `tracker/webui/` — a local console on 127.0.0.1 that
  reads the database live and can run the CLI. Six views (Projects, Map, Queue,
  Coverage, Commands, Runs) built from the Meridian design system, ported from a
  Claude Design mockup. Distinct from `tracker export html`, which stays: the
  export is one emailable file frozen at write time, this is a server.

  The runner is the security boundary and is deliberately narrow. `POST /api/run`
  takes a command name and a flag object, validates both against a catalog
  introspected from Typer, and builds an argv **list** — no shell, so `;` and
  backticks are inert, and an unknown flag is an error rather than something
  passed through. `--db` is injected by the server rather than accepted from the
  request, so a run cannot be pointed at another database and cannot silently
  resolve a default from its working directory. The five LLM-spending commands
  require their own name typed back. One run at a time, because SQLite takes one
  writer and the second would die partway through having already paid for its
  calls. `--no-run` serves the views without the runner.

  No network requests at all: React, htm, d3, topojson, three.js, Lucide, the
  Census boundary TopoJSON and three OFL webfonts are vendored (3 MB, flattened
  because `.gitignore` carries unanchored `dist/` and `build/` rules), and the
  server sends `default-src 'self'`. No build step and no `package.json` — the
  Meridian bundle is already compiled to `React.createElement` and `htm` supplies
  the templates.

  Run history is files, not a table: `data/runs/<id>.jsonl` plus an index. A
  command's stdout is operational exhaust with a different lifetime from the
  tracked data, and the schema is not the place for it. "Projects changed" is
  counted by comparing `updated_at` across the run rather than diffed, because
  field-level history does not exist and a count that is true beats a
  reconstruction.

- **`source.quotes`** (migration `0007_source_quotes.sql`) and
  **`gaps.provenance()`** — the sentence behind each individual value, not one
  excerpt per source. `evidence_gate` always computed the pairing; `_excerpt()`
  then collapsed it and threw the association away, so `tracker show` printed the
  same paragraph under all twelve fields. `provenance()` returns the tier, the
  quote, whether that quote is the field's own or the source excerpt falling back,
  and which citation it came from. It reuses `upsert.claims_by_field()` so the
  quote comes from the source whose value actually won the merge — for a contested
  `mw_planned`, quoting the strongest *tier* would print a sentence stating a
  number the row does not hold. `basis()` is now the tier half of it, so there is
  one definition rather than two that can drift. `tracker export json` gains `prov`
  and per-source `quotes`; schema tag `tracker/4`.

- **A `defaulted` provenance tier.** `phase` is NOT NULL and falls back to
  `announced` when no source states one; reporting that as 待确认 asserted a source
  had claimed it and failed to prove it. 37 values on the live database were
  mislabelled that way. `tracker show` renders it quietly rather than in red.

- `tracker/required.py` — the required-project matching extracted from
  `tracker verify` so the console and the command cannot disagree about what
  "present" means.

- Project scaffold: `pyproject.toml` (`dc-tracker`, console script `tracker`),
  `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`,
  `requirements.lock` (pinned transitive set; there is no uv/poetry here).
- Schema: `project`, `source`, `event`, `ingest_url`. Defined authoritatively in
  `migrations/*.sql`, mirrored by `tracker/models.py`, with
  `tests/test_db.py::test_models_match_migrations` failing the build if the two
  drift apart.
- `tracker/db.py`: engine with `foreign_keys=ON` / WAL / `busy_timeout` pragmas,
  a raw-SQL migration runner with checksum-verified immutability, and
  `open_db(readonly=True)` so read commands cannot write.
- `tracker/normalize.py`: per-field coercion for state, MW, money, dates
  (carrying precision), phase, URLs and excerpts.
- `tracker/confidence.py`: 0–3 scoring by source weight, domain independence,
  agreement and conflict.
- `tracker/dedup.py` + `tracker/upsert.py`: the single write path, with project
  fields recomputed from `source.claims` so ingestion is idempotent and
  order-independent.
- `tracker/ingest/manual.py` and `seed/sample-projects.json`: hand-curated JSON
  ingest, refusing to load a file that still holds `PLACEHOLDER` values.
- `tracker/ingest/iso_maps.py` + `tracker/ingest/pjm.py`: ISO queue ingest for
  PJM/MISO/ERCOT/CAISO, reading CSV, XLSX and JSON. Aborts before row 1 on a
  renamed column, streams in chunks of 1000, and fails loudly when zero rows
  match or the reject rate exceeds 5%.
- `tracker/prompts/` + `tracker/llm.py`: versioned prompt files stamped by content
  hash, and a MiniMax client that enforces the JSON contract in code
  (parse → repair → validate → one corrective retry).
- `tracker/ingest/fetch.py` + `tracker/ingest/crawl.py`: article fetching with
  per-host rate limiting and browser escalation, LLM extraction gated on quoted
  evidence, an on-disk article cache, and per-URL outcomes in `ingest_url`.
- **`tracker/ingest/discover.py`**: polls the RSS/Atom feeds and sitemaps in
  `seed/feeds.toml`, keyword-filters headlines in two tiers (topic + project
  signal), and queues matches for triage. Closes the gap where nothing in the
  system could find an article to read, so the database only ever held what an
  operator typed in by hand. Uses stdlib `xml.etree` and `tomllib` — no new
  dependency.
- **`tracker sync`** — the whole pipeline in one command: discover new candidate
  articles, extract them, refresh existing projects by re-reading their sources,
  then list. Both crawl phases are capped since each article costs an LLM call,
  and `--dry-run` previews a run for free.
- `crawl.stale_sources()` selects the citations of existing projects that have not
  been re-read recently, which is how project data gets *updated* rather than only
  added. Placeholder URLs are excluded, being unfetchable by definition.
- `tracker list --limit N`, which reports the total it capped ("2 of 3").
- **`tracker ingest geo`** and `tracker/ingest/geo.py` — derives `county`, `lat`
  and `lon` from two free US Census files (place-to-county crosswalk and the 2024
  place gazetteer). No API key, no LLM, no per-row cost. Three of the twelve fields
  are a lookup rather than a research problem: articles write "Mount Pleasant,
  Wisconsin" without the county, and never print coordinates at all — which is why
  `lat`/`lon` were 0%, the evidence gate having correctly discarded every
  unquotable value the model produced. On the live database this moved `county`
  from 49% to 80% and coordinates from 0% to 89%.

  It refuses to guess two things. A city spanning several counties (Houston and
  Austin touch four each) leaves `county` NULL and is reported, because choosing
  one would invent a fact — that is what holds county to 80% rather than 100%. And
  every derived coordinate discloses in its own excerpt that it is the centre of
  the place, not the project site.

  Consolidated city-counties are resolved by alias: Census publishes Augusta as
  "Augusta-Richmond County consolidated government (balance)", and without the
  alias Augusta GA and Indianapolis IN were the only project cities that failed to
  match. Exact names always win over aliases.
- `confidence` now excludes any source whose `extractor` begins with `derived:`
  before scoring, and `SourceView` carries `extractor` to make that visible. A
  Census row is a real, checkable citation but it is not testimony about the
  project; counting it would let one press release plus a lookup read as two
  independent domains and reach confidence 3.
- **`tracker export html`** and `tracker/templates/dashboard.html` — the whole
  dataset as one self-contained page: sortable table over the 12 fields, a
  five-segment track strip per project, filters (state, phase, blocked-on,
  confidence, quoted-only), a per-project drawer with citations and milestones,
  capacity-behind-an-obstacle bars, and a coordinate plot.

  No network requests of any kind — no CDN, no webfont, no tile server — so it
  works offline and can be emailed. The dataset is inlined rather than fetched from
  a sibling `.json` because `fetch()` from a `file://` page is blocked as
  cross-origin, and a two-file build would open to an empty table unless the reader
  happened to be running a web server.

  Colour means **trust, never category**: the five tracks are a sequence so they get
  an ordinal neutral ramp, and every hue is reserved for how much to believe a value
  (amber for 待确认, blue for inferred) or for something being wrong (rose for a
  blocked track). The signature element is a provenance ledger that recomputes on
  every filter, so "what do the figures I am currently looking at rest on" is always
  on screen — the question this dataset exists to answer.

  Every measurement is set in a monospace stack with tabular figures and prose in the
  system sans, so number columns align and the family itself distinguishes a
  measurement from a description. Contrast verified in-browser against WCAG AA:
  muted labels 4.83:1 on surface, 待确认 amber 10.04:1, blocked rose 7.17:1.
- **Operator newsrooms as sources.** Seven company press-release archives added as
  `[[sitemap]]` entries. A release opens with the three fields that are hardest to
  find anywhere else — "X announces a $N billion campus, online in YEAR" — and is
  `company_filing`, weight 3. Measured across the whole database beforehand: trade
  press yielded 0.57 of those three fields per citation, and the *two* company
  sources in it yielded 1.50. The vein was untouched, not exhausted.

  A `company` field marks an entry as an operator's own newsroom, which does two
  things. `matches_known_project` stops requiring the company in the slug, because
  the domain already proves it — measured, that took the yield from 15 articles
  over 8 projects to 28 over 13, since a release titled "New Hillsboro campus
  announced" names the city and not the company. And `classify_source_type` learns
  the host is first-party. The locality-or-name-token requirement is unchanged, so
  precision holds: a careers page still matches nothing.

  Not included, with reasons recorded in `seed/feeds.toml`: QTS 404s; Flexential
  and Iron Mountain answer 403/429; STACK, Switch, Vantage, EdgeConneX and
  news.microsoft.com serve child sitemaps only to browser-like clients (curl 200,
  httpx 403 on the same URL — a TLS-fingerprint rule). Their robots.txt explicitly
  permits crawling and advertises the sitemap, so those need `--browser`.
- **A Bocha (博查) backend**, for networks where the other three cannot be signed
  up for — its registration works from mainland China with no Cloudflare
  challenge. It handles the trap that Bocha answers **HTTP 200 with the real
  status in the body's `code` field**, so the status line alone does not mean the
  call succeeded.

  Measured honestly, its index does not fit this tool. A project query returned
  sohu, zhihu, xueqiu and 163; `site:datacenterfrontier.com` returned that site's
  *homepage* rather than any article; and querying the **exact headline** of an
  article already in the database returned no trade-press URL at all. It is an
  index-coverage gap, not a query-syntax one. End to end against the live API:
  10 hits, 10 filtered, 0 survived. Useful for learning that a project exists,
  not for obtaining a citation — so it resolves **last** in `auto` order and can
  never displace a better index.
- **A language gate on search candidates** (`normalize.looks_english`). A host
  blocklist only stops the reposters you already know; one measured Bocha run
  surfaced `dahe.cn`, `topnews.cn` and `uyijian.zhiding.cn`, and the long tail is
  endless. Script is the property that generalises, so a candidate whose title and
  snippet are more than 10% CJK is dropped before it costs a fetch and an LLM call.
- `_SKIP_HOSTS` gained the Chinese portals and UGC platforms that Bocha favours,
  plus document dumps and academic indexes. Every one of sohu, zhihu, toutiao,
  csdn, researchgate and dl.acm.org passed the old filter.
- **Search is pluggable, with Brave and Serper alongside Google.** `PROVIDERS` maps
  a name to a class and `build_provider()` resolves it; `TRACKER_SEARCH_PROVIDER`
  pins one, and `auto` (the default) takes whichever backend holds a key. An
  explicit name without its key fails loudly rather than falling back to a
  different engine, because silently searching somewhere else is worse than
  stopping.

  Brave is the recommended addition: an independent index rather than a Google or
  Bing reseller, so it widens coverage instead of re-asking the same engine — one
  header, no cloud account, 2000 queries/month free. Its free tier answers HTTP 429
  for *pacing* (about one query a second) and not only for an exhausted monthly
  allowance, so a first 429 is retried after a pause instead of ending the run.
  Serper is included too but is Google's index under a simpler API, so it widens
  the quota rather than the coverage.

  **There is no Bing backend: Microsoft retired the standalone Bing Search APIs on
  2025-08-11**, and their documentation page now carries `is_retired: true`, so no
  new subscription key can be created. The successor, Grounding with Bing Search in
  Azure AI Foundry, is licensed for grounding a model's reply rather than for
  building a stored database of facts and citations — precisely what this tool
  does. Asking for it by name prints that explanation, with the Brave drop-in,
  rather than "unknown provider".

  `Settings.has_search_keys()` now means "some backend is configured";
  `has_google_keys()` is the specific check, and `GoogleCSEProvider` uses it so a
  Brave-only setup cannot construct it and fail later at request time.
- **`tracker enrich ID`** and `tracker/ingest/enrich.py` — throws every retrieval
  method at ONE project, in rounds, until a round stops paying. `tracker sync`
  spreads a budget across the database; this inverts it for when you want one row
  complete. Six harvesters run cheapest-first (derive → queue → retry → archive →
  search → refresh) so an expensive one never runs for a field a free one fills,
  and the loop stops on "a round filled nothing new", not at a fixed article count.

  Search queries are templates anchored on the project's quoted company and
  locality rather than LLM-generated: the project is already known, so there is
  nothing to infer, and an unanchored "data center investment billion" returns the
  industry instead of the site.

  `--dry-run` harvests and reports **without fetching or extracting**.
  `crawl.run(dry_run=True)` still fetches every page and still pays for every LLM
  call — it only declines to commit — and on the most expensive command in the tool
  a preview that bills you is a trap.

  Fields a null is correct for are excluded from its score rather than counted as
  failures, so a project with nothing built is not marked down for having no
  `mw_built`. Measured ceiling on a 94-project database with no search key: 17
  projects had unread archive articles and 77 had none, because the configured
  archives never covered them — the honest limit is the corpus, not the budget.
- `tracker/gaps.py` gained `for_project()`, per-field state for a single project
  (`filled` / `missing` / `not_applicable`), and `geo.run()` gained
  `only_project_id` so a single-project command cannot silently rewrite every row.
- **`tracker gaps`** and `tracker/gaps.py` — per-field coverage measured against
  the rows where the field can legitimately be set, not against every project.
  Raw NULL counts pointed effort at work that cannot succeed: 61 announced
  projects were being counted as `mw_built` misses when nothing is built on any of
  them, and `blocker` looked like a 2%-covered backlog when the truth is that
  most projects have no blocker. Fields whose absence carries no information are
  reported as unmeasurable rather than as a low score, and the report closes with
  the measurable fields that have the most rows left to fill.
- **A `risk` table** (`migrations/0004_risk.sql`) — obstacles as typed, dated,
  severity-ranked rows with their own citation, replacing the single free-text
  `project.blocker`. That column could not hold more than one obstacle when the
  PRD's own list names seven; could never be cleared, because `upsert._resolve`
  returns the existing value when a field has no claims, so a resolved obstacle sat
  on the row forever; and could not be counted, which is what the chip/cloud/power
  read-through needs.

  It also needed a carve-out to be evidenced at all. A blocker sentence is a
  paraphrase, so it can never be a verbatim substring of its own quote — both
  blockers in the live database fail `_stated_in` against their own article text —
  and `_SUMMARY_FIELDS` covers that by trusting the model's *label* over a verified
  quote. `risks[].quote` is the stronger form of the same idea: the quote must be
  real **and** must contain wording for the category it is filed under, checked
  against `_RISK_EVIDENCE` exactly as `phase` is checked against `_PHASE_EVIDENCE`.
  So `blocker` left `_SUMMARY_FIELDS`; obstacles became storable by tightening the
  check rather than loosening it.

  `category` is a closed vocabulary mapped to the PRD's obstacle list
  (`grid_capacity`, `transmission`, `permitting`, `environmental`,
  `equipment_supply`, `chip_supply`, `financing`, `offtake`,
  `community_opposition`, `water`, plus `unclassified`). `severity` is
  `watch`/`material`/`blocking`, **ordered** — that order decides which risk
  becomes `project.blocker`. `summary` may be a paraphrase and `quote` holds the
  verified verbatim sentence beside it, the same split `notes` already draws, so
  the gate needed no weakening.

  0004 backfills any existing `blocker` into an `unclassified` risk carrying the
  source that asserted it, so upgrading loses nothing. Verified against a copy of
  the live database: both rows migrated, both cited.
- Schedule slippage is measured. When a recomputed `expected_online` lands later
  than the stored one, `upsert._record_slippage` writes an `event(delayed)` carrying
  both dates and attaches `delay_days` to the project's most severe open risk.
  `expected_online` keeps its `PREFER_WEIGHT` policy: switching to newest-wins to
  make slips visible would have thrown away the source-quality ordering, and
  recording the movement as history keeps both.

  **A number is only attached across a year boundary.** The column stores no
  precision and `norm_date_detail` coarsens hedged dates into it — a bare "2027"
  lands on 2027-01-01 and "late 2027" on 2027-10-01 — so a source restating the same
  year more precisely is indistinguishable from a 273-day delay. Coarsening always
  stays inside the stated year, so a move into a later year cannot be an artefact
  and a move within one might be. The event is written either way, saying which case
  it is; only the unambiguous one gets counted.

  No risk is invented when none is open. A date moving says the timeline changed,
  not why, and manufacturing an obstacle from it would put an uncited guess into the
  field an operator acts on.
- **`tracker exposure`** — planned capacity sitting behind an open obstacle, rolled
  up `--by category | state | company | customer`. Severity is reported as three
  MW columns rather than collapsed into one number: collapsing needs a weighting,
  a weighting is a judgement rather than anything a source said, and this tool does
  not present judgements as facts. `--weighted` adds the single number for whoever
  wants it and prints the weights it used. Projects with an open risk but no cited
  capacity are counted in their own column rather than treated as zero MW at risk.
  Grouping by anything but category counts each project once, in its most severe
  open category, so a project obstructed three ways is not triple-counted.
- **`tracker risks`** — obstacles across the database grouped by kind, each with the
  projects it blocks and the planned MW behind them. This is the query one free-text
  sentence per project could not answer, and it is what carries the read-through:
  MW blocked on transmission is a power and utility signal, MW blocked on `offtake`
  or `chip_supply` is a cloud and semiconductor one. An uncited risk is labelled as
  such rather than presented alongside quoted ones.
- `tracker list --risk <category> --severity <level>`, composing with the existing
  filters. Matching is an EXISTS over *open* risks, so a project obstructed three
  ways is still one row and a resolved obstacle does not match.
- `tracker show` renders each risk with its severity, dates, delay and quote;
  `tracker stats` gains a "by open risk" table with MW at risk; `tracker review`
  calls out an open `blocking` risk that has no citation.
- Exports carry risks. CSV appends a `risks` column of open `category:severity`
  pairs — appended at the end, not slotted in beside `blocker` where it reads
  better, because that tuple is a positional contract. JSON gains a nested `risks`
  array including resolved ones, since it has a `status` field to say so. The JSON
  schema tag moves to `tracker/2`.
- Obstacle extraction: `extract-v1` returns a `risks[]` array instead of a
  `blocker` string, `crawl._risks` gates each entry, and `upsert._upsert_risks`
  writes them the way `_upsert_events` writes milestones — dedup on
  `(category, first_seen)` in Python, so re-ingest stays idempotent even for an
  undated obstacle where the UNIQUE constraint cannot help.

  `project.blocker` is now derived by `upsert._derive_blocker` and is in a new
  `DERIVED_FIELDS` set that the claims-merge loop skips. Sources still record a
  `blocker` claim so `source.fields` says which citation supports it, but the value
  is written and never read. Two consequences worth naming: an obstacle can finally
  be **cleared** (resolve the risk and the column goes NULL), and the citation for
  `blocker` now lives in `risk.source_id` — `test_every_field_is_cited` was extended
  to follow it there rather than dropped.

  An article that stops mentioning an obstacle does not clear it: that is not
  evidence it is gone. Re-reading updates wording and severity in place but never
  revives a risk an operator resolved — `status` belongs to the operator.
- `ingest manual` accepts a `risks` array; a bare `blocker` string still loads and
  becomes one `unclassified` risk cited to the record's first source, so curated
  files written before the table keep working. Two curated risks sharing a category
  and date are refused at validation, where the operator can see which lines
  collide, rather than as an IntegrityError partway through a write.
- **A third discovery tier, `risk_signal`, so obstacle news reaches the queue.**
  Every `signal` term was announcement-shaped — announce, expand, invest, build,
  campus, megawatt — which silently discarded every article about a project going
  *wrong*. Measured against the real filter, all of these were dropped for having
  "no project signal": "Loudoun supervisors reject data center rezoning
  application", "Georgia Power says transmission upgrades delay data center
  energization", "Transformer shortage pushes back hyperscale data center
  timelines", "Moratorium halts new data center development in Fayetteville",
  "Water use concerns stall Tucson data center vote". So the corpus the extractor
  ever saw was announcements only, and no schema change can recover an obstacle
  from an article that was never queued.

  A risk term satisfies the signal tier alone, but `topic` must still match, so a
  transformer shortage at a steel mill is still dropped and the `exclude` tier
  still runs first — commentary, analyst notes and share-price coverage stay out.
  The list is operator-editable in `seed/feeds.toml` and absent from a config
  entirely means the filter behaves exactly as before.

  Measured over one live poll of all eight feeds: 200 entries, 38 kept by the two
  tiers, 44 by the three, of which two were real project obstacles and four topical
  commentary — the same over-collect-and-triage ratio the queue is designed for.
  Short terms are padded (`" sue "`) like `" mw"` in the signal tier: unpadded,
  `sue` matched *issued* and queued "PJM Issued First Backup-Generator Warnings" as
  litigation news.

  This is deliberately **not** a lever for `blocker` coverage; `tracker gaps`
  reports that field as unmeasurable precisely because absence is usually the
  truth. The point is to stop discarding the articles where an obstacle is real.
- Among the queued articles covering a tracked project, the ones reporting an
  obstacle are crawled first (`pending(spec=...)`, `pending_risk_count`). Those are
  the highest-value calls available: a project's own press release never names its
  blocker, so an adversarial second source is the only thing that can record one.
- **The queue is prioritised toward depth.** A queued article covering a project
  already tracked becomes a SECOND source, which fills fields one article cannot
  and lifts confidence from 2 to 3; draining oldest-first instead just grew the
  database sideways into more single-source rows. Measured before the change: 26 of
  29 projects had exactly one source. `--breadth-first` restores the old order.
- **`tracker sync --deep`** walks site archives via their sitemaps, following a
  sitemap index one level and preferring article sitemaps over Company/Event ones.
  A measured run found 799 matching URLs going back to 2015, 477 of them new —
  with no API key, no quota, and no credentials to set up, because sitemaps are
  published expressly for machines to read. This is now the recommended way to
  fill the database; search is optional.
- **`tracker/ingest/search.py`** — search-based discovery. `tracker search` runs
  Google's official Custom Search JSON API, and `--from-llm N` has MiniMax propose
  the queries. Model-proposed project names are search leads only: nothing it says
  is stored, and a project appears only after a real article was fetched and its
  values backed by verbatim quotes. Also `tracker sync --search N`.
- `tracker queue --failed` and `tracker sync --retry-failed`, plus an always-on
  report of unread URLs grouped by host. They were previously invisible: `discover`
  never re-queues a known URL and the pending queue only holds `discovered`, so a
  run could say "queue is empty, 0 failed" while a dozen articles sat unread.
- `datacenterknowledge.com` added as a feed: 50 entries per poll, ~22 matching, and
  unlike datacenterdynamics its articles are fetchable over plain HTTP.
- `tracker/export.py`: deterministic Markdown, CSV and JSON export.
- `tracker/cli.py`: `init`, `ingest {manual,pjm,crawl}`, `discover`, `queue`,
  `list`, `show`, `stats`, `review`, `verify`, `export`, `version`.
- 701 tests, green offline with no API key and no network. 93% coverage on both
  `normalize.py` and `confidence.py`. A `network`-marked test checks the feed URLs
  still resolve.

### Fixed

- **An expired console session was invisible.** Every API call 401'd once the
  session cookie ran out, but the page kept showing whatever it already had and
  each feature reported its own misleading local reason — the 3D map said "3d
  unavailable offline" when the actual cause was an expired login. `api()` now
  redirects to `/` on a 401 rather than letting the caller render a stale page
  as if it were live.

- **A failed module/atlas load in the map and 3D-campus widgets was cached
  forever.** `p = p || import(...)` memoises the *rejected* promise too, so one
  transient failure (a dropped connection, or the 401 above hitting the module
  fetch) left the widget reporting "unavailable offline" for the rest of the
  page's life even after the cause was gone. The promise is now cleared on
  failure and the note becomes a "tap to retry" control
  (`dc-campus.js`, `dc-map.js`, `dc-map3d.js`).

- **A quote popover was unreachable on a touchscreen.** It only ever opened on
  `mouseenter`, so a phone or tablet — no hover — could see the underline
  styling implying a quote existed but never see the quote itself. A click/tap
  now toggles it open and sticky (dismissed by Escape, an outside tap, or
  scrolling) alongside the existing hover behavior; deliberately not given a
  keyboard tab stop, since 124 rows × 12 fields is 1,488 tab stops for a feature
  the drawer already exposes per-field with a real tab order.

- **Two events or risks with the same `(type, date)` in one record crashed the
  whole run.** `_upsert_events`/`_upsert_risks` looked up "already have this
  one?" in a map built once from what was already in the table before the
  record started, so a second same-key row within the same record was never
  seen as a duplicate — both were added, and the flush hit
  `uq_event_project_type_date`, failing the entire upsert including everything
  else the record carried. Seen live on an SEC filing listing two capacity
  expansions dated the same day, a shape a news article rarely produces, which
  is why this survived until filings became a source. Fixed by registering each
  new row into the map immediately after adding it, not just what pre-existed.

- **`html_to_text` mis-read numeric HTML entities**, silently dropping or
  corrupting figures next to them. `&#160;` (a non-breaking space, written
  numerically rather than as `&nbsp;`) appears 17,469 times across 39 SEC
  filings and routinely sits between a number and its unit: `"$ 13.5&#160;billion"`
  parsed as `$13` — a billion-fold error — and `"393&#160;MW"` matched nothing at
  all, silently losing the value. The old table hand-covered nine named entities
  and no numeric ones; replaced with `html.unescape`, which handles both. News
  HTML mostly emits `&nbsp;` (covered already), so this stayed invisible until a
  numeric-entity-heavy source — filings — arrived.

- **The entrance animation smeared the page.** A ghost of the coverage strip kept
  being painted at the bottom of the viewport, below the table, after scrolling.

  `animation-fill-mode: both` retains the final keyframe, and `rise` ends on a
  transform — an identity one, but a transform. That makes the element a
  containing block for every `position: sticky` descendant and keeps it
  composited indefinitely. The table has sticky id and company columns inside
  every row, so 124 rows were each holding a transform, and so was the container
  wrapping the whole table.

  Three changes: `backwards` instead of `both` everywhere, so the transform
  exists only while the animation is actually running; the entrance class is
  applied to the first twelve rows rather than all 124, since animating a hundred
  rows at once is a hundred compositing layers for no visible gain; and anything
  containing a sticky descendant — the table rows and the view container — fades
  without moving. Translation is a nice-to-have, not breaking `position: sticky`
  is not. Verified: zero sticky containers hold a transform, and the sticky
  columns stay pinned while the table scrolls sideways.

- **`--browser` never worked, on any path.** `enrich --browser`, `sync --browser`
  and `ingest crawl --browser` all built a `Crawl4AIFetcher` and passed it down
  without ever entering it, so the first page that needed escalating died with
  `Crawl4AIFetcher must be used as an async context manager`.

  Nobody could have entered it from where it was built: launching Chromium is
  async and every caller is synchronous — `crawl.run` owns the `asyncio.run`. So
  `fetch_all` now starts and stops it, being the only code already inside the
  loop. Lazily, on the first page that actually needs it, because most runs never
  escalate and a browser costs seconds and a process. It is started once per run
  however many pages escalate, and always shut down afterwards: Chromium does not
  exit on its own and a leaked one outlives the command. A browser that fails to
  start now logs once and lets the run finish on plain HTTP, rather than raising
  the same traceback for every URL.

- **`--browser` without the optional extra failed in the wrong place.** The
  `crawl4ai` import lives in `__aenter__`, so constructing the fetcher could not
  raise `MissingDependency` and `enrich`'s `try/except` around it was unreachable
  — the run got as far as the first escalation before noticing. All three
  commands now call `Crawl4AIFetcher.ensure_available()` up front, so the failure
  lands on the flag.

- **Rich ate the install instruction.** `_fail` printed its message as markup, so
  the crawl4ai error told you to run `pip install -e "."` — Rich did not
  recognise `[crawl]` as a style and dropped it. The message explaining how to
  fix the problem was broken by the same mechanism. Error text is data and is now
  escaped.

- **`_print_standing` was registered as a CLI command.** A stray `@app.command()`
  on a private helper put a broken `-print-standing` in `tracker --help`; it took
  a `project` string argument and would have crashed on any invocation.
- **`stats` and `gaps` emitted no JSON at all on an empty database.** Both
  returned early with prose even under `--json`, so a consumer piping stdout into
  a parser got something unparseable instead of "zero projects".
- README's "Known gaps" still said the crawl path had only been run against
  fixtures, never live — stale since `de4821c`'s live-run defect fixes and now
  since `tracker enrich`'s live verification against project #93.
- **A first-party press release scored below the trade-press rewrite of it.**
  `classify_source_type` recognised a company only by subdomain — `news.microsoft
  .com`, `about.fb.com`, `blog.google` — so an operator publishing at
  `www.stackinfra.com/news/…` fell through to `general_media`, weight 1, against
  trade press's 2. The single most authoritative source for capacity, investment
  and timeline was the lowest-weighted one. It now consults the operator hosts
  declared in `seed/feeds.toml`.
- **A foreign-language quote could evidence a project's `phase`.** Every other
  field is protected from a translated repost for free, because "230兆瓦" matches
  no MW pattern, "12亿美元" no currency pattern, and neither matches any English
  phase keyword. But `phase` is a `_SUMMARY_FIELD`, and that carve-out trusts the
  model's *label* over a verified quote — precisely so an honest paraphrase is not
  discarded for sharing no substring with its own evidence. It had no language
  check. Measured against a real Chinese repost of a US announcement,
  `phase=construction` was stored while every quantity in the same sentence was
  correctly dropped. The carve-out now requires its quote to look English too.
- **A company-name suffix is no longer treated as a distinctive project token.**
  `project_identities` excluded name tokens that appear in the *normalized* company
  key, but `company_key` strips corporate suffixes — so "STACK Infrastructure" keys
  to `stack` and left `infrastructure` looking distinctive in "STACK Infrastructure
  Hillsboro Campus", while every STACK slug contains `stack-infrastructure`.
  Company-wide stories ("raises $400 million to fund growth", "expansion into
  Asia-Pacific markets") were harvested as evidence about one Hillsboro project: 8
  false matches for that project alone, and any operator whose name ends in a
  stripped word — Infrastructure, Data Centers, Energy, Systems, Realty — leaked the
  same way. Tokens are now excluded against the raw company name as well as the key,
  which keeps the Facebook → meta aliasing working. Found while testing `tracker
  enrich`, which read 7 such articles and filled nothing.
- Read commands refuse a database that is behind on migrations, naming `tracker
  init` as the fix, instead of failing with a raw SQLAlchemy "no such table"
  traceback. Found on a real v3 database once `risk` landed: `tracker risks`,
  `show`, `stats` and `exposure` all query a table it does not have. A read command
  opens the file `mode=ro` and so cannot migrate on the operator's behalf; saying
  which command will is the only useful thing it can do.

### Changed

- Four additions beyond the PRD's three-table schema, each unblocking a stated
  PRD requirement: `source.claims` (Q2's "keep both conflicting values" and the
  confidence agreement rule), `source.extractor` (prompt-version traceability),
  `project.county` (ISO queues report county, not city), and the `ingest_url`
  table (the PRD's `fetch_error` marker cannot live on `source`, which requires
  a `project_id`).
- Confidence caps at 2 for a single source however authoritative; 3 requires
  independent corroboration or operator verification.
- ISO queue ingest is scoped as candidate generation, not a project feed: the
  public queues are *generator* queues with no data-center column, so matching is
  a keyword heuristic, confidence caps at 1, and queue MW is disclosed in `notes`
  rather than written to `mw_planned`. See README "Why".
- Crawl4AI is an optional extra used for fetching only, not for LLM extraction,
  so that prompt versioning is truthful and the JSON contract is enforced in
  testable code. `httpx` is the default fetcher.
- The database is treated as a build artifact and is not committed; `seed/*.json`
  and `data/raw/*.csv` are, and reproducibility is a documented replay.
- Hedged dates resolve instead of vanishing: `late 2027` → 2027-10-01 at quarter
  precision, `H1 2027` → half precision, `by 2028` → year precision, each with a
  note recording the original phrasing. Only genuinely unanchored phrasing
  (`next spring`) and directional hedges (`before 2028`) stay NULL. Treating every
  hedge as unparseable was discarding most of `expected_online`.

### Fixed

- **The evidence gate no longer discards values over the model's own bookkeeping.**
  It required a quote *tagged* with each field's name, so a correct value whose
  verbatim quote was filed under a different field was thrown away. T5@Augusta
  returned `mw_planned: 200` with the sentence "…a 140-acre, 200 megawatt campus
  in Georgia" attached to `name`, and lost the capacity. Across the first 90
  projects this discarded **89 correctly-evidenced values from 64 projects** — 60
  of them `phase`, which being NOT NULL silently became the `announced` default,
  so the stored phase distribution was an artefact of the gate rather than of the
  projects.

  A value now survives if any *verified* quote actually asserts it, whichever
  field the model filed it under. Quotes are still required to be real substrings
  of the fetched article, and the new check is strictly stronger than the one it
  replaces: numbers and dates are compared after normalization (so "1.2GW"
  evidences `1200.0`), which means a genuine sentence citing a *different* number
  no longer launders an invented value. `phase` is matched on article wording
  ("broke ground" → `construction`) because it is a judgement, never a quotable
  string; `blocker` and `notes` keep the label check, being paraphrases that
  legitimately share no substring with their own evidence.
- **Two overlapping runs no longer collide silently.** SQLite takes one writer, so
  a second `tracker sync` failed partway with a raw forty-line SQLAlchemy
  traceback — after it had already paid for LLM calls. A lock file now refuses the
  second run up front, reclaiming the lock if the holding process has died, and any
  remaining "database is locked" is translated into a message that says what to do.
- `crawl.run` commits after each URL rather than once per run. One transaction
  spanning 150 articles held the write lock for around 25 minutes, and a failure at
  article 149 discarded the other 148.

### Fixed (earlier)

Found by running the crawl path against the live MiniMax API for the first time:

- Rich read `[crawl]` in the "install the extra" hint as a style tag and
  deleted it, so the message told the operator to run `pip install -e "."` —
  omitting the one thing it existed to communicate. Same bug silently emptied
  the `--browser` help text.
- **A placeholder URL is no longer treated as a citation.** It is dropped before
  any weighting, so it can neither supply the "strongest source" nor count
  toward domain independence. Observed live: a real Microsoft project reached
  confidence 3 because a placeholder seed row contributed a weight-3
  `company_filing` for a URL that does not exist. Placeholder-only rows now
  score 0 and are routed to `tracker review`, which is the whole point of the
  seed guard.
- The "dropped unsupported value(s)" note over-reported. Identity fields are
  restored after the evidence gate (a project row cannot exist without them), and
  `notes` is a summary rather than a citable claim — but both were still listed as
  dropped. A false statement in the one place an operator looks to judge data
  quality is worse than no statement.
- Note lines contributed by an ingest record are now scoped to that record, so
  re-extracting an article *replaces* its own disclosures instead of leaving a
  stale variant beside the new one. Observed live: a corrected note appeared next
  to the wrong one it superseded.
- The possible-duplicate warning is derived rather than contributed, so it is
  recomputed from current state every run. It previously vanished when a record
  was re-ingested, even though the ambiguity had not been resolved.
- `--check` used a 16-token budget, but the M2.x/M3 models emit chain-of-thought
  inside `<think>` blocks in the content field, so the entire budget went to
  reasoning and the check reported a truncated thought instead of the answer. It
  now also reports `finish_reason` and whether the model spent thinking tokens.
- The test suite no longer reads the operator's `.env`. Deleting the environment
  variables was not enough; once a real `.env` existed, a test asserting "no key
  configured" passed on a clean machine and failed locally.
- The project's `.env` is read by absolute path. pydantic-settings resolves a
  relative `env_file` against the current directory, so the API key in
  `<project>/.env` was invisible whenever `tracker` ran from anywhere else —
  the normal case now the CLI is on PATH. A `.env` in the current directory is
  still read and still takes precedence.
- The missing-key error named `MINIMAX_API_KEY`; the variable actually read is
  `TRACKER_MINIMAX_API_KEY`.
- `migrations/`, the prompt files and the article cache are located relative to
  the installed package rather than the current directory. `tracker init` run from
  outside the project tree previously failed looking for a `migrations/` folder in
  the operator's home directory.
- Feed filtering normalizes URL slugs, so `data-center` matches the term
  `data cent` and `900MW` matches `mw`. Without it, matching against the URL —
  the only way to filter a sitemap entry or a feed with empty titles — never
  matched anything.
- Feed filtering matches the URL *path*, not the host. `datacenterfrontier.com`
  contains "datacent", so every article on a specialist outlet satisfied the topic
  tier from its domain name alone.
