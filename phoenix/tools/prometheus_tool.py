import os

import requests

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9091")


def query_prometheus(promql: str) -> dict:
    """Run an instant PromQL query and return Prometheus's raw JSON response.

    Read-only by construction: this only ever calls Prometheus's /api/v1/query
    endpoint, which has no side effects on Prometheus's own state.
    """
    try:
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": promql},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        return {"status": "error", "error": str(exc)}
