"""Scenario 4: auth-service's database connection pool is a real constraint.

DB_POOL_SIZE used to be a number /health echoed back, so shrinking it changed
nothing observable: no alert fired and the agent had no symptom to investigate.
The pool now bounds how many /validate requests can hold a connection at once.
A request that cannot get one within POOL_WAIT_SECONDS is rejected with a 503
and a log line saying why, and /health reports the saturation in words.

These tests describe that contract. They never name the injected value, so they
hold for any pool size the service is configured with.
"""

import importlib.util
import logging
import pathlib
import sys
import threading
import types

import pytest

pytest.importorskip("fastapi")

APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "services" / "auth-service" / "app.py"


@pytest.fixture
def load(monkeypatch):
    stub = types.ModuleType("prometheus_fastapi_instrumentator")

    class _Instrumentator:
        def instrument(self, app, **kwargs):
            return self

        def expose(self, app, **kwargs):
            return self

    stub.Instrumentator = _Instrumentator
    monkeypatch.setitem(sys.modules, "prometheus_fastapi_instrumentator", stub)

    def _load(pool_size: str):
        monkeypatch.setenv("DB_POOL_SIZE", pool_size)
        spec = importlib.util.spec_from_file_location(f"auth_service_pool_{pool_size}", APP_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.POOL_WAIT_SECONDS = 0.05
        module.QUERY_SECONDS = 0.3
        return module

    return _load


def _burst(module, count: int) -> list:
    """Call /validate from `count` threads at once; each result is the payload or the HTTPException."""
    results: list = [None] * count

    def call(index):
        try:
            results[index] = module.validate()
        except Exception as exc:  # noqa: BLE001 -- the rejection is what is being measured
            results[index] = exc

    threads = [threading.Thread(target=call, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def _rejected(results) -> list:
    return [r for r in results if getattr(r, "status_code", None) == 503]


def test_requests_within_the_pool_all_succeed(load):
    module = load("4")

    results = _burst(module, 4)

    assert all(r == {"valid": True, "user": "demo-user"} for r in results)


def test_requests_beyond_the_pool_are_rejected_not_queued_forever(load):
    module = load("2")

    results = _burst(module, 6)

    assert len(_rejected(results)) == 4
    assert sum(1 for r in results if isinstance(r, dict)) == 2


def test_a_released_connection_serves_the_next_request(load):
    module = load("1")

    first = _burst(module, 1)
    second = _burst(module, 1)

    assert first == second == [{"valid": True, "user": "demo-user"}]


def test_a_rejection_logs_why_it_happened(load, caplog):
    module = load("1")

    with caplog.at_level(logging.ERROR):
        _burst(module, 3)

    assert any("connection pool exhausted" in record.getMessage() for record in caplog.records)


def test_health_reports_the_configured_pool_size_and_no_detail_when_healthy(load):
    module = load("10")

    body = module.health()

    assert body["db_pool_size"] == "10"
    assert body["status"] == "ok"
    assert "detail" not in body


def test_health_says_so_in_words_after_requests_were_rejected(load):
    module = load("1")
    _burst(module, 3)

    body = module.health()

    assert body["db_pool_size"] == "1"
    assert body["status"] == "degraded"
    assert "connection pool" in body["detail"]
    assert "rejected" in body["detail"]
