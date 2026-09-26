-- 0029_notify_ledger: when a fact really entered the database, and what each
-- person has already been sent.
--
-- **`recorded_at`, because `created_at` turned out to answer a different
-- question.** Migration 0018 stamped a milestone or obstacle with its citation's
-- `fetched_at`, on the argument that the fetch is the moment we learned the fact.
-- It is not: enrichment and the overnight loop re-read cached articles, so a fact
-- is routinely *written* days or weeks after its page was fetched, and arrives
-- carrying the old date. Measured on the live database, 2026-08-31 to 09-25: 46
-- of 246 new milestones and 21 of 50 new obstacles were written more than three
-- days after their `created_at`. A mailer that asks "what did we learn since the
-- last run" can never see those rows, and it did not — a quoted, material
-- permitting obstacle stamped 09-02 was written after 09-22 and was never mailed.
-- `created_at` keeps its meaning (the fetch); `recorded_at` is the row's insert.
--
-- **The backfill is a lower bound, not a guess.** Ids are handed out in insert
-- order, and a row cannot be inserted before its own page was fetched, so row N
-- was inserted no earlier than the latest `created_at` among rows 1..N. That is
-- later than — and closer to the truth than — the row's own `created_at` wherever
-- the row was backdated, and equal to it wherever it was not. Rows with no
-- `created_at` take the same bound; NULL only where nothing precedes. (The bound
-- can overshoot only for rows older than 0018, whose `created_at` that migration
-- copied from a fetch that may postdate the row. Those are months old and no rule
-- that reads this column reaches back that far.)
--
-- **`closed_at` is when we recorded an obstacle leaving `open`.** `resolved_at` is
-- a date, and nothing recorded the moment of the change, so "cleared since the
-- last email" could only be approximated to the day. Backfilled from
-- `resolved_at`, which every current writer sets to the day it resolves the row.
--
-- **`notify_run`, `notify_delivery`, `notify_sent`: the mailer's memory.** The
-- mailer had none; it chose what to send by the clock ("the last N days"), so a
-- missed run lost its updates, a reboot moved the window, a re-run sent everything
-- twice, and one refused recipient stopped the rest. Now every run is a row, every
-- message attempted is a delivery (sent or failed, with the provider's id or
-- error), and every update a person was sent is keyed to them, so no update is
-- sent to anybody twice and anything unsent is still owed. `tracker notify status`
-- reads these; nothing else writes them.

ALTER TABLE event ADD COLUMN recorded_at DATETIME;

ALTER TABLE risk ADD COLUMN recorded_at DATETIME;

ALTER TABLE risk ADD COLUMN closed_at DATETIME;

UPDATE event
   SET recorded_at = (SELECT MAX(e2.created_at) FROM event e2 WHERE e2.id <= event.id);

UPDATE risk
   SET recorded_at = (SELECT MAX(r2.created_at) FROM risk r2 WHERE r2.id <= risk.id);

UPDATE risk
   SET closed_at = resolved_at
 WHERE status <> 'open' AND resolved_at IS NOT NULL;

CREATE INDEX ix_event_recorded_at ON event (recorded_at);

CREATE INDEX ix_risk_recorded_at ON risk (recorded_at);

CREATE TABLE notify_run (
    id           INTEGER  PRIMARY KEY,
    started_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- NULL while running, and forever if the process died mid-run: that is the
    -- signature of a crash, and `notify status` reports it as one.
    finished_at  DATETIME,
    sent         INTEGER  NOT NULL DEFAULT 0,
    failed       INTEGER  NOT NULL DEFAULT 0,
    skipped      INTEGER  NOT NULL DEFAULT 0
);

CREATE TABLE notify_delivery (
    id           INTEGER  PRIMARY KEY,
    run_id       INTEGER  REFERENCES notify_run (id) ON DELETE SET NULL,
    account_id   INTEGER  REFERENCES account (id) ON DELETE CASCADE,
    -- The address it went to, as it was then. An account's address can change.
    email        TEXT     NOT NULL,
    -- `updates` carries news, `quiet` is the blockers-only message on a day with
    -- none, `alert` tells an administrator a run failed somebody.
    kind         TEXT     NOT NULL,
    status       TEXT     NOT NULL,
    -- The clock the selection was made against. The next "new since your last
    -- email" starts here, not at `sent_at`, so a fact recorded while this message
    -- was being built and posted is not skipped.
    prepared_at  DATETIME NOT NULL,
    sent_at      DATETIME,
    updates      INTEGER  NOT NULL DEFAULT 0,
    blockers     INTEGER  NOT NULL DEFAULT 0,
    subject      TEXT,
    message_id   TEXT,
    attempts     INTEGER  NOT NULL DEFAULT 0,
    error        TEXT,

    CONSTRAINT ck_notify_delivery_kind CHECK (kind IN ('updates', 'quiet', 'alert')),
    CONSTRAINT ck_notify_delivery_status CHECK (status IN ('sent', 'failed'))
);

CREATE INDEX ix_notify_delivery_account ON notify_delivery (account_id, prepared_at);

CREATE TABLE notify_sent (
    id           INTEGER  PRIMARY KEY,
    account_id   INTEGER  NOT NULL REFERENCES account (id) ON DELETE CASCADE,
    -- `feed.Signal.key`: `event:<id>`, `risk:<id>:opened`, `risk:<id>:cleared`.
    signal_key   TEXT     NOT NULL,
    delivery_id  INTEGER  NOT NULL REFERENCES notify_delivery (id) ON DELETE CASCADE,

    CONSTRAINT uq_notify_sent_account_signal UNIQUE (account_id, signal_key)
);
