-- 0043_company: companies as entities, with their spellings, kind and parent.
--
-- Until now a company was a string: each row stored the spelling its first source
-- used, and `dedup.company_key` folded spellings to one key with an alias list in the
-- code and the operator roster. That made "META" and "Meta Platforms, Inc." one
-- company everywhere a key was compared, but there was nowhere to say what a company
-- *is* — its one name, what kind of company it is, whose subsidiary — and no way to
-- fold two spellings without a deploy.
--
-- `company` is one row per identity. `key` is the identity every row is already
-- filed under (`dedup.company_key`), so nothing that stores or compares keys
-- changes; the row holds what a key could not: the name printed everywhere
-- (`name`, and who chose it), the kind (from the roster or EDGAR list), and a parent
-- (AWS's parent is Amazon). `company_alias` holds every spelling that folds to it —
-- the code's list and the roster's, written here so they can be listed and read, and
-- a person's, which `company_key` then reads too (`tracker company alias`). A
-- person's alias is the only one this table is the source of; the others are
-- re-seeded from the code by `tracker init`.
--
-- `project.company_id` and `project_party.company_id` point each operator and each
-- party at its company. Derived, like the tranches: recomputed whenever a row is
-- written and by `tracker init`, so they follow an alias the moment it is added.

CREATE TABLE company (
    id          INTEGER  PRIMARY KEY,
    key         TEXT     NOT NULL,
    name        TEXT     NOT NULL,
    name_by     TEXT     NOT NULL DEFAULT 'rows',
    kind        TEXT,
    parent_id   INTEGER  REFERENCES company (id) ON DELETE SET NULL,
    parent_by   TEXT,
    note        TEXT,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME,

    CONSTRAINT uq_company_key UNIQUE (key),
    CONSTRAINT ck_company_key CHECK (length(key) > 0),
    CONSTRAINT ck_company_name CHECK (length(name) > 0),
    CONSTRAINT ck_company_not_own_parent CHECK (parent_id IS NULL OR parent_id <> id)
);

CREATE INDEX ix_company_parent_id ON company (parent_id);

CREATE TABLE company_alias (
    id          INTEGER  PRIMARY KEY,
    company_id  INTEGER  NOT NULL REFERENCES company (id) ON DELETE CASCADE,
    spelling    TEXT     NOT NULL,
    decided_by  TEXT     NOT NULL DEFAULT 'code',
    decided_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_company_alias_spelling UNIQUE (spelling),
    CONSTRAINT ck_company_alias_spelling CHECK (length(spelling) > 0)
);

CREATE INDEX ix_company_alias_company_id ON company_alias (company_id);

ALTER TABLE project ADD COLUMN company_id INTEGER REFERENCES company (id) ON DELETE SET NULL;
CREATE INDEX ix_project_company_id ON project (company_id);

ALTER TABLE project_party ADD COLUMN company_id INTEGER REFERENCES company (id) ON DELETE SET NULL;
CREATE INDEX ix_project_party_company_id ON project_party (company_id);
