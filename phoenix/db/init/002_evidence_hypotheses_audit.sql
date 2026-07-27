CREATE TABLE evidence (
    id BIGSERIAL PRIMARY KEY,
    incident_id BIGINT NOT NULL REFERENCES incidents(id),
    source TEXT NOT NULL CHECK (source IN ('prometheus', 'loki', 'docker')),
    iteration INTEGER NOT NULL DEFAULT 1,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    summary TEXT NOT NULL,
    raw_data JSONB NOT NULL
);

CREATE INDEX idx_evidence_incident ON evidence (incident_id);

CREATE TABLE hypotheses (
    id BIGSERIAL PRIMARY KEY,
    incident_id BIGINT NOT NULL REFERENCES incidents(id),
    iteration INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    description TEXT NOT NULL,
    score NUMERIC NOT NULL,
    score_breakdown JSONB NOT NULL
);

CREATE INDEX idx_hypotheses_incident ON hypotheses (incident_id);

-- Append-only by design: no updated_at, no soft-delete flag.
-- Immutability is enforced at the ROLE level (see 003_create_app_role.sh),
-- not by omitting UPDATE/DELETE statements from application code.
CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    incident_id BIGINT NOT NULL REFERENCES incidents(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    node TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail JSONB NOT NULL,
    reasoning_text TEXT
);

CREATE INDEX idx_audit_log_incident ON audit_log (incident_id);
