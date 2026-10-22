-- 0048_fact_claim: the fact table carries everything a campus claim is ranked on.
--
-- Until now the readers that show where a figure came from — the export's `prov`
-- and claim table, the API's `fields`, the project page's evidence — read each
-- citation's JSON (`source.claims`, `quotes`, `claim_meta`, `fields`,
-- `unconfirmed_fields`, `unconfirmed_reasons`) and the master table was a copy
-- nothing read. They read the table now (client plan step 4.5), so a campus
-- statement must hold all the JSON held about it, not a summary:
--
--   value_json  the value exactly as the citation recorded it, as JSON. `value` is
--               text for the download and `number` a REAL for sums; neither keeps
--               300.0 apart from 300, and the claim table prints the one it was.
--   quoted      the citation lists the field among those a sentence it quotes
--               supports (`source.fields`). Kept when a ruling later takes the
--               claim out of the merge: the sentence is still real.
--   doubted     the citation lists the field 待确认 (`source.unconfirmed_fields`):
--               no sentence the gate could verify, or a ruling took it out.
--
-- `tier` stays the reader's one-word classification; these are what it was decided
-- from, and the merge's own ranking needs them separately (a superseded claim that
-- was quoted still sorts as one that was). Set on campus statements only — tranche,
-- party, milestone and obstacle statements keep their standing in `tier` and
-- `reason` — and NULL on the others.
--
-- A cache, so it is rebuilt rather than migrated: the table is recreated empty and
-- `tracker init`, which runs straight after the migrations, refills it.

DROP TABLE fact;

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
    value_json  TEXT,
    quoted      INTEGER,
    doubted     INTEGER,

    CONSTRAINT ck_fact_entity
        CHECK (entity IN ('campus', 'tranche', 'party', 'milestone', 'obstacle')),
    CONSTRAINT ck_fact_tier
        CHECK (tier IN ('confirmed', 'directory', 'derived', 'inferred', 'unconfirmed', 'ruled_out')),
    CONSTRAINT ck_fact_held CHECK (held IN (0, 1)),
    CONSTRAINT ck_fact_quoted CHECK (quoted IS NULL OR quoted IN (0, 1)),
    CONSTRAINT ck_fact_doubted CHECK (doubted IS NULL OR doubted IN (0, 1))
);

CREATE INDEX ix_fact_project_id ON fact (project_id);
CREATE INDEX ix_fact_field ON fact (entity, field);
CREATE INDEX ix_fact_source_id ON fact (source_id);
