-- 0014_source_published_at: when the article was published, not when we read it.
--
-- `upsert.claims_by_field` sorts claims by (confirmed, weight, fetched_at, url)
-- and `fetched_at` is the moment the crawler happened to visit the page. Where
-- two sources tie on credibility -- which is the common case, since most of this
-- corpus is trade press -- the tiebreak is therefore decided by crawl order, and
-- crawl order is arbitrary with respect to the truth.
--
-- Measured on the live database, six stored values were decided this way against
-- publication order:
--
--   * Aligned Phoenix   65 MW from a 2017 article over 400 MW from a 2022 one
--   * Hyperion (#10)    Meta's $10B, published 2025-08-22, over the $27B that
--                       replaced it, published 2025-11-05 -- while the row's own
--                       notes read "expanded to up to $50 billion"
--
-- That is a floor rather than a total. It counts only ties, because an old
-- high-weight source beating a new low-weight one is the weighting policy
-- working as designed and needs its own argument.
--
-- The date was already being collected. `discover` records it on `ingest_url`
-- from the feed, where it is 78% populated and has never been read by anything
-- downstream -- there was no column on `source` to carry it to, and the merge is
-- the only place it matters.
--
-- Backfilled here, unlike `unconfirmed_reasons` in 0013, and the difference is
-- worth stating because the two look alike. That column recorded a decision some
-- past extraction made, so inferring it now would have invented a refusal no gate
-- ever issued. This one records when a publisher published something, which is a
-- fact about the URL and not about our reading of it -- the copy on `ingest_url`
-- is the same fact, already ours, sitting one join away.
--
-- NULL stays meaningful after the backfill: a URL supplied by hand rather than
-- found in a feed has no publication date anywhere, and the sort must fall back
-- to `fetched_at` for those rather than treating them as infinitely old.

ALTER TABLE source ADD COLUMN published_at DATETIME;

UPDATE source
   SET published_at = (
       SELECT ingest_url.published_at
         FROM ingest_url
        WHERE ingest_url.url = source.url
   )
 WHERE published_at IS NULL;

-- The merge reads this on every recompute, once per claim per field, so it is
-- worth an index even though the table is small today.
CREATE INDEX ix_source_published_at ON source (published_at);
