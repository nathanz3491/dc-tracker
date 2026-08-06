-- 0011_thin_content_status: a URL that was fetched but was not an article.
--
-- A fetch can return 200 and 600 characters of navigation furniture — a teaser
-- card, a page shell, a "Read More >>" stub. Nothing distinguished that from an
-- article, so the model was handed one, invented a plausible project from the
-- title, and every evidence quote then failed against a page that contained no
-- sentences to quote. `build_records` restored the identity fields from the
-- ungated values and wrote the row anyway.
--
-- `thin_content` is the outcome for a page refused on that basis, *before* the
-- LLM call. It is deliberately a distinct status rather than reusing
-- `no_project`: the model never read this page, so nothing was judged about it,
-- and it is worth retrying if the site later serves the full body — which
-- `no_project` (settled, never retried) would prevent.
--
-- SQLite cannot ALTER a CHECK constraint, so the table is rebuilt. Safe here for
-- the same reason 0003 gave: no foreign keys point in or out of ingest_url, so a
-- copy-and-rename cannot orphan anything.

CREATE TABLE ingest_url_new (
    id             INTEGER PRIMARY KEY,
    url            TEXT    NOT NULL,
    run_id         TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    http_status    INTEGER,
    via            TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    content_sha1   TEXT,

    title          TEXT,
    feed           TEXT,
    published_at   DATETIME,

    first_seen_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_tried_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_ingest_url_url UNIQUE (url),
    CONSTRAINT ck_ingest_url_status CHECK (status IN ('discovered', 'ok', 'fetch_error', 'parse_error', 'llm_error', 'no_project', 'thin_content', 'skipped')),
    CONSTRAINT ck_ingest_url_attempts CHECK (attempts >= 0)
);

INSERT INTO ingest_url_new (
    id, url, run_id, status, http_status, via, attempts, error, content_sha1,
    title, feed, published_at, first_seen_at, last_tried_at
)
SELECT
    id, url, run_id, status, http_status, via, attempts, error, content_sha1,
    title, feed, published_at, first_seen_at, last_tried_at
FROM ingest_url;

DROP TABLE ingest_url;

ALTER TABLE ingest_url_new RENAME TO ingest_url;

-- Dropped with the old table, so recreate both.
CREATE INDEX ix_ingest_url_status ON ingest_url (status);
CREATE INDEX ix_ingest_url_published_at ON ingest_url (published_at);
