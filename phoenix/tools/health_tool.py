"""inspect_health: container state + /health endpoint combined.

Read-only. Used by Observer as the health signal.
"""

from __future__ import annotations

import os

import requests

from phoenix.tools.docker_tool import get_container_state

SERVICE_PORTS = {
    "checkout-service": int(os.environ.get("CHECKOUT_PORT", "8001")),
    "auth-service": int(os.environ.get("AUTH_PORT", "8002")),
    "payment-service": int(os.environ.get("PAYMENT_PORT", "8003")),
    "worker-service": int(os.environ.get("WORKER_PORT", "8004")),
    "api-gateway": int(os.environ.get("GATEWAY_PORT", "8005")),
}


def inspect_health(service_name: str) -> dict:
    """Return container state + app /health JSON for a service."""
    container = get_container_state(service_name)
    port = SERVICE_PORTS.get(service_name)
    app_health: dict = {}
    if port is not None:
        try:
            r = requests.get(f"http://localhost:{port}/health", timeout=5)
            r.raise_for_status()
            app_health = r.json()
        except requests.RequestException as exc:
            app_health = {"status": "error", "error": str(exc)}
    return {"container": container, "app": app_health}
