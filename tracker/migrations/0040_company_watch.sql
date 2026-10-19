-- 0040_company_watch: when each company was last searched for news as a company.
--
-- The campus watch (`news_watch`, 0039) asks about each campus the database holds,
-- so it cannot hear of a campus the database does not hold: Microsoft buying land
-- in a county with no Microsoft row, a builder's first site in a new state. The
-- company watch asks about the company itself -- "Microsoft" "data center", since
-- the last look -- for every company with three or more campuses here, weekly, and
-- daily for a company a reader watches.

CREATE TABLE company_watch (
    -- `dedup.company_key`: "meta" for Meta, Meta Platforms and Facebook alike.
    company_key    TEXT     PRIMARY KEY,

    -- When the month-long first look ran. NULL until it has.
    baseline_at    DATETIME,

    -- The last search. The next looks back to here.
    checked_at     DATETIME NOT NULL,

    -- Totals over the row's life, as in `news_watch`.
    queries        INTEGER  NOT NULL DEFAULT 0,
    results        INTEGER  NOT NULL DEFAULT 0,
    queued         INTEGER  NOT NULL DEFAULT 0,
    last_queued_at DATETIME,

    CONSTRAINT ck_company_watch_counts CHECK (queries >= 0 AND results >= 0 AND queued >= 0)
);
