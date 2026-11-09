-- 0051_conditions: rules that apply to a place, not to one campus.
--
-- Every fact in this database belongs to a campus an article named. A rule that
-- applies to a whole state or town names none, so the reader dropped it: in the week
-- after the 2026-11-03 elections 72 articles about the vote were filed "no project",
-- and the Texas governor-elect's permit moratorium reached 21 of 205 Texas projects —
-- only the ones some stock story happened to name. A `condition` is that rule, read
-- once, quoted, and matched by code to every project in its place and stage
-- (`tracker.conditions`).
--
-- `condition_source` is one row per article that stated it. The condition's status
-- and summary are derived from those rows (the newest article's word wins), the same
-- way every value on a project is a function of its sources.
--
-- `exposure` says which projects a condition reaches and why. It is rebuilt wholesale
-- by `tracker conditions match` and never edited by hand; `first_seen` survives a
-- rebuild so "newly affected" means something.
--
-- `condition_read` records which pages the reader has read under which prompt, so a
-- page is paid for once per prompt.

CREATE TABLE condition (
    id             INTEGER  PRIMARY KEY,
    title          TEXT     NOT NULL,
    kind           TEXT     NOT NULL,
    channel        TEXT     NOT NULL,
    scope          TEXT     NOT NULL,
    state          TEXT,
    county         TEXT,
    place          TEXT,
    region         TEXT,
    input          TEXT,
    applies_to     TEXT     NOT NULL DEFAULT 'unbuilt',
    min_mw         REAL,
    status         TEXT     NOT NULL,
    summary        TEXT     NOT NULL,
    effective_on   DATE,
    ends_on        DATE,
    first_reported DATE,
    last_reported  DATE,
    match_key      TEXT     NOT NULL,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_condition_scope CHECK (scope IN ('state', 'county', 'place', 'grid_region', 'national', 'input')),
    CONSTRAINT ck_condition_status CHECK (status IN ('proposed', 'pledged', 'passed', 'in_force', 'blocked', 'lifted', 'rejected')),
    CONSTRAINT ck_condition_applies CHECK (applies_to IN ('new', 'unbuilt', 'all')),
    CONSTRAINT ck_condition_state CHECK (state IS NULL OR (length(state) = 2 AND state = upper(state)))
);
CREATE INDEX ix_condition_state ON condition (state);
CREATE INDEX ix_condition_match_key ON condition (match_key);

CREATE TABLE condition_source (
    id             INTEGER  PRIMARY KEY,
    condition_id   INTEGER  NOT NULL REFERENCES condition (id) ON DELETE CASCADE,
    url            TEXT     NOT NULL,
    title          TEXT,
    published_at   DATETIME,
    read_at        DATETIME NOT NULL,
    status         TEXT     NOT NULL,
    summary        TEXT     NOT NULL,
    -- JSON object: the article's own sentences, verified, by what they are cited for
    -- (`rule`, `place`, `status`, `threshold`).
    quotes         TEXT     NOT NULL,
    CONSTRAINT uq_condition_source UNIQUE (condition_id, url)
);
CREATE INDEX ix_condition_source_condition ON condition_source (condition_id);

CREATE TABLE exposure (
    condition_id   INTEGER  NOT NULL REFERENCES condition (id) ON DELETE CASCADE,
    project_id     INTEGER  NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    fit            TEXT     NOT NULL,
    why            TEXT     NOT NULL,
    first_seen     DATETIME NOT NULL,
    PRIMARY KEY (condition_id, project_id),
    CONSTRAINT ck_exposure_fit CHECK (fit IN ('exact', 'likely'))
);
CREATE INDEX ix_exposure_project ON exposure (project_id);

CREATE TABLE condition_read (
    url            TEXT     PRIMARY KEY,
    stamp          TEXT     NOT NULL,
    read_at        DATETIME NOT NULL,
    found          INTEGER  NOT NULL DEFAULT 0,
    refused        INTEGER  NOT NULL DEFAULT 0,
    error          TEXT
);
