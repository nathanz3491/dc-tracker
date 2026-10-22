-- 0047_golive_check: when a campus past its online date was last searched for go-live news.
--
-- The client's rule for a suspected delay is "past its expected online date, and a
-- search still finds no news of it going live". Until this the flag skipped the
-- search: it read only what the database already held, and the news watch's first
-- look reaches back one year, so a campus that opened years ago without an article
-- we read stayed "late" for ever — Savvis MP2 (2014), CH3 (2018), NTT Mesa (2022).
-- The watch now searches each such campus from before its stated date to today and
-- stamps the time here; `delays.assess` flags a campus only after a search made
-- once its date had passed found nothing that puts it running.

ALTER TABLE project ADD COLUMN golive_checked_at DATETIME;
