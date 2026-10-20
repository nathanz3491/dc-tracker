-- 0041_excluded_key: the identities a row was taken out under, so no reading puts it back.
--
-- A reading sometimes names a facility of another kind beside the campuses it is
-- about: one round-up of "products for data centers" gave the database a Ford
-- battery plant, a Corning cable plant and a Siemens switchgear plant. `tracker
-- exclude` deletes such a row, and that alone would last until the next re-read of
-- the same article, which computes the same `dedup_key` and inserts the row again --
-- the argument 0010 makes for `project_alias`, with a no in place of a yes.
--
-- So every key the row could come back under is written here, and `upsert_record`
-- creates no row under any of them. A row that already exists is never touched by
-- this table: only an insert is refused. `tracker exclude --lift <id>` forgets the
-- keys taken from one row.
--
-- No foreign key: the row this names is deleted in the same transaction, and the
-- record has to outlive it. `project_id` stays meaningful because `project` ids are
-- AUTOINCREMENT and never handed out again; `company` and `name` are copied so the
-- listing can say what was removed.

CREATE TABLE excluded_key (
    id          INTEGER  PRIMARY KEY,
    dedup_key   TEXT     NOT NULL,
    project_id  INTEGER  NOT NULL,
    company     TEXT     NOT NULL,
    name        TEXT     NOT NULL,
    reason      TEXT     NOT NULL,
    decided_by  TEXT     NOT NULL DEFAULT 'operator',
    decided_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_excluded_key UNIQUE (dedup_key),
    CONSTRAINT ck_excluded_key CHECK (length(dedup_key) > 0)
);

CREATE INDEX ix_excluded_key_project_id ON excluded_key (project_id);
