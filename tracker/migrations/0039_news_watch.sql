-- 0039_news_watch: what discovery has looked at, and when each campus was last
-- searched for news.
--
-- `seen_url` is every entry a feed, a news search or an operator's sitemap showed
-- discovery, with what was decided about it. Two things needed it, measured on
-- 2026-10-18:
--
--   * The keyword filter dropped Meta's "Kuna, we are online!" on every walk of
--     Meta's sitemap from 09-15 to 10-16 and left no trace, so the miss could not
--     be seen, counted, or undone when the filter was fixed. A dropped entry is now
--     kept with the reason, and `tracker discover --rejudge` puts it through
--     today's filter again.
--   * An operator's newsroom is read every night now, and every post it adds is
--     queued. "Added" needs a memory of what the newsroom listed before; without
--     one, the first walk of Meta's sitemap would have queued 3,000 old pages.
--
-- Kept apart from `ingest_url`, which is the queue: a URL stored there blocks
-- every later route to it, and counts as read in the queue's statistics. A URL here
-- only says it was seen.

CREATE TABLE seen_url (
    id             INTEGER  PRIMARY KEY,

    -- `canonical_url` form; one row per page whatever spelling it was seen under
    -- (`url_identity` decides, the way `queue_candidates` does).
    url            TEXT     NOT NULL,

    -- The [[feed]] or [[sitemap]] name it was first seen in.
    source         TEXT     NOT NULL,
    title          TEXT,
    published_at   DATETIME,

    -- queued:  put in the queue by this sighting.
    -- known:   already in the queue or read when first seen.
    -- dropped: refused by the keyword filter (`reason` says which term, or none).
    -- listed:  on an operator's newsroom when discovery first walked it, so not
    --          news; recorded so that only what the newsroom adds later is queued.
    verdict        TEXT     NOT NULL,
    reason         TEXT,

    first_seen_at  DATETIME NOT NULL,
    last_seen_at   DATETIME NOT NULL,

    CONSTRAINT uq_seen_url_url UNIQUE (url),
    CONSTRAINT ck_seen_url_verdict CHECK (verdict IN ('queued', 'known', 'dropped', 'listed'))
);

CREATE INDEX ix_seen_url_source ON seen_url (source, first_seen_at);
CREATE INDEX ix_seen_url_verdict ON seen_url (verdict, first_seen_at);

-- One row per campus the news watch has searched for (`tracker watch`).
--
-- The watch asks a news search about each campus on a schedule set by its stage,
-- for news since `checked_at`. A campus's first search (`baseline_at` NULL) looks
-- back a year instead, so the first pass over the database catches up on what
-- the research that filled each row never looked for: stage changes.
CREATE TABLE news_watch (
    project_id     INTEGER  PRIMARY KEY REFERENCES project (id) ON DELETE CASCADE,

    -- When the year-long first look ran. NULL until it has.
    baseline_at    DATETIME,

    -- The last search, of any kind. The next looks back to here.
    checked_at     DATETIME NOT NULL,

    -- Totals over the row's life: searches sent, results returned, results kept
    -- by the filter and queued, and when one was last queued.
    queries        INTEGER  NOT NULL DEFAULT 0,
    results        INTEGER  NOT NULL DEFAULT 0,
    queued         INTEGER  NOT NULL DEFAULT 0,
    last_queued_at DATETIME,

    CONSTRAINT ck_news_watch_counts CHECK (queries >= 0 AND results >= 0 AND queued >= 0)
);

CREATE INDEX ix_news_watch_checked ON news_watch (checked_at);
