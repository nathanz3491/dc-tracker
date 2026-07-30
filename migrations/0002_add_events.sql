-- 0002_add_events: date-stamped project milestones.
--
-- Optional per the PRD ("used when we have date-stamped milestones"), so it
-- lives in its own migration and a DB stopped at version 1 is still valid.

CREATE TABLE event (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    event_date   DATE    NOT NULL,
    event_type   TEXT    NOT NULL,
    description  TEXT    NOT NULL,

    -- Nullable, ON DELETE SET NULL: an operator may record a milestone during
    -- `review` with no single citation, and deleting a superseded source must
    -- not delete the historical fact that the event happened.
    source_id    INTEGER REFERENCES source (id) ON DELETE SET NULL,

    -- Without this, re-running an ingest duplicates every event on every run.
    -- Accepted cost: two `expanded` events on the same date for one project
    -- collapse into one.
    CONSTRAINT uq_event_project_type_date UNIQUE (project_id, event_type, event_date),
    CONSTRAINT ck_event_type CHECK (event_type IN ('announced', 'permit_filed', 'groundbreaking', 'energized', 'first_customer', 'delayed', 'expanded'))
);

CREATE INDEX ix_event_project_id ON event (project_id);

CREATE INDEX ix_event_date ON event (event_date);
