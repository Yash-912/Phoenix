"""Scenario 4: config regression — shrink auth pool via env note + marker.

Note: live env change needs container recreate; V1 records the marker and
hits a chaos note endpoint. Full env-swap lands with Tier 2 work.
Usage: python -m chaos.config_pool [--reset]
"""

from __future__ import annotations

import sys

from chaos.lib.deploy_tracker import write_deployment_marker


def inject() -> dict:
    marker = write_deployment_marker(
        service="auth-service",
        image_tag="same",
        git_commit="same",
        config={"DB_POOL_SIZE": "1"},
        deployed_by="chaos/config_pool.py",
    )
    print(f"recorded config regression (pool=1): {marker['timestamp']}")
    print("NOTE: apply with: docker compose up -d --no-deps auth-service (DB_POOL_SIZE=1)")
    return marker


def reset() -> None:
    marker = write_deployment_marker(
        service="auth-service",
        image_tag="same",
        git_commit="same",
        config={"DB_POOL_SIZE": "10"},
        deployed_by="chaos/config_pool.py --reset",
    )
    print(f"recorded config restore (pool=10): {marker['timestamp']}")
    return marker


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    else:
        inject()
