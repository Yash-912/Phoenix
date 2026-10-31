import os

# pyrefly: ignore [missing-import]
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False)

UPSERT_ACTIVE_INCIDENT = """
INSERT INTO incidents (service_name, alertname, severity, raw_payload)
VALUES (%(service_name)s, %(alertname)s, %(severity)s, %(raw_payload)s)
ON CONFLICT (service_name, alertname) WHERE status = 'active'
DO UPDATE SET
    last_seen_at = now(),
    alert_count = incidents.alert_count + 1,
    raw_payload = EXCLUDED.raw_payload
RETURNING id, (xmax = 0) AS inserted;
"""

RESOLVE_INCIDENT = """
UPDATE incidents
SET status = 'resolved', last_seen_at = now()
WHERE service_name = %(service_name)s
  AND alertname = %(alertname)s
  AND status = 'active'
RETURNING id;
"""

# incidents.status is the Alertmanager alert lifecycle (active or resolved), not
# the investigation outcome. The outcome lives in incident_memory.status, and is
# null until the investigation has finished.
LIST_INCIDENTS = """
SELECT i.id, i.service_name, i.alertname, i.severity, i.status,
       m.status AS investigation_status,
       i.first_seen_at, i.last_seen_at, i.alert_count
FROM incidents i
LEFT JOIN incident_memory m ON m.incident_id = i.id
ORDER BY i.first_seen_at DESC;
"""


def upsert_active_incident(service_name: str, alertname: str, severity: str, raw_payload: dict) -> tuple[int, bool]:
    with pool.connection() as conn:
        row = conn.execute(
            UPSERT_ACTIVE_INCIDENT,
            {
                "service_name": service_name,
                "alertname": alertname,
                "severity": severity,
                "raw_payload": Jsonb(raw_payload),
            },
        ).fetchone()
        return row[0], row[1]


def resolve_incident(service_name: str, alertname: str) -> int | None:
    with pool.connection() as conn:
        row = conn.execute(
            RESOLVE_INCIDENT,
            {"service_name": service_name, "alertname": alertname},
        ).fetchone()
        return row[0] if row else None


def list_incidents() -> list[dict]:
    columns = [
        "id", "service_name", "alertname", "severity", "status",
        "investigation_status", "first_seen_at", "last_seen_at", "alert_count",
    ]
    with pool.connection() as conn:
        rows = conn.execute(LIST_INCIDENTS).fetchall()
    return [dict(zip(columns, row)) for row in rows]


# --- dashboard reads ---------------------------------------------------------
# Everything below is a SELECT. The dashboard never writes through these.

# The list the dashboard shows: the alert state beside the investigation outcome. Only
# terminal runs reach incident_memory, so a model outage, a crashed investigation and a
# run paused for approval are read from the audit trail and the approvals table instead.
OVERVIEW = """
SELECT i.id, i.service_name, i.alertname, i.severity, i.status,
       i.first_seen_at, i.last_seen_at, i.alert_count,
       m.status AS investigation_status,
       m.root_cause_category,
       (m.record ->> 'tier_used')::int AS tier_used,
       EXISTS (SELECT 1 FROM audit_log a WHERE a.incident_id = i.id AND a.event_type = 'llm_unavailable') AS llm_unavailable,
       EXISTS (SELECT 1 FROM audit_log a WHERE a.incident_id = i.id AND a.event_type = 'investigation_failed') AS investigation_failed,
       EXISTS (SELECT 1 FROM audit_log a WHERE a.incident_id = i.id AND a.event_type = 'symptom_cleared') AS symptom_cleared,
       (SELECT count(*) FROM approvals p
         WHERE p.incident_id = i.id AND p.status = 'pending' AND p.expires_at > now()) AS pending_approvals,
       (SELECT count(*) FROM audit_log a WHERE a.incident_id = i.id) AS audit_events
FROM incidents i
LEFT JOIN incident_memory m ON m.incident_id = i.id
ORDER BY i.first_seen_at DESC
LIMIT %(limit)s;
"""

TRACE_INCIDENT = "SELECT id, service_name, alertname, severity, status, first_seen_at, last_seen_at, alert_count FROM incidents WHERE id = %(id)s;"
TRACE_EVIDENCE = "SELECT id, source, iteration, collected_at, summary, raw_data FROM evidence WHERE incident_id = %(id)s ORDER BY id;"
TRACE_HYPOTHESES = "SELECT id, iteration, created_at, description, score, score_breakdown FROM hypotheses WHERE incident_id = %(id)s ORDER BY id;"
TRACE_AUDIT = "SELECT id, created_at, node, event_type, detail, reasoning_text FROM audit_log WHERE incident_id = %(id)s ORDER BY id;"
TRACE_APPROVALS = """
SELECT id, action, container, args, tier, status, incident_summary, plan_reasoning, evidence,
       requested_at, expires_at, decided_at, decided_by, decision_channel
FROM approvals WHERE incident_id = %(id)s ORDER BY id;
"""
TRACE_MEMORY = "SELECT incident_id, service_name, root_cause_category, status, record, created_at FROM incident_memory WHERE incident_id = %(id)s;"

PENDING_APPROVALS = """
SELECT p.id, p.incident_id, i.service_name, p.action, p.container, p.args, p.tier, p.status,
       p.incident_summary, p.plan_reasoning, p.evidence, p.requested_at, p.expires_at,
       p.decided_at, p.decided_by, p.decision_channel
FROM approvals p JOIN incidents i ON i.id = p.incident_id
WHERE p.status = 'pending' AND p.expires_at > now()
ORDER BY p.requested_at DESC;
"""


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    cursor = conn.execute(sql, params or {})
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def overview(limit: int = 200) -> list[dict]:
    with pool.connection() as conn:
        return _rows(conn, OVERVIEW, {"limit": limit})


def incident_rows(incident_id: int) -> dict | None:
    """Every row Phoenix wrote for one incident, or None when there is no such incident."""
    params = {"id": incident_id}
    with pool.connection() as conn:
        incident = _rows(conn, TRACE_INCIDENT, params)
        if not incident:
            return None
        memory = _rows(conn, TRACE_MEMORY, params)
        return {
            "incident": incident[0],
            "evidence": _rows(conn, TRACE_EVIDENCE, params),
            "hypotheses": _rows(conn, TRACE_HYPOTHESES, params),
            "audit": _rows(conn, TRACE_AUDIT, params),
            "approvals": _rows(conn, TRACE_APPROVALS, params),
            "memory": memory[0] if memory else None,
        }


def pending_approvals() -> list[dict]:
    with pool.connection() as conn:
        return _rows(conn, PENDING_APPROVALS)


# The first answer wins because this UPDATE only matches a row still pending and
# not yet expired. A second answer matches nothing and changes nothing. Expiry
# is refused here, not left to the sweep, so an answer that arrives after the
# deadline cannot be an approval even in the gap before the sweep runs.
DECIDE_APPROVAL = """
UPDATE approvals
SET status = %(status)s, decided_by = %(approver)s, decided_at = now(), decision_channel = %(channel)s
WHERE id = %(id)s AND status = 'pending' AND expires_at > now()
RETURNING id, incident_id, action, container, status, decided_by, decided_at;
"""

APPROVAL_CURRENT_STATE = """
SELECT status FROM approvals WHERE id = %(id)s;
"""


def decide_approval(approval_id: int, status: str, approver: str, channel: str) -> tuple[str, dict | None]:
    """Record one answer. Returns (outcome, detail).

    outcome is 'decided' (detail is the row), 'not_found', 'expired' (pending but
    past its deadline, so refused), or 'already_decided' (detail holds the status
    that won).
    """
    with pool.connection() as conn:
        row = conn.execute(
            DECIDE_APPROVAL,
            {"status": status, "approver": approver, "channel": channel, "id": approval_id},
        ).fetchone()
        if row:
            columns = ["id", "incident_id", "action", "container", "status", "decided_by", "decided_at"]
            return "decided", dict(zip(columns, row))
        current = conn.execute(APPROVAL_CURRENT_STATE, {"id": approval_id}).fetchone()
    if current is None:
        return "not_found", None
    if current[0] == "pending":
        return "expired", None
    return "already_decided", {"status": current[0]}
