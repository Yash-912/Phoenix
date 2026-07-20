CREATE TABLE incidents (
    id BIGSERIAL PRIMARY KEY,
    service_name TEXT NOT NULL,
    alertname TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'resolved')),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    alert_count INTEGER NOT NULL DEFAULT 1,
    raw_payload JSONB NOT NULL
);

-- Only one ACTIVE incident per (service, alertname) may exist at a time.
-- This partial unique index is what makes dedup atomic and race-safe —
-- see the FastAPI layer's INSERT ... ON CONFLICT, which targets this exact index.
CREATE UNIQUE INDEX idx_incidents_active_dedup
    ON incidents (service_name, alertname)
    WHERE status = 'active';
