"""Worker service: async queue consumer.

Chaos targets: CPU spike, crash, memory leak (Scenario 3).
"""

import os
import time

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Worker Service")

CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "false").lower() == "true"
CPU_SPIKE = False
LEAK: list | None = None
PAUSED = False


@app.get("/")
def root():
    return {"service": "worker", "status": "running", "paused": PAUSED}


@app.get("/health")
def health():
    return {"status": "ok", "paused": PAUSED, "queue_depth": 0}


@app.get("/process")
def process():
    if PAUSED:
        return {"status": "paused"}
    if CPU_SPIKE:
        end = time.time() + 1.5  # burn CPU briefly per request
        while time.time() < end:
            pass
    if LEAK is not None:
        LEAK.append("x" * 10000)
    return {"status": "processed"}


@app.post("/chaos/cpu/enable")
def chaos_cpu_enable():
    if not CHAOS_ENABLED:
        return {"error": "chaos disabled"}
    global CPU_SPIKE
    CPU_SPIKE = True
    return {"cpu_spike": True}


@app.post("/chaos/cpu/disable")
def chaos_cpu_disable():
    global CPU_SPIKE
    CPU_SPIKE = False
    return {"cpu_spike": False}


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


@app.post("/chaos/pause")
def chaos_pause():
    global PAUSED
    PAUSED = True
    return {"paused": True}


@app.post("/chaos/resume")
def chaos_resume():
    global PAUSED
    PAUSED = False
    return {"paused": False}


@app.post("/chaos/crash")
def chaos_crash():
    if not CHAOS_ENABLED:
        return {"error": "chaos disabled"}
    os._exit(1)


Instrumentator().instrument(app).expose(app)
