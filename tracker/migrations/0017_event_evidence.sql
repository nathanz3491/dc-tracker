-- 0017_event_evidence: give a milestone the same evidence tier a risk has.
--
-- `events[]` was the last extracted structure with no gate behind it at all. The
-- prompt says "Only milestones whose date you can quote" — a request with no
-- mechanism, which is precisely the distinction the crawl module's docstring
-- draws — and `_events` checked type, date and description, then wrote the row.
-- No quote was asked for, none was verified, and the console renders the result
-- as "Milestones as reported, each pointing at the source that stated it", which
-- overstates it: the source is the page the model was reading, not a sentence
-- anybody checked.
--
-- Observed live on Fairwater (#1): a `groundbreaking` milestone dated 2026-06-23
-- whose own description reads "Open house event held to announce opening" — an
-- open house recorded as breaking ground, two years after the site actually
-- broke ground, and it feeds the track strip like any verified milestone. The
-- events table also carries the project's energisations, which the tracks
-- module calls the most informative signal in the dataset; all of it rested on
-- the model's say-so.
--
-- Same two columns risks got (0004 gave them `quote`, 0012 gave them
-- `unconfirmed`), same semantics: the quote is verified verbatim against the
-- fetched article with the same recovery path, a failed quote demotes the event
-- rather than deleting it, and the reason lands in `unconfirmed` from
-- tracker.vocab.UNCONFIRMED_REASONS.
--
-- The backfill is where this deliberately DIFFERS from 0012, and the difference
-- is the point. 0012 left existing risks NULL because "every risk that survived
-- the old gate was, by definition, confirmed by it". No gate ever ran on an
-- event, so NULL-means-confirmed would be a false statement about every
-- existing row. They are marked `no_quote`: extracted, never verified, exactly
-- what the tier means everywhere else. A later re-read under the new prompt
-- confirms them properly, and the stale-prompt selector already knows which
-- sources those are.

ALTER TABLE event ADD COLUMN quote TEXT
    CONSTRAINT ck_event_quote_len CHECK (quote IS NULL OR length(quote) <= 500);
ALTER TABLE event ADD COLUMN unconfirmed TEXT;

UPDATE event SET unconfirmed = 'no_quote';
