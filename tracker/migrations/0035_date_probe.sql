-- 0035_date_probe: remember asking a publisher for an article's date.
--
-- `tracker backfill dates --refetch` requests a page to read its publish date
-- from the page's own metadata. It kept no record of having asked, so a page
-- that states no date — 500 of the 1,694 undated citations on 2026-10-10 were
-- census.gov data pages, 81 were a facility directory — would be requested again
-- on every run, and a nightly loop that turned `--refetch` on would spend its
-- whole limit on the same pages forever, never reaching the news articles whose
-- dates decide what the morning email carries.
--
-- So each request is a row. A page asked recently is skipped: for 90 days when it
-- answered with no date, for 14 when the request itself failed, which is more
-- often a bad night than a permanent refusal. A page that answered with a date
-- leaves the backlog by having one.
--
-- Not on `ingest_url`: `dates.py` promises to write nothing there but the date
-- itself, because that table's `status`, `attempts` and `last_tried_at` are the
-- crawl's bookkeeping and a date lookup is not a crawl.

CREATE TABLE date_probe (
    id        INTEGER  PRIMARY KEY,
    url       TEXT     NOT NULL,
    asked_at  DATETIME NOT NULL,

    -- 'dated': the page stated one. 'none': it answered and stated none.
    -- 'failed': the request did not succeed.
    outcome   TEXT     NOT NULL,

    CONSTRAINT ck_date_probe_outcome CHECK (outcome IN ('dated', 'none', 'failed'))
);

CREATE INDEX ix_date_probe_url ON date_probe (url, asked_at);
