-- 0054_console_session: console sign-ins that outlive a restart.
--
-- The console kept its sessions in memory, and the host's deployer restarts it on
-- every commit, so every deploy signed every reader out: eleven deploys on the night
-- of 2026-11-09 alone. A sign-in is now also written here, so a restarted console
-- that does not recognise a cookie looks it up once and carries on.
--
-- `token_hash` is the SHA-256 of the cookie's token, never the token: a copy of this
-- file (`scripts/sync_db.py` pulls one to the dev machine) holds nothing that signs
-- anybody in. `stamp` is the digest of the credential the session was granted on, the
-- same one the console checks every few seconds, so a password changed, an account
-- deleted or `tracker users signout` still ends the session after a restart.
-- Rows past `expires_at` are deleted as new sessions are written.

CREATE TABLE console_session (
    token_hash     TEXT     PRIMARY KEY,
    account_id     INTEGER  NOT NULL REFERENCES account (id) ON DELETE CASCADE,
    stamp          TEXT     NOT NULL,
    expires_at     DATETIME NOT NULL,
    created_at     DATETIME NOT NULL
);
CREATE INDEX ix_console_session_expires ON console_session (expires_at);
