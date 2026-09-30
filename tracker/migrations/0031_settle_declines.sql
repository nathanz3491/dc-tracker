-- 0031_settle_declines: remember what enrich's settle step refused.
--
-- `enrich` ends each row with a settle step: every field two quote-backed sources
-- disagree about goes to the judgement tier, which picks one or refuses. A pick
-- supersedes the losing claims, so that field is not contested next time. A refusal
-- writes nothing, by design — and so nothing stopped the same question coming back.
-- The overnight loop re-selected the same fifteen rows every round and paid again
-- for every refusal they carried.
--
-- The fix is the one 0025 made for the other paid phases: a decline keyed on a hash
-- of exactly what the model was shown, here the claims behind each option. A new or
-- superseded claim is a different question and is asked; so is anything after the
-- cooldown. `subject` is `<project id>:<field>`.
--
-- SQLite cannot ALTER a CHECK constraint, so the table is rebuilt. Safe for the
-- reason 0025 gave for having no foreign keys: nothing points into model_decline
-- and it points at nothing.

CREATE TABLE model_decline_new (
    id          INTEGER  PRIMARY KEY,
    kind        TEXT     NOT NULL,
    subject     TEXT     NOT NULL,
    fingerprint TEXT     NOT NULL,
    outcome     TEXT     NOT NULL,
    reason      TEXT,
    decided_by  TEXT     NOT NULL DEFAULT 'agent',
    decided_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_model_decline_subject UNIQUE (kind, subject),
    CONSTRAINT ck_model_decline_kind CHECK (kind IN ('pair', 'logic', 'audit', 'risk', 'settle'))
);

INSERT INTO model_decline_new (
    id, kind, subject, fingerprint, outcome, reason, decided_by, decided_at
)
SELECT
    id, kind, subject, fingerprint, outcome, reason, decided_by, decided_at
FROM model_decline;

DROP TABLE model_decline;

ALTER TABLE model_decline_new RENAME TO model_decline;
