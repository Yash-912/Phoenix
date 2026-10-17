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

LIST_INCIDENTS = """
SELECT id, service_name, alertname, severity, status,
       first_seen_at, last_seen_at, alert_count
FROM incidents
ORDER BY first_seen_at DESC;
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
        "first_seen_at", "last_seen_at", "alert_count",
    ]
    with pool.connection() as conn:
        rows = conn.execute(LIST_INCIDENTS).fetchall()
    return [dict(zip(columns, row)) for row in rows]


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
