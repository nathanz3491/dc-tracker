-- 0050_answer_key: verified answers for chosen campuses, to score the database against.
--
-- Nothing in the tracker measured whether a stored value is *true*. The quote check
-- measures that a sentence exists, the tiers count open questions, and every rule was
-- justified by the incident that prompted it. On 2026-10-30 a researched draft for the
-- 50 campuses carrying most of the money disagreed with 83 of 259 stored answers. A
-- change to how values are chosen can only be weighed against answers known to be
-- right, so they are kept here, one per campus and field, beside the rows they judge
-- (`tracker.answer_key`, `tracker answers`).
--
-- `value` is the answer as text: a stage, a number, or an ISO date read at
-- `precision`. NULL with status `verified` means the field should be empty ("no
-- current online date"). `low`/`high` give a range when the truth is one ("$6-10B").
-- Only `verified` answers are scored; `unsettled` ones record that the evidence did not
-- agree, and `draft` ones are scored only on request.

CREATE TABLE answer_key (
    id           INTEGER  PRIMARY KEY,
    project_id   INTEGER  NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    field        TEXT     NOT NULL,
    value        TEXT,
    low          REAL,
    high         REAL,
    precision    TEXT,
    basis        TEXT,
    status       TEXT     NOT NULL,
    source_url   TEXT,
    quote        TEXT,
    note         TEXT,
    method       TEXT     NOT NULL,
    recorded_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_answer_key_project_field UNIQUE (project_id, field),
    CONSTRAINT ck_answer_key_field CHECK (
        field IN ('phase', 'mw_planned', 'investment_usd', 'expected_online', 'first_announced')
    ),
    CONSTRAINT ck_answer_key_status CHECK (status IN ('verified', 'unsettled', 'draft')),
    CONSTRAINT ck_answer_key_precision CHECK (
        precision IS NULL OR precision IN ('day', 'month', 'quarter', 'half', 'year')
    ),
    CONSTRAINT ck_answer_key_range CHECK (low IS NULL OR high IS NULL OR low <= high),
    CONSTRAINT ck_answer_key_method CHECK (length(method) > 0)
);

CREATE INDEX ix_answer_key_project_id ON answer_key (project_id);
