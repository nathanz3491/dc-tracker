-- 0053_condition_checked: when the news watch last searched for word of a rule.
--
-- A rule is read from the articles the news step happens to fetch, so one that is
-- not settled yet — a bill proposed, a candidate's pledge, an ordinance passed but
-- not in force, an order a judge blocked — kept its last status until some campus
-- story mentioned it again. A pledge that became an order, or a ban a court struck
-- down, could sit unread for weeks while every project in that place was flagged
-- under the old word. The news watch now searches each unsettled rule every few days
-- ("Texas data center moratorium") and stamps the time here, so it asks again only
-- when the rule is due and the night's few rule searches go to the ones waited on
-- longest (`tracker.ingest.watch.conditions_due`).

ALTER TABLE condition ADD COLUMN checked_at DATETIME;
