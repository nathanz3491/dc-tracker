-- 0027_url_failure_streak: how many tries in a row a URL has failed the same way.
--
-- `attempts` counts every request ever made for a URL and `error` holds only the
-- latest failure, so nothing could tell a URL that failed once last week from one
-- that has failed identically on every run for a month. Both got retried every
-- time `sync --retry-failed` (or `--full`) or enrich's retry harvester came round,
-- and the second kind is the expensive one. Measured on a copy of production: 9
-- URLs failing "reply truncated at the token limit" had been tried 66 times -- and
-- one such try can cost ~98,000 output tokens -- and 14 failing with the same SSL
-- "EOF" error 190 times, still being retried on 2026-09-22.
--
-- `failures` is the length of the current streak. A try that fails the same way as
-- the last one (same status, same HTTP status, same error once its digits and any
-- quoted reply are set aside) extends it; any success, or a different failure,
-- starts it again. `discover.MAX_SAME_FAILURES` is where a streak stops being
-- retried automatically; an explicit `tracker ingest crawl --url` still reads it.
--
-- A URL that was read successfully and whose later re-read fails keeps its `ok`
-- (or `no_project`): the citation from the good read still stands, and demoting it
-- put 35 cited URLs into the retry pool. There the column counts consecutive failed
-- re-reads, which is what the refresh phase backs off on.
--
-- Backfilled so every URL failing today gets exactly ONE more automatic try under
-- the new rule: the history needed to know how long each streak really is was never
-- recorded, and one more try is the bound that neither re-spends a month of
-- identical failures nor gives up on a URL that failed once. The limit was 3 when
-- this ran, so a streak of 2 leaves one.
ALTER TABLE ingest_url ADD COLUMN failures INTEGER NOT NULL DEFAULT 0;

UPDATE ingest_url
SET failures = 2
WHERE status IN ('fetch_error', 'parse_error', 'llm_error', 'thin_content');
