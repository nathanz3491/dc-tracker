-- 0005_track_milestones: milestones for the tracks a project advances along.
--
-- The PRD's stage ladder is not one ladder. It names 刚宣布 -> 已经买地 ->
-- 申请政府许可和电力接入 -> 平整土地、建设厂房 -> 安装设备并投入运营, and those are
-- progress on FIVE separate tracks: site control, permits, power, construction and
-- commercial. A project can own its land and be stuck on interconnection, or be
-- fully permitted with no customer. `project.phase` is one enum and cannot say so.
--
-- The obstacle list in the same PRD maps onto the same five tracks -- 电网没有足够
-- 电力 is the power track, 审批时间较长 the permit track, 变压器交付 the construction
-- track -- so stage and obstacle are one structure viewed twice. That also answers
-- the PRD's hardest question, 接下来出现什么信号才能证明项目在推进: the next unreached
-- milestone on whichever track is blocked.
--
-- Implemented by extending `event` rather than by adding states to
-- `PHASE_PROGRESSION`. That ordering drives the furthest-along merge rule in
-- upsert._resolve, so inserting states into it would silently change how every
-- project's phase merges across sources. An event is already dated and carries its
-- own source, which is exactly what a milestone needs.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt. Columns,
-- constraints and index names are reproduced exactly from 0002 apart from the
-- widened vocabulary; tests/test_db.py::test_models_match_migrations fails the
-- build if they drift.
--
-- Deliberately does NOT touch `PRAGMA foreign_keys`. The 12-step ALTER procedure
-- needs it off only when *other* tables reference the one being rebuilt, and
-- nothing references `event`. Toggling it here broke FK enforcement for the rest
-- of the connection's life -- `PRAGMA foreign_keys` is a no-op inside a
-- transaction, so the restoring `ON` cannot be relied on to fire, and
-- test_foreign_keys_are_enforced caught it.

CREATE TABLE event_new (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    event_date   DATE    NOT NULL,
    event_type   TEXT    NOT NULL,
    description  TEXT    NOT NULL,
    source_id    INTEGER REFERENCES source (id) ON DELETE SET NULL,

    CONSTRAINT uq_event_project_type_date UNIQUE (project_id, event_type, event_date),
    CONSTRAINT ck_event_type CHECK (event_type IN (
        'announced',
        -- Site control: an option taken or a purchase closed. The PRD's 已经买地,
        -- which previously hid inside `announced` and is the first irreversible
        -- commitment a developer makes.
        'land_acquired',
        'permit_filed',
        -- Filing and approval are different facts, often years apart. Only the
        -- second means the permit track has actually advanced.
        'permit_approved',
        -- The power track's decisive milestone. A signed interconnection agreement
        -- is what separates a project that will be energised from one queued
        -- behind a substation upgrade.
        'interconnection_agreement',
        -- Grading and earthworks: visible progress that precedes a building, and
        -- the cheapest confirmation that a project is physically real.
        'site_work',
        'groundbreaking',
        -- Servers and cooling going in. Between a finished shell and a live site,
        -- and where equipment-delivery delays actually show up.
        'equipment_install',
        'energized',
        'first_customer',
        'delayed',
        'expanded'
    ))
);

INSERT INTO event_new (id, project_id, event_date, event_type, description, source_id)
SELECT id, project_id, event_date, event_type, description, source_id FROM event;

DROP TABLE event;
ALTER TABLE event_new RENAME TO event;

CREATE INDEX ix_event_project_id ON event (project_id);

CREATE INDEX ix_event_date ON event (event_date);
