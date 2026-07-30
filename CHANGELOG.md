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
