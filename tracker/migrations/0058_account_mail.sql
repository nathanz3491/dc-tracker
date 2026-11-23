-- 0058_account_mail: every email the console sends about an account, and how it ended.
--
-- The morning mailer has kept a ledger since 0029, and each run tells the
-- administrators what failed. The console's own emails had none: a sign-up's
-- confirmation link, a password reset, "your account is ready", an administrator's
-- "new sign-up waiting". Each got one attempt, and a failure went to the console's
-- log and nowhere else, so a confirmation that never arrived looked exactly like
-- somebody who changed their mind.
--
-- One row per email, written once it was tried (`account_mail.send`): `sent` with
-- Resend's id, or `failed` with the reason. The morning run asks Resend what became
-- of the past week's sent ones, as it does for its own, and reports every failure it
-- has not reported yet, in the administrators' alert and to the outside monitor
-- (`notify.check_account_mail`). `reported_at` is what makes it say so once.
--
-- `purpose` names the email: `confirm`, `registered` (a sign-up with an address that
-- already has an account), `reset`, `approved`, `signup-waiting` (to an
-- administrator). Not a CHECK: a new kind of account email should not need a table
-- rebuild, and an unknown one is still reported, as "an account email".
--
-- `account_id` is the recipient's account when there is one, and goes with it, as
-- `notify_delivery` does: deleting an account deletes the record of what was mailed
-- to it.

CREATE TABLE account_mail (
    id           INTEGER  PRIMARY KEY,
    account_id   INTEGER  REFERENCES account (id) ON DELETE CASCADE,
    email        TEXT     NOT NULL,
    purpose      TEXT     NOT NULL,
    subject      TEXT,
    status       TEXT     NOT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    message_id   TEXT,
    attempts     INTEGER  NOT NULL DEFAULT 0,
    error        TEXT,
    reported_at  DATETIME,

    CONSTRAINT ck_account_mail_status CHECK (status IN ('sent', 'failed'))
);

CREATE INDEX ix_account_mail_created ON account_mail (created_at);
