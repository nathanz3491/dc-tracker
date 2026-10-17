-- 0038_ingest_url_read_by: which extraction prompt last read each URL.
--
-- `crawl.stale_by_prompt` re-reads articles whose citations an older prompt wrote,
-- and knew a URL had been re-read only when its citation was re-stamped. A re-read
-- that writes no citation — the page names no single campus, is not in English, is
-- not an article, or names a campus `--existing-only` will not create — re-stamps
-- nothing, so the URL stayed at the head of the queue and was read again the next
-- night. Measured on 2026-10-17: 45 of the nightly 60 had been read the night
-- before, some 20 to 84 times, and 1,699 stale URLs were waiting behind them.
--
-- `read_by` is the prompt stamp of the last read that reached the model or was
-- refused by the reader itself (`ok`, `no_project`, `thin_content`, `skipped`). A
-- fetch or model failure leaves it alone: nothing read the page. `stale_by_prompt`
-- skips a URL the current prompt has already read.
--
-- Backfilled for the prompt current since 2026-08-31: `extract-v1@2c4fc373` has
-- written every crawl citation since `extract-v1@4ea77aad`'s last on 2026-08-30, so
-- a URL whose last settled try came after that was read by it. `failures = 0`
-- leaves out a URL whose last try failed: a failed re-read keeps the earlier good
-- read's status but moves `last_tried_at`, and that try read nothing. Without this
-- the first night after the fix would re-read the same 45 once more.

ALTER TABLE ingest_url ADD COLUMN read_by TEXT;

UPDATE ingest_url
SET read_by = 'extract-v1@2c4fc373'
WHERE last_tried_at >= '2026-08-31'
  AND failures = 0
  AND status IN ('ok', 'no_project', 'thin_content', 'skipped');
