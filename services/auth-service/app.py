"""Auth service: JWT issuance/validation + user lookup.

Chaos targets: config regression (DB_POOL_SIZE shrink), bad deploy.

DB_POOL_SIZE bounds how many /validate requests can hold a database connection
at once. A request that cannot get one within POOL_WAIT_SECONDS is rejected with
a 503, so a pool too small for the load is a real outage rather than a number
/health echoes back, and /health says so in words while it is happening.
"""

import collections
import logging
import os
import threading
import time

from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Auth Service")

# Same logger uvicorn writes through, so the line reaches the container's
# stdout (and from there Loki) without any logging setup of its own.
log = logging.getLogger("uvicorn.error")

CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "false").lower() == "true"
SLOW = False
LEAK: list | None = None

POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "10"))
POOL_WAIT_SECONDS = 0.5
QUERY_SECONDS = 0.3  # how long the credential lookup holds its connection
REJECTION_WINDOW_SECONDS = 300

_pool = threading.BoundedSemaphore(POOL_SIZE)
_rejections: collections.deque = collections.deque()
_rejections_lock = threading.Lock()


def _record_rejection() -> None:
    with _rejections_lock:
        _rejections.append(time.monotonic())


def _recent_rejections() -> int:
    cutoff = time.monotonic() - REJECTION_WINDOW_SECONDS
    with _rejections_lock:
        while _rejections and _rejections[0] < cutoff:
            _rejections.popleft()
        return len(_rejections)


@app.get("/")
def root():
    return {"service": "auth", "status": "running"}


@app.get("/health")
def health():
    body = {"status": "ok", "db_pool_size": os.environ.get("DB_POOL_SIZE", "10")}
    rejected = _recent_rejections()
    if rejected:
        body["status"] = "degraded"
        body["detail"] = (
            f"connection pool saturated: {rejected} requests rejected in the last "
            f"{REJECTION_WINDOW_SECONDS}s (pool size {POOL_SIZE})"
        )
    return body


@app.get("/validate")
def validate(token: str = "demo"):
    if not _pool.acquire(timeout=POOL_WAIT_SECONDS):
        _record_rejection()
        log.error(
            "connection pool exhausted: no connection free after %.1fs (pool size %d)",
            POOL_WAIT_SECONDS,
            POOL_SIZE,
        )
        raise HTTPException(status_code=503, detail="connection pool exhausted")
    try:
        time.sleep(QUERY_SECONDS)
        if SLOW:
            time.sleep(2)
        if LEAK is not None:
            LEAK.append("x" * 10000)
        return {"valid": True, "user": "demo-user"}
    finally:
        _pool.release()


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
