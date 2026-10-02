"""Payment service: payment processing with idempotency.

Chaos targets: slow query (Scenario 2), memory leak, ambiguous blip (Scenario 5).
"""

import logging
import os
import time

from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Payment Service")

CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "false").lower() == "true"
SLOW_QUERY = False
LEAK: list | None = None

# Epoch seconds until which /charge fails generically. 0.0 means no blip is
# active. Checked per-request rather than toggled by a start/stop pair alone
# so the failure is bounded even if nothing ever calls /chaos/blip/stop -- a
# chaos script that crashes mid-run must not leave the service broken forever.
BLIP_UNTIL = 0.0

logger = logging.getLogger("payment")

# Same fix demo-checkout needed: uvicorn configures its own loggers and leaves
# the root logger alone, so without this a logger.error() call goes out through
# a bare "%(message)s" with no severity anywhere in the line, and the ordinary
# way of finding errors -- grep for ERROR, or a LogQL error filter -- finds
# nothing.
logging.basicConfig(format="%(levelname)s %(message)s")


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
    if time.time() < BLIP_UNTIL:
        # Deliberately generic: no mention of a deploy, a config value, a
        # crash, or a network condition. This line is Scenario 5's whole
        # mechanism -- an incident that is genuinely undetermined has to leave
        # evidence that doesn't support any category, not evidence that was
        # never collected.
        logger.error("payment request failed, please retry")
        raise HTTPException(status_code=500, detail="request failed, please retry")
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


@app.post("/chaos/blip/start")
def chaos_blip_start(duration_seconds: int = 45):
    """Fail /charge generically for `duration_seconds`, then recover on its own.

    This is the whole of Scenario 5: a real failure burst with no deploy
    marker, no config marker, and a log line that matches none of
    scoring.CATEGORY_KEYWORDS. Self-clearing, not reset_all-dependent, is the
    point -- the ambiguity is that the incident resolves itself before any
    cause can be pinned on it, the same as a transient blip in a real system.
    """
    if not CHAOS_ENABLED:
        return {"error": "chaos disabled"}
    global BLIP_UNTIL
    BLIP_UNTIL = time.time() + duration_seconds
    return {"blip_until": BLIP_UNTIL}


@app.post("/chaos/blip/stop")
def chaos_blip_stop():
    global BLIP_UNTIL
    BLIP_UNTIL = 0.0
    return {"blip": "stopped"}


@app.post("/chaos/crash")
def chaos_crash():
    if not CHAOS_ENABLED:
        return {"error": "chaos disabled"}
    os._exit(1)


Instrumentator().instrument(app).expose(app)
