-- 0055_phase_denied: a campus an authority turned down has a stage of its own.
--
-- tracker: foreign_keys off
--
-- `phase` could say a campus was paused or cancelled, and nothing else ended a
-- project. A county voting down the rezoning is neither: the developer has not
-- given up (King of Prussia's sued the township within weeks), and nothing is on
-- hold. The answer key records two such campuses as `denied` (Vermaland La Osa,
-- MLP Ventures King of Prussia, 2026-10-31), and the reader already wrote that
-- word for both, but the column refused it, so neither could ever be stored right.
--
-- `denied` joins `paused` and `cancelled` outside the progression
-- (`vocab.PHASE_TERMINAL`): a stop overrides a stage, and the newest stop wins.
--
-- SQLite cannot change a CHECK in place, so the table is rebuilt exactly as 0032
-- rebuilt it, with the columns 0043 and 0047 added since, and with foreign keys off
-- for the same reason 0032 gives (a DROP with them on cascades into every child).
-- The id sequence is carried over: AUTOINCREMENT's whole point is that a number
-- once issued, and merged away since, is never issued again.

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

    -- Added by 0043 and 0047.
    company_id                 INTEGER REFERENCES company (id) ON DELETE SET NULL,
    golive_checked_at          DATETIME,

    CONSTRAINT ck_project_phase CHECK (phase IN ('announced', 'permitting', 'construction', 'operational', 'paused', 'cancelled', 'denied')),
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
    mw_planned_basis, mw_built_basis, company_id, golive_checked_at
)
SELECT
    id, name, company, customer, city, county, state, country, lat, lon, dedup_key,
    mw_planned, mw_built, investment_usd, phase, first_announced, expected_online,
    blocker, notes, confidence, created_at, updated_at, last_verified_at,
    h200_equivalent, first_announced_precision, expected_online_precision,
    mw_planned_basis, mw_built_basis, company_id, golive_checked_at
FROM project;

-- The copy leaves the new table's sequence at the largest id present; the old one
-- remembers every id ever issued. Keep the larger, before the old row goes with its
-- table. (A rename carries the sequence row with it.)
UPDATE sqlite_sequence
SET seq = (SELECT seq FROM sqlite_sequence WHERE name = 'project')
WHERE name = 'project_new'
  AND seq < (SELECT seq FROM sqlite_sequence WHERE name = 'project');

DROP TABLE project;

ALTER TABLE project_new RENAME TO project;

CREATE UNIQUE INDEX uq_project_dedup_key ON project (dedup_key);
CREATE INDEX ix_project_company ON project (company);
CREATE INDEX ix_project_state ON project (state);
CREATE INDEX ix_project_phase ON project (phase);
CREATE INDEX ix_project_confidence ON project (confidence);
CREATE INDEX ix_project_company_id ON project (company_id);
