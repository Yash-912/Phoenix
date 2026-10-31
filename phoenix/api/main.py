import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

# pyrefly: ignore [missing-import]
import psycopg
from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles

import db
import incident_trace
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


# --- dashboard reads ---------------------------------------------------------
# Read-only. Nothing here writes, and nothing here decides an approval: the dashboard's
# Approve and Deny buttons call POST /approvals/{id}/decision below, behind its token.


def _read(fetch, *args):
    """Run a database read; a database that cannot be reached is a 503, not a stack trace."""
    try:
        return fetch(*args)
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {type(exc).__name__}") from exc


@app.get("/dashboard-config")
def dashboard_config():
    """What the page needs before it can draw: the outcome vocabulary and whether
    approving is switched on. The token itself is never returned."""
    return {
        "approvals_enabled": bool(os.environ.get("PHOENIX_APPROVAL_TOKEN")) and bool(_approver_list()),
        "outcomes": incident_trace.OUTCOMES,
        "tiers": incident_trace.TIERS,
    }


@app.get("/incidents/overview")
def incidents_overview(limit: int = 200):
    rows = _read(db.overview, max(1, min(limit, 500)))
    return {"incidents": [incident_trace.overview_row(row) for row in rows]}


@app.get("/incidents/{incident_id}/trace")
def incident_trace_view(incident_id: int):
    rows = _read(db.incident_rows, incident_id)
    if rows is None:
        raise HTTPException(status_code=404, detail=f"no incident {incident_id}")
    return incident_trace.build_trace(rows)


@app.get("/incidents/{incident_id}/audit")
def incident_audit_view(incident_id: int):
    """The raw audit rows, redacted. The detail page's one-click-deeper layer."""
    rows = _read(db.incident_rows, incident_id)
    if rows is None:
        raise HTTPException(status_code=404, detail=f"no incident {incident_id}")
    return incident_trace.audit_rows(rows["audit"])


@app.get("/approvals")
def list_approvals(status: Literal["pending"] = "pending"):
    return {"approvals": incident_trace.approval_queue(_read(db.pending_approvals))}


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
    outcome, detail = db.decide_approval(approval_id, status, body.approver, body.channel)
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


# The page is plain files, served from the same origin as the API. Mounted last so it
# can never shadow a route above. Absent in a checkout that has no dashboard directory.
class _RevalidatedFiles(StaticFiles):
    """Static files the browser must re-check on every load.

    The page is a set of ES modules. With no Cache-Control a browser caches each one by
    its own age, so right after a deploy it can pair a new module with an old one it
    imports from, and the page stays on its loading skeleton. no-cache still lets the
    ETag answer 304, so a repeat visit costs a header, not a download.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


_DASHBOARD_DIR = Path(__file__).parent / "dashboard"
if _DASHBOARD_DIR.is_dir():
    app.mount("/dashboard", _RevalidatedFiles(directory=_DASHBOARD_DIR, html=True), name="dashboard")
