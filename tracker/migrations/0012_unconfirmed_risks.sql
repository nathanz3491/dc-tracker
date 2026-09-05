-- 0012_unconfirmed_risks: give a risk the same 待确认 tier a field already has.
--
-- Migration 0006 gave *values* a third outcome. The PRD's rule —
--
--   遇到无法确认的信息，可以标记为"待确认"，不要猜测
--
-- was applied to every field and to no risk. A risk whose quote failed the gate
-- was deleted outright: `_risks` had no 待确认 path, so the obstacle, its
-- category, its severity and the model's summary all went on the floor. That is
-- the only place in the ingest path that still destroyed extracted information,
-- and it destroyed the one class of fact no press release ever states.
--
-- `unconfirmed` holds *why*, not merely *that*, from tracker.vocab
-- .UNCONFIRMED_REASONS — because the two common causes ask for opposite work.
-- `no_quote` wants another source. `quote_off_target` means the sentence is real
-- and was filed against the wrong category, which is a correction. NULL means
-- the gate confirmed it, which is what every existing row is.
--
-- `quote` stays NULL for these. A quote that failed its check is not stored
-- beside the thing it failed to support: `evidence_gate` already refuses that
-- pairing for fields, on the grounds that a mislabelled quote states something
-- the row does not, and the console renders a risk's quote as its evidence.

ALTER TABLE risk ADD COLUMN unconfirmed TEXT;

-- Every risk that survived the old gate was, by definition, confirmed by it, so
-- the backfill is the column default. Recorded here rather than left implicit:
-- NULL means "the gate confirmed this", never "we do not know".
