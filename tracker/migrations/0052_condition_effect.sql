-- 0052_condition_effect: whether a rule makes building harder or easier.
--
-- The first dry run of the condition reader, 2026-11-09, kept 76 rules from 40 pages,
-- and some were the opposite of an obstacle: Utah's law letting a 100 MW load build its
-- own power, a sales-tax exemption for data-center equipment, an airport district that
-- allows data centers by right. Shown as "exposed to", a rule that helps a project
-- reads as one that threatens it. `restricts` is a ban, moratorium, pause, new cost or
-- condition; `eases` is an exemption, by-right zoning, a fast track. Every rule read
-- before this column existed restricted (none had been stored), hence the default.

ALTER TABLE condition ADD COLUMN effect TEXT NOT NULL DEFAULT 'restricts' CONSTRAINT ck_condition_effect CHECK (effect IN ('restricts', 'eases'));
