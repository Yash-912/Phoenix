import os

from fastapi import FastAPI, HTTPException

from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Demo Checkout Service")

CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "false").lower() == "true"
BROKEN = False


@app.get("/")
def root():
    return {"service": "checkout", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok", "broken": BROKEN}


@app.get("/checkout")
def checkout():
    if BROKEN:
        raise HTTPException(status_code=500, detail="bad deploy v18")
    return {"order_id": "demo-order-1", "status": "confirmed"}


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
