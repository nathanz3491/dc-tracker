-- 0018_discovered_at: when WE learned a milestone or an obstacle, as against
-- when it happened.
--
-- The updates page asks a question the schema could not answer: what is new
-- since I last looked? Every existing date answers a different question.
-- `event.event_date` is when the milestone happened, `risk.first_seen` is the
-- date the *source* puts on the obstacle, `source.published_at` is when the
-- publisher published. None of them is "when this row appeared in our database",
-- and on this dataset the difference is not academic:
--
--   * event dates span 1997-01-01 to 2040-01-01;
--   * the 2026-08-11 crawl batch inserted milestones dated 2021 and 2022.
--
-- That is not a fault, it is how the pipeline works — a crawl reads one article
-- and imports a project's whole back-history — but it means a feed keyed on
-- `event_date` shows the same old news every morning, and a feed keyed on
-- "recent" hides a 2023 fact we learned an hour ago that changes the picture.
-- Both readings matter and they are different columns.
--
-- **Nullable, no server default, and that is SQLite's rule rather than a
-- choice.** ALTER TABLE ADD COLUMN refuses a CURRENT_TIMESTAMP default, so the
-- write path sets it (`models.utcnow`, like `upsert` already does for
-- `project.updated_at`) and the column is the safety net's absence rather than
-- its presence. A row written by hand with no timestamp reads NULL, which the
-- feed treats as "date unknown" instead of as "just now".
--
-- **The backfill takes the citation's `fetched_at`, and where there is no
-- citation it leaves NULL.** 2,749 of 2,825 events and 654 of 654 risks carry a
-- `source_id`, and that source's fetch is genuinely the moment the fact entered
-- the database. For the remainder — milestones an operator recorded during
-- `review` with no single citation — nothing anywhere records when they were
-- entered, and stamping them with the migration's own clock would assert that
-- every one of them was discovered today. That is the same mistake 0017
-- refused when it marked pre-gate events `no_quote` rather than `confirmed`:
-- do not invent a fact to fill a column.

ALTER TABLE event ADD COLUMN created_at DATETIME;

ALTER TABLE risk ADD COLUMN created_at DATETIME;

UPDATE event
   SET created_at = (SELECT s.fetched_at FROM source s WHERE s.id = event.source_id)
 WHERE source_id IS NOT NULL;

UPDATE risk
   SET created_at = (SELECT s.fetched_at FROM source s WHERE s.id = risk.source_id)
 WHERE source_id IS NOT NULL;

-- The updates page's own filter, on both tables. Small tables today, but this is the
-- one column the feed sorts and ranges over on every page load.
CREATE INDEX ix_event_created_at ON event (created_at);

CREATE INDEX ix_risk_created_at ON risk (created_at);
