-- 0032_project_autoincrement: a project's number is never handed to another campus.
--
-- tracker: foreign_keys off
--
-- `project.id` was a plain INTEGER PRIMARY KEY, and SQLite gives a new row one more
-- than the largest id still present. So when the newest rows are merged away, their
-- numbers go to the next campus created. Measured on 2026-10-04: #1557 had named
-- three different campuses in a week and #1556 two, and the merge notes on #404,
-- #552, #1299 and #1311 -- "merged project(s) #1557 into this row" -- pointed at
-- whatever unrelated row held the number now. A note, a decision or a person's
-- memory of "#1557" cannot be trusted while that is possible.
--
-- AUTOINCREMENT makes SQLite remember the largest id ever issued (in
-- `sqlite_sequence`) and never go below it. SQLite cannot add it to an existing
-- table, so the table is rebuilt.
--
-- **Why the line above.** Eight tables reference `project` with ON DELETE CASCADE.
-- With foreign keys on, dropping the old table runs an implicit DELETE that
-- cascades into every citation, event, risk and block -- and so does renaming it
-- aside first, because the children's references follow the rename. Both were
-- rehearsed on a copy of production, and both emptied every child table. The line
-- asks the runner to switch foreign keys off before the transaction, where SQLite
-- honours it, and to commit only if `PRAGMA foreign_key_check` then finds nothing
-- (db.FOREIGN_KEYS_OFF). This is SQLite's documented procedure for this change.
--
-- Columns, constraints and indexes are reproduced exactly from 0001 and the
-- ALTERs in 0008, 0015 and 0024, in the same column order.

CREATE TABLE project_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identity -------------------------------------------------------------
    name              TEXT    NOT NULL,
    company           TEXT    NOT NULL,   -- operator / builder, NOT the utility
    customer          TEXT,               -- end tenant when != company

    -- Location. `city` is NULL for rows sourced from an ISO queue, which
    -- reports County only, and `county` is NULL for rows sourced from news,
    -- which reports a municipality. At least one must be present.
    city              TEXT,
    county            TEXT,
    state             TEXT    NOT NULL,
    country           TEXT    NOT NULL DEFAULT 'US',
    lat               REAL,
    lon               REAL,

    -- Dedup identity, computed by tracker.dedup.dedup_key(). Format:
    --   "<company_key>|<city|county>:<locality_key>|<STATE>"
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
    last_verified_at  DATETIME,

    -- Added by 0008, 0015 and 0024.
    h200_equivalent            INTEGER,
    first_announced_precision  TEXT,
    expected_online_precision  TEXT,
    mw_planned_basis           TEXT,
    mw_built_basis             TEXT,

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

INSERT INTO project_new (
    id, name, company, customer, city, county, state, country, lat, lon, dedup_key,
    mw_planned, mw_built, investment_usd, phase, first_announced, expected_online,
    blocker, notes, confidence, created_at, updated_at, last_verified_at,
    h200_equivalent, first_announced_precision, expected_online_precision,
    mw_planned_basis, mw_built_basis
)
SELECT
    id, name, company, customer, city, county, state, country, lat, lon, dedup_key,
    mw_planned, mw_built, investment_usd, phase, first_announced, expected_online,
    blocker, notes, confidence, created_at, updated_at, last_verified_at,
    h200_equivalent, first_announced_precision, expected_online_precision,
    mw_planned_basis, mw_built_basis
FROM project;

DROP TABLE project;

ALTER TABLE project_new RENAME TO project;

CREATE UNIQUE INDEX uq_project_dedup_key ON project (dedup_key);
CREATE INDEX ix_project_company ON project (company);
CREATE INDEX ix_project_state ON project (state);
CREATE INDEX ix_project_phase ON project (phase);
CREATE INDEX ix_project_confidence ON project (confidence);

-- The copy leaves `sqlite_sequence` at the largest id present, but numbers above it
-- have already been issued and merged away: on 2026-10-04 the highest id any
-- production note names is #1560, one above the largest row. Starting the count
-- there means no number a note has used is issued again. A new, empty database
-- has no such history and still starts at 1.
UPDATE sqlite_sequence SET seq = 1560
WHERE name = 'project' AND seq < 1560 AND EXISTS (SELECT 1 FROM project);
