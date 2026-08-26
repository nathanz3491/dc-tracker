-- 0020_accounts: who is reading the console, and how somebody new gets in.
--
-- The console was gated by one shared password read from the environment
-- (`TRACKER_CONSOLE_PASSWORD`). That is the right shape for one operator on
-- loopback and the wrong shape for several people reading a published console:
-- everybody signs in as the same principal, so the page cannot tell them apart,
-- and the watchlist it draws is therefore a property of the database rather than
-- of the reader. An account is what makes "my watchlist" a sentence that means
-- something.
--
-- **The identity is an email, stored twice.** `email` is what somebody typed and
-- is what gets shown back to them; `email_key` is the trimmed lowercase form and
-- is what UNIQUE and every lookup use, so "Alice@Ex.com" and "alice@ex.com" are
-- one account rather than two. Keeping both is the same choice `watch` makes for
-- `entry`/`company_key` and `project` for `dedup_key`: a normalized key cannot be
-- displayed, and text as typed cannot be matched.
--
-- **The CHECK on the email is deliberately weak** — a non-empty local part, an
-- `@`, a non-empty domain, and nothing else. A schema constraint that refuses an
-- address is one nobody can work around without a migration, and the useful
-- version of that check is the one in `tracker/accounts.py`, where the refusal
-- can say what was wrong with it.
--
-- **`last_seen_at` is separate from `created_at`** for the reason
-- `project.last_verified_at` is separate from `updated_at`: they answer different
-- questions, and collapsing them loses the one somebody actually asks ("is this
-- account still in use, or is it a leftover?").
--
-- **Nothing here is a role.** Every account can do exactly what the shared
-- password allowed, which after this change is: read the dataset, and keep a
-- watchlist. What the console may do at all stays a property of the *server* —
-- the `--ai` and `--watch-edits` flags — because that is a decision made by
-- whoever started it, not by whoever signed in.

CREATE TABLE account (
    id             INTEGER PRIMARY KEY,

    -- As typed. Display only; never matched on.
    email          TEXT     NOT NULL,

    -- Trimmed and lowercased. The identity, and the only thing looked up.
    email_key      TEXT     NOT NULL,

    -- Optional display name. Absent is normal and fine — the email is the label.
    name           TEXT,

    -- `scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`, written by
    -- `tracker.accounts.hash_password`. Self-describing so the cost parameters
    -- can be raised later without a migration and rows written under the old
    -- ones keep verifying: `source.extractor` is a versioned self-describing
    -- string for exactly this reason.
    password_hash  TEXT     NOT NULL,

    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at   DATETIME,

    CONSTRAINT uq_account_email_key UNIQUE (email_key),
    CONSTRAINT ck_account_email_key CHECK (email_key = lower(email_key) AND email_key LIKE '_%@_%'),
    CONSTRAINT ck_account_email CHECK (length(email) > 0),
    CONSTRAINT ck_account_password_hash CHECK (length(password_hash) > 0)
);

-- One single-use code that lets somebody create their own account.
--
-- **Why invites rather than open sign-up.** Behind a tunnel the login page is a
-- public URL. Open registration there hands an account to anyone who finds it,
-- and while an account can no longer run a command it can still read the whole
-- dataset. So creating one is either an act at the terminal (`tracker users
-- add`) or the redemption of a code somebody at the terminal minted.
--
-- **The code is hashed, never stored.** This database travels between machines
-- through `scripts/sync_db.py` and sits in `backups/`; a plaintext code in it is
-- a live credential in every copy, which is the same argument that keeps
-- `password_hash` a hash. The code is shown once, by the command that mints it,
-- and is not recoverable afterwards.
--
-- **`redeemed_at` alone is the spent flag.** `redeemed_by` is an audit link and
-- goes NULL if that account is later deleted, so the two are deliberately *not*
-- constrained to agree — a paired CHECK would make `ON DELETE SET NULL` fail and
-- turn deleting an account into a foreign-key error.
CREATE TABLE invite (
    id           INTEGER PRIMARY KEY,

    -- sha256 of the code, hex. Not scrypt: a 160-bit random code has no
    -- guessable keyspace to slow an attacker down in, so the salt and the work
    -- factor would buy nothing a password needs them for.
    code_hash    TEXT     NOT NULL,

    -- Who it was minted for, free text, so a list of outstanding invites is
    -- readable a fortnight later.
    note         TEXT,

    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Not nullable: an invite that never expires is a permanent hole, and the
    -- command that mints one defaults it rather than asking.
    expires_at   DATETIME NOT NULL,

    redeemed_at  DATETIME,
    redeemed_by  INTEGER REFERENCES account (id) ON DELETE SET NULL,

    CONSTRAINT uq_invite_code_hash UNIQUE (code_hash),
    CONSTRAINT ck_invite_code_hash CHECK (length(code_hash) > 0)
);

CREATE INDEX ix_invite_redeemed_at ON invite (redeemed_at);
