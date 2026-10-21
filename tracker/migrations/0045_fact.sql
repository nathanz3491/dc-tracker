-- 0045_fact: every statement every source makes, in one table.
--
-- The client asked for a master data table: every channel in one place, each figure
-- classified by how far it can be trusted (A4). The facts already existed — per
-- citation, as JSON on `source` (claims, quotes, axes, tranches, parties) and as the
-- `event` and `risk` rows — but in five shapes, so "every statement about Abilene's
-- capacity, from every source, and which one the page shows" was a different query
-- for each.
--
-- One row per statement: what it is about (`entity` — campus, tranche, party,
-- milestone, obstacle — and `entity_key` within the campus), the `field`, the
-- `value` as written (and `number` when it is one, so it can be summed), its `unit`,
-- the citation (`source_id`), when it was true (`as_of`), how far it can be trusted
-- (`tier`), why not further (`reason`), the sentence (`quote`), the axes the gate
-- recorded (`axes`, JSON), and whether it is the value the row holds (`held`).
--
-- A cache, like `capacity_block`: rebuilt wholesale from the sources whenever a row
-- is written and by `tracker init`, so it can never disagree with them for long and
-- nothing is migrated into it. Nothing reads it to decide a value; it is how the
-- values that were decided can be read, all at once.

CREATE TABLE fact (
    id          INTEGER  PRIMARY KEY,
    project_id  INTEGER  NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    entity      TEXT     NOT NULL,
    entity_key  TEXT     NOT NULL DEFAULT '',
    field       TEXT     NOT NULL,
    value       TEXT,
    number      REAL,
    unit        TEXT,
    source_id   INTEGER  REFERENCES source (id) ON DELETE SET NULL,
    as_of       DATE,
    tier        TEXT     NOT NULL,
    reason      TEXT,
    quote       TEXT,
    axes        TEXT,
    held        INTEGER  NOT NULL DEFAULT 0,

    CONSTRAINT ck_fact_entity
        CHECK (entity IN ('campus', 'tranche', 'party', 'milestone', 'obstacle')),
    CONSTRAINT ck_fact_tier
        CHECK (tier IN ('confirmed', 'directory', 'derived', 'inferred', 'unconfirmed', 'ruled_out')),
    CONSTRAINT ck_fact_held CHECK (held IN (0, 1))
);

CREATE INDEX ix_fact_project_id ON fact (project_id);
CREATE INDEX ix_fact_field ON fact (entity, field);
CREATE INDEX ix_fact_source_id ON fact (source_id);
