-- 0046_invariant_tally: how many places break each relationship, night by night.
--
-- `tracker invariants` measures how the figures relate (勾稽关系) across the whole
-- database, and the morning report should say whether the night moved them: the
-- tranche review settled twelve campuses, or a re-read opened three. A count is only
-- a change against the last count, so each night's is kept here
-- (`tracker invariants --record`): one row per relationship per run.

CREATE TABLE invariant_tally (
    id          INTEGER  PRIMARY KEY,
    taken_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    key         TEXT     NOT NULL,
    violations  INTEGER  NOT NULL,
    mw          REAL     NOT NULL DEFAULT 0,

    CONSTRAINT ck_invariant_tally_counts CHECK (violations >= 0 AND mw >= 0)
);

CREATE INDEX ix_invariant_tally_key ON invariant_tally (key, taken_at);
