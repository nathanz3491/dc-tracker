-- 0021_watch_owner: a watchlist belongs to a person, not to the database.
--
-- 0019 introduced `watch` as one list, because there was one reader. Its own
-- argument for being a table rather than a file was that "it is one reader's
-- current interest, expected to turn over monthly, and it is edited by the person
-- reading the console" — and that argument, taken one step further, says the row
-- has an owner. With several accounts on a published console, a shared list shows
-- everybody everybody else's interests and lets any of them delete yours.
--
-- So `account_id` joins the identity, and the uniqueness rule moves with it:
-- `(company_key, project_key)` becomes `(account_id, company_key, project_key)`.
-- Two people watching xAI is two rows and not a conflict.
--
-- **Nothing is copied, and that is a measurement rather than a shrug.** `watch`
-- held 0 rows when this was written, so there is no entry to attribute and no
-- data to lose. It matters that it was checked: with rows present there would be
-- no honest owner to give them to — the console deliberately has no shared list,
-- so "everybody's" is not an available answer, and inventing a placeholder
-- account to hold them would put a credential-less row in `account` purely to
-- satisfy a foreign key.
--
-- **The rebuild, rather than an ALTER.** SQLite cannot add a NOT NULL column
-- without a default and cannot change a UNIQUE constraint in place, so the table
-- is recreated. Safe for the same reason 0011 gave: no foreign key points *at*
-- `watch`, so a copy-and-rename cannot orphan anything. The one pointing out, to
-- `account`, resolves because 0020 ran first.
--
-- **ON DELETE CASCADE, on purpose.** Deleting an account should take their
-- watchlist with it: it is a statement of that person's interest and means
-- nothing without them. `foreign_keys = ON` is set per connection in
-- `db._apply_pragmas`, so this is enforced rather than decorative.

CREATE TABLE watch_new (
    id           INTEGER PRIMARY KEY,

    -- Whose list this is. NOT NULL: an unowned watch is a shared list by another
    -- name, and the reason this migration exists is that there is no such thing.
    account_id   INTEGER NOT NULL REFERENCES account (id) ON DELETE CASCADE,

    entry        TEXT    NOT NULL,
    company_key  TEXT    NOT NULL,
    project_key  TEXT    NOT NULL DEFAULT '',
    note         TEXT,
    added_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Per account, so two people may watch the same company. `project_key` is
    -- still '' rather than NULL for "the whole company", for the reason 0019
    -- spelled out: SQLite treats NULLs as distinct, so a nullable column would
    -- let one account insert "watch xAI" twice.
    CONSTRAINT uq_watch_entity UNIQUE (account_id, company_key, project_key),
    CONSTRAINT ck_watch_company_key CHECK (length(company_key) > 0)
);

DROP TABLE watch;

ALTER TABLE watch_new RENAME TO watch;

-- Dropped with the old table. `company_key` keeps its index for the resolve
-- path, and `account_id` gets one because every console read starts by selecting
-- one account's rows.
CREATE INDEX ix_watch_company_key ON watch (company_key);

CREATE INDEX ix_watch_account_id ON watch (account_id);
