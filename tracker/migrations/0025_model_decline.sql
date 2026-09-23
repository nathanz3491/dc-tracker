-- 0025_model_decline: what a model looked at and could not decide.
--
-- Five paid phases of the overnight loop ask a model a question and, when it cannot
-- answer, write nothing: a duplicate pair it left alone or rated below the floor, a
-- logic finding whose ruling the rails refused, an implausible figure it declined, an
-- obstacle it judged unclear, a row whose missing fields it found nobody had
-- published. Nothing recorded meant nothing remembered, so the next round selected the
-- same items in the same order and paid for the same answer — ~45,000-260,000 tokens
-- an agent run, every round, every night, until two rounds in a row failed to move a
-- count. Measured on the snapshot this was written against: 31 eligible pairs against
-- a per-round limit of 25, 141 open unquoted obstacles of which 2 had ever been
-- refuted, and a sync whose agent pass spent ~4.2M tokens to find one fact across 25
-- rows it will pick again next time.
--
-- **Keyed on what the model saw, not on the item.** `fingerprint` hashes the evidence
-- the question was put with — both rows' citations for a pair, the evidence block for
-- an audit finding, the article behind an obstacle. A new citation, a re-extraction or
-- a merge changes it, and the question is asked again on the next run, which is the
-- only time a second answer could differ from the first. `decided_at` bounds it from
-- the other side: the agent phases can search the open web, and the web changes
-- without anything here changing, so a decline also lapses after a cooldown
-- (`tracker.declines.COOLDOWN_DAYS`).
--
-- **Why a table and not a sentence in `project.notes`**, which is where decisions
-- live. A decline is not a decision about the data — nothing on the row changed, and
-- no reader of the row learns anything from it — and it has to expire, which prose
-- cannot express without every reader re-deriving a hash. It is spend bookkeeping,
-- and it sits beside the rows rather than inside them for the same reason
-- `not_duplicate` does.
--
-- One row per (kind, subject): the latest look replaces the previous one. `subject`
-- is text because the kinds key on different things — `12-34` for a pair, `56:code`
-- for a finding, `78` for an obstacle or a row. A subject whose project is merged away
-- simply never matches again; nothing here references `project` with a foreign key,
-- because a pair's two ids and a finding's code do not fit one.

CREATE TABLE model_decline (
    id          INTEGER  PRIMARY KEY,
    kind        TEXT     NOT NULL,
    subject     TEXT     NOT NULL,
    fingerprint TEXT     NOT NULL,
    outcome     TEXT     NOT NULL,
    reason      TEXT,
    decided_by  TEXT     NOT NULL DEFAULT 'agent',
    decided_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_model_decline_subject UNIQUE (kind, subject),
    CONSTRAINT ck_model_decline_kind CHECK (kind IN ('pair', 'logic', 'audit', 'risk', 'gapfill'))
);
