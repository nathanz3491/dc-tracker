-- 0049_headline_screen: which headline-screen prompt last kept a queued page.
--
-- The crawl can have a model read the headlines of the pages it is about to read and
-- set aside the ones that plainly cannot be news of a US campus (`tracker.ingest.
-- headlines`). Unlike the address rules, whose pages are asked again on every run,
-- that verdict is a paid call, so a page it kept is stamped here and not asked again
-- under the same prompt: a backlog page waiting several nights would otherwise be
-- asked every night, and each asking is another chance to lose it.

ALTER TABLE ingest_url ADD COLUMN screened_by TEXT;
