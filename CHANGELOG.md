# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

First working version. Nothing has been released yet, so everything below is the
initial build of the v1 PRD.

### Added

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
