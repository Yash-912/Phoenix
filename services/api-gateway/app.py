"""API gateway: routes to upstream services.

Chaos targets: bad deploy (Scenario 1), cascade.
"""

import os

import requests
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="API Gateway")

CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "false").lower() == "true"
BROKEN = False

AUTH_URL = os.environ.get("AUTH_URL", "http://auth-service:8000")
PAYMENT_URL = os.environ.get("PAYMENT_URL", "http://payment-service:8000")
CHECKOUT_URL = os.environ.get("CHECKOUT_URL", "http://checkout-service:8000")


@app.get("/")
def root():
    return {"service": "gateway", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok", "broken": BROKEN}


@app.get("/route/{service}")
def route(service: str):
    if BROKEN:
        # Scenario 1: bad deploy returns 500s
        return {"error": "bad gateway deploy v18"}, 500
    targets = {"auth": AUTH_URL, "payment": PAYMENT_URL, "checkout": CHECKOUT_URL}
    base = targets.get(service)
    if not base:
        return {"error": f"unknown service {service}"}, 404
    try:
        r = requests.get(f"{base}/health", timeout=3)
        return {"service": service, "upstream": r.json()}
    except requests.RequestException as exc:
        return {"error": str(exc)}, 502


@app.post("/chaos/break")
def chaos_break():
    if not CHAOS_ENABLED:
        return {"error": "chaos disabled"}
    global BROKEN
    BROKEN = True
    return {"broken": True}


@app.post("/chaos/heal")
def chaos_heal():
    global BROKEN
    BROKEN = False
    return {"broken": False}


@app.post("/chaos/crash")
def chaos_crash():
    if not CHAOS_ENABLED:
        return {"error": "chaos disabled"}
    os._exit(1)


Instrumentator().instrument(app).expose(app)
