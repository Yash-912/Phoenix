"""Scenario 3's mechanism: a real cache lifecycle bug, not a raw list.

_cache_store_unbounded (the bug) and _cache_store_bounded (the fix) share
the same dict and the same lookup semantics -- the only difference is
whether old entries are ever evicted. These tests pin that difference
directly, pin that the dispatch in _process_job actually routes on
LEAK_ENABLED, and pin that _consume_one_tick -- the piece that makes the
leak grow on its own, without needing an external load generator -- calls
into that same real path rather than a separate one nothing else tests.
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
        def instrument(self, app):
            return self

        def expose(self, app, **kwargs):
            return self

    stub.Instrumentator = _Instrumentator
    monkeypatch.setitem(sys.modules, "prometheus_fastapi_instrumentator", stub)

    spec = importlib.util.spec_from_file_location("worker_service_cache_under_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unbounded_store_never_evicts_past_the_cap(module):
    module._CACHE_MAX_SIZE = 5
    for i in range(20):
        module._cache_store_unbounded(f"job-{i}", {"status": "processed"})

    assert len(module._cache) == 20


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


def test_process_job_routes_to_unbounded_when_leak_enabled(module):
    module.LEAK_ENABLED = True
    module._CACHE_MAX_SIZE = 1
    module._process_job("a")
    module._process_job("b")

    assert len(module._cache) == 2


def test_process_job_routes_to_bounded_when_leak_disabled(module):
    module.LEAK_ENABLED = False
    module._CACHE_MAX_SIZE = 1
    module._process_job("a")
    module._process_job("b")

    assert len(module._cache) == 1


def test_consume_one_tick_grows_the_real_cache_when_leak_enabled(module):
    """The piece that replaces an external load generator: ticking the
    consumer must exercise the same _process_job path /process does, so the
    leak genuinely grows from the service's own simulated traffic."""
    module.LEAK_ENABLED = True
    for _ in range(10):
        module._consume_one_tick()

    assert len(module._cache) == 10


def test_consume_one_tick_does_nothing_while_paused(module):
    module.PAUSED = True
    module.LEAK_ENABLED = True

    module._consume_one_tick()

    assert len(module._cache) == 0


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


def test_health_reports_the_real_cache_size(module):
    module._cache["a"] = {}
    module._cache["b"] = {}

    assert module.health()["cache_size"] == 2
