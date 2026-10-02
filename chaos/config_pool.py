"""Scenario 4: config regression — shrink auth-service's DB connection pool.

Goes through chaos/lib/deployer.apply_config, the same path
rollback_config uses to restore it: one code path means the restore cannot
drift from the regression it undoes.

Usage: python -m chaos.config_pool [--reset]
"""

from __future__ import annotations

import sys

from chaos.lib.deployer import apply_config


def inject() -> dict:
    marker = apply_config(
        "auth-service",
        {"DB_POOL_SIZE": "1"},
        deployed_by="chaos/config_pool.py",
    )
    print(f"applied config regression (pool=1): {marker['timestamp']}")
    return marker


def reset() -> dict:
    marker = apply_config(
        "auth-service",
        {"DB_POOL_SIZE": "10"},
        deployed_by="chaos/config_pool.py --reset",
    )
    print(f"restored config (pool=10): {marker['timestamp']}")
    return marker


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    else:
        inject()
