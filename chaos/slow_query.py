"""Scenario 2: query regression — enable slow query on payment.

Usage: python -m chaos.slow_query [--reset]
"""

from __future__ import annotations

import sys

import requests

from chaos.lib.deploy_tracker import write_deployment_marker

PAYMENT_URL = "http://localhost:8003"


def inject() -> dict:
    r = requests.post(f"{PAYMENT_URL}/chaos/slow/enable", timeout=5)
    r.raise_for_status()
    marker = write_deployment_marker(
        service="payment-service",
        image_tag="slow-query",
        git_commit="slow-join",
        config={"slow_query": True},
        deployed_by="chaos/slow_query.py",
    )
    print(f"injected slow query: {marker['timestamp']}")
    return marker


def reset() -> None:
    r = requests.post(f"{PAYMENT_URL}/chaos/slow/disable", timeout=5)
    r.raise_for_status()
    write_deployment_marker(
        service="payment-service",
        image_tag="healthy",
        git_commit="indexed",
        config={"slow_query": False},
        deployed_by="chaos/slow_query.py --reset",
    )
    print("reset slow query off")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    else:
        inject()
