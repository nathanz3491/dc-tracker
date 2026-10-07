-- 0034_feed_probe: ask a closed publisher again, once a week, and reopen it if it answers.
--
-- Thirteen feeds and an archive were marked `closed` in `seed/feeds.toml` on
-- 2026-10-02, when Cloudflare began answering every page on them with a challenge.
-- The note beside each says a block can lift and that re-opening is deleting one
-- line — but nobody looks, and a publisher that reopened on a Tuesday would stay
-- unread until somebody happened to try it by hand.
--
-- So `tracker discover` requests each closed entry's own URL once every seven
-- days and records what came back here. An entry whose latest check answered
-- with a real feed or sitemap is polled again from that night on.
--
-- **The answer lives in the database, not in the file.** The file is code: the
-- host's checkout is reset to the pushed commit every two minutes, so a reopening
-- written there would be undone before the next poll. The `closed` line stays as
-- the record of why the entry was shut, and a person deletes it once a reopening
-- has held.
--
-- **Every check is a row, newest wins.** A reopening that fails again on the next
-- poll is written as a failed check at once, so a publisher that flaps is closed
-- again the same night rather than hammered for a week. History is cheap — 14
-- entries, one row each a week — and it is what answers "when did DCD close, and
-- has it ever reopened".

CREATE TABLE feed_probe (
    id          INTEGER  PRIMARY KEY,

    -- The `name` of the [[feed]] or [[sitemap]] entry in seed/feeds.toml.
    name        TEXT     NOT NULL,
    url         TEXT     NOT NULL,
    checked_at  DATETIME NOT NULL,

    -- 1 when the URL answered with a feed or sitemap holding at least one entry.
    open        INTEGER  NOT NULL,

    -- HTTP status, when there was a response at all.
    status      INTEGER,

    -- What was seen: "12 entries", or the failure ("HTTP 403 (Cloudflare challenge…)").
    detail      TEXT,

    CONSTRAINT ck_feed_probe_open CHECK (open IN (0, 1))
);

CREATE INDEX ix_feed_probe_name ON feed_probe (name, checked_at);
