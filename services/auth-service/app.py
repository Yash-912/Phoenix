"""Auth service: JWT issuance/validation + user lookup.

Chaos targets: config regression (DB_POOL_SIZE shrink), bad deploy.
"""

import os
import time

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Auth Service")

CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "false").lower() == "true"
SLOW = False
LEAK: list | None = None


@app.get("/")
def root():
    return {"service": "auth", "status": "running"}


@app.get("/health")
def health():
    pool_size = os.environ.get("DB_POOL_SIZE", "10")
    return {"status": "ok", "db_pool_size": pool_size}


@app.get("/validate")
def validate(token: str = "demo"):
    if SLOW:
        time.sleep(2)
    if LEAK is not None:
        LEAK.append("x" * 10000)
    return {"valid": True, "user": "demo-user"}


@app.post("/chaos/slow/enable")
def chaos_slow_enable():
    if not CHAOS_ENABLED:
        return {"error": "chaos disabled"}
    global SLOW
    SLOW = True
    return {"slow": True}


@app.post("/chaos/slow/disable")
def chaos_slow_disable():
    global SLOW
    SLOW = False
    return {"slow": False}


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
