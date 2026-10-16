"""The approval record: written before a run pauses, read when it resumes.

Unlike the audit writers in persist.py, nothing here degrades to "carry on". The
audit trail is a record of a run that is already happening; an approval is what
lets an action happen. If the store cannot be written or read, the caller has to
stop, so every function here raises ApprovalStoreError and the remediator turns
that into an escalation.

The graph only ever writes a request and reads a decision. Answering a request is
the API's job (phoenix/api/db.py decide_approval), and the one thing the graph
also does is expire requests nobody answered in time.
"""

from phoenix.graph import persist

APPROVAL_COLUMNS = (
    "id, incident_id, request_key, status, action, container, args, tier, "
    "requested_at, expires_at, decided_at, decided_by, decision_channel"
)

INSERT_APPROVAL = f"""
INSERT INTO approvals
    (incident_id, request_key, action, container, args, tier, incident_summary,
     plan_reasoning, evidence, expires_at)
VALUES
    (%(incident_id)s, %(request_key)s, %(action)s, %(container)s, %(args)s, %(tier)s,
     %(incident_summary)s, %(plan_reasoning)s, %(evidence)s,
     now() + make_interval(secs => %(ttl_seconds)s))
ON CONFLICT (request_key) DO NOTHING
RETURNING {APPROVAL_COLUMNS}
"""

SELECT_BY_REQUEST_KEY = f"SELECT {APPROVAL_COLUMNS} FROM approvals WHERE request_key = %(request_key)s"

SELECT_BY_ID = f"SELECT {APPROVAL_COLUMNS} FROM approvals WHERE id = %(id)s"

# Only a pending row can be expired, and only once its time has passed. The
# decision side has the mirror-image guard, so a row cannot be answered and
# expired by two writers: whichever UPDATE reaches it first decides it.
EXPIRE_STALE = f"""
UPDATE approvals
SET status = 'expired', decided_at = now(), decided_by = 'system:expiry', decision_channel = 'expiry'
WHERE status = 'pending' AND expires_at <= now()
RETURNING {APPROVAL_COLUMNS}
"""


class ApprovalStoreError(RuntimeError):
    """The approval record could not be written or read. The action must not run."""


def _row_dict(row, columns: list[str]) -> dict:
    return dict(zip(columns, row))


_COLUMNS = [name.strip() for name in APPROVAL_COLUMNS.split(",")]


def _pool():
    pool = persist._get_pool()
    if pool is None:
        raise ApprovalStoreError("no database connection, so the approval cannot be recorded")
    return pool


def request_approval(
    *,
    incident_id: int,
    request_key: str,
    action: str,
    container: str,
    args: tuple[str, ...],
    tier: int,
    incident_summary: str,
    plan_reasoning: str,
    evidence: list[dict],
    ttl_seconds: int,
) -> dict:
    """Open the request for this exact action, or return the one already open.

    request_key identifies the step that asked, so a node that runs again after
    it resumes gets its own request back rather than a second one. The row it
    gets back may already be decided; the caller reads the status, it does not
    assume a fresh pending row.
    """
    params = {
        "incident_id": incident_id,
        "request_key": request_key,
        "action": action,
        "container": container,
        "args": persist._jsonb(list(args)),
        "tier": tier,
        "incident_summary": incident_summary,
        "plan_reasoning": plan_reasoning,
        "evidence": persist._jsonb(evidence),
        "ttl_seconds": ttl_seconds,
    }
    try:
        pool = _pool()
        with pool.connection() as conn:
            row = conn.execute(INSERT_APPROVAL, params).fetchone()
            if row is None:
                row = conn.execute(SELECT_BY_REQUEST_KEY, {"request_key": request_key}).fetchone()
            conn.commit()
    except ApprovalStoreError:
        raise
    except Exception as exc:  # noqa: BLE001 - any store failure means the action does not run
        raise ApprovalStoreError(f"approval could not be recorded: {exc}") from exc
    if row is None:
        raise ApprovalStoreError("approval was neither inserted nor found by its request key")
    return _row_dict(row, _COLUMNS)


def read_approval(approval_id: int) -> dict | None:
    """The current row, read fresh from the database. None if it does not exist."""
    try:
        pool = _pool()
        with pool.connection() as conn:
            row = conn.execute(SELECT_BY_ID, {"id": approval_id}).fetchone()
    except ApprovalStoreError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApprovalStoreError(f"approval {approval_id} could not be read: {exc}") from exc
    return _row_dict(row, _COLUMNS) if row else None


def expire_stale_approvals() -> list[dict]:
    """Mark every unanswered request past its time as expired. Returns those rows.

    Raises ApprovalStoreError when the database cannot be reached. The watcher
    treats that as "nothing expired yet" and tries again on its next pass.
    """
    try:
        pool = _pool()
        with pool.connection() as conn:
            rows = conn.execute(EXPIRE_STALE).fetchall()
            conn.commit()
    except ApprovalStoreError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApprovalStoreError(f"expired approvals could not be swept: {exc}") from exc
    return [_row_dict(row, _COLUMNS) for row in rows]
