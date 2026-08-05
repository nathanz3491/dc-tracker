-- 0010_project_alias: merge decisions that outlive the next crawl.
--
-- `tracker merge` folds rows that turned out to be one campus, but the folded
-- row's identity is not gone from the world: the Abilene campus was stored as
-- Crusoe, Oracle, OpenAI and "OpenAI/Oracle" because each company's press keeps
-- writing about it from its own angle. `upsert_record` matches on exact
-- `dedup_key` only, so the next crawl of an OpenAI-angle article re-creates
-- `openai|city:abilene|TX` as a fresh row — the operator's merge quietly undone.
--
-- This table is the durable record. `merge` writes one row per folded identity,
-- `upsert_record` consults it after an exact-key miss and routes the record to
-- the survivor instead of inserting. Same argument 0009 makes for `block_alias`:
-- a hand-merge with nowhere durable to live evaporates on the next crawl.
--
-- Global rather than per-project, unlike `block_alias`: a `dedup_key` is already
-- unique across the whole `project` table, so scoping would add a column that
-- can only ever hold one value per key. The target is a project id, not another
-- key — ids survive the survivor's own field changes, and pointing at ids keeps
-- chains flat by construction: when a survivor is itself later merged, `merge`
-- repoints every alias aimed at it, so resolution is one lookup, never a walk.
--
-- ON DELETE CASCADE: a survivor deleted outright takes its aliases with it, and
-- the next crawl falls back to a clean insert rather than a dangling redirect.

CREATE TABLE project_alias (
    id              INTEGER  PRIMARY KEY,
    from_dedup_key  TEXT     NOT NULL,
    to_project_id   INTEGER  NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    decided_by      TEXT     NOT NULL DEFAULT 'operator',
    decided_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_project_alias_from UNIQUE (from_dedup_key),
    CONSTRAINT ck_project_alias_from CHECK (length(from_dedup_key) > 0)
);

CREATE INDEX ix_project_alias_to_project_id ON project_alias (to_project_id);
