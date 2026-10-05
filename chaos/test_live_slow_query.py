"""Scenario 2 injector, proven against the real stack -- not a fake cursor.

Opt-in (PHOENIX_RUN_CHAOS_LIVE=1) because it needs the Phoenix Lab docker
compose stack and toggles chaos on the live payment-service. It lives under
chaos/ rather than phoenix/ on purpose: it is about the injector, not about
application behaviour, and it can only hold on a build that still contains the
regression -- once Tier 3's fix to _find_charge_slow is merged there is
nothing left to inject, and this test is supposed to say so. The normal
application contract (phoenix/test_payment_service_charge.py) is the one that
must always pass.

Run with:
    PHOENIX_RUN_CHAOS_LIVE=1 python -m pytest chaos/test_live_slow_query.py -v -s
"""

import os
import statistics
import subprocess
import time

import pytest
import requests

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOENIX_RUN_CHAOS_LIVE") != "1",
    reason="live chaos test; opt in with PHOENIX_RUN_CHAOS_LIVE=1 against the running lab",
)

PAYMENT_URL = "http://localhost:8003"
ORDER_ID = "chaos-live-baseline"
SAMPLES = 5
# Postgres is started with log_min_duration_statement=200 (docker-compose.yml).
SLOW_STATEMENT_MS = 200


def _timed_charge() -> float:
    started = time.perf_counter()
    requests.get(f"{PAYMENT_URL}/charge", params={"order_id": ORDER_ID}, timeout=60).raise_for_status()
    return time.perf_counter() - started


def _median_latency() -> float:
    return statistics.median(_timed_charge() for _ in range(SAMPLES))


def _postgres_log_since(since: str) -> str:
    result = subprocess.run(
        ["docker", "logs", "--since", since, "postgres"],
        capture_output=True, text=True, timeout=60, shell=False,
    )
    return result.stdout + result.stderr


@pytest.fixture
def clean_payment_service():
    requests.post(f"{PAYMENT_URL}/chaos/slow/disable", timeout=10).raise_for_status()
    yield
    requests.post(f"{PAYMENT_URL}/chaos/slow/disable", timeout=10)


def test_slow_query_injector_makes_the_real_charge_lookup_slow(clean_payment_service):
    assert requests.get(f"{PAYMENT_URL}/health", timeout=10).json()["slow_query"] is False
    _timed_charge()  # first call inserts ORDER_ID, so every later call is a lookup hit
    baseline = _median_latency()

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    requests.post(f"{PAYMENT_URL}/chaos/slow/enable", timeout=10).raise_for_status()
    assert requests.get(f"{PAYMENT_URL}/health", timeout=10).json()["slow_query"] is True
    degraded = _median_latency()

    log = _postgres_log_since(started_at)
    slow_lines = [
        line for line in log.splitlines()
        if "duration:" in line and "FROM charges" in line and "WHERE" not in line.upper()
    ]

    print(f"\nbaseline median /charge latency (indexed lookup): {baseline * 1000:.1f} ms")
    print(f"chaos    median /charge latency (injected regression): {degraded * 1000:.1f} ms")
    print(f"postgres slow-statement log lines captured: {len(slow_lines)}")
    for line in slow_lines[:3]:
        print(f"  {line.strip()[:200]}")

    assert degraded > baseline * 5, (baseline, degraded)
    assert degraded * 1000 > SLOW_STATEMENT_MS, degraded
    assert slow_lines, "postgres logged no whole-table slow statement while chaos was on"
