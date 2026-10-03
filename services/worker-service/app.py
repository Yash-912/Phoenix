"""Worker service: async queue consumer.

Chaos targets: CPU spike, crash, memory leak (Scenario 3).

Scenario 3's bug lives in _cache_store_unbounded/_cache_store_bounded below.
A queue consumer caching its own job results is a real pattern (avoids
redoing work if the same job_id is redelivered) -- the bug is that the
unbounded version never evicts, so the cache grows for as long as the
process runs. The bounded version is the fix: same cache, same lookup
semantics, just capped. LEAK_ENABLED switches which policy is in effect,
mirroring payment-service's SLOW_QUERY toggle.

The background consumer thread exists because this service's own docstring
has always claimed to be "an async queue consumer" without ever actually
consuming anything -- nothing in this lab called /process on an interval, so
the leak could never grow on its own the way a real production queue
consumer's cache would. The thread makes that claim true: it simulates the
steady trickle of real job traffic a queue consumer would have, so the
alert rule (WorkerServiceMemoryGrowth, a 30m derivative sustained for 10m)
has something genuine to detect rather than a value nothing ever changes.
"""

import os
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Only fires when uvicorn actually runs this app's ASGI lifecycle, not
    # when a test imports/execs this module to call its functions directly
    # -- the same reason the blip tests stub Instrumentator rather than
    # letting it register real middleware.
    threading.Thread(target=_consume_forever, daemon=True).start()
    yield


app = FastAPI(title="Worker Service", lifespan=_lifespan)

CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "false").lower() == "true"
CPU_SPIKE = False
LEAK_ENABLED = False
PAUSED = False

# One job's cached result. Payload size matches the old leak's "x" * 10000 so
# the growth rate this replaces is comparable, not just "also nonzero".
_JOB_PAYLOAD_SIZE = 10000
_CACHE_MAX_SIZE = 100

_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def _cache_store_unbounded(job_id: str, result: dict) -> None:
    """The bug: never evicts. Every job this process has ever handled stays
    resident for the life of the process."""
    with _cache_lock:
        _cache[job_id] = result


def _cache_store_bounded(job_id: str, result: dict) -> None:
    """The fix: cap the cache at _CACHE_MAX_SIZE, evicting the oldest entry
    (insertion order -- a plain dict preserves it) once full."""
    with _cache_lock:
        if job_id not in _cache and len(_cache) >= _CACHE_MAX_SIZE:
            oldest_job_id = next(iter(_cache))
            del _cache[oldest_job_id]
        _cache[job_id] = result


def _process_job(job_id: str) -> dict:
    result = {"status": "processed", "job_id": job_id, "payload": "x" * _JOB_PAYLOAD_SIZE}
    if LEAK_ENABLED:
        _cache_store_unbounded(job_id, result)
    else:
        _cache_store_bounded(job_id, result)
    return result


def _consume_one_tick() -> None:
    """One iteration of the consumer loop, split out from _consume_forever
    so a test can exercise it without running an infinite loop on a thread.
    """
    if not PAUSED:
        _process_job(str(uuid.uuid4()))


def _consume_forever() -> None:
    """Simulates a real queue consumer's steady workload: one job every
    quarter second, forever, for as long as the process is up. PAUSED stops
    it the same way pause_worker would stop a real consumer; CPU_SPIKE is
    deliberately not checked here -- that chaos target only affects the
    manually-triggered /process endpoint below, not this background loop.
    """
    while True:
        _consume_one_tick()
        time.sleep(0.25)


@app.get("/")
def root():
    return {"service": "worker", "status": "running", "paused": PAUSED}


@app.get("/health")
def health():
    return {"status": "ok", "paused": PAUSED, "queue_depth": 0, "cache_size": len(_cache)}


@app.get("/process")
def process():
    if PAUSED:
        return {"status": "paused"}
    if CPU_SPIKE:
        end = time.time() + 1.5  # burn CPU briefly per request
        while time.time() < end:
            pass
    _process_job(str(uuid.uuid4()))
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
    global LEAK_ENABLED
    LEAK_ENABLED = True
    return {"leak": "started"}


@app.post("/chaos/leak/stop")
def chaos_leak_stop():
    global LEAK_ENABLED
    LEAK_ENABLED = False
    with _cache_lock:
        _cache.clear()
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
