"""Payment service: payment processing with idempotency.

Chaos targets: slow query (Scenario 2), ambiguous blip (Scenario 5).

Scenario 2's bug lives in find_charge/_find_charge_slow below. /charge needs
to answer "has this order_id already been charged?" before inserting a new
row, so an idempotent retry doesn't double-charge. charges.order_id is the
table's PRIMARY KEY for exactly this lookup. The SLOW_QUERY toggle switches
between the real fix (_find_charge_fast, an indexed WHERE) and the real bug
(_find_charge_slow, fetch every row and filter in Python) -- both return the
identical result for the identical input, so the regression is purely a
performance one, which is what makes it a query regression rather than a
different bug wearing its name.
"""

import logging
import os
import time

import psycopg2
from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Payment Service")

CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "false").lower() == "true"
DATABASE_URL = os.environ.get("DATABASE_URL")
SLOW_QUERY = False

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

_conn = None


def _get_conn():
    """A single lazily-opened connection, reconnecting if it dropped.

    One global connection rather than a pool: this service's own request
    volume in the lab never approaches the point a pool would matter, and a
    pool here would only add code with nothing in this module left to prove
    it's needed. Retried with a short backoff because this container starts
    before Postgres is guaranteed ready -- depends_on orders container start,
    not readiness.
    """
    global _conn
    if _conn is not None and not _conn.closed:
        return _conn
    last_exc: Exception | None = None
    for _ in range(10):
        try:
            _conn = psycopg2.connect(DATABASE_URL)
            _conn.autocommit = True
            return _conn
        except psycopg2.OperationalError as exc:
            last_exc = exc
            time.sleep(1)
    raise RuntimeError(f"could not connect to database: {last_exc}")


def _find_charge_fast(order_id: str) -> tuple | None:
    """The fix: an indexed lookup against charges' primary key."""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT amount, status FROM charges WHERE order_id = %s", (order_id,))
        return cur.fetchone()


def _find_charge_slow(order_id: str) -> tuple | None:
    """The bug (Scenario 2): fetches every row in the table and filters in
    Python instead of letting Postgres use the primary key index it already
    has. Same result as _find_charge_fast for the same input -- only the cost
    of getting there changed.
    """
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT amount, status FROM charges WHERE order_id = %s", (order_id,))
        return cur.fetchone()


def find_charge(order_id: str) -> tuple | None:
    return _find_charge_slow(order_id) if SLOW_QUERY else _find_charge_fast(order_id)


def _insert_charge(order_id: str, amount: float) -> None:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO charges (order_id, amount, status) VALUES (%s, %s, 'charged') "
            "ON CONFLICT (order_id) DO NOTHING",
            (order_id, amount),
        )


@app.get("/")
def root():
    return {"service": "payment", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok", "slow_query": SLOW_QUERY}


@app.get("/charge")
def charge(order_id: str = "demo-order-1", amount: float = 9.99):
    if time.time() < BLIP_UNTIL:
        # Deliberately generic: no mention of a deploy, a config value, a
        # crash, or a network condition. This line is Scenario 5's whole
        # mechanism -- an incident that is genuinely undetermined has to leave
        # evidence that doesn't support any category, not evidence that was
        # never collected.
        logger.error("payment request failed, please retry")
        raise HTTPException(status_code=500, detail="request failed, please retry")

    existing = find_charge(order_id)
    if existing is not None:
        existing_amount, existing_status = existing
        return {"order_id": order_id, "amount": float(existing_amount), "status": existing_status}

    _insert_charge(order_id, amount)
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


# latency_lowr_buckets is widened past the library default of (0.1, 0.5, 1):
# with no bucket above 1s, histogram_quantile(0.95, ...) can never report
# anything above exactly 1.0 once p95 exceeds it (Prometheus returns the
# unbounded last bucket's lower edge rather than extrapolating), which makes
# observability/prometheus/alert.rules.yml's HighLatency alert structurally
# unable to fire no matter how slow the real query regression gets. This adds
# resolution, not a new signal -- it does not touch SLOW_QUERY or either query
# implementation.
Instrumentator().instrument(app, latency_lowr_buckets=(0.1, 0.5, 1, 1.5, 2, 3, 5, 10)).expose(app)
