-- 0044_party_tenure: a party whose role on a campus has ended.
--
-- A campus's parties are rebuilt from its sources, and a source that once named a
-- tenant keeps naming it after the tenant has gone: Cyxtera at Prime's Comstock
-- campus, whose lease was rejected in Cyxtera's 2023 bankruptcy, was still credited
-- the campus as its buyer, because nothing could say "former". The rows are a cache,
-- so the statement cannot live on them; it lives here, keyed the way the rows are —
-- (project, party_key, role) — and is copied onto each rebuilt row as `ended_on` and
-- `ended_reason`.
--
-- An ended party stays on the page, marked former, and stays evidence of which
-- campus a row is (a former tenant still names the site). It stops being a current
-- buyer: attribution, Capex and the customer fill read only parties whose role has
-- not ended. `ended_on` may be unknown; the reason may not.

CREATE TABLE party_tenure (
    id          INTEGER  PRIMARY KEY,
    project_id  INTEGER  NOT NULL REFERENCES project (id) ON DELETE CASCADE,
    party_key   TEXT     NOT NULL,
    role        TEXT     NOT NULL,
    ended_on    DATE,
    reason      TEXT     NOT NULL,
    source_id   INTEGER  REFERENCES source (id) ON DELETE SET NULL,
    decided_by  TEXT     NOT NULL DEFAULT 'operator',
    decided_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_party_tenure_project_key_role UNIQUE (project_id, party_key, role),
    CONSTRAINT ck_party_tenure_role
        CHECK (role IN ('developer', 'owner', 'operator', 'customer', 'utility', 'contractor')),
    CONSTRAINT ck_party_tenure_key CHECK (length(party_key) > 0),
    CONSTRAINT ck_party_tenure_reason CHECK (length(reason) > 0)
);

CREATE INDEX ix_party_tenure_project_id ON party_tenure (project_id);

ALTER TABLE project_party ADD COLUMN ended_on DATE;
ALTER TABLE project_party ADD COLUMN ended_reason TEXT;
