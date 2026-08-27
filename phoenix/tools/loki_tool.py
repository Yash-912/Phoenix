import os
import time

import requests

LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100")


def query_loki(logql: str, minutes: int = 15) -> dict:
    """Query Loki for logs matching a LogQL query over the last `minutes` minutes.

    Read-only: only ever calls Loki's /loki/api/v1/query_range endpoint.
    """
    now_ns = time.time_ns()
    start_ns = now_ns - (minutes * 60 * 1_000_000_000)

    try:
        response = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": logql, "start": start_ns, "end": now_ns, "limit": 100},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        return {"status": "error", "error": str(exc)}
