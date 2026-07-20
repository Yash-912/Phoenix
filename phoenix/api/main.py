from contextlib import asynccontextmanager

from fastapi import FastAPI

import db
from schemas import AlertmanagerWebhookPayload


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
