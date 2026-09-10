-- 0023_project_party: who plays which role on a site.
--
-- `project` carries one `company` and one `customer`. The extraction prompt has
-- always had to say what `company` means, and the answer it settled on is the
-- problem written down: "Who builds AND operates the site." Two roles in one
-- string, a third in `customer`, and no room at all for the utility or the
-- landowner.
--
-- Measured, and it is the largest single source of wrong numbers in this
-- database. `docs/duplicate-shapes.md` replays the 90 folds an operator
-- performed by hand: **48 of them had no key-level signal connecting the two
-- rows**, "because every key comparison holds the company fixed". The Abilene
-- campus was stored four times --
--
--   crusoe|city:abilene|TX      the party that builds it
--   openai|city:abilene|TX      the party that occupies it
--   oracle|city:abilene|TX      the party that leases it
--   openai oracle|city:abilene|TX
--
-- -- and Richland Parish twice, as Meta and as Entergy Louisiana. Each name is
-- correct. Each mints its own `dedup_key`. Every one of those rows contributed
-- its full `mw_planned` to a buyer's position.
--
-- The one signal that exists today, `dedup.shared_parties_across_companies`,
-- compares tokens *inside* the two company strings, so it fires on
-- "OpenAI/Oracle" against "Oracle" and cannot fire when four articles each name
-- one party. The parties are in the articles; the schema had nowhere to put them.
--
-- **The argument for a table is the one 0004_risk.sql already made**, and it is
-- the same four points. A site has several parties at once, not one. A party must
-- be able to *change* -- a campus is sold, a tenant signed, an operator replaced
-- -- and `upsert._resolve` returns the existing value when a field has no claims,
-- so a scalar can be replaced but never cleared. "How much capacity does Crusoe
-- build for somebody else" is a counting question and free text cannot answer it,
-- which is what a closed `role` vocabulary buys. And each party needs its own
-- evidence: the sentence naming the tenant is not the sentence naming the
-- builder.
--
-- **A cache, not a fact of record.** Rebuilt wholesale from `source.parties` on
-- every upsert -- same status as `capacity_block`, `confidence` and
-- `h200_equivalent`, and it inherits their obligation: a second pass must be a
-- no-op, or every number in the database is whichever pass ran last.
--
-- **`project.company` and `project.customer` survive as derived columns**,
-- exactly as `blocker` survives as the summary of the most severe open risk. That
-- is deliberate and load-bearing: `dedup_key` is `company|locality|state` and is
-- UNIQUE, so re-deriving `company` from the party set keeps the key, the index,
-- the twelve tracked fields and the export shape unchanged. This migration adds
-- an axis; it does not re-key 437 live rows. Splitting one operator's two
-- campuses in one city is a different change and needs a `campus` column --
-- `docs/design-decisions.md` names it as an accepted residual risk.
--
-- **Why `source.parties` is a sibling column and not a key inside
-- `source.claims`.** 0009_capacity_block.sql closes on this rule and it has not
-- changed: `claims` is a flat field->scalar map and at least six places iterate
-- it assuming exactly that -- claims_by_field, derive_fields,
-- confidence.find_conflicts, logic.check_collisions, gaps._winning_source,
-- export.to_json_object -- and 0004_risk.sql matches it with
-- `claims LIKE '%"blocker"%'`. Nesting a list there invites a quiet break in
-- each. `quotes`, `unconfirmed_fields` and `blocks` were all added as sibling
-- columns for this reason.
--
-- **UNIQUE is on (project, key, role), not (project, key).** One company
-- genuinely is both developer and operator of the same campus -- that is the
-- ordinary case for Meta and for Microsoft -- and collapsing the two would store
-- the fact that they build it or the fact that they run it, never both.
--
-- No backfill in SQL. The roles live in article text, and inferring one from a
-- column would manufacture a claim no source made. `tracker backfill parties`
-- seeds a row from each project's existing `company` and `customer` with the
-- winning source's own quote attached, which is free, re-runnable and reads only
-- what is already on disk; everything richer waits for a re-crawl.

CREATE TABLE project_party (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project (id) ON DELETE CASCADE,

    -- The words the winning source used, so the row can be read back in the
    -- publisher's own vocabulary. Cosmetic: identity rests on `party_key`.
    name        TEXT    NOT NULL,
    -- dedup.company_key(name). "Amazon Web Services" and "AWS" converge here, so
    -- two articles naming one party in two ways produce one row rather than two.
    party_key   TEXT    NOT NULL,
    -- Closed vocabulary, matching vocab.PARTY_ROLES. `utility` and `contractor`
    -- earn their place by arriving today whether we want them or not: Entergy
    -- Louisiana is in the live database as a `company`, and the prompt has a
    -- standing instruction to refuse the utility precisely because it keeps
    -- turning up. A role they belong in beats an exclusion rule.
    role        TEXT    NOT NULL,

    -- The verbatim sentence the evidence gate verified for this party, on the
    -- same terms as capacity_block.quotes: per party, because the sentence naming
    -- the tenant is not the sentence naming the builder.
    quote       TEXT,
    -- Why the gate could not confirm it, from vocab.UNCONFIRMED_REASONS. NULL
    -- means confirmed. Same shape and meaning as source.unconfirmed_reasons: a
    -- party nobody could quote is kept and refused the status of fact, never
    -- dropped -- 0012_unconfirmed_risks.sql made that argument for risks after
    -- this schema had spent a while deleting them.
    unconfirmed TEXT,

    source_id   INTEGER REFERENCES source (id) ON DELETE SET NULL,

    CONSTRAINT uq_project_party_project_key_role UNIQUE (project_id, party_key, role),
    CONSTRAINT ck_project_party_role CHECK (
        role IN ('developer', 'owner', 'operator', 'customer', 'utility', 'contractor')
    ),
    CONSTRAINT ck_project_party_name CHECK (length(name) > 0),
    CONSTRAINT ck_project_party_key CHECK (length(party_key) > 0)
);

CREATE INDEX ix_project_party_project_id ON project_party (project_id);

-- Queried by key across projects, which is the whole point of the table: "every
-- site OpenAI occupies" and "every row naming this party in one locality", the
-- second being what raises a duplicate the company column cannot.
CREATE INDEX ix_project_party_party_key ON project_party (party_key);

-- JSON array on the source: [{"name", "role", "quote"}, ...], gated per entry
-- before it is written. Sibling to `claims`, `quotes`, `blocks` -- see above.
ALTER TABLE source ADD COLUMN parties TEXT;
