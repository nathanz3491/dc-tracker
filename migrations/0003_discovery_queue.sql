-- 0003_discovery_queue: let ingest_url hold candidates found by `tracker discover`.
--
-- Two changes:
--   1. A new `discovered` status, for a URL a feed surfaced but nothing has read
--      yet. This is what turns ingest_url from a record of what happened into a
--      work queue.
--   2. Discovery metadata (title, feed, published_at) so a candidate can be
--      triaged from its headline BEFORE spending an LLM call on it. Without the
--      title, `tracker queue` could only show a bare URL, which is not enough to
--      decide whether an article is worth reading.
--
-- SQLite cannot ALTER a CHECK constraint, so the table is rebuilt. Safe here
-- because nothing references ingest_url and it references nothing: no foreign
-- keys point in or out, so a copy-and-rename cannot orphan anything.

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

    -- Discovery metadata. NULL for a URL supplied by hand via --urls.
    title          TEXT,
    feed           TEXT,               -- which feed surfaced it
    published_at   DATETIME,           -- as reported by the feed

    first_seen_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_tried_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_ingest_url_url UNIQUE (url),
    CONSTRAINT ck_ingest_url_status CHECK (status IN ('discovered', 'ok', 'fetch_error', 'parse_error', 'llm_error', 'no_project', 'skipped')),
    CONSTRAINT ck_ingest_url_attempts CHECK (attempts >= 0)
);

INSERT INTO ingest_url_new (
    id, url, run_id, status, http_status, via, attempts, error, content_sha1,
    first_seen_at, last_tried_at
)
SELECT
    id, url, run_id, status, http_status, via, attempts, error, content_sha1,
    first_seen_at, last_tried_at
FROM ingest_url;

DROP TABLE ingest_url;

ALTER TABLE ingest_url_new RENAME TO ingest_url;

-- Dropped with the old table, so recreate.
CREATE INDEX ix_ingest_url_status ON ingest_url (status);

-- New: `tracker queue` and `ingest crawl --from-queue` both read the pending
-- candidates oldest-first, and a discovery run rechecks what it has already seen.
CREATE INDEX ix_ingest_url_published_at ON ingest_url (published_at);
