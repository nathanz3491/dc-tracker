-- 0036_api_key: keys for the read-only public API.
--
-- A key belongs to one account and reads what that account reads: the same
-- dataset, and the account's own watchlist for `/v1/updates`. A person makes and
-- revokes their keys on their own Account page; nothing else creates one.
--
-- **Only the key's sha256 is stored**, like an invite's code and a mailed link's
-- token. The database travels between machines (`scripts/sync_db.py`), so a
-- stored key would be a working credential in every copy. A key is 256 bits of
-- randomness, so a plain hash is enough: there is nothing to guess, which is what
-- a slow password hash protects against.
--
-- `prefix` is the first characters of the key, kept in the clear so a person can
-- tell their keys apart in a list and match one against a log. It is too short to
-- use.
--
-- Revoking sets `revoked_at` rather than deleting the row, so a key that stops
-- working still shows when it was made, last used and turned off. Deleting the
-- account deletes its keys; disabling it stops them working (`apikeys.verify`).

CREATE TABLE api_key (
    id            INTEGER  PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER  NOT NULL REFERENCES account (id) ON DELETE CASCADE,
    name          TEXT     NOT NULL,
    prefix        TEXT     NOT NULL,
    key_hash      TEXT     NOT NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at  DATETIME,
    revoked_at    DATETIME,

    CONSTRAINT uq_api_key_hash UNIQUE (key_hash),
    CONSTRAINT ck_api_key_name CHECK (length(trim(name)) BETWEEN 1 AND 60)
);

CREATE INDEX ix_api_key_account ON api_key (account_id, revoked_at);
