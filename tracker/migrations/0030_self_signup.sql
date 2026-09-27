-- 0030_self_signup: anyone may ask for an account; an administrator decides who gets one.
--
-- Accounts used to come from two places only: `tracker users add` at the host, and
-- an invite code minted there. The sign-in page now also takes a plain sign-up —
-- an email and a password, no code — and that path is deliberately slower than the
-- other two, because it is the one a stranger can use:
--
--   1. the address is confirmed by a link mailed to it (`email_verified_at`), since
--      the morning email goes there and must belong to the person asking;
--   2. an administrator approves the account (`approved_at`) before it can sign in,
--      because every account reads the whole dataset.
--
-- An invite code skips both, as the operator decided: somebody holding a code was
-- chosen at the terminal already. So does `tracker users add`.
--
-- **`approved_at` is the one question sign-in asks.** NULL means the account may
-- not sign in yet; a time says when it was let in. Every account that existed
-- before this migration was let in when it was made, and is backfilled so.
--
-- **`ai_allowed` is per account now.** The model panels spend tokens on every
-- click, and an account a stranger made should not be able to run up that bill.
-- Invited accounts and those made at the terminal have it; a plain sign-up does
-- not until an administrator switches it on. The console-wide `--ai` flag still
-- decides whether the panels exist at all. Every existing account keeps the
-- panels it had.
--
-- **`self_signup`** records that the account came from the sign-up form, for the
-- admin page's "joined" line; an invite is already recorded on `invite`.
--
-- **`account_token`: one row per link mailed.** A confirmation link or a password
-- reset link carries a random token; only its sha256 is stored, for the reason an
-- invite's code is only stored hashed — this database travels between machines
-- and sits in backups. Single use (`used_at`), and each has its own expiry. The
-- rows are also the per-address throttle: how many links an address was sent in
-- the last hour is a count here.

ALTER TABLE account ADD COLUMN email_verified_at DATETIME;
ALTER TABLE account ADD COLUMN approved_at DATETIME;
ALTER TABLE account ADD COLUMN ai_allowed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE account ADD COLUMN self_signup INTEGER NOT NULL DEFAULT 0;

UPDATE account SET approved_at = created_at, ai_allowed = 1;

CREATE TABLE account_token (
    id           INTEGER  PRIMARY KEY,
    account_id   INTEGER  NOT NULL REFERENCES account (id) ON DELETE CASCADE,
    -- `confirm` proves the address; `reset` sets a forgotten password.
    purpose      TEXT     NOT NULL,
    token_hash   TEXT     NOT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at   DATETIME NOT NULL,
    used_at      DATETIME,

    CONSTRAINT uq_account_token_hash UNIQUE (token_hash),
    CONSTRAINT ck_account_token_purpose CHECK (purpose IN ('confirm', 'reset'))
);

CREATE INDEX ix_account_token_account ON account_token (account_id, purpose, created_at);
