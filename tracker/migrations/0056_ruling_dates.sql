-- 0056_ruling_dates: when a decision was made, and when an answer stopped holding.
--
-- A ruling (`superseded`, `misread` in `source.unconfirmed_reasons`) held forever,
-- and nothing on the citation said when it was made or by whom; on 2026-11-13 the
-- only way to date the 4,932 standing rulings was to diff 101 nightly backups. A
-- decision is about the world as it was on the day it was made, so the day is what
-- lets a later report reopen it (`tracker.expiry`).
--
-- `source.rulings`: JSON, field -> {on, by}, written beside the reason by
-- `conflicts.supersede` and `blocks.rule_out` and removed by `conflicts.lift_ruling`.
-- Rulings made before this migration stay undated; `expiry` treats them as made on
-- the day it ran, the latest they can have been made.
--
-- `answer_key.reopened_on` / `reopened_by`: a report published after the answer was
-- recorded says the field changed. The answer is then not scored and not applied,
-- and its citation is set aside, until `answers load --replace` records it again.

ALTER TABLE source ADD COLUMN rulings TEXT;

ALTER TABLE answer_key ADD COLUMN reopened_on DATE;

ALTER TABLE answer_key ADD COLUMN reopened_by INTEGER REFERENCES source (id) ON DELETE SET NULL;
