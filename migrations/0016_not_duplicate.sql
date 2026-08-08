-- 0016_not_duplicate: the answer "these two are different sites", written down.
--
-- `tracker duplicates` proposes; it never merges. That was the right call and it
-- left one thing missing: there was nowhere to record a *no*. Measured on the live
-- database, 2 of the 4 suspected groups were plainly wrong — Element Critical's
-- Houston One against Switch's Houston campus, Aligned Phoenix against NTT
-- Phoenix — and an operator who worked that out had no way to say so. The pair
-- came back on the next run, and on every run after it, ahead of the two real
-- duplicates. A report that cannot be answered stops being read.
--
-- Worse than noise: `capex.rollup` reads the same pairs and sets one row of every
-- suspected group aside, so a false pair silently removes a real campus from the
-- buyer table. Parking a pair therefore has to reach that path too, which it does
-- because both read `suspected_duplicates`.
--
-- **Pairwise, not per group.** A group is a transitive closure computed at read
-- time; storing "1, 2 and 3 are distinct" as a group would say nothing about a
-- fourth row that appears next week and pairs with 2. Pairs compose: parking
-- (a,b) and (a,c) leaves (b,c) still askable, which is the honest state — nobody
-- has looked at it.
--
-- `a_id < b_id` is enforced rather than merely observed, so a pair has exactly one
-- spelling and UNIQUE means what it says. Callers use `pairs.park`, which orders
-- the ids for them.
--
-- `decided_by` mirrors `project_alias` and `logic.record_decision`: "operator" and
-- "model" are different claims about how much reading happened, and six months
-- later this column is the only place that difference survives. A model may park a
-- pair, and a reader must be able to tell that one did.
--
-- ON DELETE CASCADE on both sides: merging one of the two rows away makes the
-- question moot, and a dangling parked pair would suppress a future pairing
-- involving a recycled id.

CREATE TABLE not_duplicate (
    id         INTEGER  PRIMARY KEY,
    a_id       INTEGER  NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    b_id       INTEGER  NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    decided_by TEXT     NOT NULL DEFAULT 'operator',
    reason     TEXT,
    decided_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_not_duplicate_pair UNIQUE (a_id, b_id),
    CONSTRAINT ck_not_duplicate_order CHECK (a_id < b_id)
);

CREATE INDEX ix_not_duplicate_a_id ON not_duplicate (a_id);
CREATE INDEX ix_not_duplicate_b_id ON not_duplicate (b_id);
