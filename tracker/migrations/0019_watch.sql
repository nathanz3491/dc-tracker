-- 0019_watch: the entities whose news the updates page is about.
--
-- The console's landing page used to describe the dataset. It now answers a
-- narrower and more useful question — what changed, on the things I named, since
-- I last looked — and that requires storing which things those are.
--
-- **A table, not a file, and the split this repo already draws decides it.**
-- `seed/required-projects.txt` is a file because it encodes the PRD's definition
-- of done: it belongs to the specification, changes when the contract changes,
-- and wants to be in a diff. A watchlist is the opposite: it is one reader's
-- current interest, expected to turn over monthly ("this month I care about
-- these two, next month maybe another"), and it is edited by the person reading
-- the console rather than by whoever is editing the code. That makes it data,
-- and data is made on the host and travels in the database.
--
-- **Matching is by normalized key, kept beside the text as typed.** `entry` is
-- what somebody wrote — "xAI" or "xAI | Colossus" — and is what gets shown back
-- to them. `company_key` is `dedup.company_key()` of the company part, the same
-- normalization the dedup identity and `required.match` use, so "Microsoft
-- Corporation" and "Microsoft" are one watch rather than two. Storing both is
-- deliberate: a key alone cannot be displayed, and text alone cannot be matched.
--
-- **`project_key` is '' rather than NULL for "the whole company", and that is
-- the UNIQUE constraint's doing.** SQLite treats NULLs as distinct, so a
-- nullable column would let "watch xAI" be inserted twice — precisely the
-- duplicate this constraint exists to refuse. `risk` met the same problem with
-- `first_seen` and solved it in Python because a real date could not be
-- invented; here the empty string is not an invented value, it is exactly what
-- "no project named" means.

CREATE TABLE watch (
    id           INTEGER PRIMARY KEY,

    -- As typed, including the separator: "xAI" or "xAI | Colossus".
    entry        TEXT    NOT NULL,

    -- dedup.company_key() of the company part. Never empty: a watch on a bare
    -- project name with no company would match across operators, which is not a
    -- watchlist, it is a search.
    company_key  TEXT    NOT NULL,

    -- Lowercased project name, or '' for every project of the company. Matched
    -- as a substring in either direction, exactly like `required.match`.
    project_key  TEXT    NOT NULL DEFAULT '',

    -- Free text: why this is being watched. Shown on the digest so a list
    -- somebody else edited still explains itself.
    note         TEXT,

    added_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_watch_entity UNIQUE (company_key, project_key),
    CONSTRAINT ck_watch_company_key CHECK (length(company_key) > 0)
);

CREATE INDEX ix_watch_company_key ON watch (company_key);
