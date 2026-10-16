-- 0037_api_allowed: whether an account may use the public API, set per account
-- by an administrator.
--
-- Same shape and the same starting rule as `ai_allowed` (0030). Accounts made at
-- the terminal or by invite have it; a plain sign-up does not until an
-- administrator switches it on, since a stranger's key is a stranger reading the
-- whole dataset by program. Every account that exists now keeps the access it
-- had, which was all of it: until this migration any active account could make
-- keys.
--
-- **Switching it off stops the keys, it does not revoke them.** `apikeys.verify`
-- reads this on every request, so the account's keys stop working at once and
-- work again when it is switched back on, without the owner having to make and
-- redistribute new ones. The account cannot make new keys while it is off, and
-- can still revoke the ones it has.

ALTER TABLE account ADD COLUMN api_allowed INTEGER NOT NULL DEFAULT 0;

UPDATE account SET api_allowed = 1;
