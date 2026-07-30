-- 0004_risk: obstacles as typed, dated, cited rows instead of one sentence.
--
-- `project.blocker` was a single nullable TEXT column ("one sentence, biggest
-- current obstacle"). Four things it cannot do, each of which the PRD asks for:
--
--   1. Hold more than one. The PRD names seven obstacle kinds and a real project
--      has several at once -- grid AND opposition AND equipment. One column has to
--      pick, so the others are lost.
--   2. Be cleared. `upsert._resolve` returns the existing value when a field has no
--      claims, so a blocker could be replaced but never set back to NULL. A
--      resolved obstacle sat on the row forever.
--   3. Be aggregated. "How much planned capacity is blocked on transmission in
--      ERCOT" is the question that matters for the power and chip read-through, and
--      free text cannot answer it.
--   4. Survive the evidence gate. The gate requires a non-numeric value to appear
--      verbatim in a quote, but the prompt asked the model to *write* one sentence
--      naming the obstacle. A written sentence is a paraphrase, so it can never be
--      a verbatim substring: both blockers in the live database fail the current
--      gate against their own article text. They are survivors of the older
--      label-trusting gate, and coverage was heading to zero, not up.
--
-- `project.blocker` survives as a DERIVED column -- the summary of the most severe
-- open risk -- so the PRD's twelve fields, the confidence coverage metric and the
-- export shape are all unchanged.
--
-- Note on the UNIQUE constraint below: SQLite treats NULLs as distinct, so it does
-- not bite for rows whose `first_seen` is NULL. `upsert._upsert_risks` therefore
-- dedups in Python on (category, first_seen), where None is a perfectly good dict
-- key, exactly as `_upsert_events` does. The constraint is a backstop; the write
-- path is the only writer. Making the column NOT NULL was the alternative and it
-- was rejected: the only always-available fallback is the fetch date, which moves
-- on every refresh and would therefore insert a duplicate risk on every run.

CREATE TABLE risk (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES project (id) ON DELETE CASCADE,

    -- What kind of obstacle, from a closed vocabulary. Free text here would defeat
    -- the whole purpose: aggregation.
    category     TEXT    NOT NULL,

    -- watch | material | blocking, ordered least to most severe in
    -- tracker.vocab.RISK_SEVERITIES. That order decides which risk becomes
    -- `project.blocker`.
    severity     TEXT    NOT NULL,

    -- open | resolved | superseded. Only `open` counts toward `project.blocker`.
    status       TEXT    NOT NULL DEFAULT 'open',

    -- One sentence, and explicitly ALLOWED to be a paraphrase -- which is why
    -- `quote` exists beside it. Same split `notes` already draws: a summary is
    -- never a citable claim, so the quote is what gets rendered as evidence.
    summary      TEXT    NOT NULL,

    -- A verbatim sentence from the article, verified by the evidence gate to really
    -- appear in the fetched text. Capped like `source.excerpt`, for the same
    -- copyright and database-size reasons. NULL only for a risk asserted by hand.
    quote        TEXT,

    -- The date the SOURCE dates the obstacle to, when it says. Deliberately not
    -- defaulted to "now": see the note above.
    first_seen   DATE,
    resolved_at  DATE,

    -- Set when a source quantifies a slip, so schedule risk becomes a number
    -- rather than a sentence.
    delay_days   INTEGER,

    -- Which citation asserts this. Nullable and ON DELETE SET NULL for the same
    -- reason as `event.source_id`: an operator may record an obstacle during
    -- `review` with no single citation, and deleting a superseded source must not
    -- delete the historical fact that the obstacle was reported.
    source_id    INTEGER REFERENCES source (id) ON DELETE SET NULL,

    -- Accepted cost, mirroring `uq_event_project_type_date`: two obstacles of one
    -- category first seen on the same date collapse into one row.
    CONSTRAINT uq_risk_project_category_seen UNIQUE (project_id, category, first_seen),
    CONSTRAINT ck_risk_category CHECK (category IN ('grid_capacity', 'transmission', 'permitting', 'environmental', 'equipment_supply', 'chip_supply', 'financing', 'offtake', 'community_opposition', 'water', 'unclassified')),
    CONSTRAINT ck_risk_severity CHECK (severity IN ('watch', 'material', 'blocking')),
    CONSTRAINT ck_risk_status CHECK (status IN ('open', 'resolved', 'superseded')),
    CONSTRAINT ck_risk_quote_len CHECK (quote IS NULL OR length(quote) <= 500),
    CONSTRAINT ck_risk_delay_days CHECK (delay_days IS NULL OR delay_days >= 0),
    CONSTRAINT ck_risk_resolved_at CHECK (resolved_at IS NULL OR status <> 'open')
);

CREATE INDEX ix_risk_project_id ON risk (project_id);

CREATE INDEX ix_risk_category ON risk (category);

CREATE INDEX ix_risk_status ON risk (status);

-- Preserve what the old column already held rather than dropping it on the floor.
--
-- `unclassified` because the sentence does not say which category it is, and
-- guessing from keywords at migration time would be inventing a fact in a
-- migration -- the worst possible place for it. Re-crawling the articles under the
-- new prompt reclassifies them properly.
--
-- `source_id` comes from the citation that actually asserted the blocker, so the
-- backfilled row is cited exactly as strongly as the value it replaces. The
-- subquery yields NULL when no such source exists, which `tracker review` then
-- surfaces as an uncited obstacle.
INSERT INTO risk (project_id, category, severity, status, summary, first_seen, source_id)
SELECT
    p.id,
    'unclassified',
    'material',
    'open',
    p.blocker,
    NULL,
    (
        SELECT s.id FROM source s
        WHERE s.project_id = p.id AND s.claims LIKE '%"blocker"%'
        ORDER BY s.id
        LIMIT 1
    )
FROM project p
WHERE p.blocker IS NOT NULL;
