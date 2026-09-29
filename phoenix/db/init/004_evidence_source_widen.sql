-- Widen evidence.source to cover the 5 observer tools.
-- Original CHECK in 002 allowed only ('prometheus','loki','docker');
-- observer now records tool names: query_prometheus, query_loki,
-- get_container_state, inspect_health, get_recent_deployments.
ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_source_check;
ALTER TABLE evidence ADD CONSTRAINT evidence_source_check CHECK (
    source IN (
        'prometheus', 'loki', 'docker',
        'query_prometheus', 'query_loki', 'get_container_state',
        'inspect_health', 'get_recent_deployments'
    )
);
