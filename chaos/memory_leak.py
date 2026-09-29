"""Scenario 3: memory leak — start unbounded cache growth on worker.

Usage: python -m chaos.memory_leak [--reset]
"""

from __future__ import annotations

import sys

import requests

from chaos.lib.deploy_tracker import write_deployment_marker

WORKER_URL = "http://localhost:8004"


def inject() -> dict:
    r = requests.post(f"{WORKER_URL}/chaos/leak/start", timeout=5)
    r.raise_for_status()
    marker = write_deployment_marker(
        service="worker-service",
        image_tag="leaky",
        git_commit="leak-cache",
        config={"leak": True},
        deployed_by="chaos/memory_leak.py",
    )
    print(f"injected memory leak: {marker['timestamp']}")
    return marker


def reset() -> None:
    r = requests.post(f"{WORKER_URL}/chaos/leak/stop", timeout=5)
    r.raise_for_status()
    write_deployment_marker(
        service="worker-service",
        image_tag="healthy",
        git_commit="fixed-cache",
        config={"leak": False},
        deployed_by="chaos/memory_leak.py --reset",
    )
    print("reset leak off")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    else:
        inject()
