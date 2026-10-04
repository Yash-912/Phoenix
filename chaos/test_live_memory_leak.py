"""Scenario 3 injector, proven against the real worker process -- not a dict
in a unit test.

Opt-in (PHOENIX_RUN_CHAOS_LIVE=1) because it needs the Phoenix Lab docker
compose stack and toggles chaos on the live worker-service. It lives under
chaos/ rather than phoenix/ on purpose: it is about the injector, not about
application behaviour, and it can only hold on a build that still contains the
unbounded store -- once Tier 3's fix to _cache_store_unbounded is merged the
leak can no longer be injected, and this test is supposed to say so. The
normal application contract (phoenix/test_worker_service_cache.py) is the one
that must always pass.

Run with:
    PHOENIX_RUN_CHAOS_LIVE=1 python -m pytest chaos/test_live_memory_leak.py -v -s
"""

import os
import re
import time

import pytest
import requests

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOENIX_RUN_CHAOS_LIVE") != "1",
    reason="live chaos test; opt in with PHOENIX_RUN_CHAOS_LIVE=1 against the running lab",
)

WORKER_URL = "http://localhost:8004"
NORMAL_CACHE_CAP = 100  # services/worker-service/app.py _CACHE_MAX_SIZE
JOBS = 3000
MIN_RSS_GROWTH_BYTES = 10 * 1024 * 1024


def _cache_size() -> int:
    return requests.get(f"{WORKER_URL}/health", timeout=10).json()["cache_size"]


def _rss_bytes() -> float:
    text = requests.get(f"{WORKER_URL}/metrics", timeout=10).text
    match = re.search(r"^process_resident_memory_bytes\s+([0-9.eE+]+)", text, re.MULTILINE)
    assert match, "worker exposes no process_resident_memory_bytes"
    return float(match.group(1))


def _drive_jobs(count: int) -> None:
    for _ in range(count):
        requests.get(f"{WORKER_URL}/process", timeout=10).raise_for_status()


@pytest.fixture
def clean_worker():
    requests.post(f"{WORKER_URL}/chaos/leak/stop", timeout=10).raise_for_status()
    yield
    requests.post(f"{WORKER_URL}/chaos/leak/stop", timeout=10)


def test_leak_injector_grows_the_real_cache_and_process_memory(clean_worker):
    # Normal behaviour first: heavy traffic must not push the cache past its cap.
    _drive_jobs(JOBS // 10)
    bounded_size = _cache_size()
    assert bounded_size <= NORMAL_CACHE_CAP, bounded_size

    rss_before = _rss_bytes()
    requests.post(f"{WORKER_URL}/chaos/leak/start", timeout=10).raise_for_status()
    progression = []
    for _ in range(5):
        _drive_jobs(JOBS // 5)
        progression.append((_cache_size(), _rss_bytes()))
        time.sleep(0.5)
    rss_after = progression[-1][1]

    print(f"\ncache cap (normal behaviour): {NORMAL_CACHE_CAP}, size under normal load: {bounded_size}")
    print(f"RSS before leak: {rss_before / 1e6:.1f} MB")
    for index, (size, rss) in enumerate(progression, start=1):
        print(f"  after batch {index}: cache_size={size} rss={rss / 1e6:.1f} MB")

    sizes = [size for size, _ in progression]
    assert sizes[0] > NORMAL_CACHE_CAP, sizes
    assert sizes == sorted(sizes) and sizes[-1] > sizes[0], sizes
    assert rss_after - rss_before > MIN_RSS_GROWTH_BYTES, (rss_before, rss_after)
