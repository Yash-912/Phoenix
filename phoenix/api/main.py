import hmac
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException

import db
from schemas import AlertmanagerWebhookPayload, ApprovalDecision


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.pool.open()
    yield
    db.pool.close()


app = FastAPI(title="Phoenix API", lifespan=lifespan)


@app.post("/webhooks/alertmanager")
def receive_alertmanager_webhook(payload: AlertmanagerWebhookPayload):
    results = []
    for alert in payload.alerts:
        service_name = alert.labels["service"]
        alertname = alert.labels["alertname"]
        severity = alert.labels.get("severity", "unknown")

        if alert.status == "firing":
            incident_id, created = db.upsert_active_incident(
                service_name, alertname, severity, alert.model_dump(mode="json")
            )
            results.append({"incident_id": incident_id, "created": created})
        elif alert.status == "resolved":
            incident_id = db.resolve_incident(service_name, alertname)
            results.append({"incident_id": incident_id, "resolved": incident_id is not None})

    return {"processed": len(results), "results": results}


@app.get("/incidents")
def get_incidents():
    return db.list_incidents()


def _approver_list() -> frozenset[str]:
    """The same list phoenix/graph/safety_policy.approver_list reads. The API image
    does not carry the graph code, so the rule is repeated here rather than imported."""
    raw = os.environ.get("PHOENIX_APPROVERS", "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


@app.post("/approvals/{approval_id}/decision")
def decide_approval(
    approval_id: int,
    body: ApprovalDecision,
    authorization: str | None = Header(default=None),
):
    """Approve or deny one pending request. The first answer wins; later ones change nothing.

    Two checks sit in front of the database. The caller must present the approval
    token (PHOENIX_APPROVAL_TOKEN), and the named approver must be on the configured
    list. With no token configured the endpoint refuses everything. Until OIDC
    identity exists, the token is what proves the caller is the dashboard or Slack
    bridge, and the approver name is what that caller asserts.

    An answer after the deadline is refused here, and the graph turns the expiry into
    an escalation when it resumes. Nothing in this endpoint can approve an expired
    request.
    """
    token = os.environ.get("PHOENIX_APPROVAL_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="approvals are disabled: PHOENIX_APPROVAL_TOKEN is not set")
    if not authorization or not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(status_code=401, detail="missing or wrong approval token")
    if body.approver not in _approver_list():
        raise HTTPException(status_code=403, detail="that identity is not on the approver list")

    status = "approved" if body.decision == "approve" else "denied"
    outcome, detail = db.decide_approval(approval_id, status, body.approver, "api")
    if outcome == "decided":
        return {"approval_id": approval_id, "status": status, "decided_by": body.approver, "result": "recorded"}
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail=f"no approval {approval_id}")
    if outcome == "expired":
        raise HTTPException(
            status_code=409,
            detail={"status": "expired", "result": "refused: the deadline has passed, and expiry is never an approval"},
        )
    raise HTTPException(
        status_code=409,
        detail={"status": detail["status"], "result": "ignored: the first answer already won"},
    )
