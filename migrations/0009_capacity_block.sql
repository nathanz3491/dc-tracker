-- 0009_capacity_block: the tranche of a campus that has its own state.
--
-- `project` carries one `phase`, one `mw_planned`, one `mw_built`, one `customer`.
-- That was adequate when a data center was either built and serving customers or
-- not built. A modern AI campus is several of those at once, and the row cannot
-- say it. Measured on the live database at the time this landed: 28 projects were
-- partly built, 15 were `construction` with megawatts already live, 12 had power
-- energised while construction was mid-track, 49 had a customer named with nothing
-- built, and 52 had sources describing a phase with its own capacity that was
-- being discarded into a single `mw_planned`.
--
-- `tracks.py` already made half this argument and answered it with five
-- independent tracks. Those are per *project*, so a campus with phase 1 energised
-- and phase 2 unpermitted reports `power: energized, permits: approved` as though
-- the whole thing were finished. This table is the missing dimension.
--
-- Three design points, each of which has a test.
--
-- **Identity is a derived key, never a name a source chose.** `blocks.block_key`
-- folds "Phase 1", "Phase I", "phase one" and "first phase" onto `phase-1`, and
-- makes a filing's "AZP-3 Phase 3" meet an article's "Phase 3" *of AZP-3*. It will
-- never decide that `phase-1` and `azp-2` are one block on a similarity score —
-- that is an operator's call, recorded in `block_alias`. Same asymmetry `dedup.py`
-- argues for projects: a wrong merge is invisible and destroys two facts, a
-- flagged ambiguity is visible and costs a click.
--
-- **A project row is not one campus.** `dedup_key` is `company|city|state`, so one
-- row holds every facility an operator has in one municipality — AZP-2 and AZP-3
-- are two campuses, not two phases of one. The likeliest corruption this table can
-- cause is a generic "Phase 1" from two different campuses colliding on one key
-- and summing their megawatts. Hence `parent`, `generic`, and excluding an
-- unplaceable block from the rollup rather than guessing.
--
-- **Blocks are a cache, rebuilt wholesale from `source.blocks`.** Same status as
-- `confidence` and `h200_equivalent`, and it inherits their obligation: `tracker
-- init` recomputes, and a second pass must be a no-op. Deriving rather than
-- accumulating is what makes re-ingest idempotent and gives `merge` and
-- `recompute_from_sources` the behaviour for free.
--
-- No backfill here. The conversion needs the article text, not the schema, so
-- existing rows stay blockless until `tracker backfill blocks` re-reads them — and
-- a project with no blocks is left exactly as it is, which is what lets this land
-- on 227 rows without changing any of them.

CREATE TABLE capacity_block (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES project (id) ON DELETE CASCADE,

    -- Derived by blocks.block_key(). The UNIQUE below is a backstop; the write
    -- path dedups in Python first, because two same-key blocks in one payload
    -- would otherwise abort the whole article.
    block_key       TEXT    NOT NULL,
    -- The words the winning source used. Cosmetic: identity rests on the key, so
    -- a source rewording its label cannot re-key the block.
    label           TEXT    NOT NULL,
    -- The facility a generic label belongs to, when a source named one. This is
    -- what lets "Phase 3" of AZP-3 meet "AZP-3 Phase 3".
    parent          TEXT,
    -- 1 when the label names a kind of thing rather than which campus ("Phase 1",
    -- "Building A", "8 MW expansion"). Stored rather than recomputed because the
    -- rollup and the ambiguity rule read it on every pass.
    generic         INTEGER NOT NULL DEFAULT 0,

    mw              REAL,
    status          TEXT    NOT NULL DEFAULT 'planned',
    -- Per block, which is the point: a pre-lease is a customer attached to a
    -- specific tranche of megawatts, and one string per project cannot say which.
    customer        TEXT,
    expected_online DATE,
    energized_on    DATE,
    investment_usd  INTEGER,

    -- JSON object, block field -> the verbatim sentence the evidence gate
    -- verified. Per field, not one quote for the row, because project 39's whole
    -- failure was money from one facility sitting beside capacity from another.
    quotes          TEXT,
    -- Comma list, same shape and meaning as source.unconfirmed_fields: extracted,
    -- kept, and refused the status of fact.
    unconfirmed_fields TEXT,

    source_id       INTEGER REFERENCES source (id) ON DELETE SET NULL,

    CONSTRAINT uq_capacity_block_project_key UNIQUE (project_id, block_key),
    CONSTRAINT ck_capacity_block_status CHECK (
        status IN ('planned', 'permitting', 'under_construction', 'shell_complete',
                   'energized', 'serving', 'paused', 'cancelled')
    ),
    CONSTRAINT ck_capacity_block_mw CHECK (mw IS NULL OR mw >= 0),
    CONSTRAINT ck_capacity_block_investment CHECK (investment_usd IS NULL OR investment_usd >= 0),
    CONSTRAINT ck_capacity_block_generic CHECK (generic IN (0, 1)),
    CONSTRAINT ck_capacity_block_label CHECK (length(label) > 0)
);

CREATE INDEX ix_capacity_block_project_id ON capacity_block (project_id);

-- Operator-owned, and it has to be a table rather than a note.
--
-- Two sources call one tranche "Phase 1" and "AZP-2"; no string function can know
-- that, and blocks are rebuilt wholesale from `source.blocks` on every upsert, so
-- a hand-merge with nowhere durable to live would evaporate on the next crawl.
-- This is that durable place.
CREATE TABLE block_alias (
    id          INTEGER  PRIMARY KEY,
    project_id  INTEGER  NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    from_key    TEXT     NOT NULL,
    to_key      TEXT     NOT NULL,
    decided_by  TEXT     NOT NULL DEFAULT 'operator',
    decided_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_block_alias_project_from UNIQUE (project_id, from_key),
    CONSTRAINT ck_block_alias_not_self CHECK (from_key <> to_key)
);

CREATE INDEX ix_block_alias_project_id ON block_alias (project_id);

-- Its own column, deliberately not a `"blocks"` key inside `source.claims`.
-- `claims` is a flat field->scalar map and at least six places iterate it assuming
-- exactly that — claims_by_field, derive_fields, confidence.find_conflicts,
-- logic.check_collisions, gaps._winning_source, export.to_json_object — and
-- 0004_risk.sql matches it with `claims LIKE '%"blocker"%'`. Nesting a list there
-- invites a quiet break in each. `quotes` and `unconfirmed_fields` were both added
-- as sibling columns for this reason.
ALTER TABLE source ADD COLUMN blocks TEXT;
