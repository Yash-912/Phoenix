"""Scenario 1: bad deployment — break checkout, record marker.

Usage: python -m chaos.deploy_bad_v18 [--reset]
Idempotent: repeated runs keep BROKEN on, single latest marker per minute is fine.
"""

from __future__ import annotations

import sys

import requests

from chaos.lib.deploy_tracker import write_deployment_marker

CHECKOUT_URL = "http://localhost:8001"


def inject() -> dict:
    r = requests.post(f"{CHECKOUT_URL}/chaos/break", timeout=5)
    r.raise_for_status()
    marker = write_deployment_marker(
        service="checkout-service",
        image_tag="v18",
        git_commit="bad-v18",
        config={"broken": True},
        deployed_by="chaos/deploy_bad_v18.py",
    )
    print(f"injected bad deploy v18: {marker['timestamp']}")
    return marker


def reset() -> None:
    r = requests.post(f"{CHECKOUT_URL}/chaos/heal", timeout=5)
    r.raise_for_status()
    write_deployment_marker(
        service="checkout-service",
        image_tag="v17",
        git_commit="good-v17",
        config={"broken": False},
        deployed_by="chaos/deploy_bad_v18.py --reset",
    )
    print("reset to healthy v17")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    else:
        inject()
