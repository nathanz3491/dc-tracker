# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

First working version. Nothing has been released yet, so everything below is the
initial build of the v1 PRD.

### Fixed

- **`phase` was the one NOT NULL field the agent could rule on, and it cost three
  rounds of a ten-hour run** (`tracker/triage.py`, `tracker/cli.py`,
  `tests/test_triage.py`).

  `apply_rule_out` blanks a field before re-deriving it — `audit._rule_against`'s
  trick, and correct for every nullable column. `phase` is not nullable, so `= None`
  plus a flush raised `IntegrityError`, and the exception escaped mid-batch: the
  **entire logic phase of rounds 1, 2 and 3 died** after the first phase ruling. All
  the finding reduction in those rounds came from the duplicate half instead. Blank
  to `DEFAULT_PHASE`, which is what `recompute_from_sources` falls back to two lines
  from its own end.

  The runner now isolates each finding too: a database error while *applying* a
  ruling is reported against that row and rolled back rather than ending the batch.
  One bad finding at 3am must not cost the night.

### Changed

- **The agent stopped paying `max` reasoning effort on every turn of every loop**
  (`tracker/config.py`, `tracker/llm.py`, `tracker/cli.py`).

  `logic resolve --agent` reused `reasoning_extractor`, whose whole argument for
  `max` is that `infer` is **one call per project**. The agent makes nine to twelve
  calls per finding, so it inherited an effort tier chosen on the opposite premise.
  New `agent_extractor` and `Settings.deepseek_agent_effort`, defaulting to `high`;
  `--llm` is one call and keeps `max`.

- **The loop's history is append-only, and now says why** (`tracker/agent.py`,
  `tracker/llm.py`, `tests/test_agent.py`).

  Shortening stale tool results looks like the obvious saving — the conversation is
  re-sent every turn, so an article read on turn 3 goes over the wire again on turns
  4 through 10. It was implemented and then reverted, because it is wrong on this
  provider: DeepSeek bills on the message *prefix*, turn N's request is turn N-1's
  plus an append, and an edited prefix cannot hit the cache. The trim would have
  raised the bill it was written to lower.
  `test_the_history_is_append_only_so_the_prefix_stays_cacheable` asserts every
  request begins with the previous one byte for byte, so it cannot come back.

  `LLMReply.cache_hit_tokens` / `cache_miss_tokens` are read out of the provider's
  `usage` — several field spellings accepted, because they have not been stable and
  an unrecognised one is indistinguishable from a 0% cache rate — and totalled onto
  `AgentResult`. `logic resolve` prints the hit rate, so the next run measures what
  the cache is worth rather than assuming it.

### Added

- **`enrich --agent`: a model that goes and finds the missing field, and cites it**
  (`tracker/gapfill.py`, `tracker/cli.py`, `tests/test_gapfill.py`).

  `fields_present` is the **sole** condition holding 283 of 437 rows at T1 — every
  other T2 condition passes on all of them. So after a night of settlement work,
  nothing in `logic resolve` or `duplicates resolve` can move a single one of those
  rows: they are not wrong, they are empty. That is a different job and it needed a
  different tool.

  `enrich`'s existing harvest searches from a fixed table: `_FIELD_QUERIES` maps
  `mw_planned` to the literal phrases "megawatts capacity" and "MW data center
  campus". That is the same fixed-menu shape that capped `logic.decide` at 94 of 526
  findings — it can only ask what somebody wrote down in advance. The agent looks at
  the actual row, picks its own searches, reads the pages and reports what it found.

  **The fact arrives as a citation, never as a column**, which is `triage`'s lesson
  reached from the other side: a scalar is a cache of the claim set, so an assignment
  is undone by the next `backfill derive`. The terminal tool reports a *sentence in a
  document at a URL*; `upsert_record` attaches it as a source whose `claims` carry the
  value and whose `quotes` carry the sentence, and the merge policy derives the field.
  That is also what makes it count: a quote-backed value is `reported` rather than
  `inferred`, and **`capex` does not sum `inferred`**.

  Five refusals, each with a test, because this must not become `infer` with a search
  engine bolted on: a quote that is not in the text, a quote from a URL the run never
  opened (a search snippet is not a document), a quote under 40 characters, a value
  that will not coerce, and a field that is not actually a gap. `existing_only=True`
  plus `route_to` mean a run can only ever add citations to the row it was asked
  about — never create one.

  `--agent` runs *after* the harvest rounds, on what they could not reach. The cheap
  rung costs a few hundred tokens and the agent costs ~77,000 a row, so pointing it
  only at the residue is the whole point.

### Added

- **A loop that runs until the backlogs stop moving** (`scripts/overnight.sh`,
  `README.md`).

  Neither backlog is a queue that drains, which is why every other script here
  does one pass and this one does rounds. Ruling a claim out re-derives the row and
  a re-derived row can raise a finding the old value hid — 52 resolutions took the
  total from 530 to 529. Merging changes the survivor's claim set, which can match
  a third row that did not match before — answering 13 pairs took the group count
  from 47 to 48. So the target is a fixed point, and the stop condition is two
  consecutive rounds with no reduction in either count.

  It steers on two numbers from one process — logic findings not yet answered, and
  duplicate *groups* rather than pairs, because nine rows for one campus make 36
  pairs and one group and a loop counting pairs would read a single merge as huge
  progress.

  **Sized against measurement, not hope.** The agent reads whole articles: ~45,000
  tokens per finding, 495 unanswered, so ~22M tokens and roughly ten hours for the
  first sweep. `--tokens` caps it at 25M by default and `--hours` at 8. Later
  rounds are far cheaper because a finding the agent answered *or declined* is
  recorded and never re-offered.

  Honest about its own ceiling: `block_label_ambiguous` and its relatives — ~250
  findings — are about tranche identity, and superseding a claim about a field
  cannot fix them. The agent will read the sources and decline, once each. The
  header says so, so a stubborn residue is not read as a failure.

- **`logic resolve` and `duplicates resolve` now settle by ruling on claims, and
  the agent is the default** (`tracker/triage.py`, `tracker/cli.py`,
  `tests/test_triage.py`, `tests/test_triage_pairs.py`).

  **The repair was never durable, and that is the headline.** A project scalar is a
  cache of the claim set. `recompute_from_sources` re-derives it on the next
  ingest, merge or `backfill derive` — and the deployer runs `tracker init`, so a
  recompute happens on **every deploy**. Every field-assigning action in `logic.py`
  is therefore transient: `_clear_built`, `_clear_expected_online`,
  `_clear_first_announced`, `_raise_planned_to_built`. Measured directly on the
  live database — clear `mw_built` on #14, commit, derive, and 230.0 comes back.
  That is why a run resolving `built_exceeds_planned` **18 times** left
  `no_inversions` at exactly 30 failures, and why 52 resolutions moved 2 rows.
  `audit.py` had already learned this and reshaped every action around
  `conflicts.supersede`; `logic.py` never did. `test_assigning_the_column_does_not_survive` pins the old behaviour so the new path cannot drift back into it.

  So the model is no longer asked what a field should be. It is asked **which
  citations are wrong about it** — a question about evidence it can answer from the
  article — and the claim is superseded, which survives every recompute because the
  merge policy then derives the field from what is left. The field is emptied
  before the recompute rather than assigned after, so a field whose every claim has
  been ruled out stays empty without this code ever choosing a number.

  **For duplicates, one rail is deliberately gone.** `merge_blocked` refuses a
  cross-granularity pair categorically because "a model is not a person with a
  map". True of a model shown two rows and nothing else — and 28 of 47 live groups
  are exactly that shape, with the model already reasoning containment unprompted
  (*"El Mirage is a city within that county"*, 0.80–0.85, all 45 left
  unactionable, holding **78 rows** out of T1). An agent that can read the articles
  and search can be given a map. The rails that are *fact checks* rather than menus
  all stay, each with a test: distance beyond `FAR_APART_KM`, ordinal siblings,
  a confidence floor, and a quote verbatim from something the run actually read.

  `--agent` is the default; `--auto`, `--llm` and `--no-llm` all suppress it, so a
  script pinned to the old behaviour keeps it. A bare `logic resolve` with no key
  falls back to the interactive walkthrough rather than failing, because making the
  agent the default must not take that away from somebody without one.

- **A model that can go and look, and say what it concluded** (`tracker/agent.py`,
  `tracker/llm.py`, `tests/test_agent.py`).

  Four commands ask a model to check a claim against the sources — `logic
  resolve`, `duplicates resolve`, `audit resolve`, `risks confirm` — and each was
  one `complete()` call with a hand-built context block and a hand-written menu of
  answers. Both halves of that shape were the ceiling, and both are measured.

  **The menu.** `logic.decide` returns "nothing to choose between" *before it calls
  the model at all* whenever `ACTIONS[code]` is empty, and it is empty for 11 of
  the 16 rule codes — **432 of 526 findings** on the live database, 334 of them
  block problems. Measured against that: of the 94 findings the model *was* shown,
  it acted on 52 and declined 10. It was never the cautious party; the list of
  things it was permitted to say was the constraint.

  **The context.** `_triage_context` shows a 280-character excerpt per field while
  the article behind it sits in the cache or one request away, and nothing asked
  for it.

  So this is one loop rather than a better prompt in four places. A caller passes
  tools and a terminal tool; the model reads articles, searches the web, pulls up
  neighbouring rows, and answers by calling the terminal tool with its own
  conclusion. `evidence_toolkit` ships the five reads every such caller wants, so
  `enrich` and the two `resolve` commands compose rather than each growing a
  context builder.

  `llm.ToolExtractor` is a **new protocol beside** `Extractor`, not a widening of
  it: a dozen fakes in the suite implement `complete` and none needed changing, and
  a provider without tool support now fails loudly instead of silently answering
  with the tools dropped — which would read as a cautious model and be a dropped
  request.

  **What it deliberately does not do is decide whether an answer is believed.**
  That bar differs per caller: a duplicate verdict wants a distance check, a field
  edit wants a quote. One policy serving four questions is exactly how
  `check_collisions` and `_resolve` came to disagree about `phase` and report 48
  repairs that never landed. `verbatim()` is offered for the quote half, because
  the evidence tier decides whether `capex` sums the value at all — an agent
  writing without quotes would do correct work that reaches no published total.

  **Budgets.** 20,000 tokens a turn, retried once at 50,000 on `finish_reason ==
  "length"`, and only when no tool call came back — a truncated reply that still
  produced a complete call is usable as it stands. The escalation does not carry
  into the next step, so one long deliberation does not reprice the run. The old
  8,000 was measured truncating replies mid-reasoning: one unusable answer in a
  63-finding run, and a duplicates verdict lost outright to "the reply was cut off
  while reasoning".

  **Articles are fetched lazily, not pre-warmed.** The cache holds 498 of 2,103
  cited urls, and filling the rest would be ~1,600 speculative requests at hosts
  that have already answered this database with 634 403s. `read_article` fetches
  what a run actually asks for and caches it, so every run makes the next cheaper.
  Measured on a 150-url tranche: 145 succeeded.

  One bug the tests caught before it shipped: `{}` is both what a failed argument
  parse falls back to *and* what a correct call to a no-argument tool looks like,
  so reading emptiness as failure broke every such tool. `ToolCall.parse_failed`
  now says which happened.

- **A console runner for the two families that need a judgement**
  (`scripts/resolve.sh`, `README.md`).

  Logic collisions and suspected duplicates are the two reports where the question
  is "which of these two claims is right", and the answer costs an LLM call.
  `settle.sh` runs them inside the whole loop; this runs only them, for when that
  is what you want answered.

  Ordered logic first, because a collision settled on a row changes the claim set
  the duplicate judgement is then made against, and duplicates last because it is
  the only step that deletes anything. It prints what is open before spending, asks
  once before folding when there is a terminal, declines to fold when there is not
  and `--yes` was not given, and takes a `VACUUM INTO` snapshot regardless.

  Two things it is honest about rather than quiet about. `logic resolve --auto
  --apply` is **currently a no-op**: it reports 48 rows whose `phase` drifted, then
  applies the repair via `recompute_from_sources`, which re-derives `phase` and
  lands back on the stored value — so it prints 48 repairs and writes none, every
  run. `check_collisions`'s winner and `_resolve`'s answer for `phase` disagree.
  Verified on the live database: #14 and #9 are still `operational` after a run
  that claimed to move both to `construction`, the phase histogram is unchanged,
  and a re-run reports the same 48. And of 47 suspected duplicate groups, 28 are
  city-vs-county and 8 are name-overlap, so `merge_blocked` will refuse roughly
  three quarters unattended — which is the rails working, not failing.

  One bug caught while writing it, worth recording because it would have been
  invisible: `set -o pipefail` plus `head` is a trap. `head` closes the pipe once
  it has its lines, `tracker` takes SIGPIPE, and the pipeline reports failure — so
  the preflight summary would have killed the script through its own ERR trap.
  Measured directly: the `head` form exits 1, the `sed -n '1,12p'` form exits 0.

- **One command that settles, because `sync` only recomputes**
  (`scripts/settle.sh`, `README.md`).

  `tracker sync` names its sixth phase `settle`, and that phase is `derive.run()`
  plus `recompute_confidence()` — two pure recomputations. Every command that
  actually settles an open question is a separate one, and nothing chained them.
  Measured on the live database: 471 projects, **27 at T2 COMPLETE and 0 at T3
  SETTLED**, with `warnings_settled` failing on 253 rows and `duplicates_answered`
  on 103.

  The automation was never the missing piece. `duplicates resolve` already defaults
  to `--llm` on and `--ask` off, `risks confirm` has no `--ask` at all, and `audit
  resolve` goes straight to the model when there is no terminal. What was missing
  was an order to run them in, and the order carries the whole argument: **dates
  before derive**, because 64% of queued URLs have no publication date and a merge
  tiebreak silently falls back to crawl order — settling collisions first records
  the wrong winner as settled; **geo before duplicates**, because a merge rail
  refuses a pair whose stored coordinates are over 25 km apart and coordinates
  exist on 39% of rows, so on most pairs that rail cannot fire at all; **duplicates
  last**, because it is the only step that deletes rows.

  `--refetch-dates` is its own flag rather than part of the free tier, because the
  free tier is cheap and this is not: 60 dates are readable out of the URL path and
  **965 need one HTTP request each**. Tranched and resumable, so repeated runs walk
  the backlog down.

  Three tiers, following the convention `sync` already sets: free by default,
  `--llm` for the model-decided phases, `--merge` for the folding. `--merge` takes
  a `VACUUM INTO` snapshot first — never a `cp`, for the WAL reason
  `scripts/sync_db.py` documents. Every model phase runs with stdin on
  `/dev/null`, so an unexpected prompt hits EOF and stops rather than hanging a
  scheduled run while holding the write lock. It closes on `tracker clean
  --snapshot --since 1`, which makes consecutive runs comparable instead of each
  one being a fresh opinion.

  Not in it: `tracker blocks` finds 110 mergeable tranche groups across 69
  projects and there is no `blocks fold` to run. That one needs code.

### Changed

- **An empty watchlist watches nothing** (`migrations/0022_watch_all.sql`,
  `tracker/feed.py`, `tracker/webui/`, `tracker/cli.py`).

  `feed.digest` read `watching_everything = not entities`: an account that had
  named nothing was shown every project. The argument for it was that a blank page
  teaches nobody what the console is for, and that argument is real — it lost to a
  worse one. "Watching" then depended on a row count nobody could see, so on the
  live database two accounts that had asked for nothing were both shown all 456
  projects, and the two pages were **indistinguishable from a watchlist that had
  leaked between them**. Reported as exactly that.

  Wanting the whole database is legitimate, so it stopped being a state people
  fall into and became one they choose: `account.watch_all`, off by default, with
  a toggle in the console's watchlist panel and `tracker watch all --on/--off` for
  the terminal. Per account, so turning it on touches nobody else's page.

  **`tracker digest` with no `--user` is unchanged**, and it is a different
  question rather than an exception: it asks for every account's entries and falls
  back to the whole database only when there are none anywhere. A console with no
  accounts behaves the same way, because there is nobody whose preference to read.
  In neither case has a person said "nothing" — there is no person.

  One consequence worth stating: `notify send` no longer needs to *refuse* an
  empty watchlist, because the digest is now genuinely empty and the run is quiet
  by arithmetic. The refusal stays for an account that has turned `watch_all` on,
  which is the case it was really written for — a page somebody opens deliberately
  is not mail that arrives uninvited.

  No CHECK constraint on the column, and that is a SQLite limitation rather than a
  preference: it cannot be added with ALTER, rebuilding `account` would drop the
  foreign key `watch.account_id` points at, and a trigger is not available either
  because the migration loader splits on semicolons and documents that it can do
  so as long as there are no procedural blocks. `Account.watch_all` is
  `Mapped[bool]`, so the type is the guarantee.

### Added

- **`tracker notify` sends each person one email carrying everything on their
  watchlist that moved** (`tracker/notify.py`, `tracker/prompts` untouched,
  `tracker/cli.py`, `tracker/config.py`).

  Delivery through Resend. The unit is **a person and a window, never a signal**:
  fourteen updates is one message with fourteen cards, because a channel that
  sends fourteen separate emails is one people filter into a folder, and a
  filtered channel protects nobody — the same argument `feed.notable` makes about
  the bar for interrupting somebody, one layer out.

  **The message is never truncated.** `digest --notify` caps its *terminal* output
  at `NOTIFY_MAX_ITEMS` and counts the rest, which suits a stream scrolling past.
  An email is a document somebody works, and one ending "…and 3 more, not listed"
  sends them somewhere else to find the remainder — so the email lists every
  update, however long that makes it. Measured on the template: a card is 2.2 KB,
  twenty-five render to 54.8 KB, and Gmail clips past ~102 KB, so the outside
  boundary is about **46 updates in one window**. A nightly run averages 4.3;
  a `--days 30` catch-up would cross it, which is an argument for nightly.

  **Rendering is pure and separate from sending.** `render` takes a digest and
  returns a string — no socket, no settings — so the whole template is tested
  offline and `notify preview` shows the exact bytes `send` would post, with no
  key configured and nothing leaving the machine.

  **Meridian, inlined rather than imported.** The design system is React 19 plus
  Tailwind v4 and an email client runs neither, so the token *values* are
  transcribed into `TOKENS` and applied inline — the one place in this codebase
  where a literal hex is correct rather than forbidden, with its source named so
  the two can be diffed. `good`/`bad`/`neutral` are keyed on `feed.EVENT_SIGN`, a
  closed vocabulary, so the palette cannot disagree with the digest about which
  way a signal cuts. Tables and inline styles throughout; no image, no script, no
  web font, because clients block remote content by default and a message that
  depends on it is broken on first open. There is always a plain-text part.

  **The same two refusals as `digest --notify`.** An account with no watchlist is
  skipped rather than mailed the whole database, and a quiet window sends nothing.
  Missing key or missing sender fails at construction, before a single message is
  built.

  `TRACKER_NOTIFY_FROM` has **no default**, deliberately: a sending address is a
  real domain, this repo is public, and the rule that keeps the production
  hostname out of tracked files (`CLAUDE.md` §6) applies to it too.

### Changed

- **A notification has to be about something that happened recently, not merely
  something we learned recently** (`tracker/feed.py`, `tracker/cli.py`).

  `notable` had three gates — quote-backed, not future-dated, material — and the
  second only ever excluded the *future*. Nothing excluded the deep past. The
  digest window is on `created_at`, and a crawl reads one article and imports a
  project's whole back-history, so every milestone in it arrived "today".

  Measured on the live database over thirty days, of 354 signals that would have
  notified: **107 described something more than three years old**, 162 more than
  one year. A nightly mailer would have paged somebody about a 2021 groundbreaking
  because an article mentioning it was read yesterday. This is the README's own
  "new means new to us" distinction, which the *page* honours by printing both
  dates and which a notification cannot honour by printing anything — an
  interruption is read after it has already interrupted.

  `feed.stale` is the fourth gate, at 90 days. Same thirty days: 354 signals
  become 129, the nightly average falls from 11.8 to 4.3, and the worst night — a
  large sync on 2026-08-11 — falls from **135 to 21**. An *undated* signal is kept,
  because `happened` is None for an obstacle nobody dated and an open obstacle is
  a statement about now.

  **`--notify` now refuses an empty watchlist.** `digest` shows the whole database
  when no list is set, which is right for a page — a blank page teaches nobody what
  it is for — and wrong for mail, which arrives uninvited. Measured: two accounts,
  zero watch rows, 193 projects that had moved. `--whole-database` says you meant
  it, so the capability survives as a choice rather than a default. The refusal
  writes to stderr and exits 2, so a cron piped into a mailer sends nothing rather
  than mailing its own error.

  **A burst is capped** at `NOTIFY_MAX_ITEMS` (20), with the remainder counted in a
  final line naming the command that shows them. Ingest arrives in bursts by
  nature and the recency gate reduces the worst night rather than flattening it; a
  cap that hid its own effect would read as "that was everything", which is the one
  thing a notification must not imply. `--json` is uncapped — a program can page,
  and truncating a payload is how a consumer silently under-reports.

### Changed

- **`tracker point` identifies each article after reading it, and acts on the
  answer** (`tracker/point.py`, `tracker/prompts/point-v2.txt`,
  `tracker/upsert.py`, `tracker/ingest/crawl.py`, `tracker/cli.py`).

  `point --url` asked which row a *typed name* looked like, printed the answer, and
  then discarded it — `_point_read` handed the links to `crawl.run` and let the
  dedup key decide alone, saying so on screen: *"expected to land on #1301; the
  dedup key decides, not this."*

  A dedup key cannot express **this town is in that county**. Measured live on the
  Nscale Monarch campus: the row already held `nscale|county:mason|WV`, the
  herald-dispatch article named Point Pleasant, the key came out
  `nscale|city:point pleasant|WV`, and one campus became rows #1301 and #1352 —
  plus a duplicate pair for somebody to settle by hand. The name-shaped question
  could not have caught it. The article names the operator *and* the place, which
  is the evidence that settles it.

  So the order is reversed: fetch → extract → identify from the record → route.
  `point.Identity` reads an `IngestRecord`, feeds both the prefilter and a new
  `point-v2` prompt built around place rather than name, and `crawl.run` gained a
  `route` hook it calls **per record** — one URL list can describe several campuses,
  and the question is only answerable once each article has been read. `--url` no
  longer pays for the up-front call at all.

  **The rails did not move**, because the asymmetry they protect has not changed.
  The shortlist is still deterministic, an id off it is still refused
  (`_read_match` is now shared by both entry points so the two prompts cannot drift
  apart on that), and anything below `MIN_CONFIDENCE` still builds a new row — the
  recoverable mistake. Every failure lands there too: no candidates, a dead
  provider, an unparseable reply.

  **Routing is not merging.** `upsert_record(route_to=...)` attaches the article's
  claims to that row and discloses it in the notes, in a sentence deliberately
  different from the `project_alias` one — nothing recorded this decision, so the
  note says the other identity is still unclaimed and names `tracker merge`. A
  stale id costs the run its merge, never its evidence. `route_to` with `force_new`
  raises rather than picking one.

  Batch ingestion is untouched and a test pins that: with no router the same
  article still splits, because `ingest crawl` and `sync` have nobody asking about
  a particular campus and the disclosed split is the right outcome there.

### Added

- **Extractions overlap, so a crawl is no longer paced by one LLM call at a time**
  (`tracker/parallel.py`, `tracker/llm.py`, `tracker/config.py`,
  `tracker/ingest/crawl.py`).

  `ingest/fetch.py` has run four requests abreast since it was written — a global
  semaphore, one gate per host, a politeness delay. The LLM layer beside it had
  none, and the model is most of a run's elapsed time: a fetch is seconds, an
  insert is milliseconds, and an extraction with reasoning on is tens of seconds.
  The serial half was the whole cost.

  `TRACKER_LLM_CONCURRENCY` (default 6) is how many extractions are in flight.
  Nothing about what gets stored changes, and the three reasons are the design:
  `extract_one` was already a pure function of a `FetchResult` with no session in
  sight; `upsert` recomputes every field from the full claim set, so it is
  order-independent and idempotent by construction; and the writes stay on one
  thread, keeping `crawl._checkpoint`'s commit-per-article — an interrupted run
  still resumes where it stopped rather than losing the batch.
  `test_concurrent_extraction_writes_the_same_rows_as_serial` runs the same input
  at 1 and at 3 into two databases and compares the rows.

  **Input order, not completion order.** `map_ordered` submits everything and
  yields results in the order they went in, so a run's log reads as it always did.
  Ordering by whichever call returned first would make two runs over one queue
  print different output and turn any test that pins it into an intermittent
  failure. It costs nothing: the pool runs `limit` tasks regardless of which one
  the consumer is waiting for, so head-of-line blocking delays a write —
  milliseconds — never a call.

  **Batching into one prompt was rejected, not overlooked.** It would be cheaper,
  and it breaks the evidence gate: with five articles in one call the model can
  attribute article 3's sentence to article 1's project, producing a quote that
  verifies against the batch and not against the citation it is filed under. That
  is a silent failure in the mechanism the whole dataset rests on. Concurrency
  spends exactly the same tokens on exactly the same prompts.

  **A local model gets 1 worker** (`TRACKER_OLLAMA_CONCURRENCY`), because local
  inference is compute-bound: a second request queues behind the first and competes
  for the same VRAM instead of halving the wall clock. `Settings.llm_workers()` is
  one resolver rather than each call site reading whichever field it remembers, and
  at 1 `map_ordered` runs inline with no pool at all — the serial path stays
  bit-for-bit the path it was, which is what lets a failure under concurrency be
  bisected against a serial run.

### Changed

- **One pooled HTTP client per destination, instead of a fresh connection per LLM
  call** (`tracker/llm.py`). Every call went through the module-level `httpx.post`,
  which opens a connection, completes a TLS handshake and discards both. Invisible
  while one call was in flight; not once several are.

  The `trust_env` guarantee moved with it, and is stronger for it. Keeping local
  inference off the system proxy used to mean passing `trust_env=False` on each
  individual call, which held only as long as every future call site remembered —
  and there are now five. It is a *constructor* argument on the local client, so a
  call site cannot forget. `test_ollama_traffic_never_transits_a_proxy` asserts it
  on the client, with a companion test proving no Ollama path can reach the API
  client instead.

- **LLM retry backoff is jittered** (`TRACKER_LLM_RETRY_JITTER`, default 0.25).
  With one call in flight the fixed `min(2**attempt, 30)` was fine. With several,
  workers rate-limited by the same response computed the same delay, slept in
  lockstep and retried in lockstep — a thundering herd aimed at the endpoint that
  had just asked for less traffic. A server's own `Retry-After` still wins on
  magnitude and gets the jitter too, since the point is that N workers told the
  same thing must not act in unison.

### Added

- **The console has accounts, and each one keeps its own watchlist**
  (`tracker/accounts.py`, `migrations/0020_accounts.sql`,
  `migrations/0021_watch_owner.sql`, `tracker/webui/auth.py`,
  `tracker/watchlist.py`).

  `TRACKER_CONSOLE_PASSWORD` was one shared secret for every reader, and the cost
  was not only authentication: **the landing page could only ever draw one
  watchlist**, because with no way to tell two people apart, "the things I am
  watching" was not a sentence the data could express. A `watch` row was a
  property of the database rather than of the reader — so everybody saw everybody
  else's interests, and any of them could delete yours.

  `tracker users add you@example.com` makes an account: an email, a password
  hashed with **`scrypt` from the standard library**, and nothing else. No new
  dependency, in a project that vendors its whole front end rather than take a
  CDN. The stored form is self-describing — `scrypt$n$r$p$salt$hash` — so the cost
  parameters can be raised later without a migration and rows written under the old
  ones keep verifying, which is the same reasoning `source.extractor` carries its
  own version for.

  **Zero accounts still means an open console**, exactly as an unset password did:
  `tracker serve` on loopback needs no setup, because reaching 127.0.0.1 already
  means having the machine. What refuses is publishing — `serve --tunnel` will not
  put a page with no way to gate it on the open internet. Creating the first
  account closes the gate on a *running* console within a few seconds: the server
  counts rows rather than reading a flag it was handed at startup, because
  `tracker users add` runs in a different process and a flag read once would leave
  a published console open until somebody noticed.

  **Invites, because there is no open registration.** Behind a tunnel the login
  page is a public URL, and an account can still read the whole dataset even
  though it can no longer run anything. `tracker users invite` mints a single-use
  code, prints it once, and stores only its sha256 — this database is copied
  between machines by `scripts/sync_db.py` and kept in `backups/`, where a
  plaintext code would be a live credential in every copy. The holder redeems it on
  the sign-in page and chooses their own email and password, which is the point:
  a password you picked and sent them is a password in a chat log. Every refusal —
  unknown code, expired, already spent — is the same sentence, because the
  difference is only useful to somebody probing.

  **The terminal reads across everybody and writes to one person.** Bare
  `tracker watch` lists every account's entries with an owner column, because a
  terminal on the host is looking at the database rather than at one person's slice
  of it; `--user` narrows it, and `tracker digest --user alice@example.com`
  reproduces exactly the page alice sees, which is the form to schedule if the
  nightly note is going to her. `watch add` and `watch rm` **require** `--user`:
  there is no shared list to fall back on, and writing without naming an owner
  would put an entry on somebody's page that they did not ask for.

  Details worth recording. Lockout is counted per client and globally but **never
  per email**, which would let anyone who knows an address lock its owner out. An
  unknown address and a wrong password produce the same message *and* the same
  work — a miss still spends one scrypt — so neither the wording nor the timing
  says which addresses have accounts. `redeem` creates the account before marking
  the code spent, so a typo costs a retry rather than the invite. And
  `allow_watch` on the wire answers "may *this reader* edit a watchlist" rather
  than "is the feature on", folding in both `--no-watch-edits` and whether anybody
  is signed in — sent identically by `/api/dataset` and `/api/updates`, because
  whichever the page happened to believe would otherwise decide.

### Removed

- **The console can no longer run commands** (`tracker/webui/server.py`,
  `tracker/webui/static/app.js`, `tracker/webui/static/ansi.js`).

  Gone: the `/dev` face and its Pipeline, Commands and Runs views; `GET
  /api/commands`, `/api/runs`, `/api/run/<id>`, `/api/run/<id>/stream`,
  `/api/discover`; `POST /api/run`, `/api/workflow`, `/api/run/cancel`; the
  `window.DC_MODE` split and roughly 900 lines of front end — the command form, the
  workflow panel, the command line, the run history and the terminal output pane.
  They 404 now rather than 403: there is no runner to refuse.

  **Not because it was broken.** It worked, and three doors stood in front of it: a
  typed-name confirmation for anything that spends money or deletes rows, a
  single-writer check because SQLite takes one, and the rule that the console
  assembles an argument list and never a command string. All three were correct.

  It went because **nobody used it**. The database is changed from the CLI, by one
  person, on the host — which `CLAUDE.md` has said all along — so the runner was
  three security properties that had to stay correct forever, behind a public URL,
  in exchange for a feature with no users. Deleting it removes the class of
  question rather than answering it again each time the page changes.

  `tracker tui` is where the buttons live now, and it is the better home: it runs
  in a terminal on the machine that owns the database, so "who may start this?" is
  answered by ssh rather than by a cookie. **`webui/catalog.py`, `runner.py`,
  `runs.py` and `workflows.py` survive** the deletion of their only HTTP caller,
  because the TUI imports them — the introspection and the process handling were
  never the problem, and `tests/test_tui.py` already covered the same mechanism the
  deleted route tests did.

  `help` moved from the `/dev` set into the reading console. It explains tiers,
  tracks and confidence — what a reader needs in order not to misread the data —
  and was only filed under the machinery because that is where the tab sat.

  The capex page keeps its suspected-duplicate panel and loses its merge button.
  The information is a read a reader wants — a duplicate is how a number gets
  counted twice, and the totals are right above it — while folding rows is a
  decision with no undo that belongs at a terminal.

- **`TRACKER_CONSOLE_PASSWORD`** (`tracker/config.py`, `.env.example`). Replaced by
  the `account` table. The two reasons it was an environment variable rather than a
  flag still hold and are why `tracker users add` prompts: a plain `str` leaks into
  any traceback Typer or Rich prints, and a `--password` flag lands in shell
  history and in `ps` for every user on the machine.

### Changed

- **`serve --no-run` and `cloudflare --no-run` are accepted and do nothing**, for
  one release. `deploy/serve.sh` on the host is outside the repo (`deploy/` is
  gitignored, per `CLAUDE.md` §5) so the poller does not update it, and it will keep
  passing `--no-run` after this lands. A flag that errored would turn the next
  launchd restart into an argument-parsing failure with nothing serving the console
  — an outage caused by a *removal*, which is the worst kind to debug. It warns and
  carries on, and is hidden from `--help` so nothing invites its use.

### Added

- **Duplicate detection reaches the cases it was structurally blind to, and every
  pair now carries all the evidence there is for it** (`tracker/dedup.py`,
  `tracker/capex.py`, `tracker/dupresolve.py`,
  `tracker/prompts/duplicates-resolve-v2.txt`, `scripts/measure_duplicates.py`).

  `tracker duplicates resolve` could settle at most 37% of its own backlog before
  the model was asked anything, and that ceiling was arithmetic rather than
  caution. Measured on the live database: of 49 pairs it would ask about, 31 carried
  `identity` — a city-versus-county key match — and nothing else, which
  `merge_blocked` refuses categorically. The evidence was not absent. `capex`'s
  second pass recorded the key match and discarded every other signal it had
  computed, so 12 of those pairs were silently sharing a distinctive name token, 8
  a real tranche key, and 6 a byte-identical company *and* name. The second pass
  now records all four signals, and `identity` became a qualifier rather than a
  verdict: `identity+tranche` is a question a model can answer, `identity` alone
  still is not.

  **A third pass, keyed on the tranche rather than on the locality.** Both existing
  passes start from a key — one compares rows filed under one locality, the other
  rows whose dedup keys describe one place at two granularities — so a campus stored
  as a city and as a county whose *names differ* was in neither.
  `is_cross_granularity_match` needs the locality names to agree once "County" is
  dropped, and "Abilene" is not "Shackelford". Stargate, stored as Crusoe's Abilene
  row and Oracle's Shackelford County row, was invisible to the report while both
  rows carried the tranche key `county.shackelford`. Seven pairs arrive this way,
  including Cipher's Stingray facility filed under both `andrews` and
  `andrews county`, DataBank's DFW3 stored as Plano and as Dallas, and the
  IREN/Iris Energy Sweetwater rename.

  **Rarity moved from the row to the pair.** `identifying_block_keys` kept a tranche
  key only when it appeared in exactly one locality — correct for two rows in one
  town, and exactly backwards for a cross-granularity duplicate, which is two
  localities by construction. `shared_identity_keys` asks instead whether the key
  appears in any locality *but these two*, which is the same rule for a
  same-locality pair and the opposite one for the case that matters.

  **`exact` is a new and strongest evidence class** — same company, same name, once
  normalized. Six suspected pairs held both and every one was reported under the
  weakest class the report has, because `distinctive_name_tokens` strips generic
  industry words and the locality: "Stafford Technology Campus" in Stafford reduces
  to nothing, so two identical names produced no name evidence at all. The
  strictness that stopped `centers` pairing Aligned with NTT is what hid them.

  **Three vocabulary rules, because widening recall reopens the hole precision
  closed.** A *facility number* (`iad-3`, `va-2`, `ord-1`) names a market and a
  sequence number: identity inside one market, an airport across two. A *market
  sequence* is the same thing spelled with the town (`hillsboro-1`, `chicago-2`,
  `sweetwater-1`) — reported, because it is the only thing connecting some real
  duplicates, and never merged on, because `hillsboro-1` is held by Flexential's
  Hillsboro site and NTT's. A key made only of *type words, digits and locality
  words* names a kind of tranche and not one: `capacity-1`, `permanent.plant.power`,
  and `expansion.houston`, which is the key that paired Element Critical's Houston
  One with Switch's Houston campus. `blocks.generic` and `TYPE_WORDS` are untouched
  — those govern whether megawatts are summed, and this governs pairing only.

  **A rail for the failure the widening would have introduced.** Applied Digital's
  `Polaris Forge 1` in Ellendale and `Polaris Forge 2` in Harwood are two real
  campuses that both hold `forge-2.polaris`, because one article listed the pair.
  Every signal agrees and one digit does not. `sibling_ordinals` refuses an
  unattended merge when two names reduce to the same stem and carry differing
  ordinals — which also catches Aligned's `SLC02` against `SLC-04`, and deliberately
  does not catch "Sweetwater Data Center" against "IREN Sweetwater 1", where only
  one name carries a number.

  `party` also had to be narrowed to stay worth trusting: `company_parts(a) &
  company_parts(b)` is non-empty for two rows of one company, and the second pass
  buckets *by* company, so recording it there would have handed every pair hard
  evidence and offered to fold NTT's Itasca campus into NTT's Chicago one, 31.7 km
  away. It now means what it was built for — one company string naming the *other's*
  operator, "OpenAI/Oracle" against "Oracle". No pair on the live database rested on
  it alone, so the narrowing costs nothing today.

  Measured before and after with the new `scripts/measure_duplicates.py`, which
  answers "what would `--merge` do" for free by putting a hypothetical confident
  verdict through the real rails — `resolve --dry-run` cannot, because it pays for
  every call it makes:

  | | before | after |
  | --- | --- | --- |
  | groups / pairs | 40 / 55 | 41 / 57 |
  | `identity` and nothing else | 31 | 20 |
  | pairs the rails would merge at 0.95 | 18 | 15 |
  | pairs found that the report could not previously see | — | 7 |
  | pairs dropped as vocabulary | — | 5 |

  Fewer merges and better ones: the 15 include Stargate, Cipher's Stingray, the
  IREN rename and all six exact-name pairs, while the false pairs that used to sit
  above them — `iad-3`, `va-2`, `expansion.houston` — are gone or demoted to
  report-only. The five dropped are every one of them a documented false pair:
  `expansion.houston` (Element Critical against Switch), `expansion.hillsboro`,
  `ashburn` and `douglasville` (locality names), and `permanent.plant.power`.

  **`capex` moves, and the direction is not uniform.** The buyer table sets aside
  16,796 MW against 15,202 before, because more rows are correctly held out pending
  a decision. Two positions change materially and both are corrections: Oracle
  loses 1,400 MW and $25B because its Shackelford County row is now grouped with
  Crusoe's Abilene one, and IREN drops from two projects to none — both of the rows
  that carried its own name are duplicates of a row leased to Microsoft, so that
  capacity was being counted under a second buyer.

- **Every command that spends LLM calls can choose its model: `--llm-provider
  deepseek|ollama`** (`tracker/llm.py`, `tracker/config.py`, `cli.py`,
  `.env.example`, `docs/ingesting.md`).

  A second implementation of the one `Extractor` protocol: `OllamaExtractor`
  speaks to a local Ollama server through its native `/api/chat`. Native rather
  than the OpenAI-compatible shim for two properties the shim does not expose,
  and one of them is load-bearing: `options.num_ctx` rides on every request
  (default 32768) because Ollama's own default context is a few thousand tokens
  and input beyond it is TRUNCATED SILENTLY — an extraction would quietly read
  half the article and the evidence gate would reject quotes the model never
  saw. The other is `think` as a first-class flag, so the no-think tier stays a
  request parameter on both providers.

  Everything provider-shaped stays in one file. The JSON contract, the thinking
  filter (Ollama's `thinking` field folds into the same `<think>` tags DeepSeek's
  `reasoning_content` does), the tier policy (extraction and `infer` reason, the
  drawer does not), and the retry discipline — including retrying the 503 a local
  server answers while 18 GB of weights map in — are identical whichever class
  answers. `reasoning_extractor` no longer leaks `deepseek-v4-pro` into a local
  call, where it would 404 on every `infer`.

  Fifteen commands take the flag, and that is measured rather than promised: a
  test walks `catalog.LLM_COMMANDS` and fails on any LLM command without it, so
  the sixteenth cannot arrive bare. The flag is `--llm-provider`, not `--llm`,
  because `--llm/--no-llm` already means "let a model decide at all" on the three
  resolve commands. The console and TUI render it as a dropdown, and completion
  offers both values.

  DeepSeek stays the default deliberately: stored values carry no record of which
  model produced them, so a model switch must be a decision — per run with the
  flag, per machine with `TRACKER_LLM_PROVIDER=ollama` — and never an accident.
  Failures grew a common parent for the same reason the protocol exists:
  `LLMUnavailable` covers "no key" and "no server / no model" both, every call
  site catches it, and each message says its own fix.

- **`tracker tui` — a full-screen terminal interface with every CLI command in
  it** (`tracker/tui/`, `cli.py`, `webui/catalog.py`, `docs/tui.md`).

  There were two interfaces and a gap between them. The CLI prints a snapshot and
  forgets it, so comparing two projects is two commands and a scroll and a filter
  worth keeping is a shell history entry. The console keeps state and draws
  properly, but it wants a browser, a password and a tunnel — and the machine that
  owns the database is reached by ssh, where a browser is not what you have.

  Six panes: **overview** (headline numbers, field coverage drawn as bars, open
  obstacles ranked by cited capacity), **projects** (the table filtered live across
  company, name, locality, phase and customer at once, with one project opened
  beside it), **coverage** (rostered operators against the rows we hold, absent
  first, with the unrostered tail), **capex** (end customer × year), **queue**
  (what is waiting, and what could not be read, grouped by host), and **run**.

  **The run pane offers every command the CLI has, structurally rather than by
  maintenance.** It reads `webui.catalog`, the same Typer introspection the
  console's palette uses, so a command added to `cli.py` appears with its real
  flags, types, defaults, choices and help on the next start. A test asserts the
  pane offers exactly the catalog's set, because "the TUI has all the CLI's
  functions" has to be a property of the code and not a promise in a changelog.

  Runs go through `webui.runner` rather than a new subprocess, so the TUI inherits
  its three properties instead of growing a second, laxer copy: no shell anywhere
  (`gaps; rm -rf /` is refused because `rm` is a word no command has), one writer
  at a time, and the command's own name typed back before anything that spends
  tokens or deletes rows. Output keeps its colour — the child gets `FORCE_COLOR`
  and the pane renders the escapes, because red-is-a-rejection is the CLI's own
  signalling and stripping it discards exactly what a reader is scanning for.

  Provenance travels with every value in the detail pane — `derived`,
  `unconfirmed`, `inferred` are marked rather than rendered as plain figures — and
  the five progress tracks are drawn side by side, which is the view that makes the
  model legible: site control and permits can be finished while power is years out.

  **Verifiable with nobody at the terminal.** `tracker tui --check` boots the real
  app against the real database headlessly, fills every pane and exits non-zero if
  any failed; `--screenshot` writes the frame it rendered as an SVG. That is how
  the interface gets confirmed working on a host reached only by ssh, and it fails
  on a renamed column rather than reporting a version string.

  `textual` joins the base dependencies — an interface behind an install flag is
  one nobody on the host has — but is imported lazily all the same, so a checkout
  whose dependency sync has not run yet still imports, and the deployer's refusal
  check still passes. `tui` is in the console's blocked list: a full-screen
  terminal app cannot render into a browser, and starting one there would hold the
  single run slot until the timeout while nothing appeared.

- **A roster of the operators this database is supposed to know about, and a
  command that goes and gets the ones it has none of** (`seed/operators.toml`,
  `tracker/roster.py`, `tracker/prospect.py`, `cli.py`, `webui/catalog.py`).

  Every discovery path here was source-driven — poll the feeds, sweep an archive,
  ask a model to brainstorm projects, read a filing — so all of them answered *what
  has been published* and none could answer *who are we missing*. An operator
  nobody wrote about last month was indistinguishable from one that does not exist.

  Measured on the live database: 300 projects, 102 distinct company spellings, and
  **zero rows for Nebius**, a top-five AI cloud running a Kansas City campus.
  CoreWeave had none under its own name either — it appeared only as a tenant
  inside two Core Scientific projects. `gaps`, `verify` and `stats` were all silent,
  because all three measure the rows that exist.

  `seed/operators.toml` is the expectation written down: 73 operators across
  `hyperscaler`, `ai_lab`, `neocloud` and `landlord`, with the aliases each files
  under. Hand-written rather than generated, because a model asked each run would
  answer differently and nothing would say what it forgot. Not
  `edgar-companies.toml`, because that list is scoped by CIK and structurally
  cannot hold Vantage, STACK, Aligned, Crusoe, Lambda or QTS. A test asserts every
  EDGAR company that owns campuses also appears here, so the two cannot drift.

  `tracker coverage` diffs it against the database — `absent`, `thin`, `covered`
  per operator — and prints the reverse gap too: companies with projects that no
  entry claims, which is how the file grows. It is a read and spends nothing.

  `tracker prospect` chases the gaps. Three lead sources per operator, cheapest
  first: candidates **already in the queue** that name the operator and were never
  read, then sitemap slugs naming it (both free, no key), then four templated
  searches plus one per US campus a model proposes. **Nothing the model says is
  stored** — the same asymmetry the search path rests on, asserted directly by
  `test_a_campus_the_model_invented_never_reaches_the_database`. The run ends by
  re-measuring coverage and printing `Nebius 0 -> 2 row(s)`, because "queued 40
  URLs" says what it spent and only the second number says what it got.

  The queue source is the one worth naming, because it was a starvation bug hiding
  behind a correct decision. The extract phase is depth-first on purpose — a call
  spent on an article about a tracked project buys a second source and lifts
  confidence — so an article about an operator with *no* rows matches no known
  project and sorts last behind a permanent supply of better candidates. A queue can
  therefore hold a Nebius URL indefinitely while the database holds no Nebius row.
  Inside `sync --prospect N` these URLs are moved to the front of the extract
  phase's list, and the run says how many of the batch got there that way.

  Matching folds the spellings one operator files under: both sides normalized
  through `dedup.company_key`, stripped of the words every data center company
  shares, then compared as token subsets — so "Nebius" finds "Nebius Group N.V."
  and "Aligned" finds "Aligned DataCenters" with no alias needed. The subset runs
  one way only, so "Cipher Mining" finds "Cipher Mining Inc." and never a bare
  "Cipher": a wrong fold credits one operator with another's capacity and nothing
  downstream detects it. Loose matches print with a `~`.

- **A `[[company]]` entry in `edgar-companies.toml` may carry its own `forms`**
  (`tracker/ingest/edgar.py`, `seed/edgar-companies.toml`).

  Nebius was on that list from the start and produced no filings at all, because
  every query asked for 10-K, 10-Q and 8-K and Nebius Group N.V. is a Dutch foreign
  private issuer: it files 20-F and 6-K. Not a reduced yield — zero. The override is
  per company rather than a wider shared list, which would have doubled the cost of
  every domestic filer to reach one foreign one. The second of two independent blind
  spots behind the same missing operator: one in what we looked for, one in where we
  looked.

### Fixed

- **Ollama calls no longer transit the machine's proxy** (`tracker/llm.py`).

  Deployed, verified, and the first live `--llm-provider ollama` check answered
  502 — from the proxy, not the model. httpx's default honours not only
  `HTTP_PROXY` but the OS's own proxy configuration (`urllib.request.getproxies()`
  reads macOS SystemConfiguration), so on a host running a system-wide proxy,
  requests to `127.0.0.1:11434` were routed to the proxy at 127.0.0.1:7897, which
  cannot reach the caller's loopback. curl succeeded on the same URL while
  `os.environ` showed no proxy anywhere, which is what made it worth writing
  down. Every Ollama call now sets `trust_env=False`: local inference never has a
  reason to leave the machine. The API provider keeps the default — reaching the
  API may be exactly what the proxy is for.

### Changed

- **The duplicates report leads with what a reader can act on, and names every
  class** (`tracker/capex.py`, `tracker/cli.py`, `tracker/webui/dataset.py`,
  `tracker/webui/static/app.js`).

  `EVIDENCE_ORDER` put `identity` first, on the stated grounds that a structural
  key match outranks a textual resemblance. The consequence was that the report
  opened with 31 of its 49 pairs in the one class no automated path can settle, and
  `_EVIDENCE_STYLE` had no entry for it — so the majority of the report printed a
  bare `?` where its reason belonged. Order is now `exact`, `tranche`, `party`,
  `identity`, `name`, and every class has a label.

  The console's duplicate review draws the same thing: a badge naming the group's
  class and, under it, one line per pair saying what raised it — `#3 + #182: both
  hold tranche county.shackelford`. The class and the sentence are both computed by
  `capex.strongest_evidence` and `DuplicatePair.why`, the functions the CLI's report
  calls, and `EVIDENCE_LABELS` is one table both read: ranking five unequal classes
  in the browser would be a second implementation of a rule with nothing to say when
  the two start to disagree, which is the objection `docs/architecture.md` opens
  with.

- **`duplicates resolve` settles a whole group in one run, and reads more of each
  row** (`tracker/dupresolve.py`, `tracker/prompts/duplicates-resolve-v2.txt`).

  Four rows for one campus produce six pairs, and the first merge deleted a row the
  other five named — so they reported "one of the rows is gone" and the operator
  ran the command again, paying for another set of calls. The live database has
  eight groups of three and two of four, including the Ashburn group where
  RagingWire and NTT hold `va-4`, `va-5` and `va-6` under four names. The run now
  carries its own merges forward and asks later pairs about the surviving row.

  Three changes to what the model is given, each a case where the block was
  actively misleading rather than merely thin. The **distance** now says what it
  measures: coordinates are Census *place centroids*, so two rows in one town read
  0.0 km apart whether they are one building or two miles apart — 245 pairs on the
  live database sit within 3 km of each other for that reason — and printing the
  number bare invited the rule backwards. **Both the city and the county** are
  printed, where before it was `city or county`, so the one question being asked —
  is this town inside that county — had to be recovered from a raw dedup key. And
  the **citation window** takes eight quotes at two per publisher, newest first,
  rather than four sorted longest-first: the sentence that settles identity is
  usually short, and length-first regularly filled the window with one press
  release while the median row holds seven sources.

  `duplicates-resolve-v2` follows. Its rule 6 told the model to answer "unclear"
  whenever granularity was the tie, on the grounds that the tool would refuse the
  merge anyway — true when a cross-granularity pair could carry no other evidence,
  and now an instruction to decline the very pairs this change made answerable. The
  sibling rule and the centroid caveat are stated there too, so the model reasons
  about them rather than being silently overruled.

- **The TUI's run pane completes what you are typing, and gives the screen back to
  the output** (`tracker/tui/completion.py`, `tui/commands.py`, `tui/app.py`,
  `webui/catalog.py`).

  Reported as unhealthy to work with, and correctly. The pane answered "what can
  this command take" with a table of every flag — twenty rows on `sync` — which sat
  in the top half of the screen permanently and left the output a quarter of it.
  Reading output is what the pane is for.

  Typing is now completed: command names while the first words are going in (whole
  names, so `ing` offers `ingest crawl` rather than a group that cannot run), then
  that command's flags with type and default and minus the ones already in the
  line, then a closed vocabulary's values, then **project ids and operator names
  read out of this database** — `enrich ` offers `430  Fermi America — Project
  Matador`, biggest campus first, and `prospect ` offers the rostered operators with
  no rows first, quoted when the name has a space in it. Tab takes the highlighted
  candidate, up and down move, escape dismisses and then leaves the box for the pane
  keys. Nothing is run or stored by completing; the line still goes through
  `parse_command_line` and `build_argv`.

  That is what let the table go. The flags are one wrapped line ending in `+8 more`,
  the full help appears for whichever flag is being typed, and the log takes
  everything left over — 20 rows of output on a 34-row terminal against 8 before. A
  test asserts the output area is taller than the reference block, so a stray
  `height:` cannot hand the screen back.

  Two fixes fell out of building it. `catalog._enum_hints` can now key a vocabulary
  by `(command, flag)` as well as by flag name, because `--kind` means operator
  classes on `coverage` and filer classes on `ingest edgar` — the EDGAR list
  includes `utility`, which `coverage` refuses, so one shared list would have
  offered a value that fails. **The console gets those dropdowns too**, where both
  flags were free-text boxes. And a default of `0` no longer renders as no default
  at all: the membership test `flag.default in (None, False, "")` swallowed it,
  because `0 == False`, which hid the defaults of `--prospect`, `--enrich` and
  `--select` — flags whose entire meaning is "off unless you pass a number".

- **The TUI's output pane behaves like a terminal** (`tracker/tui/commands.py`,
  `webui/runner.py`).

  Reported after a real run: the output looked shredded. Two causes, one visible.

  **Every long line was wrapped twice.** `_child_env` hardcoded `COLUMNS=160` and
  the pane wrapped what came back to its own width, so a line arrived broken at 160
  and was broken again mid-sentence, with the continuation starting at column zero.
  `Runner.start` now takes the width, the TUI passes its log's — less the
  scrollbar, which appears once output overflows and therefore *after* the width is
  measured, which is how a table sized to the full width lost its last column — and
  the pane no longer reflows anything. A line still too wide scrolls sideways. The
  console reached the same conclusion in CSS first; its stylesheet already said
  there is no width but `COLUMNS` at which wrapping works.

  **And scrollback now exists.** `pageup`/`pagedown`, `shift+up`/`shift+down` and
  `shift+home`/`shift+end` scroll the output without leaving the prompt, and the
  output stops chasing its tail the moment you scroll back — a terminal does not
  yank you off what you are reading when the next line lands. `shift+end` returns
  to the tail and re-arms the pinning. `ctrl+l` clears.

  The finished-run summary is written to the log once and the status line now says
  what to do next instead of repeating it; the same sentence in both, a row apart,
  read as a stutter.

  **And `tracker tui --check` was passing before it had checked anything.** The
  panes fill from a worker thread, so the walk found no exceptions and reported
  "every pane filled" — true only because nothing had been filled yet. It waits for
  the read now and fails with what went wrong if the wait runs out. The tests had
  the same hazard and were intermittently red for the same reason.

- **`--dry-run` no longer claims to spend nothing** (`cli.py`, `docs/ingesting.md`).

  It writes nothing, and that is all it ever promised: discover still polls and
  searches, prospect still searches and still asks the model for campus names, and
  refresh and enrich still fetch articles and put them through the extractor —
  "what would this change" cannot be answered without doing the reading. The old
  wording invited a free preview that was never free. The one phase a dry run
  genuinely does not spend on is extract, for a mechanical reason: the candidates
  discover found were rolled back with everything else, so there is nothing queued
  to read.

- **`tracker tui --screenshot` names fonts the reader has** (`tracker/tui/`).

  Rich's SVG template declares `@font-face` rules that fetch Fira Code from a CDN.
  A viewer that will not make that request falls through to its own default
  monospace, so frames exported from a terminal that looked right arrived looking
  wrong — reported exactly that way. The export now rewrites that CSS to a local
  stack (Cascadia, Consolas, SF Mono, Menlo, DejaVu, Noto) and drops the remote
  faces, which also means a screenshot of production is not a file that phones a CDN
  when somebody opens it.

- **`tracker sync` is the master command it was described as, not one job**
  (`cli.py`, `docs/ingesting.md`, `README.md`).

  It ran discover → extract → refresh → list, and the README claimed it ran
  "discover → crawl → enrich", which it never did. It now runs up to seven phases:
  discover, **prospect**, extract, refresh, **enrich**, **settle**, list.

  The two new spending phases are off unless asked for (`--prospect N`,
  `--enrich N`), so a bare `sync` is the same cheap keep-current run it always was,
  and `--full` turns everything on — the "bring the database up to date" button.
  `--full` does not overrule a number given beside it. `settle` is free and
  deterministic: derived values and confidence are both pure functions of a row's
  citations and are only recomputed when something writes to the row, so a run that
  added sources and stopped left them stale.

  Each phase keeps its own cap because they buy different things — breadth,
  currency, coverage, depth — and one shared budget would silently favour whichever
  ran first. Phases are numbered against the plan the run chose (`1/5` by default,
  `1/7` with `--full`); a phase not asked for is absent from the count rather than
  printed as skipped every time, which would only train a reader to ignore the
  labels.

  An ordinary run now ends by pointing at `tracker coverage`, because the gap it
  cannot see for itself is the one where a "0 failed, queue empty" report is
  perfectly true and an operator was never in the database at all.

### Added

- **The console's landing page now answers "what changed on what I care about"**
  (`tracker/feed.py`, `tracker/watchlist.py`, `migrations/0018`, `0019`,
  `webui/server.py`, `static/app.js`, `cli.py`).

  Asked for directly: the projects table already holds the inventory and holds it
  better, so a reader who wants to look something up goes there. What nothing
  answered was the question somebody actually arrives with — *which of the things
  I care about moved since I last looked, and was it good or bad.*

  Every example that came up was already a row type we store, which is the useful
  part of this change: 又搞定了一个电 is `event.interconnection_agreement`,
  那个社团又来干我了 is `risk.community_opposition`, 终于上线了 is `energized` /
  `first_customer`, 宣布追加投资 is `expanded`. So the sign is a lookup over closed
  enums rather than a model's opinion, and it cannot say something different
  tomorrow. **Updates** replaces **Overview** as the landing page and as `/`.

  **The one thing the schema could not answer was "since when".** Every existing
  date answers a different question — `event_date` is when a milestone happened,
  `risk.first_seen` is the date a source puts on an obstacle,
  `source.published_at` is when a publisher published — and none of them is when
  the row entered our database. That distinction is not academic here: stored
  event dates span 1997-01-01 to 2040-01-01, and the 2026-08-11 crawl batch
  inserted milestones dated 2021 and 2022, because a crawl reads one article and
  imports a project's whole back-history. A feed keyed on `event_date` shows the
  same old news every morning; one keyed on "recent" hides a 2023 fact we learned
  an hour ago that changes the picture.

  Migration 0018 adds `created_at` to `event` and `risk`, backfilled from each
  citation's `fetched_at` (2,749 of 2,825 events and 654 of 654 risks carry a
  `source_id`) and left NULL for the rest — nothing anywhere recorded when those
  were entered, and stamping them with the migration's own clock would assert that
  every one was discovered today. Same discipline as 0017's `no_quote` backfill.
  The window filters on that column and every line prints both dates.

  Migration 0019 adds `watch`: a company ("xAI") or one project of one company
  ("xAI | Colossus"), matched by `dedup.company_key` so "Microsoft Corporation"
  and "Microsoft" are one watch. A table rather than a seed file, because
  `seed/required-projects.txt` encodes the PRD's definition of done and belongs in
  a diff, while a watchlist is one reader's current interest and turns over
  monthly. A watch covers what that company is *building* and what others are
  building **for** it — `project.customer` and the per-block customers — because
  in this dataset the interesting news about a hyperscaler is routinely filed
  under a developer's name.

  Materiality is not a vibe. `feed.SCALE` puts the PRD's decisive milestones
  (interconnection agreement, energisation, first customer, a dated slip) above
  the ones that restate an intention, and a signal that reaches the awaited
  milestone on a **blocked** track outranks everything — that is
  `tracks.ProjectStanding.watch_for` arriving, which is the single most
  informative thing this dataset can say. Unconfirmed signals are ranked but held
  in their own tray: a model's answer is not a fact, and a briefing is the last
  place to abandon that.

  **Three faults found by running it, all fixed before shipping.** Pointed at
  the live 300-project copy, the page reported Meta's Hyperion as *energized, the
  blocker moved* — on the strength of two events reading "Partial energisation
  **expected** 2027" and "full Phase 1 **expected** online 2028", on a campus that
  has never drawn power. That is the exact trap `tracks.standing` filters with
  `as_of`, and the feed now makes the same call: a future-dated milestone is
  marked expected, scores lowest, and never counts as an advance. Second, the same
  moment reported by two articles produced two signals — Louisa County's withdrawn
  CUP application, twice, with two dates. `feed.fold` groups by (project, kind,
  label) and carries the rest as `restatements`, which is the discipline
  `export._timeline_json` already applies on the stored side. Third, on the page
  itself: a *resolved* `permitting` risk rendered as its category over its own
  summary sentence — "permitting" above "xAI is operating 27 gas turbines without a
  required air permit" — which reads as a live violation. A risk row stores the
  obstacle's description, so `kind` has to carry the other half of the fact;
  `Signal.headline` composes it ("permitting — cleared") in one place both the page
  and the CLI read.

  A fourth was in the route rather than the reading, and the comment two lines
  below it already said so: `POST /api/watch` called `self._body()` a second time
  after `do_POST` had already read the body, so every watchlist edit from the page
  hung until the browser gave up. `tests/test_webui.py` now asserts the route
  answers at all, which is the test that was missing.

- **A notification bar, higher than the page's** (`feed.notable`,
  `tracker digest --notify`). The page shows everything; a notification interrupts
  a person, and a channel that interrupts too often gets muted, at which point it
  protects nobody. So three gates, all of which must be cleared.

  *Checkable*: an unconfirmed signal never notifies, whatever it says — waking
  somebody over a sentence no quote stood up for is the fastest way to make the
  channel ignorable, and `tracker risks confirm` exists to settle those first.
  *Already happened*: a future-dated milestone is a schedule and schedules page
  nobody. *Material*, which admits exactly five things — the awaited milestone on a
  **blocked** track arriving, a decisive milestone (interconnection agreement,
  energisation, first customer), a dated slip, and an obstacle at `material`
  severity or worse opening or clearing.

  What it excludes is the point: an announcement, a filed permit, earthworks, an
  equipment delivery, land bought, a new row appearing. All on the page, none of
  them a reason to look up from something else.

  `OBSTACLE_OFFSET` is what puts a *material* obstacle level with a decisive
  milestone, and it is deliberate rather than a tuning artifact: an obstacle is
  actionable and a milestone is not. Without it only `blocking` obstacles crossed
  the bar, which would have dropped the case this was asked for — a local group
  opposing a site is recorded `material` far more often than `blocking`.

  Nothing remembers what it already sent, and nothing needs to: the window is on
  `created_at`, so a row falls inside exactly one `--days 1` window and a nightly
  job notifies about it once. Measured on the live copy with two companies watched,
  a one-day window: 29 updates on the page, 11 across the bar — energisations,
  interconnection agreements, community opposition, dated slips.

- **`tracker watch` and `tracker digest`** (`tracker/cli.py`). `watch` lists the
  entities and says how each project matched; `watch add` / `watch rm` edit it.
  `digest` prints the same reading the page renders, marks which lines would have
  notified, and `digest --notify --markdown --days 1` is the nightly note: only
  what crossed the bar, nothing at all when nothing did, and exit 1 on a quiet
  night so a shell can tell silence from a failure. That is what makes "tell me
  promptly" not depend on somebody opening a browser.

  `watch add`/`rm` deliberately do **not** take the single-writer file lock.
  That lock is right for a crawl or a backfill — it is held for hours and guards
  derived data — and wrong for one row of a table no derived value reads. Refusing
  a watchlist edit because tonight's ingest is halfway through would be the worse
  answer, so both the command and the console use a plain read-write engine and
  SQLite's own `busy_timeout`.

- **`--watch-edits/--no-watch-edits`** on `serve` and `cloudflare`
  (`tracker/cli.py`, `webui/server.py`). A third capability flag beside `--run`
  and `--ai`, for the reason there is a second one: these are different risks.
  `POST /api/watch` is the only write a `--no-run` console performs, and its
  blast radius is one row of `watch` — a statement about whose news to show, which
  nothing derives from and no ingest reads. It cannot touch a project, a citation
  or a figure, cannot start a run, cannot spend a token, and it is behind the
  password like every other route. The single-writer *file* lock is deliberately
  not taken: it is held for the hours a crawl runs, and a watchlist edit that
  failed because the nightly ingest was halfway through would be the worse answer.
  SQLite's `busy_timeout` covers a single-row insert, and real contention returns
  503 rather than a silent no-op.

### Fixed

- **The watchlist editor was a bare text box, which is a demand that you already
  know what the database calls things** (`static/app.js`, `app.css`). Reported as
  unusable, and correctly: nothing prompted you with the 300 projects, 90-odd
  operators and named tenants sitting in the payload the page was already holding,
  and this repo had a searchable picker for exactly that problem since the command
  palette replaced its 224-option dropdowns.

  It is now a picker over three kinds of candidate, because a watch has three
  shapes: an **operator** covers everything it builds, a **tenant** covers what
  others build for it, and a **project** narrows to one campus. The row says which,
  since "xAI" and "xAI | Colossus" are different subscriptions and the text alone
  does not show the difference. Ranking puts an exact hit above a prefix above a
  substring, and an operator above the twelve projects it contains — the operator
  is the subscription that covers all twelve. Arrow keys move, Enter takes, Escape
  closes, the highlight scrolls itself into view, and the resting state offers the
  largest operators and tenants so an empty box teaches what is in there.

  **Already-watched rows are computed from the server's own answer**, never
  re-derived: each entry ships the `project_ids` it resolved to, so a candidate is
  "watching" when its projects are already covered. Reimplementing
  `dedup.company_key` — legal-suffix stripping, the alias table — in JavaScript
  would have drifted from the Python that actually decides. Those rows sort last
  and the arrows skip them, because a highlighted row that Enter cannot take reads
  as a broken picker.

  Text matching nothing is still a legitimate watch — set one before the project is
  tracked and it starts reporting when the project appears — so Enter takes it
  verbatim and the empty state says so rather than refusing.

- **The editor did not exist until the digest arrived, and vanished on every
  window change** (`static/app.js`). It read `allow_watch` off `/api/updates`, so
  the input mounted only when that request landed, and `setPayload(null)` on each
  refetch unmounted it again — taking whatever was half-typed with it. It now reads
  `allow_watch` and its candidates from `/api/dataset`, which the shell holds at
  first paint, and a refetch dims the previous answer instead of blanking it.
  Errors land under the box that caused them rather than in a page-level banner.

- **The chips did nothing.** "xAI · 3 updates, 1 bad" invites a click, so clicking
  one now narrows the list to that watch. The tallies carry titles and accessible
  labels: a bare ▼ beside a number is a glyph, not a fact.

### Added

- **`tracker duplicates resolve` — a model settles the suspected duplicates, and
  the rails decide what it is allowed to do** (`tracker/dupresolve.py`,
  `prompts/duplicates-resolve-v1.txt`, `cli.py`, `webui/catalog.py`).

  `tracker duplicates` proposed and never disposed, which `merge.py` argues for
  directly: "a wrong merge destroys two projects and leaves no trace, while a wrong
  split is visible and recoverable." The cost of that caution was a report nobody
  answered — 30 pairs on the live database, 29 of them across genuinely different
  company names, so no alias table will ever fix them, each waiting on a person to
  open two rows and read their citations. This is that first pass.

  One call per pair. Three answers, trusted unequally because their consequences
  are not equally reversible:

  * **different** parks the pair with `decided_by = "model (0.87)"`. That string is
    not new — migration 0016 shipped it as the example value, with the comment "a
    model may park a pair; a reader must be able to tell that one did" — and
    `duplicates unpark` undoes it. It is also the half that pays: `capex.rollup`
    holds one row of every suspected group out of the buyer table, so a false pair
    keeps a real campus's capacity out of a published number until somebody rules
    it out.
  * **same** merges, but only with `--merge`, and only past every rail.
  * **unclear** leaves the pair in the report, which the prompt is explicit is a
    real answer rather than a failure.

  **`merge_blocked` is the safety argument and it refuses more than it allows.** No
  merge below 0.9 — far above the 0.6 a park needs, because unparking undoes a park
  and nothing undoes a merge. No merge on a pair whose only evidence is a shared
  name word, per `capex`'s own line that "a shared name word is a word". No merge on
  a pair whose only evidence is a cross-granularity key match, because that is
  `dedup.py`'s founding invariant — a county row and a city row are never merged
  automatically, since "Racine County, WI" and "Mount Pleasant, WI" may or may not
  be one project and nothing in the row can tell. And no merge between two rows
  whose stored coordinates are more than 25 km apart: geography outranks the model.
  A person at the keyboard (`--ask`) may still merge what the rails refuse a model,
  because the rails guard an *unattended* decision.

  **Which row survives is not the model's choice.** Most citations, then most fields
  filled, then the lower id — deterministic and reviewable, and nearly
  consequence-free because a merge recomputes every field from the combined claims.
  The model's whole output is one word from three, a confidence and a sentence: it
  cannot name a survivor, cannot edit a field, and cannot reach past the rails.

  Registered in the console under both gates it earns — `LLM_COMMANDS` because it
  spends a call per pair, and `DESTRUCTIVE` because with `--merge` it deletes rows —
  so the palette makes the name be typed back before it runs.

  `--dry-run` asks and reports without writing, and it holds a writable connection
  to do it: a `mode=ro` connection cannot even flush the park it is about to roll
  back. Its own test says so, because that is how it was found.

  **Two faults the first live run found.** `MAX_TOKENS` was 700, which read as a
  model that could not answer: five of six pairs came back "unusable reply" on the
  production database, and the one that answered was the pair with the least to
  think about. The budget covers the model's *reasoning*, not its three-field
  answer, so it is 8000 like `audit`'s. And reporting a truncated reply as
  "unusable" pointed at the prompt when the answer was the budget — the two failures
  now report separately, using `logic._ran_out_of_room`, which learned the same
  lesson the same way. The one decision that did land was worth the run: Fairwater
  against Project Nova, parked at 0.95 because the coordinates are 9.9 km apart in
  different municipalities and one of the two is a cancelled site.

- **A watch chip's tally counted restatements the card list folds** (`feed.py`).
  Measured live: the xAI chip read 134 updates over a list that showed 41 cards for
  the same watch, because the per-entity counts were taken before `fold` collapsed
  "one moment reported by three publishers" into one signal — the same double-count
  the card list was fixed for, left behind in the number above it. Counted from the
  folded lists now, and still before `--limit`, because the chip describes the
  window and the limit describes the page.

### Changed

- **`GET /api/landing` is now `GET /api/publishers`, and answers less**
  (`tracker/webui/server.py`, `static/app.js`). It was named for a page it no
  longer serves. Its evidence census and tier sweep — 3.5 seconds between them —
  existed for the trust bands on the old Overview; with that page gone, the route
  keeps only `sources.survey` (0.24s), which is what the Sources view actually
  reads it for. Nothing is lost: the census and the sweep are `tracker stats` and
  `tracker clean`, where they were computed from all along. The trust ramp
  (`--dc-trust-1..5`), the stacked tier bar and two rules that had already lost
  their last consumer (`.dc-magbar`, `.dc-attn`) go with it.

### Fixed

- **A published console refused the one thing it could safely do**
  (`tracker/webui/server.py`, `cli.py`, `static/app.js`).

  The AI overview, the inferred analysis and the capex overview all read
  "Unavailable on a read-only console" on the public console, because they were
  gated on `allow_write` — the same flag that stops the page spawning a CLI
  command. Those are different risks. Those panels *read* a row and spend tokens;
  `_infer`'s own docstring says `tracker infer` has never written its answer
  anywhere, and the briefing is cached by content fingerprint.

  So `--ai/--no-ai` is now its own switch, defaulting to whatever `--run` is —
  nothing changes for anyone who does not ask. A public console can be
  `--no-run --ai`: it answers a question with a model and still cannot mutate the
  database or run `ingest` from a browser.

  Half the old justification had also gone stale. The serve script's comment read
  "production has no reason to write: the development machine is the only writer",
  which inverted when `sync_db.py` replaced `ship_db.py` — the production host is
  the writer now. What stands is the other half: a public URL in front of a
  process that spawns commands is what `tunnel.py` warns about, and that is what
  `--no-run` keeps shut.


- **A corrected figure could not come down: `Policy.MAX` and `Policy.MIN` ignored the
  ratchet** (`tracker/upsert.py`, `logic.py`, `blocks.py`).

  `resolve` takes a `ratchet` flag and threaded it into the PHASE branch only, so MAX
  folded the stored value in as one of its own candidates and MIN did the same with
  the stored date. Both write paths pass `ratchet=False` — they re-derive from the
  complete claim set, so the value the row happens to be carrying gets no vote — and
  for two of the three scanning policies that argument did nothing at all.

  The visible cost: **superseding a claim demoted the citation and left the number
  alone.** `conflicts.apply_outcome` marks the losing claim and re-derives, on purpose,
  so that a value is never a thing somebody typed — and on `mw_built` the re-derivation
  resolved the stored figure straight back. Stargate Abilene (#3) held 1,200 MW against
  a single well-quoted 200; project #2 held 1,691 MW against citations supporting at
  most 1,000.

  `logic check` could not see it either, and for a sharper reason: it asked
  `resolve_field` with the ratchet on, which makes the comparison tautological. A
  wrong-high `mw_built` resolves to itself, so `stored_disagrees` was never True and
  `logic resolve` — the one repair a machine is allowed to make — had nothing to
  repair. `resolve_field` now takes `ratchet` and `check_collisions` passes `False`,
  while still passing the stored value, because FILL_ONLY consults it as policy rather
  than as a ratchet.

  Turning the ratchet off does not make a field clearable: with no candidate the policy
  can read, the stored value is returned exactly as before. That return is what
  `DERIVED_FIELDS` cites as the reason `blocker` cannot live in the merge loop, and
  only a rival the policy can actually compare may lower a MAX field.

  `blocks.reconcile` carried the other half of the same bug. It promises to fill a null
  and never overwrite a value, and the Hyperion fix made that true of `mw_planned` —
  while the next clause still raised `mw_built` to the tranche sum, three lines later,
  undoing the merge that had just lowered it. `mw_built` is now disclosed exactly as
  `mw_planned` is: a sum above the cited figure is a question about double-counting,
  not an answer. `phase` still climbs, because a ladder cannot double-count.

- **A claim a decision ruled against came back as a last resort** (`tracker/upsert.py`).

  `resolve` discards 待确认 claims only when a *confirmed* rival exists, because an
  unquoted value still beats nothing. `superseded` is a different question — it records
  that a human or the conflict solver ruled a figure out — and it was going through the
  same conditional filter. So superseding the only claim for a field handed the ruled-out
  figure straight back, and "no source we trust states this" resolved to the source
  nobody trusted. That is why `audit`'s clear-the-capacity answers were no-ops on
  single-source rows. Such claims now leave the merge outright, while staying visible
  and attributed in `export` and the console — which is the whole reason for superseding
  a claim rather than deleting it.

- **Every `tracker audit` repair was silently reverted by the next recompute**
  (`tracker/audit.py`, `blocks.py`, `prompts/audit-resolve-v1.txt`).

  Each action assigned a project scalar, and a scalar is a cache: `recompute_from_sources`
  re-derives it from the claim set on the next ingest, merge or `backfill derive`. The
  repair vanished and the finding came back, which is how the same 11,250 MW colocation
  expansion survived every run of the command. `audit`'s own remedy text for
  `investment_below_build_cost` had named the right mechanism all along — "mark the old
  claim `superseded` rather than editing the field" — and that finding was the one with
  no action at all.

  Every action now rules a *claim* out and lets the merge engine re-derive the field.
  `investment_below_build_cost` gained the two it describes. `same_figure_two_units`
  rules against claims a factor of ~100+ above the lowest, derived from the check's own
  `UNIT_RATIOS` so an action cannot rule against something the check would not have
  called a unit misread.

  `_divide_by_1000` is gone. Dividing the stored figure by 1000 was epistemically sound
  — a unit misread means the source said 36,000 and meant kilowatts — and durably
  impossible: the claim still says 36,000, so the corrected 36 has nowhere to live, and
  inventing a citation for it is the one thing the evidence model refuses. The claim is
  ruled out instead and the field empties, which is answerable: `gaps` reports a null as
  MISSING and routes the row back into enrichment, whereas a hand-divided figure looks
  cited and is not.

  Block repairs get the tier one level down, `blocks.mark_mw_unconfirmed`, because `mw`
  is not a `WRITABLE_FIELD` and `supersede` would drop it silently. The tranche's figure
  stays visible and stops being counted — which matters more than it looked, since
  `rollup` does not apply `account`'s out-of-scale filter and still feeds `reconcile`.
  And because `source.blocks` has no `DECIDED_REASONS` carry, `settled_codes` learned to
  check those decisions against the blocks: a block message names no Project column, so
  `_EDIT` never matched it and the code stayed settled forever — the same muzzling that
  function exists to prevent, in the one place it still happened.

  Also fixed: four `TypeError`s from `{was:g}` on a column an earlier action on the same
  project had already emptied, reachable because `cli` builds the whole pending list
  before applying anything. And `audit check` now ignores ruled-out claims, without
  which the finding count could never fall.

- **An overclaimed `phase` was stored as a confirmed fact whenever the model labelled
  its quote `"phase"`** (`tracker/ingest/crawl.py`, `normalize.py`).

  `_PHASE_EVIDENCE` exists because `phase` is a judgement, not a value copied out of the
  text — an article says "broke ground", never `phase: construction`. It was also
  unreachable half the time: `_SUMMARY_FIELDS` still listed `phase`, and that carve-out
  `continue`s before `_stated_in` runs, so any verified sentence filed under the label
  `"phase"` confirmed whatever phase came with it. Reproduced: `phase="operational"`
  beside the real sentence "Acme Corp announced plans to build…" was stored confirmed.
  `phase` is the worst field for that, because `_resolve_ladder` merges by taking the
  furthest-along value, so an overclaimed `operational` outranks every later correction.

  `phase` is out of `_SUMMARY_FIELDS`, which now holds only `notes`, on the same grounds
  `blocker` left it: a wording table is a strictly stronger form of the same carve-out,
  because the quote must be real *and* must contain wording for the label it is filed
  under. The language check moved with it, into `_stated_in`'s phase branch, and got
  stronger on the way — it used to see only the quote the model *labelled*, and now sees
  whichever quote is being tested, so a Chinese paragraph with one English lifecycle
  word left in it is refused too. A refusal reads `quote_off_target`, not `no_quote`:
  the article does have a sentence about the phase, and it says something else.

  The three fixture payloads come out byte-identical, phase included — their quote is
  "Microsoft has begun construction on Fairwater", which is what evidence for a phase is
  supposed to look like.

- **A cancelled project normalized to `construction`** (`tracker/normalize.py`,
  `vocab.py`).

  `norm_phase`'s substring fallback scanned the longest synonym first, and length is
  uncorrelated with meaning: every terminal synonym is short, so "construction paused",
  "construction halted", "cancelled after construction began", "permitting withdrawn"
  and "operational but suspended" all resolved to a *progression* phase. A stopped
  project recorded as advancing — and `capex` excludes `PHASE_TERMINAL` from the totals,
  so it kept counting one. `_resolve_ladder` and `blocks.furthest_status` both already
  read "a terminal state always wins"; this was the one place that did not.

  Terminal synonyms are scanned first now, longest-first within each group, and the whole
  order is derived from `_PHASE_SYNONYMS` and compiled once rather than written down
  twice. Reordering alone would have been a new bug — `dead` is inside "deadline" and
  `hold` inside "household" — so matching is on word boundaries, which also retires four
  misreads that were already live: "installed" read as `paused` via `stalled`, "delivery"
  as `operational` via `live`, "decommissioned" as `operational` via `commissioned`, and
  "inactive" as `permitting` via `active`. The boundary strands no synonym, asserted by a
  test over the whole table; the plural and nominalized forms a source actually uses as a
  status ("cancellation", "plans", "approvals", "operations", "announcement") are keys of
  their own, because losing "cancellation" would lose a terminal state.

  Nothing in the ISO path changes: `pjm` consults `iso_map.status_map` first and every
  real PJM/MISO status is an exact entry there. The comment defending longest-first had
  no test behind it either — "zoning approval", the table's only composite case, maps to
  `permitting` whichever key wins. "pre-construction work begins in May" now pins it.

- **A rejected duplicate pair was re-proposed on every crawl** (`tracker/upsert.py`).

  Only the *reports* consulted `not_duplicate`. `_find_duplicate_candidate` did not, so
  after an operator parked a pair the next ingest of either row rewrote the derived
  "possible duplicate of project #N" note and re-capped `confidence` at 1 — the ingest
  path quietly overruling a recorded decision, for as long as the row kept being read.

### Added

- **`tracker sources policy` — the measurement finally acts** (`tracker/policy.py`,
  `seed/sources.toml`, `tracker/cli.py`, `crawl.py`, `enrich.py`).

  `tracker sources` has ranked publishers by what they actually decide for a
  while, and ended every run by saying so: *"Nothing here changes a weight. This is
  the evidence for doing so."* `tracker feeds` said the same about retirement, and
  told the operator to go and comment a line out of a TOML file. Two commands that
  reached a verdict and handed it to a text editor.

  This writes `seed/sources.toml`, and `sync`, `enrich` and `ingest crawl` obey it:
  `priority` domains are offered first when a run is working to a budget, `ignore`
  domains are not queued or fetched again. On the live database it proposes **16
  priority and 1 ignore** out of 654 publishers.

  **It changes what gets read, never what a stored citation is worth.** Weight
  stays per `source_type` and hand-edited; applying the policy to 300 projects left
  every field byte-identical, and a test snapshots whole rows to keep it that way.

  Three things worth recording about the rules:

  * **The obvious threshold was useless.** `ignore` on "decides nothing" yields
    nine publishers at a sensible floor, one at a strict one. `priority` on
    `LOW_YIELD` promoted **75 of the 94** judgeable publishers — a priority list
    containing nearly everything is not an ordering. Priority is now measured
    against the fleet's own decisions-per-citation, which is 0.58 today, promotes
    16, and self-adjusts as the corpus grows.
  * **`ignore` fires on zero, never on thin.** `funnel.LOW_YIELD` is documented
    there as reported and never proposed, and that discipline carries down: thin is
    a prompt to look, zero is a proposal.
  * **The refusals carry more than the proposals.** A publisher we mostly cannot
    *fetch* looks identical to a worthless one from the citation count alone, so
    "cannot read" is checked before anything can propose ignoring it. Also refused:
    a domain still configured as a feed (retire it there, or discovery keeps
    polling and discarding) and an operator's own newsroom.

  The one resolver delegates to `confidence.registrable_domain`, so what the
  ranking prints is directly pasteable into the file — there are five different
  URL→host normalisations in this codebase and a policy keyed on the wrong one
  would silently never fire. Matching is on label boundaries, with a regression
  test for the `x.com`-blocks-`equinix.com` bug `search.py` already records.

- **A sources page, and the article behind any citation** (`app.js`, `app.css`,
  `server.py`, `export.py`).

  1,928 article URLs across 683 publishers were in the database with no screen that
  listed them — the only way to see a source was to open one project's drawer and
  scroll. `fig. 06 — sources` lists every publisher ordered by what it decided,
  every article under it, and opens one in a large modal.

  Built almost entirely from data already being computed: the per-publisher record
  rides on `/api/landing`, which has shipped a `sources` block since `tracker
  sources` existed and **nothing ever rendered it**; the citations come from
  `projects[].sources[]`. The modal is the Meridian `Dialog`, vendored with the
  bundle and unused until now — Escape handling and body-scroll locking already in
  it. No new API route.

  `sources[].published_at` is now exposed (schema tag `tracker/6`). It was on the
  row and reachable only inside `claims_by_field`, so a page listing citations
  could show the crawl date and nothing else — the exact confusion `backfill dates`
  exists to remove.

- **`tracker enrich` settles what it just put into dispute** (`tracker/ingest/enrich.py`,
  `tracker/cli.py`, `.env.example`). Harvesting sources is what *creates*
  contested fields, and until now a run ended by handing that disagreement to a
  sort — quote-backed first, then source weight, then date — which cannot tell a
  superseded figure from a rival one. A new last stage sends every still-contested
  field (not only what the run itself added) through `conflicts.disputes`/`solve`,
  the same machinery `tracker logic conflicts` exposes standalone, and applies a
  resolution the same way. `--skip-settle` turns it off; `--dry-run` covers it like
  everything else; a missing reasoning-model API key degrades to a skipped stage
  rather than losing the articles the run already paid to read. Settlements and
  refusals both print — a refusal is the answer on a field two publishers
  genuinely disagree about, not silence.

  `deepseek_reasoning_model` defaults to `deepseek-v4-pro` (was
  `deepseek-v4-flash`) as part of this change: `infer` and this new settle step
  are both one call per project or per contested field, hundreds rather than the
  thousands extraction pays for, so the heavier model is affordable exactly where
  these two calls happen.

- **`tracker backfill derive` — the free repair pass** (`tracker/derive.py`).

  Every value on a project is a function of its citations, and the function is
  only *applied* when something writes to the row. So six months of fixes to the
  merge policy, the evidence gate and the block rollup never reached a stored
  project: `enrich`, the command actually reached for, only ever *adds* a source.
  `tracker init` recomputes confidence, accelerators and blocks and stops there.

  No LLM, no network, no migration — it re-reads `source.claims`, already on disk,
  and rewrites what they imply. On the live database it moves **322 values across
  213 of 300 projects**: 205 note blocks, **81 blockers**, 16 phases, 9 capacities,
  5 cities and 5 accelerator counts.

  The 81 blockers are the finding. `blocker` is derived from the risk rows and
  nothing re-derived it after a risk was resolved, so projects carried an obstacle
  that had been cleared — and several rows whose obstacles are *all* resolved were
  still showing one. Those go to empty, which the old free-text column could never
  do and the derived one had never actually been asked to. 76 of 300 rows now name
  a blocker.

  It also found a real ordering bug on its first run — see *Fixed* below. That is
  the command working: a repair pass whose second run is not a no-op is telling you
  the derivation is wrong, and it told us within a minute of existing.

  Running it twice changes nothing, and that is the test. `--dry-run` is a
  transaction that is never committed rather than a second code path, so the
  preview cannot be a preview of something else.

- **`tracker logic conflicts` — one model, every source, one contested field**
  (`tracker/conflicts.py`, `prompts/resolve-v1.txt`, `resolve-check-v1.txt`).

  Every other value here was extracted from one article *in isolation*, and the
  disagreements between articles are then settled by a sort. That cannot tell a
  superseded figure from a rival one: Hyperion held Meta's 2024 $10B over its 2026
  $50B because both come from weight-3 sources, both are quote-backed, and crawl
  order decided it.

  This is the one path where a model compares two contradicting sentences. It sees
  every quote-backed claim about one field at once — value, verbatim quote,
  publisher, and *when they published*. 492 fields on the live database qualify.

  Four rules it is built around, three of them departures from the obvious design.
  **It cannot type a value**: the options are figures publishers actually printed,
  already stored with their quotes, so a fabricated sentence has nowhere to enter.
  **Refusing is a first-class answer** — the flowchart this came from has no box
  for it, and two credible publishers with nothing to separate them is not a coin
  toss. **Two calls per field, hard**: one to decide, one adversarial call to knock
  it down, because a "go round again" arrow with no limit is unbounded spend. And
  **`--apply` never assigns the field** — it marks the losing claims `superseded`
  on their own citations and re-derives, so the row still equals what its citations
  imply and the 2024 article still says what it said.

  Identity fields are excluded outright: "Hyperion" against "Richland Parish Data
  Center" is two names for one campus, `FILL_ONLY` says churn there is worse than
  staleness, and ruling against a claim would not even move the value. That is 174
  of 666 candidates removed before a call is made.

- **`upsert.blocker_rationale` — why *this* obstacle out of twenty-seven.**

  `blocker` is one sentence chosen from every open risk a project carries, and the
  console showed the winner without ever saying the others were considered. It
  shares `choose_blocker` with the write path, so the explanation cannot name a
  different risk than the column holds — and when several ranked equally and the
  tie fell to the lowest row id, it says the choice was **arbitrary**, which is the
  case a confident-sounding sentence would hide. Shown in the drawer under
  `blocker` and printed by `tracker show`.

- **`upsert_record(..., existing_only=True)`, and `ingest crawl --existing-only`.**

  The guard a re-read needs, and the exact opposite of `force_new`. Re-reading
  Hyperion's own coverage also yields "Project Everest" — a real name in those
  articles and not a campus this database decided to track. When nothing matches
  and no merge alias routes the record, nothing at all is written, and the refusal
  is counted and logged so a genuinely new campus can be added deliberately.

- **`scripts/measure_reextraction.py`** — whether re-reading everything with
  today's instructions is worth ~2,000 calls, answered by re-reading 40 cached
  articles first. It reports values gained, lost and changed, and separately
  whether more values *lost* their quote than gained one — which is the outcome a
  bare "changes" count would dress up as progress.

- **Two consoles: `/` to read, `/dev` to work.** The eight tabs put the machinery
  on the same footing as the data — three of the eight top-level choices were
  about running the tool rather than about what it found. `/` now carries
  Overview, Projects, Map and Capex and nothing on it changes anything; `/dev`
  carries Pipeline, Commands and Help.

  One bundle serves both. The server sets `window.DC_MODE` on the shell and the
  front end picks its view set — splitting the file would reintroduce the serial
  round-trip per import that `assets.bundle_css` exists to remove, on the page we
  most want fast. The mode is a *display* choice: what `/dev` can do is still
  governed by `allow_write`, so `serve --no-run` renders it inert, and a test
  asserts that asking for `/dev` grants nothing.

- **`GET /api`** — a hand-written index of every route: what it answers, what it
  reads, whether it writes, what it costs. The console is driven from a terminal
  about as often as from a browser and "what can I ask this server?" had no answer
  short of reading `_route_get`. Hand-written for the reason `catalog.GROUPS` is,
  and a test compares it against the routes the handler actually dispatches — it
  caught six undocumented ones the first time it ran.

- **An Overview landing page for the console, and seven views instead of eight**
  (`webui/static/app.js`, `app.css`, `server.py`, `clean.attention`).

  The console landed on the projects table: caption, six-field filter card,
  coverage strip and eighteen columns at equal weight, before a number. Overview
  leads with what the dataset is *for* — 73.9% of 1,454 stored values carry a
  verbatim quote, 3 are presented as established with nothing behind them — then
  the tier mix as one bar, then what is holding rows back with the command that
  answers each. Portfolio second, pipeline reduced to a line.

  Split across two requests on purpose: the portfolio band paints from the dataset
  already in hand while the trust band arrives behind a skeleton from
  `/api/landing`, whose census and tier sweep cost ~2.5s. `/api/dataset` is
  refetched after every run, so putting the scan there would have slowed the whole
  console to answer one view's question.

  Queue, Coverage and Runs became sections of `Pipeline`; `goto` keeps their old
  keys working for the run-finished redirect. `Eyebrow` prose and the projects
  filter card now fold by default at every width.

  `clean.CONDITION_LABELS` and `clean.attention` are new, beside `REMEDIES` and by
  its logic: a scorecard naming a failure an operator cannot act on is a
  complaint, and `vintage_current` names it in this module's vocabulary rather
  than the reader's.

  Two defects found by looking rather than by reasoning. `_overview` silently
  shadowed the existing AI-overview POST handler — Python keeps the last
  definition, so every GET reached a handler expecting a body; the route is
  `/api/landing` now and a test asserts the two cannot collide again. And the
  first trust ramp ran the full lightness range, putting UNSOURCED — the worst
  tier, the one you most need to see — at 1.48:1 against the surface, where it
  read as a gap in the bar rather than a segment. Both ends are compressed now;
  the minimum is 1.81 light, 2.12 dark.

- **Validated chart colour, and a finding about the set we ship.** Meridian's
  `--chart-1..5` fails the palette validator in both modes: light has an adjacent
  pair at ΔE 2.7 under deuteranopia and **7.8 with normal colour vision** — below
  the 15 floor, so full-colour readers cannot separate it either, and secondary
  encoding does not excuse that one — plus two of five under the chroma floor.
  Dark is worse: all five outside the L 0.48–0.67 band. It must not carry adjacent
  categorical series.

  The console's own rule — *colour means trust, never category* — points at the
  answer: the clean tiers are **ordered**, so they take one hue in five steps
  (`--dc-trust-1..5`), and phase and customer are magnitude with a single accent.
  Dark is re-stepped against the dark surface rather than flipped, and the dark
  band is *darker* than light's, which is the opposite of the intuitive answer.

  If a categorical set is ever genuinely needed, these pass all six checks —
  light on `#faf6ef`: `#a86112 #1f5fd8 #b02246 #6d3fc4 #4f6b12`; dark on
  `#1b1410`: `#c47a1e #4a7fe8 #d24a63 #8f6ae0 #7a9426`. Tritan lands at 6.0–6.4,
  inside the floor band, so direct labels and a 2px gap are mandatory with them.

- **`tracker sources`** (`tracker/sources.py`, `tests/test_sources.py`). Which
  publishers actually decide a stored value, derived rather than asserted. The
  meeting asked for a hand-ranked 1–10 weight per host; that is `source_type`'s
  mistake at higher resolution — ten unverifiable numbers per publisher instead of
  three per category — so this counts what each host's claims *did* instead.
  `decided` is a value it won; **`contested` is the subset where a rival asserted
  something different, and is the only column that is evidence of anything.**

  Three things it learned the hard way. Ranking by decided-per-citation put eight
  `.gov` pages cited once apiece above every trade outlet, because an unopposed win
  on a single-source project is free — per-citation orderings now need five
  citations. Identity fields are excluded: `name`/`company`/`city`/`state` are
  `FILL_ONLY`, so a win there records crawl order, and counting them made `inert`
  come out as 0 across all 2,758 rows. And Census geography is not a publisher —
  ranking it produced `www2.census.gov: 184 cited, 184 inert, 0 decided`.

  Attribution asks `upsert.resolve_field`, not `claims[0]`: `mw_built` takes the
  MAX, `first_announced` the MIN and `phase` the furthest rung, so on four of the
  twelve fields the strongest source routinely loses. `scripts/measure_stages123.py`
  now calls the same function, which is what makes the two agree structurally
  instead of coincidentally — its own first version had the `claims[0]` bug and
  over-counted Hyperion at 8 decisive citations where the correct answer is 7.

  The finding: **Data Center Frontier and DataCenterDynamics are weight 2 and
  out-decide almost every weight-3 host.** Nothing here changes a weight.

- **Publication dates read from the page** (`fetch.published_date`,
  `fetch.date_from_url`, `fetch.parse_timestamp`). `published_at` was set for only
  **326 of 2,758 citations (11.8%)** because a feed was the only thing that ever
  supplied one, which left `upsert`'s recency tiebreak falling back to `fetched_at`
  — crawl order — on the rest. Measured properly, **506 stored values** have a
  same-weight rival that disagrees, not the six the old harness could see.

  The reference case is why this is fixed at the source rather than by refining
  authority: Hyperion's $10B and $50B are **the same publisher**
  (`opportunitylouisiana.gov`), both quote-backed, both weight 3, so no
  source-type subdivision can separate them. $50B published 2026-07-13, $10B
  published 2024-12-04, and $10B won on eighteen hours of crawl order.

  Read on the raw HTML **before** `html_to_text` discards it — the ordering is the
  point, since the article cache stores converted text and none of its 585 files
  contains `datePublished`, `article:published_time`, `<time` or a JSON-LD block.
  A ladder: JSON-LD, then `article:published_time`/`og:published_time` in either
  attribute order, then `<time datetime>`, then the URL path. 7 of 10 sampled live
  pages answer, both sides of the reference case resolve, and the free offline URL
  rung alone dates 175 more citations (11.8% → 18.2%; #10 from 6 to 15).

  **Returns `None` rather than guessing**, and refuses a date before 2000 or more
  than two days ahead — a copyright year and a scheduled-content placeholder being
  what that selector actually catches. `record_url` fills and never overwrites,
  which is load-bearing rather than tidy: a cached body reports no date by
  construction, so without the guard a re-extraction pass would erase every date
  the original fetch found. `discover._parse_date` now delegates to
  `fetch.parse_timestamp`, so a feed date and a page date cannot land in the same
  column under two conventions.

- **`tracker backfill dates`** (`tracker/dates.py`, `tests/test_dates.py`). The
  fetch-time capture above fixes every article read from now on and none of the
  2,432 already stored, because those were fetched by a version that discarded the
  metadata before caching. Two rungs, cheapest first: the URL path (free, offline,
  422 rows on the live database) then one GET each, no LLM.

  **It only considers URLs where a date changes something.** Two things read the
  column — `upsert._published_at`, for a URL backing a citation, and
  `crawl.published_dates`, for one still queued. Of 5,552 undated rows only 1,778
  are either; the remaining 3,774 are `no_project`, `fetch_error` and orphans, so
  the default scope cuts the crawl by 68%. `--all` widens it anyway.

  Report-only until `--apply`, and it does **not** go through `crawl.run`: that
  path consults `_split_cached` first, so a backfill routed through it would read
  the local text file, find no metadata, and conclude the publisher states no date.
  `--limit` bounds the fetching rather than the run — capping the free pass the way
  `backfill blocks` caps LLM calls throttled it to 25 of 5,552 rows and reported 5
  dated where the true answer is 422.

- **`tracker feeds`** (`tracker/ingest/probe.py`, `tests/test_probe.py`). Finds
  feeds for publishers the record already says are worth reading. Candidates come
  from `tracker sources` — hosts that decide values and are not in `feeds.toml` —
  rather than from a model, because the answer is in the database.

  Three rungs: `robots.txt` `Sitemap:` lines, well-known paths, then the homepage's
  `<link rel="alternate">`. A `<sitemapindex>` is followed one level, which is what
  makes it work at all: `datacenterfrontier.com/sitemap.xml` is an index, parses to
  zero entries, and reads as "not a feed" — following it rediscovers
  `sitemap/Article.xml`, the entry `feeds.toml` already carries for that reason.

  **Every hit is parsed and run through the real filter**, so the report says how
  many entries would have been *queued*, not that a URL responded. That is what
  keeps it honest: `sec.gov` decides 179 values and its sitemap queues 0%, because
  EDGAR is reached by `ingest edgar` and not by polling. Proposes TOML, never
  writes.

- **`tracker feeds` now proposes retirements too**, so one command does both
  halves of the rolling loop: widen where the record says a publisher is worth
  reading, converge where it says one is not. `--no-probe` skips the network half.

  **The obvious metric is wrong and the codebase already contains the
  counter-example.** "Found the most, used the least" makes three unlike things
  identical: `applied-digital-newsroom` (17 calls, 17 misses, nothing stored),
  `datacenterdynamics` (39 queued, 12 fetch failures, nothing read) and
  `utilitydive-archive` (73 queued, never read once). Only the first is a
  candidate — the second is behind Cloudflare and kept deliberately, with ten
  lines in `seed/feeds.toml` explaining that its headlines still say which
  projects exist, and a queued-versus-cited ratio puts it top of the kill list.
  So the split is on what happened *after* the fetch: `read == 0` is never a
  verdict, and `waste` already divides by `read` rather than by volume.

  A feed is proposed only after ten calls with no citation ever; anything that
  cites at all is reported as low-yield and never proposed. The reason quotes the
  queued count, not the calls already spent — those are sunk, and what retirement
  buys is not making the next ones. It prints the `queue --drop --feed` line and
  **does not edit `feeds.toml`**, which is mostly hand-written justification
  including the comment that prevents exactly this mistake.

  Every run prints the bound: **2,148 of 2,381 wasted calls came from URLs no feed
  found**, so feed retirement addresses about a tenth of the 49%. Without that
  line the report reads as though pruning feeds fixes it.

- **`tracker queue stats`** (`tracker/funnel.py`, `tests/test_funnel.py`) and
  **`/api/discover`**. Stage 1's funnel per feed, derived from `ingest_url` so
  there is no counter to drift. The headline: **2,381 of 4,854 URLs that reached an
  LLM call produced no project — 49%.** Per feed it is actionable rather than
  depressing — `applied-digital-newsroom` is 17 calls and 17 misses, which is the
  banner-card problem `MIN_PROSE_CHARS` documents, and `cologix-newsroom` is 83%.

- **A per-run ledger** (`tracker/runlog.py`, `tests/test_runlog.py`) at
  `data/runs/ingest.jsonl`, following the `clean.jsonl` precedent. `IngestReport`
  gained `llm_calls`/`prompt_tokens`/`completion_tokens`; `ExtractionOutcome` had
  carried the token counts per URL since it was written and nothing ever summed
  them, so "what did that run cost" had no answer short of the provider's
  dashboard. Best-effort by design — a ledger that can fail a paid crawl is worse
  than no ledger.

  **The silent-timeout audit came back negative**, which is worth recording. The
  2026-08-12 review warned about a default timeout swallowing a run unreported; of
  1,081 fetch failures, 625 are HTTP 403 and 198 are 429 — deliberate blocks, which
  is what the `curl_cffi` rung already exists for — plus ~23 TLS failures and a
  handful of 5xx. `tracker queue stats` prints the breakdown, because a timeout, a
  403 and a 404 have three different remedies that one `fetch_error` count cannot
  tell apart.

- **`scripts/measure_stages123.py`** — sizes the stage 1–3 populations
  `measure_extraction.py` cannot see: the 506 crawl-order decisions, 21 generation
  blocks carrying 15,091 MW, 155 block-key forks, the 58% of the event table that
  is one milestone retold, and per-project citation yield. Reads only, spends
  nothing, `--root`/`--db` to point at an install. Findings written up in
  `docs/review-stages-1-3.md`.

- **`tracker clean`** (`tracker/clean.py`, `tests/test_clean.py`). Four tiers —
  SOURCED, SOUND, COMPLETE, SETTLED — composed from the detectors that already
  exist, reimplementing none. T1 is the bar worth chasing: the numbers this tool
  publishes are sums, so an incomplete row makes a total smaller while a row with
  an implausible figure makes it wrong. `--project N` prints the exact command
  that fixes each failure; `--plan` orders the worklist closest-first;
  `--snapshot`/`--since` keep a time series in `data/runs/clean.jsonl`, because
  the one thing a column cannot be is a time series. About seven seconds over the
  whole database, which is why nothing is cached and no ledger table exists.

  Two definitional choices carry it. `NOT_APPLICABLE` counts as complete — a
  12-of-12 bar failed 97% of rows — and 待确认 counts as *backed*, since the gate
  declaring it could not confirm a value is the gate working. A calibration test
  says that if a fully-answered row cannot reach T3, the definition is wrong
  rather than the row.

- **`logic.free_answer`**, mirroring `audit.free_answer`. Three codes qualify —
  obstacles on a finished track, milestones dated in the future, and a county name
  in `city` — because each is a *read of stored data* rather than a judgement
  about which source to trust. On the live database it answered **285 findings
  with no model and no decision**. `logic resolve` also gained the settled-code
  skip `audit resolve` has always had, so it stops re-offering every finding
  every run, and `--again` to see them anyway.

- **`clean-free` and `clean-paid` console workflows**, split because
  `needs_confirmation` is per-workflow and the free routine must not demand a
  money confirmation. Each step's `because` records why the order is what it is —
  duplicates before enrichment, blocks before the rules that gate on them,
  re-extraction only after every prompt and code fix has landed.

- **Every prompt now knows what a data center is** (`tracker/prompts/_industry.txt`,
  prepended to all eleven system messages). Reading Hyperion — 59 sources, the
  most heavily cited row we have — found `mw_planned` at 15,962 MW, `phase` at
  `operational` on a site that has never been powered, and `investment_usd` at
  $10B against 5 GW. The prompts were not missing rules: `extract-v1` already said
  `mw_planned` "is NOT the capacity of a power plant, solar farm or substation
  built to serve it". Nothing told the model how big a campus can be or what a
  gigawatt costs, so no rule had anything to bite on.

  Eight sections of durable background: the campus/building/hall hierarchy; IT
  load versus generating capacity, with the vocabulary that marks which is which;
  the five other things a dollar figure in these articles is usually about; a
  plausibility envelope in orders of magnitude; operator versus utility versus EPC
  versus financier; the lifecycle phrases misread as "running"; how a project is
  restated upward over years; and what actually obstructs one.

  **Background for judgement, never a source of values** — stated in the block and
  again in extraction's first rule, because a model answering *from* it would fill
  capacity with industry averages and clear the evidence gate while doing so.
  Prepended rather than appended so per-prompt rules come last and win.

  Its bytes are folded into every prompt's SHA-1. `extract-v1@4ea77aad` has to
  identify the whole system message, and a shared file that could change
  underneath the hash would reintroduce exactly the failure the stamp exists to
  prevent. `available()` hides the partial; a missing one degrades to the old
  behaviour with a warning rather than taking down every command that loads a
  prompt. New `tests/test_prompts.py` covers all of it.

  Alongside it, the rule each defect actually needed: three things that are never
  capacity blocks (generation and grid assets, the campus itself, a milestone) and
  that phase figures are cumulative rather than additive; a definition of
  `mw_built` that distinguishes energized-and-in-service from built, powered,
  contracted or leased, and null from 0; `operational` requiring the site to be
  running; scheduled and expected future events not being milestones; a bar for
  what counts as an obstacle rather than a topic; and, in the evidence auditor and
  the contradiction checker, the three patterns behind most real failures —
  a lifecycle word meaning something earlier, generation quoted as the site's
  capacity, and a figure the source itself presents as superseded.

### Added

- **`CLAUDE.md`: the operating rules for a two-machine setup.** Which machine you
  are on now decides what you may do, and none of that is inferable from the
  code: code goes out through GitHub and never by hand, data is made on the
  production host and ingest here writes nothing that survives, the database moves only through
  `sync_db.py`, and the five paths outside the project are listed with what
  dictates each. `deploy/README.md` stays the runbook; this is the rules.

- **`/api/health` reports the commit it is serving** (`webui/server.py`).

  A question this pipeline created: code reaches the host by a poller rather than
  by a person, so "is my fix live yet?" has no answer at the keyboard, and a
  restart is not proof the restart picked up the commit you meant. Read from
  `.git` directly rather than by running `git` — it answers a health check, and a
  subprocess per request is a cost with no return.

- **The production host is the writer** (`scripts/sync_db.py`, replacing `ship_db.py`).

  It runs ingest and enrich now, not just the console — always-on is what a job
  measured in hours wants, and it serves the console from the same file it
  writes. SQLite takes one writer and any number of readers, so the console
  stays up through a crawl.

  That inverts the data flow. `ship_db.py` pushed dev to prod because prod held
  no keys and never wrote; once that host started ingesting, pushing meant
  overwriting the only copy of work that existed. `sync_db.py` pulls by default
  and refuses either direction when the destination holds rows the source does
  not — there is no merge here, a whole file replaces a whole file.

  Most of the CLI is unaffected and runs anywhere: `gaps`, `sources`, `overview`,
  `export` and `capex` only read.

- **Production deploys to an always-on host** (`deploy/` **new**, `scripts/ship_db.py`
  **new**).

  Code travels through GitHub; data goes straight over SSH. The development
  machine stays the only writer, so production runs `serve --no-run` and needs no
  API keys at all — the read-only decision is what makes that true.

  The host polls `origin/main` every two minutes rather than receiving a webhook:
  it is behind NAT, and a poller needs no inbound surface. A commit that does not
  import is refused and the checkout rolled back, so a bad push leaves the console
  serving the previous code instead of crash-looping under launchd.

  Published through a **named** Cloudflare tunnel at a stable hostname.
  Named tunnels are free — the same product as a `trycloudflare.com` URL, with a
  hostname that survives a restart, which the quick-tunnel URL does not. The three
  commands that create it stay with the operator: a browser sign-in, a credential
  written to a home directory, a record written to a DNS zone.

  **`ship_db.py` never copies the database file.** It runs `VACUUM INTO`. A
  WAL-mode database copied on its own opens cleanly and is silently out of date —
  measured here at 16.3 MB of main file against a 7.9 MB WAL — and that exact
  mistake has already produced wrong figures in this project once. The snapshot is
  verified before it is sent and renamed into place after, so a reader sees one
  database or the other, never half of one.

  The first version of that verification checked nothing: it counted tables named
  `projects`/`sources`, the real ones are singular, so the comparison ran over an
  empty dict and reported "verified". It now refuses outright when it recognises
  no table.

  The mini pulls with a read-only deploy key, and the running copy of the poller
  sits outside the repository — a deployer that deploys itself can be bricked by
  one bad commit, since the broken version is what runs next.

### Changed

- **Every view has its own URL, and the console loads in a quarter of the time**
  (`server.py`, `app.js`, `export.py`, `gaps.py`, `dataset.py`).

  `/overview`, `/projects`, `/sources`, `/map`, `/capex` (and `/dev/...`) are real
  paths now: linkable, refreshable, and the back button works. The server stamps
  which view the URL asked for so a deep link opens on it rather than painting the
  default and swapping; navigation is `pushState`, because the bundle and the data
  are already in memory and a real navigation would be slower than the tab switch
  it replaced. An unknown path 404s instead of silently serving Overview.

  **Routing was asked for to reduce loading time, and on its own it does nothing
  for that** — same bundle, same payload. Measuring found the actual costs:

  | | before | after |
  |---|---|---|
  | `/api/dataset` | 19.1 MB | **9.9 MB** |
  | `dataset.build` | 4.03s | **1.06s** |
  | over the wire, gzipped | — | 1.5 MB |

  Two independent findings:

  * **`claims_by_field` was 48% of the payload** — 9.2 MB shipped on every load
    for a table that renders one project at a time inside a drawer many visits
    never open. It moves to `GET /api/claims?project=<id>`, fetched when a drawer
    opens and kept for the session. `prov` and `sources` stay: the projects table
    and the sources page read them.
  * **`gaps.provenance` recomputed the whole claim map once per field.** 4,191
    calls across 300 projects, each re-parsing every source's JSON to answer about
    one field — a project with 61 sources and 12 fields parsed 732 blobs instead
    of 61. It now takes the map as an argument and `_provenance_json` computes it
    once. Pure memoisation, verified byte-identical against the old path on 40
    projects, and worth **2.3 of the 4.0 seconds**.

- **The sources modal renders a reader view of the article** (`webui/article.py`
  **new**, `server.py`, `app.js`, `pyproject.toml`).

  The modal framed the publisher's page. Measured across the fifteen most-cited
  publishers, **ten refuse to be framed** — `X-Frame-Options: SAMEORIGIN`/`DENY`
  or a `frame-ancestors` directive — and those ten carry **388 of their 689
  citations**. `datacenterdynamics.com`, the most-cited publisher in the database
  at 150 citations, is one of them. No header of ours overrides a publisher's, so
  the frame could never have been the default.

  Serving the pipeline's stored text instead was worse in a different way: the
  ingest path strips every tag before the model sees it, deliberately, so the
  evidence gate matches quotes against exactly what the extractor read. Correct
  for the gate, unreadable as a page.

  So the modal now does what every read-later tool does with this problem — the
  arc90/Mozilla readability algorithm over the publisher's own HTML, rendered
  under our stylesheet. Measured on eight publishers including all the
  frame-refusing ones:

  | | |
  |---|---|
  | first open | 430–1,400 ms |
  | reopen (cached) | ~10 ms |
  | paragraphs recovered | 12–26 (1,286 on a 10-Q) |
  | navigation, promo images, footers | dropped |

  **The stored quotes are marked in the article**, which is what the reader is
  actually there for: not "here is the page" but "here is the sentence this
  number rests on". Marking survives an inline tag splitting the sentence, and
  forgives only whitespace and case — a quote absent from the page is left
  unmarked rather than fitted to the nearest similar sentence, because a
  highlight asserts "this is the evidence" and the page may have changed.

  **Chrome is thrown away in two passes bracketing readability.** It ranks by
  text density, which finds the article and is indifferent to what shares a
  container with it. So containers *named* as furniture are removed before
  scoring — ad slots, share rails, nav, related lists, comment threads, cookie
  bars, FAQ accordions — and what survives is trimmed at its seams afterwards: a
  "Related:" line embedded in the prose, a press release's contact block, the
  "Sign up at…" a publisher ends every post with, a legal disclaimer. A stop
  heading (`Frequently Asked Questions`, `Related Stories`, `Comments`) ends the
  article, along with everything after it.

  **The kill criterion was fixed before the pass was written: it must not cost a
  single marked quote.** Over fifteen publishers it cuts 101 lines and keeps all
  25 — `prnewswire.com` 44 → 22, `stackinfra.com` 58 → 32, `yahoo.com` 48 → 38.
  It caught two failures that testing by eye would not have:

  * **A `<body class="… no-sidebar">` matched the `sidebar` rule** and the whole
    document was deleted — three publishers returned empty while the pass
    reported success. Structural elements are now exempt from name matching, and
    so is any container holding more than 40% of the page's prose, since chrome
    is never most of what a page says.
  * **Cutting a tail removed its own ancestors.** Walking up and deleting from
    each parent *inclusive* discarded the parent still holding everything kept so
    far. The ancestors are walked now, never removed.

  **Mojibake the publisher baked in is repaired.** `datacenterknowledge.com`
  serves "Cote dâ€™Ivoire" — and our decode is not the one at fault: the
  response says UTF-8, the bytes *are* valid UTF-8, and they encode those three
  characters literally. 36 instances on one page. The repair runs per damaged
  sequence rather than per document, because a single character outside
  Windows-1252 anywhere on the page makes a whole-document round trip raise and
  silently do nothing; and each run is kept only if it round-trips, so real
  accents, genuine curly quotes and CJK are provably untouched.

  Charset detection was wrong in both directions before this and is now decided
  by the bytes: **valid UTF-8 beats any declaration**, because a page that
  declares Latin-1 and serves UTF-8 decodes without error as Latin-1 — every byte
  is a valid character — so believing the page produces mojibake with no
  exception to fall through.

  Images appear when the article has them and not otherwise. Readability drops
  sidebar promos, which is it working: one `datacenterdynamics.com` page carries
  fourteen images, all furniture, and its `og:image` is an advertiser's logo — so
  no lead-image fallback, which would have shown a worse lie than nothing.

  **Three independent guards, because this renders somebody else's markup.** The
  HTML is sanitized to an attribute allowlist, so `on*` handlers and constructs
  nobody anticipated go together; the frame carries `sandbox` with no `allow-`
  tokens, giving the document an opaque origin and no script at all; and the
  response carries its own `default-src 'none'`. The endpoint also refuses any
  URL the database does not already cite — verified over HTTP, an uncited URL and
  `169.254.169.254` both 404 and `file:///` 400s before anything is opened.

  Two things that only showed up in a browser:

  * **`frame-src https:` silently forbade our own frame.** Naming `frame-src` at
    all replaces the fallback chain to `default-src 'self'`, so the same-origin
    reader was blocked with the only evidence in a console message. Now
    `frame-src 'self' https:`, asserted in the CSP test.
  * **A second CSP header would have blocked every image.** Browsers intersect
    multiple policies rather than merging them, so the reader's `img-src https:`
    against the console's `img-src 'self'` would have permitted nothing. `_send`
    takes a `csp` that replaces rather than appends.

- **The console's CSP allows framing, and nothing else moved** (`server.py`). The
  sources page opens a cited article in a modal, and `frame-src` falls back through
  `child-src` to `default-src 'self'` — so without this the browser refuses the
  frame outright. `frame-src https:` is the single directive added; scripts,
  styles, fonts, images and `connect-src` are all still same-origin, and a test
  pins that so the relaxation cannot creep.

  It buys less than it looks like, and the page says so rather than pretending
  otherwise: a publisher sending `X-Frame-Options` or its own `frame-ancestors`
  still refuses, and no header of ours overrides theirs. The browser gives no
  reliable signal for it either — `onload` fires for a refusal too — so the modal
  keeps the citation's quotes, excerpt and the projects resting on it permanently
  beside the frame. If the frame comes up blank, that panel is the answer.

### Fixed

- **Confidence was scored against the blocker a row arrived with, not the one it
  left with** (`tracker/upsert.py`). `blocker` is one of the twelve tracked
  fields, so it counts toward the `populated` figure scoring reads — and it was
  derived *after* the score in both `upsert_record` and `recompute_from_sources`.

  Invisible while nothing re-derived the whole table, and `backfill derive` found
  it on its first live run: a second pass moved #79 from confidence 2 to 1, having
  cleared its resolved blocker on the first. The derivation was not a pure function
  of what is stored, which is the one thing this path cannot be. Confidence is now
  computed last in both functions, with a test that clears a risk and asserts the
  second pass moves nothing.

- **A utility's gas plant was filed as a tranche of the campus it serves**
  (`webui/dataset.py`, `blockcheck.py`, `cli._print_blocks`, `app.js`).

  `blocks.is_generation` has kept generation out of every *sum* since it was
  written, so the figures were right — and the tranche list, the section table and
  the console's "delivering" headline all still counted Entergy's running gas units
  as Hyperion's own capacity. One name, two numbers, on the same screen.

  Split now, never dropped: Entergy building 2,262 MW *for this campus* is one of
  the most important facts about it, so it moves under power rather than out of the
  page. `blockcheck.scan` skips generation before grouping, which stops it
  proposing a merge of two combined-cycle units. And the reconciliation's
  `generation` line — which had no label in the browser and rendered as "Counted
  twice over" — now says what it is, in a colour that does not imply a defect.

  Measured on #10: **3 rows and 5,962 MW** move out of its 18 tranches, and the
  section table the drawer draws goes from 15 rows to 12. The plan called this six
  rows on the strength of the megawatt figure; the other three carry no capacity
  and name no generation word — `Waterford 5` and `Richland Parish Units 1-4` are
  almost certainly the utility's too, and recognising them would need a rule about
  plant *names*, which is a site list wearing a predicate's clothes. They stay
  where they are, and that is written down rather than quietly rounded off.

- **Publication dates filled, and the tiebreak measured and left off**
  (`tracker/config.py`). `tracker backfill dates` was built last change and never
  run. It has now run: the free URL-path rung dated 159 URLs, and the publisher
  rung asked 1,772 pages and got a date out of 1,083 of them. **Citation coverage
  went from 11.8% to 67.6%.**

  **And the measurement said not to flip the tiebreak**, which is the opposite of
  what was expected. 65 values are now visibly settled by crawl order; flipping
  fixes all 65 and gets many of them wrong — of the 40 numeric ones it raises 18
  figures and lowers 22. It fixes the reference case (#10 finally takes Meta's 2026
  $50B over its 2024 $10B) and it also restores #78's customer from "Meta" to the
  older "Facebook", and replaces #389's $86.5M site figure with a $40B programme
  total.

  The failure a date cannot see is a later article about a different *scope*: one
  building of a campus, one phase of a programme, is newer without being a
  restatement of the whole. So the dates stay, the flag stays off, and the thing
  that reads them is `logic conflicts`, which reads the sentences instead of
  ranking them.

- **A publication date could be discovered and never reach the merge that reads
  it** (`tracker/dates.py`). `backfill dates` filled `ingest_url.published_at`, and
  `claims_by_field` breaks its tie on `source.published_at`. `upsert_record`
  bridges the two, but only for a URL it is ingesting — so a date learned *after* a
  citation was written never arrived. 1,600 page requests spent on a column nothing
  consults would have been the most expensive kind of no-op. The date is now copied
  onto the citations that quote the URL, fill-only, in the same run.

- **A repaired figure could be reverted and the check that found it would stay
  quiet** (`tracker/audit.py`). `settled_codes` answered by finding *code*, but
  every action in `ACTIONS` writes a project scalar or a block row — and both are
  caches that `recompute_from_sources` and `recompute_blocks` re-derive from the
  claim set. So an answered question could come undone while remaining marked as
  answered.

  Observed on Hyperion (#10), the most heavily cited row we have: a model cleared
  `mw_planned` 13,620 as uncited on 2026-08-09, `blocks.reconcile` raised it back to
  14,462 from the tranche sum, and `campus_exceeds_worlds_largest` — which fires on
  that value — was skipped from then on. The worst row in the database had switched
  off the check that would have said so, using its own repair.

  A code is now settled only while the edit that settled it survives: the recorded
  value is parsed back out of the note and compared to the row. A **dismissal** is
  different and still settles forever — it records a judgement that the figure is
  right, not an edit that can be reverted. Conservative on anything it cannot
  parse, since re-opening every unparseable note would bury the real reverts.

  Also widened the decision regex from `[a-z_]+` to `[a-z0-9_-]+`. No code contains
  a digit today, which is exactly why this was free now and would have been
  undetectable later: the failure is silent and only in the skip path.

- **`enrich` threw away every article it read** (`tracker/cli.py`). It accepted
  `cache_dir`, forwarded it to `crawl.run`, and was the one caller of ten that never
  passed it. A 36-hour `--all` run read ~3,000 articles and cached none of them; the
  newest file in the cache predated the run by two days. Three later steps read that
  cache rather than the network — `ingest crawl --stale-prompt`, `backfill blocks`,
  and `riskcheck.article_for`, which settles nothing without it — so re-extraction
  and all 443 pending risk confirmations would have had to pay for the network again.

- **`[tracker] conflict …` claimed credibility settled fields that credibility had
  not** (`tracker/upsert.py`, `tracker/logic.py`). The note read "kept
  higher-weighted value" unconditionally, which is false whenever two claims carry
  the same weight — the common case, since `government_doc` and `company_filing` are
  both 3. Hyperion's notes asserted credibility chose $10B over $50B when both
  sources were weight 3 and the tiebreak was crawl order. `Collision.why`'s phrasing
  is now a shared `logic.why_decided`, with `logic.decision()` as the public pair, so
  the row's own notes and the collision report say the same true thing.

- **A merge left the folded rows' decisions behind** (`tracker/merge.py`). Sources,
  milestones, obstacles and the identity all move; the record of what a person or a
  model *decided* did not. Since `audit.settled_codes` reads that prose, folding a
  row silently re-opened every question it had answered — and Hyperion (#10) holds
  two model decisions among its 124 note lines. Operator prose is now carried to the
  survivor and marked `[carried from a merged row]`; generated `[tracker]` and
  `[source:…]` lines are not, because they assert something about the wrong row's
  citations.

### Added

- **`ingest crawl --cached-only`** (`tracker/ingest/crawl.py`, `tracker/cli.py`).
  Reads what is on disk and refuses to fetch the rest, counting them as `not cached`
  in the run summary. `--stale-prompt` picks URLs by prompt vintage and serves from
  the cache "by default", but a miss fell through to a fetch — silently — so a
  re-read became a crawl. On the live database three quarters of the stale URLs have
  no cached text: 113 cached pages would have come with 1,754 paid fetches. Mirrors
  `backfill.run`'s `refetch=False` discipline, including reporting the skip so a run
  that covered a quarter of its worklist does not read as one that covered it.

### Changed

- **Extraction reasons now** (`tracker/llm.py`, `tracker/config.py`,
  `tracker/infer.py`). Reasoning is ON for extraction at `high` effort and for
  `tracker infer` at `max`, OFF for the drawer's briefing — a request flag, not a
  model choice, so all three tiers still run `deepseek-v4-flash`.

  Two effort settings rather than one, because the tiers have opposite cost
  shapes. Extraction runs once per *article*, so effort there multiplies by the
  size of the corpus; `infer` runs once per *project* against a whole row, asking
  for the conclusion the database cannot look up, which makes it the most
  reasoning-shaped question in the tool and the cheapest place to pay for depth. A
  single shared number would have to be wrong for one of them.

  `DeepSeekExtractor` takes `effort: str | None` instead of a `thinking` flag
  beside it, and `thinking` is now a derived property. The two were never
  independent — an effort is meaningless with reasoning off, and reasoning on
  without one is a state the API cannot express — so collapsing them makes the
  invalid combination unconstructable. The effort settings are `Literal` types, so
  a typo in `.env` fails at config load rather than as an HTTP 400 three hundred
  articles into a paid crawl.

  Reading an article for twelve fields is not transcription. It is deciding which
  of three megawatt figures is the data center's rather than the utility's, which
  of four dollar figures is this site's rather than the programme's, and whether
  "since breaking ground" describes a building site or a running one. The
  `_industry.txt` block gives the model what it needs to make those calls;
  thinking is what lets it make them instead of matching the nearest number. It is
  also the path where a wrong value gets *stored*, which makes it the last place
  to economise — and the only accuracy comparison this project has measured runs
  against economising, since the no-think model inverted a track reading and
  invented a utility.

  The briefing keeps the role `M2-her` held on MiniMax, unchanged: it reads values
  already on the page, is labelled as a model's opinion, is never stored and
  cannot move confidence, so nothing it writes reaches the database and speed is
  worth more than depth. What improves is that the same behaviour is now a flag
  rather than a different and less accurate model.

- **`max_completion_tokens` defaults to 32768, up from 4096.** A ceiling, not a
  reservation — raising it costs nothing unless the model generates more. 4096 was
  sized for MiniMax, and reasoning is spent from the same budget as the reply,
  before a character of the answer appears. At 4096 both thinking tiers starve:
  extraction recovers by paying for a second call that tells the model *not* to
  deliberate, which suppresses the reasoning it was just given, and `infer` does
  not recover at all — it logs and returns an empty Analysis, so a starved panel
  is indistinguishable from a quiet one. 32768 rather than 16384 because `infer`
  runs at `max` effort, whose whole point is to deliberate at length and whose
  failure is the silent one; the headroom is free until it is used.
  `infer.analyse` also stops hardcoding 4096 and follows the setting.

- **The model provider is DeepSeek, not MiniMax** (`tracker/llm.py`,
  `tracker/config.py`, `.env.example`). `deepseek-v4-flash` on all three tiers,
  against the single host `https://api.deepseek.com`. Every `TRACKER_MINIMAX_*`
  setting is renamed to `TRACKER_DEEPSEEK_*` and **the old names are no longer
  read at all** — deliberately not aliased, because the two providers issue their
  own keys and silently accepting a stale MiniMax key would turn a config error
  into an HTTP 401 at ingest time. The 401 handler names the migration as the
  likely cause.

  Three wire-level differences, two of them reversals of what this code did:

  * the budget parameter is `max_tokens`, not `max_completion_tokens`;
  * **thinking is a request flag and is actually honoured** —
    `thinking={"type": "enabled"|"disabled"}` with `reasoning_effort`. MiniMax
    accepted `thinking`, `reasoning_effort` and `enable_thinking` and ignored all
    three, which is the only reason the fast tier had to be a *different and less
    accurate model* (`M2-her`, the sole no-think model in that roster). That
    workaround is gone: all three tiers now run one model, and only `tracker
    infer` turns reasoning on. The measured cost of the old default — a briefing
    that wrote "All tracks complete" over a construction track that had reached
    nothing — is gone with it;
  * reasoning may arrive in a `reasoning_content` field rather than inline
    `<think>` tags. Both are handled, and the field is folded back into the tag
    shape so `split_thinking` and the stream filter stay the one reader.

  `MODEL_TOKEN_CAP` is kept but empty — the v4 models take 384K, far above
  anything asked for here — because the hazard it guards (a model answering HTTP
  400 to the ordinary budget, so no reply at all rather than a shorter one) has
  happened once and cost a debugging session.

  **The JSON contract stays in code.** DeepSeek does support
  `response_format={"type": "json_object"}`, unlike MiniMax, but its docs require
  the literal word `json` in the prompt (ours say `JSON`) and warn the endpoint
  "has a probability of returning empty content" in that mode. An empty reply is
  the one failure the parse → repair → validate → retry reader cannot recover
  from, so the flag is available as `TRACKER_DEEPSEEK_JSON_MODE` and off by
  default.

### Added

- **The console has an identity mark** (`tracker/webui/static/index.html`,
  `login.html`, `app.js`). A citation bracket with a bar that starts at the source
  and stops where the evidence stops, so the empty half of the bracket states the
  rule the whole tool is built on: an unpublished figure stays null rather than
  guessed. It replaces the placeholder favicon — a honey square with two white
  bars, which said nothing and, having a background plate, showed as a lit tile in
  dark browser chrome. The mark has no plate for that reason: it inverts with
  whatever the tab strip is.

  168 bytes of inline SVG in three places rather than one file fetched from three,
  because a request costs more than the drawing and because filling from
  `currentColor` is what lets one copy serve both themes — `--primary` is
  `#a05e1c` on cream and `#dca75f` on espresso, so the mark re-skins with
  everything else and never needs a second asset or a media query.

  In the lockup it sits on the wordmark's baseline, not the centre of its line
  box. A block-level flex item has no baseline of its own, so flexbox aligns its
  bottom edge, which lands the mark exactly on the baseline; `dc-tracker` has no
  descenders, so centring instead drops the mark 3.5px and reads as a sag next to
  24px Instrument Serif. At 16px the brackets rasterise to 2px stems with open
  counters, and the interior bar is joined to the left spine so it cannot orphan.

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

- **A fifth of what search returned was unreadable, and none of it was a
  paywall** (`tracker/ingest/fetch.py`, `pyproject.toml`). Six of ~31 fetches in
  one live `enrich 10` answered 403, including the Meta/Blue Owl press release
  that is the primary source for Hyperion's **$27B** — the figure the row has
  been holding the superseded $10B against.

  Diagnosed rather than assumed: **the same URL with the same User-Agent gets 200
  from curl and 403 from httpx.** The block is on the TLS ClientHello, and every
  host's `robots.txt` permits us (`investor.atmeta.com` says `Allow: /` with
  `Crawl-delay: 10`). So this is the over-broad-WAF case the project already
  sanctions escalating for, not anybody's access control, and DataCenterDynamics'
  genuine bot-management block stays discovery-only exactly as before.

  Escalation is now a **ladder, cheapest rung first**, which is the ordering
  `enrich` already uses for its harvesters:

  | rung | cost | what it clears |
  |---|---|---|
  | `httpx` | one request | the default; most pages |
  | `curl_cffi` (`[impersonate]`) | one request | a WAF fingerprinting the TLS handshake |
  | Chromium (`[crawl]`) | seconds | pages that assemble themselves after load |

  `curl_cffi` needs no flag — it costs an ordinary request — while Chromium stays
  behind `--browser`. `fetch_all` takes a sequence, starts each rung lazily on the
  first page that needs it, and closes every rung it entered. A single fetcher is
  still accepted, so existing callers are untouched.

  Measured end to end on the three permitted hosts: `entergy.com` 403 → **10,266
  characters of prose** via curl_cffi, `lailluminator.com` 403 → 2,844, and the
  Blue Owl release 403 → thin-200 → **8,638 via Chromium**.

  Two defects in the browser rung had to be fixed before it worked at all, and
  both had been silently making it useless on the pages it exists for:

  - **No settle time.** The Blue Owl page returned HTTP 200 and *one character* —
    a Q4 Inc. shell that fetches its own body after load. `JS_SETTLE_S = 3.0`
    turns that into 15,546 characters. Investor-relations pages are the worst case
    and the most valuable one, because `investment_usd` is the field this database
    is thinnest on and a press release states it in the first sentence.
  - **`form` was in `excluded_tags`.** ASP.NET WebForms — which every Q4 Inc.
    investor-relations site is built on — wraps the entire document body in one
    `<form runat="server">`, so excluding it deleted the article along with the
    search box. Same page, same settle, the only difference being that list:
    **1 character with `form` excluded, 9,180 without.**

- **`instagram.com` was not in the search blocklist** (`tracker/ingest/search.py`).
  One live `enrich 10` fetched four Instagram URLs and the prose floor measured
  **0 characters of prose** in every one — a reel has no sentence for the evidence
  gate to quote. Added with `threads.net` and `tiktok.com`. The 403 hosts are
  deliberately *not* blocklisted: they are permitted sources we simply cannot open
  yet, and this project keeps those visible for `--retry-failed` rather than
  disappearing them.

- **Installing the `[crawl]` extra made four tests fail, on some machines only**
  (`tests/conftest.py`, `tests/test_cli.py`). `import crawl4ai` pulls in a litellm
  fork that calls `load_dotenv()` at import time, so the developer's entire `.env`
  — search keys, provider pin, tunnel hostname and the API key itself — lands in
  `os.environ`. Neutralizing pydantic's `env_file` cannot help, because
  `os.environ` is always consulted: measured, `TRACKER_SERPER_API_KEY` absent
  before the import and present after. The autouse fixture's docstring already
  promised "no `.env` leakage" while stripping two variables by name; it now
  strips every `TRACKER_*`. And the two tests asserting the *missing*-extra
  message now hide the module explicitly instead of depending on the developer not
  having installed it — the same class of bug as the colour probes reading the
  developer's own database.

- **The tranche panel is a site plan, not a provenance ledger** (`tracker/blockcheck.py`,
  `tracker/export.py`, `app.js`). A block is a **section of a facility**, so which
  section it is comes first and its state is one of its attributes. The panel had it
  the other way round: rows sorted by `status`, arithmetic grouped by evidence tier.
  That answers "what do we believe", which is a question the rest of the drawer
  already answers, and it left Applied Digital's Polaris Forge reading as seven rows
  in status order when it is **four buildings**.

  Rows are now sections, in **identity order** — class then ordinal, never state —
  each saying what it delivers of what it holds:

  ```
  Building 2   also called Building 2 (ELN-02)     100 / 100 MW   Serving
  Building 3   also called Building 3 (ELN-03)       0 / 150 MW   Under construction
  Building 4   also called Building 4 (ELN-02 C)     0 / 150 MW   Under construction
  HPC Facility                                       0 / 100 MW   Under construction
  ```

  `delivering / held` is the distinction one `mw_planned` and one `mw_built` per
  campus cannot draw: a building under construction holds 150 MW and delivers none
  of it. The duplicate names are shown rather than hidden ("also called …") so the
  grouping can be checked instead of trusted, and `blockcheck.sections` computes it
  server-side because deciding that `Area II` is `Building 2` is a judgement.

  Two things it deliberately does not do. It **never picks between two confirmed
  capacities** — Hyperion's `Phase 1` at 2,000 MW beside `Phase 1 IT Load` at 1,500
  shows both and says they are two figures. And a section whose identity could not
  be settled says so rather than being filed under a guess. Only those two states
  take a hue; "four sources named this building" is said in words, because colour in
  this product means how much to believe a value or that a value is broken.

  Measured against the data first: **69% of blocks carry a capacity**, so
  `delivering / held` renders on most rows — but a parent string names another block
  only **8 times in 286**, so there is no hierarchy in this data to nest and building
  a tree would have been theatre.

- **A hedged figure reads as a hedged figure, on every surface** (`tracker/vocab.py`,
  `tracker/export.py`, `tracker/cli.py`, `app.js`). Fairwater's `mw_built` rests on
  *"Each exceeds 350 MW"* — a floor, stated across two sites — and every surface
  rendered it as a bare `350`. It now reads **`350+`**.

  A floor is a **suffix** rather than `≥350`, because that is how a reader outside
  this codebase writes "or more", and the floor is the case that matters most in
  this corpus. `~` and `≤` stay prefixes. The CLI and the console share the two
  affix tables, so notation cannot drift between them.

  Three things had to change before the number appeared at all:

  - **`exceeds` was not in the marker list** — the commonest hedge in this corpus,
    and the one under Fairwater's own figure. Also added: `exceeding`, `greater
    than`, `or more`, `under`, `below`, `at most`, `circa`, `estimated`. The list
    moved from `crawl` into `vocab` so the gate that *assigns* a bound and the
    surfaces that *display* one read one copy; writing this rule down twice is how
    `confidence.find_conflicts` became a third copy of another and started
    disagreeing with the other two on screen.
  - **Blocks had no bound at all.** A tranche carries no `claim_meta`, so there is
    no axis to read — but it does store the verbatim sentence, and `_mw_bound`
    derives one from that. Server-side, because the browser draws judgements and
    never makes them. Hyperion's `Phase 1` now reads `~400`.
  - **A stored `exact` falls back to the quote.** The `bound` axis reached 32% of
    claims, and everything extracted before `exceeds` existed says `exact` — so
    trusting the axis alone would keep reporting a floor as a point value until
    every row is re-read.

  `vocab.bound_from_quote` is **positional**, which closes a defect HANDOFF.md has
  carried: Hyperion's *"more than $50 billion ... up from the roughly $27 billion
  plan"* gave `approximate` to the $50B figure off the *other* number's hedge. Each
  figure now takes only a hedge within 32 characters of itself — a window measured
  against the two cases that decide it (gap 15 must match, gap 52 must not) rather
  than picked — and a stated direction outranks a stated imprecision, so *"more than
  approximately 350"* is a floor and not an estimate. The ingest gate is left
  non-positional on purpose: it is never given the figure, and that is what stops a
  refused axis from costing a value.

- **`tracker enrich <id>` did nothing at all on most projects, after fetching
  every archive sitemap to find that out** (`tracker/cli.py`,
  `tracker/ingest/enrich.py`). Two ordering defects, one symptom.

  `--target` defaulted to **9**, and `run` checks it *before* its first round. So
  on any project already holding 9+ of the 12 tracked fields — which is most of
  the good ones — `enrich 10` broke out immediately: no queue, no retry, no
  search, no refresh, no extraction. It reported `reached the 9-field target`,
  which reads as an accomplishment rather than as a refusal to work. That target
  is a *budget-sharing* rule — its own docstring says it leaves "the rest of a
  shared budget for the next project" — and there is no next project when you
  name one. Naming ids now means **no target**, i.e. exhaust the row, while
  `--select`/`--all` keep 9 to spread a shared budget. `--target 12` / `--target
  0` still override, and the first-round message now says plainly that nothing
  was harvested and how to change it.

  Worse, `run_many` swept all 22 configured sitemaps **before** anything asked
  whether a single project needed them — ~30 HTTP requests, then an immediate
  decline. The sweep is now conditional on some project actually reaching its
  harvesters (`will_harvest`, kept beside the loop's own break conditions so the
  two cannot drift apart).

  Measured on Hyperion (#10), same command both times: **0 articles read → 25**,
  behind 3 Serper queries and 23 mined Wikipedia references.

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
  rather than written to `mw_planned`. See docs/design-decisions.md.
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
