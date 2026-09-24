-- 0028_account_admin: one role, a switch to lock an account, and a way to end its sessions.
--
-- Migration 0020 made a point of having no roles: every account could read the
-- dataset and keep a watchlist, and anything more was a flag on the server. That
-- held while accounts were managed only at a terminal on the host. The operator
-- now manages them from the console as well — editing an address, locking an
-- account, signing somebody out — and a console page that can do that has to know
-- who may use it. So there is exactly one role, `is_admin`, and it gates nothing
-- but account management. It is granted and revoked only by `tracker users admin`
-- on the host: a stolen admin session can edit accounts, but it cannot make more
-- admins.
--
-- **`disabled_at`, not a delete.** Locking somebody out used to mean deleting the
-- account, and the cascade took their watchlist with it. A disabled account keeps
-- everything and cannot sign in; its open sessions end within the gate's confirm
-- interval, the same way a deleted one's do. NULL means enabled, and the time says
-- when it was switched off.
--
-- **`session_epoch` ends every session without touching the password.** A console
-- session remembers a digest of the credential it was granted on and re-checks it
-- against the row every few seconds (`accounts.session_stamp`). The epoch is
-- folded into that digest, so raising it by one makes every outstanding session's
-- stamp stale: "sign out everywhere" is one integer write, from either process.
--
-- **`updated_at`** is when an operator last changed the account — address, name,
-- role, status, password — for the detail view. NULL for a row nobody has edited.

ALTER TABLE account ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0;
ALTER TABLE account ADD COLUMN disabled_at DATETIME;
ALTER TABLE account ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE account ADD COLUMN updated_at DATETIME;
