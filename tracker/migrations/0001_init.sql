-- 0001_init: project, source, ingest_url.
--
-- This file is AUTHORITATIVE at runtime. `tracker/models.py` mirrors it for
-- querying, and `tests/test_db.py::test_models_match_migrations` fails if the
-- two drift apart. When changing the schema, change the SQL first.
--
-- Statements are separated by lines containing only `;` at the end of a
-- statement (see db.split_sql). Keep one statement per block.

CREATE TABLE project (
    id                INTEGER PRIMARY KEY,

    -- Identity -------------------------------------------------------------
    name              TEXT    NOT NULL,
    company           TEXT    NOT NULL,   -- operator / builder, NOT the utility
    customer          TEXT,               -- end tenant when != company

    -- Location. `city` is NULL for rows sourced from an ISO queue, which
    -- reports County only; `county` is NULL for rows sourced from news, which
    -- reports a municipality. At least one must be present.
    city              TEXT,
    county            TEXT,
    state             TEXT    NOT NULL,
    country           TEXT    NOT NULL DEFAULT 'US',
    lat               REAL,
    lon               REAL,

    -- Dedup identity, computed by tracker.dedup.dedup_key(). Format:
    --   "<company_key>|<city|county>:<locality_key>|<STATE>"
    -- Being UNIQUE and NOT NULL makes "never auto-merge a county-granularity
    -- row into a city-granularity row" a database invariant rather than a
    -- convention two ingest paths have to remember.
    dedup_key         TEXT    NOT NULL,

    -- Tracked facts --------------------------------------------------------
    mw_planned        REAL,               -- full planned buildout, site load
    mw_built          REAL,               -- energized / operational today
    investment_usd    INTEGER,            -- whole US dollars
    phase             TEXT    NOT NULL DEFAULT 'announced',
    first_announced   DATE,
    expected_online   DATE,
    blocker           TEXT,               -- one sentence, biggest current obstacle
    notes             TEXT,

    -- Derived / bookkeeping ------------------------------------------------
    confidence        INTEGER NOT NULL DEFAULT 0,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- updated_at = any field changed. last_verified_at = an operator asserted
    -- this row is correct (PRD open question Q4). Distinct on purpose.
    last_verified_at  DATETIME,

    CONSTRAINT ck_project_phase CHECK (phase IN ('announced', 'permitting', 'construction', 'operational', 'paused', 'cancelled')),
    CONSTRAINT ck_project_confidence CHECK (confidence BETWEEN 0 AND 3),
    CONSTRAINT ck_project_state CHECK (length(state) = 2 AND state = upper(state)),
    CONSTRAINT ck_project_country CHECK (length(country) = 2 AND country = upper(country)),
    CONSTRAINT ck_project_locality CHECK (city IS NOT NULL OR county IS NOT NULL),
    CONSTRAINT ck_project_mw_planned CHECK (mw_planned IS NULL OR mw_planned >= 0),
    CONSTRAINT ck_project_mw_built CHECK (mw_built IS NULL OR mw_built >= 0),
    CONSTRAINT ck_project_investment CHECK (investment_usd IS NULL OR investment_usd >= 0),
    CONSTRAINT ck_project_lat CHECK (lat IS NULL OR lat BETWEEN -90 AND 90),
    CONSTRAINT ck_project_lon CHECK (lon IS NULL OR lon BETWEEN -180 AND 180)
);

CREATE UNIQUE INDEX uq_project_dedup_key ON project (dedup_key);

CREATE INDEX ix_project_company ON project (company);

CREATE INDEX ix_project_state ON project (state);

CREATE INDEX ix_project_phase ON project (phase);

CREATE INDEX ix_project_confidence ON project (confidence);

-- One citation. A project has many; every non-null project field should appear
-- in at least one of its sources' `fields` (asserted by test_every_field_is_cited).
CREATE TABLE source (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    url           TEXT    NOT NULL,
    fetched_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_type   TEXT    NOT NULL,
    excerpt       TEXT,               -- verbatim quote, hard-capped at 500 chars
    fields        TEXT,               -- comma-separated project fields, derived from claims

    -- What this source actually ASSERTS, as a JSON object of {field: value}.
    -- Not in the PRD schema, and without it two of the PRD's own requirements
    -- are unimplementable: Q2 ("keep both conflicting mw_planned values") has
    -- nowhere to keep them, and confidence scoring's "agreement on key fields"
    -- rule has nothing to compare. `fields` is DERIVED from this, never hardcoded.
    claims        TEXT,

    -- Which extractor and which version produced this row, e.g.
    --   "crawl:extract-v1@3f2a91c4:MiniMax-M2.5:httpx"
    --   "pjm:iso_maps-v1:sha256=1f3a...:row=1274"
    -- Without it, "which prompt version produced this bad row?" is unanswerable
    -- and prompt iteration is unmeasurable.
    extractor     TEXT,

    -- Re-ingesting the same URL for the same project updates rather than
    -- duplicating. This is what makes ingest idempotent.
    CONSTRAINT uq_source_project_url UNIQUE (project_id, url),
    CONSTRAINT ck_source_type CHECK (source_type IN ('iso_queue', 'company_filing', 'government_doc', 'trade_press', 'general_media', 'manual')),
    CONSTRAINT ck_source_excerpt_len CHECK (excerpt IS NULL OR length(excerpt) <= 500)
);

CREATE INDEX ix_source_project_id ON source (project_id);

CREATE INDEX ix_source_type ON source (source_type);

-- Per-URL outcome of crawl ingestion.
--
-- The PRD asks us to "mark the source fetch_error and skip", but that is not
-- implementable on the `source` table: source_type is a closed enum with no
-- such member, and a source row requires a project_id -- on a fetch failure
-- there is no project. So URL outcomes get their own table, which also buys
-- idempotent re-runs (skip URLs already ok) and `--retry-failed`.
CREATE TABLE ingest_url (
    id             INTEGER PRIMARY KEY,
    url            TEXT    NOT NULL,
    run_id         TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    http_status    INTEGER,
    via            TEXT,               -- "httpx" | "crawl4ai"
    attempts       INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    content_sha1   TEXT,               -- of the fetched markdown, for change detection
    first_seen_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_tried_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_ingest_url_url UNIQUE (url),
    CONSTRAINT ck_ingest_url_status CHECK (status IN ('ok', 'fetch_error', 'parse_error', 'llm_error', 'no_project', 'skipped')),
    CONSTRAINT ck_ingest_url_attempts CHECK (attempts >= 0)
);

CREATE INDEX ix_ingest_url_status ON ingest_url (status);
