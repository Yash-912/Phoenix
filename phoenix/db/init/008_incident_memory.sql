-- Incident memory (PRD section 11): one compact structured record per incident
-- that reached a terminal state. Written once by phoenix/graph/memory.py and
-- read back only to note "this matches incident #X" in the trail. It is
-- supporting context, never an input to scoring or action selection.
--
-- incident_id is the primary key, so a second write for the same incident is
-- a no-op rather than a second record -- the first terminal outcome is the one
-- kept, the same way the audit trail keeps what was first recorded.
CREATE TABLE incident_memory (
    incident_id BIGINT PRIMARY KEY REFERENCES incidents(id),
    service_name TEXT NOT NULL,
    root_cause_category TEXT,
    status TEXT NOT NULL,
    record JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_incident_memory_match
    ON incident_memory (service_name, root_cause_category, incident_id DESC);

-- Insert and read only. No UPDATE or DELETE: like audit_log, a record that is
-- wrong is superseded by a new incident's record, not edited in place.
-- This file runs on a fresh volume after 003 (alphabetical init order) and on
-- an existing one when applied by hand, so the grant lives here, not in 003.
GRANT SELECT, INSERT ON incident_memory TO phoenix_app;
