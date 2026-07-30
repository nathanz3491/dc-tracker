# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Article discovery.** `tracker discover` polls the RSS/Atom feeds and sitemaps
  in `seed/feeds.toml`, keyword-filters headlines in two tiers (topic + project
  signal), and queues matches for triage. `tracker queue` lists and drops
  candidates; `tracker ingest crawl --from-queue` drains them. This closes the gap
  where nothing in the system could find an article to read — the database only
  ever held what an operator typed in by hand.
- Migration `0003_discovery_queue`: a `discovered` status on `ingest_url` plus
  `title`, `feed` and `published_at`, so a candidate can be judged from its
  headline before spending an LLM call on it. Rebuilds the table, since SQLite
  cannot alter a CHECK constraint.
- Feed parsing uses stdlib `xml.etree` and `tomllib` — no new dependency.

### Changed

- Hedged dates now resolve instead of vanishing: `late 2027` → 2027-10-01 at
  quarter precision, `H1 2027` → half precision, `by 2028` → year precision, each
  with a note recording the original phrasing. Only genuinely unanchored phrasing
  (`next spring`, `soon`) and directional hedges (`before 2028`) stay NULL. This
  was discarding most of `expected_online`.

### Fixed

- The project's `.env` is now read by absolute path. pydantic-settings resolves a
  relative `env_file` against the current directory, so the API key in
  `<project>/.env` was invisible whenever `tracker` ran from anywhere else —
  which is the normal case now the CLI is on PATH. A `.env` in the current
  directory is still read and still takes precedence.
- The missing-key error named `MINIMAX_API_KEY`; the variable actually read is
  `TRACKER_MINIMAX_API_KEY`.
- `migrations/`, the prompt files and the article cache are now located relative
  to the installed package rather than the current directory. `tracker init` run
  from outside the project tree previously failed looking for a `migrations/`
  folder in the operator's home directory — which only surfaced once the CLI was
  put on PATH.

### Added

- Project scaffold: `pyproject.toml` (`dc-tracker`, console script `tracker`),
  `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`,
  `requirements.lock` (pinned transitive set; there is no uv/poetry here).
- Schema: `project`, `source`, `event`, plus `ingest_url`. Defined authoritatively
  in `migrations/0001_init.sql` and `0002_add_events.sql`, mirrored by
  `tracker/models.py`, with `tests/test_db.py::test_models_match_migrations`
  failing the build if the two drift.
- `tracker/db.py`: engine with `foreign_keys=ON` / WAL / `busy_timeout` pragmas,
  a raw-SQL migration runner with checksum-verified immutability, and
  `open_db(readonly=True)` so read commands cannot write.
- `tracker/normalize.py`: per-field coercion for state, MW, money, dates
  (carrying precision), phase, URLs and excerpts. 93% covered.
- `tracker/confidence.py`: 0–3 scoring by source weight, domain independence,
  agreement and conflict. 93% covered.
- `tracker/dedup.py` + `tracker/upsert.py`: the single write path, with project
  fields recomputed from `source.claims` so ingestion is idempotent and
  order-independent.
- `tracker/ingest/manual.py` and `seed/sample-projects.json`: hand-curated JSON
  ingest, refusing to load a file that still holds `PLACEHOLDER` values.
- `tracker/ingest/iso_maps.py` + `tracker/ingest/pjm.py`: ISO queue ingest for
  PJM/MISO/ERCOT/CAISO, reading CSV, XLSX and JSON. Aborts before row 1 on a
  renamed column, streams in chunks of 1000, and fails loudly when zero rows match
  or the reject rate exceeds 5%.
- `tracker/prompts/` + `tracker/llm.py`: versioned prompt files stamped by content
  hash, and a MiniMax client that enforces the JSON contract in code
  (parse → repair → validate → one corrective retry).
- `tracker/ingest/fetch.py` + `tracker/ingest/crawl.py`: article fetching with
  per-host rate limiting and browser escalation, LLM extraction gated on quoted
  evidence, an on-disk article cache, and per-URL outcomes in `ingest_url`.
- `tracker/export.py`: deterministic Markdown, CSV and JSON export.
- `tracker/cli.py`: `init`, `ingest {manual,pjm,crawl}`, `list`, `show`, `stats`,
  `review`, `verify`, `export`, `version`.
- 579 tests, green offline with no API key. 93% coverage on both `normalize.py`
  and `confidence.py`.

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
- Crawl4AI is an optional extra used for fetching only, not for LLM extraction, so
  that prompt versioning is truthful and the JSON contract is enforced in testable
  code. `httpx` is the default fetcher.
- The database is treated as a build artifact and is not committed; `seed/*.json`
  and `data/raw/*.csv` are, and reproducibility is a documented replay.
