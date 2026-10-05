"""Scenario 4: config regression — shrink auth-service's DB connection pool.

Goes through chaos/lib/deployer.apply_config, the same path
rollback_config uses to restore it: one code path means the restore cannot
drift from the regression it undoes.

A pool of one is only a regression under load -- auth-service serves a lone
request fine -- and nothing else in this lab generates organic traffic, so the
script drives its own concurrent /validate load after applying the change. With
the pool at its normal size the same load is served without a single error, so
the failures this produces belong to the config and to nothing else.

Usage: python -m chaos.config_pool [--reset] [--no-load]
"""

from __future__ import annotations

import sys
import threading
import time

import requests

from chaos.lib.deployer import apply_config

AUTH_URL = "http://localhost:8002"
LOAD_SECONDS = 150
LOAD_WORKERS = 8  # fewer than the normal pool of 10, more than the regressed pool of 1


def _worker(deadline: float) -> None:
    while time.time() < deadline:
        try:
            requests.get(f"{AUTH_URL}/validate", timeout=5)
        except requests.RequestException:
            time.sleep(0.2)


def generate_load(seconds: int = LOAD_SECONDS, workers: int = LOAD_WORKERS) -> None:
    deadline = time.time() + seconds
    threads = [threading.Thread(target=_worker, args=(deadline,), daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def inject(load: bool = True) -> dict:
    marker = apply_config(
        "auth-service",
        {"DB_POOL_SIZE": "1"},
        deployed_by="chaos/config_pool.py",
    )
    print(f"applied config regression (pool=1): {marker['timestamp']}")
    if load:
        print(f"driving {LOAD_WORKERS} concurrent /validate clients for {LOAD_SECONDS}s")
        generate_load()
        print("load finished")
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
        inject(load="--no-load" not in sys.argv)
