"""Scenario 3: the worker-service job-result cache.

Two kinds of test live here, kept apart on purpose.

NORMAL APPLICATION TESTS describe the production contract: the cache is
bounded at _CACHE_MAX_SIZE, the oldest entry is evicted once it is full,
refreshing a key is not growth, and the ordinary job path -- both /process and
the background consumer tick -- stores through that bounded policy and so
never grows without limit. None of them depends on the injected leak existing,
so a correct fix to the unbounded store keeps them green.

CHAOS ROUTING TESTS cover only the injector's wiring: LEAK_ENABLED must send
_process_job's storage through _cache_store_unbounded, and clearing it must
send it back through _cache_store_bounded. They deliberately say nothing about
what _cache_store_unbounded does inside, so they hold before and after the leak
is repaired -- and they fail a "fix" that merely disconnects the injector
instead of repairing the store. That the injected leak really grows the cache
and the process's memory is proven against the live worker by
chaos/test_live_memory_leak.py, not here.
"""

import importlib.util
import pathlib
import sys
import types

import pytest

pytest.importorskip("fastapi")

APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "services" / "worker-service" / "app.py"


@pytest.fixture
def module(monkeypatch):
    monkeypatch.setenv("CHAOS_ENABLED", "true")
    stub = types.ModuleType("prometheus_fastapi_instrumentator")

    class _Instrumentator:
        def instrument(self, app, **kwargs):
            return self

        def expose(self, app, **kwargs):
            return self

    stub.Instrumentator = _Instrumentator
    monkeypatch.setitem(sys.modules, "prometheus_fastapi_instrumentator", stub)

    spec = importlib.util.spec_from_file_location("worker_service_cache_under_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- normal application behaviour -----------------------------------------


def test_leak_chaos_is_off_by_default(module):
    assert module.LEAK_ENABLED is False


def test_bounded_store_evicts_the_oldest_entry_once_full(module):
    module._CACHE_MAX_SIZE = 5
    for i in range(20):
        module._cache_store_bounded(f"job-{i}", {"status": "processed"})

    assert len(module._cache) == 5
    # Only the most recent 5 survive -- the oldest ones were evicted first.
    assert set(module._cache.keys()) == {f"job-{i}" for i in range(15, 20)}


def test_bounded_store_refreshing_an_existing_key_does_not_evict(module):
    """Re-caching a job already present must not count as growth -- the
    fixed cache's size must stay flat on repeat keys, not just on new ones."""
    module._CACHE_MAX_SIZE = 3
    for i in range(3):
        module._cache_store_bounded(f"job-{i}", {"status": "processed"})

    module._cache_store_bounded("job-0", {"status": "reprocessed"})

    assert len(module._cache) == 3
    assert module._cache["job-0"]["status"] == "reprocessed"


def test_process_job_keeps_the_cache_within_its_cap(module):
    module._CACHE_MAX_SIZE = 10
    peak = 0
    for i in range(200):
        module._process_job(f"job-{i}")
        peak = max(peak, len(module._cache))

    assert peak <= 10
    assert len(module._cache) == 10


def test_process_job_evicts_the_oldest_job_first(module):
    module._CACHE_MAX_SIZE = 3
    for i in range(6):
        module._process_job(f"job-{i}")

    assert list(module._cache) == ["job-3", "job-4", "job-5"]


def test_process_job_returns_the_processed_result_and_caches_it(module):
    result = module._process_job("job-a")

    assert result["status"] == "processed"
    assert result["job_id"] == "job-a"
    assert module._cache["job-a"] is result


def test_consume_one_tick_keeps_the_cache_within_its_cap(module):
    """The background consumer is the service's own steady traffic, so it is
    what decides whether memory stays flat in production: it must stay bounded."""
    module._CACHE_MAX_SIZE = 5
    for _ in range(50):
        module._consume_one_tick()

    assert len(module._cache) == 5


def test_consume_one_tick_does_nothing_while_paused(module):
    module.PAUSED = True

    module._consume_one_tick()

    assert len(module._cache) == 0


def test_process_endpoint_keeps_the_cache_within_its_cap(module):
    module._CACHE_MAX_SIZE = 4
    for _ in range(25):
        assert module.process() == {"status": "processed"}

    assert len(module._cache) == 4


def test_health_reports_the_real_cache_size(module):
    module._cache["a"] = {}
    module._cache["b"] = {}

    assert module.health()["cache_size"] == 2


# ---- chaos injector wiring -------------------------------------------------


def _record_storage_route(module, monkeypatch):
    calls = []
    monkeypatch.setattr(module, "_cache_store_unbounded", lambda job_id, result: calls.append("unbounded"))
    monkeypatch.setattr(module, "_cache_store_bounded", lambda job_id, result: calls.append("bounded"))
    return calls


def test_chaos_enabled_routes_job_storage_through_the_unbounded_store(module, monkeypatch):
    calls = _record_storage_route(module, monkeypatch)
    module.LEAK_ENABLED = True

    module._process_job("a")

    assert calls == ["unbounded"]


def test_chaos_disabled_routes_job_storage_through_the_bounded_store(module, monkeypatch):
    calls = _record_storage_route(module, monkeypatch)
    module.LEAK_ENABLED = False

    module._process_job("a")

    assert calls == ["bounded"]


def test_chaos_enabled_consumer_ticks_use_the_unbounded_store(module, monkeypatch):
    """The piece that replaces an external load generator: ticking the
    consumer must exercise the same _process_job path /process does, so a
    leak genuinely accrues from the service's own simulated traffic."""
    calls = _record_storage_route(module, monkeypatch)
    module.LEAK_ENABLED = True

    for _ in range(3):
        module._consume_one_tick()

    assert calls == ["unbounded"] * 3


def test_chaos_leak_start_enables_the_flag(module):
    module.chaos_leak_start()

    assert module.LEAK_ENABLED is True


def test_chaos_leak_stop_disables_the_flag_and_clears_the_cache(module):
    module.LEAK_ENABLED = True
    module._cache["stale-job"] = {"status": "processed"}

    module.chaos_leak_stop()

    assert module.LEAK_ENABLED is False
    assert module._cache == {}


def test_chaos_leak_start_is_refused_when_chaos_disabled(module, monkeypatch):
    monkeypatch.setattr(module, "CHAOS_ENABLED", False)

    result = module.chaos_leak_start()

    assert result == {"error": "chaos disabled"}
    assert module.LEAK_ENABLED is False
