-- 0042_block_nesting: how a campus's tranches relate, where somebody had to decide.
--
-- Sources describe one campus at several grains at once: the campus as a whole,
-- its phases, the buildings inside them, one building under three names, an
-- earlier plan the campus outgrew, and now and then another site altogether.
-- Summed as siblings, they counted the same megawatts more than once -- 123 campuses
-- on the 10-21 copy, 101,892 MW -- and nothing in `capacity_block` could say which
-- tranche is part of which: it is a cache rebuilt from the sources on every upsert,
-- so a decision written onto it would evaporate on the next crawl.
--
-- This is where those decisions live, one row per tranche somebody ruled on, keyed
-- the way `block_alias` is: by `block_key`, the tranche's derived identity, never by
-- a label a source chose. What the arithmetic can settle on its own -- one tranche
-- under several names, a stated parent that is itself a tranche, a campus-wide
-- tranche whose parts fit inside it -- is derived on every read (`blocks.structure`)
-- and never stored, so it cannot go stale. Only judgement is stored: a model's
-- (`tracker tranches review`) or a person's (`tracker tranches rule`, the project
-- page). A ruling whose tranche no source states any more is dormant, not deleted:
-- the next re-read may state it again.
--
--   part_of     the tranche is a part of `target_key` (a building of a phase, a
--               phase of the campus); it counts inside its parent, not beside it
--   same_as     the tranche is `target_key` under another name; counted once
--   separate    its own part of the campus, beside the others -- also what places
--               a "Phase 2" that names no facility
--   superseded  an earlier figure the sources have since replaced
--   elsewhere   describes another campus, not this one
--
-- `decided_by` is 'model' or the person's account name; a model never overwrites a
-- person's ruling. `confidence` is the model's own, NULL for a person.

CREATE TABLE block_nesting (
    id          INTEGER  PRIMARY KEY,
    project_id  INTEGER  NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    block_key   TEXT     NOT NULL,
    relation    TEXT     NOT NULL,
    target_key  TEXT,
    reason      TEXT,
    confidence  REAL,
    decided_by  TEXT     NOT NULL DEFAULT 'operator',
    decided_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_block_nesting_project_key UNIQUE (project_id, block_key),
    CONSTRAINT ck_block_nesting_relation
        CHECK (relation IN ('part_of', 'same_as', 'separate', 'superseded', 'elsewhere')),
    CONSTRAINT ck_block_nesting_target
        CHECK ((relation IN ('part_of', 'same_as')) = (target_key IS NOT NULL)
               OR relation = 'superseded'),
    CONSTRAINT ck_block_nesting_not_self CHECK (target_key IS NULL OR target_key <> block_key),
    CONSTRAINT ck_block_nesting_confidence
        CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE INDEX ix_block_nesting_project_id ON block_nesting (project_id);

-- The review asks one question per campus and remembers an undecided answer the way
-- every other paid phase does (0025, 0031): a decline keyed on a hash of the tranches
-- it was shown, so a campus left unsettled is not paid for again until its tranches
-- change. `subject` is the project id. SQLite cannot ALTER a CHECK constraint, so
-- the table is rebuilt, exactly as 0031 rebuilt it.

CREATE TABLE model_decline_new (
    id          INTEGER  PRIMARY KEY,
    kind        TEXT     NOT NULL,
    subject     TEXT     NOT NULL,
    fingerprint TEXT     NOT NULL,
    outcome     TEXT     NOT NULL,
    reason      TEXT,
    decided_by  TEXT     NOT NULL DEFAULT 'agent',
    decided_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_model_decline_subject UNIQUE (kind, subject),
    CONSTRAINT ck_model_decline_kind
        CHECK (kind IN ('pair', 'logic', 'audit', 'risk', 'settle', 'nest'))
);

INSERT INTO model_decline_new (
    id, kind, subject, fingerprint, outcome, reason, decided_by, decided_at
)
SELECT
    id, kind, subject, fingerprint, outcome, reason, decided_by, decided_at
FROM model_decline;

DROP TABLE model_decline;

ALTER TABLE model_decline_new RENAME TO model_decline;
