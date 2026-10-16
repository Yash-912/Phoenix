-- Approval requests for actions the execution policy holds back (Phase 7).
--
-- One row per request. The graph writes it before pausing, and the API's
-- decision endpoint is the only other writer: it flips status from 'pending'
-- once, and the WHERE clause on that UPDATE is what makes the first answer
-- win. A second answer matches no row and changes nothing.
--
-- request_key makes the graph's own write idempotent. A paused node runs again
-- from its top when it resumes, so it asks for the same request a second time;
-- the key returns the row it already has rather than opening a new one.
--
-- expires_at is set when the row is written and never moved. The decision
-- UPDATE refuses an expired row, and a sweep turns an unanswered expired row
-- into 'expired'. Expiry is an escalation on the graph side, never an approval.
--
-- The only UPDATE the application runs is the one transition out of 'pending'
-- (a decision, or the expiry sweep). Nothing rewrites a decided row.
CREATE TABLE approvals (
    id BIGSERIAL PRIMARY KEY,
    incident_id BIGINT NOT NULL REFERENCES incidents(id),
    request_key TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    container TEXT NOT NULL,
    args JSONB NOT NULL,
    tier INTEGER NOT NULL CHECK (tier IN (1, 2)),
    incident_summary TEXT NOT NULL,
    plan_reasoning TEXT NOT NULL,
    evidence JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    decided_at TIMESTAMPTZ,
    decided_by TEXT,
    decision_channel TEXT,
    CHECK ((status = 'pending') = (decided_at IS NULL))
);

CREATE INDEX idx_approvals_incident ON approvals (incident_id);
CREATE INDEX idx_approvals_pending ON approvals (expires_at) WHERE status = 'pending';

GRANT SELECT, INSERT, UPDATE ON approvals TO phoenix_app;
GRANT USAGE, SELECT ON SEQUENCE approvals_id_seq TO phoenix_app;
