import logging
import os

from fastapi import FastAPI, HTTPException

from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Demo Checkout Service")

# Both values are baked in at image build time (see Dockerfile). They are
# properties of the artifact, not of the process, which is the whole point: a
# restart recreates the process but reuses the image, so nothing an in-process
# flag could do can emulate the regression we need Tier 2 to detect.
APP_VERSION = os.environ.get("APP_VERSION", "unknown")
REGRESSION_ENABLED = os.environ.get("REGRESSION_ENABLED", "false").lower() == "true"

CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "false").lower() == "true"

logger = logging.getLogger("checkout")

# Uvicorn configures its own loggers and leaves the root logger alone, so without
# this the service's own records go out through a bare "%(message)s" format. That
# emitted "v18 checkout failed: ..." with no severity anywhere in the line, so
# the ordinary way of finding errors does not find them: grep for ERROR, or query
# Loki with an error filter, and this service looks silent. Stating the level is
# what makes the log searchable.
logging.basicConfig(format="%(levelname)s %(message)s")

# Uvicorn configures its own loggers and leaves the root logger alone, so the
# default root handler -- a bare "%(message)s" -- is what this service's own log
# records went through. That emitted "v18 checkout failed: ..." with no severity
# anywhere in the line, which means the ordinary way of finding errors does not
# find them: grep for ERROR, or query Loki with an error filter, and this service
# looks silent. Stating the level is what makes the log searchable.


@app.get("/")
def root():
    return {"service": "checkout", "status": "running", "version": APP_VERSION}


@app.get("/health")
def health():
    """Liveness only. A regression that rejects orders is still a live process,
    so this endpoint deliberately stays green in both artifacts. Verification
    that trusts this endpoint would 'pass' a broken v18, which is precisely the
    failure mode Tier 2 exists to correct.
    """
    return {"status": "ok", "version": APP_VERSION}


@app.get("/version")
def version():
    """Runtime artifact identity, served by the artifact itself.

    Lets a reader confirm which image is answering without going to the Docker
    API. Verification cross-checks this against container metadata, so a stale
    or spoofed response cannot pass on its own.
    """
    return {"service": "checkout-service", "version": APP_VERSION, "regression": REGRESSION_ENABLED}


@app.get("/checkout")
def checkout():
    if REGRESSION_ENABLED:
        # Naming the artifact in the error line is what makes the deployment
        # distinguishable in logs: an operator (or the diagnoser) can see that
        # this failure came from v18 and not from whatever ran before it.
        logger.error(
            "%s checkout failed: inventory reservation expired for order demo-order-1",
            APP_VERSION,
        )
        raise HTTPException(status_code=500, detail=f"checkout unavailable ({APP_VERSION})")
    return {"order_id": "demo-order-1", "status": "confirmed", "version": APP_VERSION}


@app.post("/chaos/crash")
def chaos_crash():
    """Kill the process without clearing anything.

    The process-local /chaos/break and /chaos/heal pair that used to sit here
    is gone deliberately. It set a flag nothing read, so `heal` reported success
    while the service stayed broken -- and a fault a restart can clear is not the
    fault Tier 2 exists for. The regression now lives in the image; redeploy v17
    to undo it, which is what chaos/reset_all.py does.
    """
    if not CHAOS_ENABLED:
        return {"error": "chaos disabled"}
    os._exit(1)


Instrumentator().instrument(app).expose(app)
