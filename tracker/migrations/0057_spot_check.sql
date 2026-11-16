-- 0057_spot_check: a random sample of each night's changed values, judged by a person.
--
-- The answer key (0050) says whether a night broke a value somebody had already
-- verified. It cannot say whether the values the night *wrote* are right: its 228
-- answers sit on the rows people have corrected hardest, applying it rewrote the
-- values it scores, and a night can write 80 values elsewhere and still read "0
-- stopped being right". The only measure of what the loop writes is a person reading
-- a fair sample of it, so the loop draws one at random every night (`tracker.spotcheck`,
-- `tracker checks`) and keeps here what the person said.
--
-- One row per night, campus and field. `before` and `after` are the value as text
-- (`changes._norm`, JSON-encoded) so a later edit to the row cannot rewrite what was
-- judged; `url`, `quote`, `via` and `why` are what stood behind `after` when it was
-- drawn. `project_id` is set to NULL when a merge removes the row, and `label` keeps
-- the name. `verdict` is NULL until somebody answers.

CREATE TABLE spot_check (
    id          INTEGER  PRIMARY KEY,
    night       DATE     NOT NULL,
    project_id  INTEGER  REFERENCES project (id) ON DELETE SET NULL,
    label       TEXT     NOT NULL,
    field       TEXT     NOT NULL,
    before      TEXT,
    after       TEXT,
    url         TEXT,
    quote       TEXT,
    via         TEXT,
    why         TEXT,
    drawn_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verdict     TEXT,
    note        TEXT,
    checked_by  TEXT,
    checked_at  DATETIME,

    CONSTRAINT uq_spot_check_night_project_field UNIQUE (night, project_id, field),
    CONSTRAINT ck_spot_check_verdict CHECK (
        verdict IS NULL OR verdict IN ('right', 'wrong', 'unsure')
    )
);

CREATE INDEX ix_spot_check_night ON spot_check (night);
