"""Payment service: payment processing with idempotency.

Chaos targets: slow query (Scenario 2), memory leak.
"""

import os
import time

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Payment Service")

CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "false").lower() == "true"
SLOW_QUERY = False
LEAK: list | None = None


@app.get("/")
def root():
    return {"service": "payment", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok", "slow_query": SLOW_QUERY}


@app.get("/charge")
def charge(order_id: str = "demo-order-1", amount: float = 9.99):
    if SLOW_QUERY:
        time.sleep(2)  # simulates missing-index slow query
    if LEAK is not None:
        LEAK.append("x" * 10000)
    return {"order_id": order_id, "amount": amount, "status": "charged"}


@app.post("/chaos/slow/enable")
def chaos_slow_enable():
    if not CHAOS_ENABLED:
        return {"error": "chaos disabled"}
    global SLOW_QUERY
    SLOW_QUERY = True
    return {"slow_query": True}


@app.post("/chaos/slow/disable")
def chaos_slow_disable():
    global SLOW_QUERY
    SLOW_QUERY = False
    return {"slow_query": False}


@app.post("/chaos/leak/start")
def chaos_leak_start():
    if not CHAOS_ENABLED:
        return {"error": "chaos disabled"}
    global LEAK
    LEAK = []
    return {"leak": "started"}


@app.post("/chaos/leak/stop")
def chaos_leak_stop():
    global LEAK
    LEAK = None
    return {"leak": "stopped"}


@app.post("/chaos/crash")
def chaos_crash():
    if not CHAOS_ENABLED:
        return {"error": "chaos disabled"}
    os._exit(1)


Instrumentator().instrument(app).expose(app)
