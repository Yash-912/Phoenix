"""worker-service's /chaos/pause is behind the same CHAOS_ENABLED gate as its other
chaos endpoints. Pausing the consumer is a fault injection like the others, and on a
service started without chaos enabled it must not be reachable by anyone who can send
a POST. Resume stays open: clearing a fault must always be possible."""

import importlib.util
import pathlib
import sys
import types

import pytest

pytest.importorskip("fastapi")

APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "services" / "worker-service" / "app.py"


def _load(monkeypatch, chaos_enabled: str):
    monkeypatch.setenv("CHAOS_ENABLED", chaos_enabled)
    stub = types.ModuleType("prometheus_fastapi_instrumentator")

    class _Instrumentator:
        def instrument(self, app, **kwargs):
            return self

        def expose(self, app, **kwargs):
            return self

    stub.Instrumentator = _Instrumentator
    monkeypatch.setitem(sys.modules, "prometheus_fastapi_instrumentator", stub)
    spec = importlib.util.spec_from_file_location(f"worker_chaos_gate_{chaos_enabled}", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pause_is_refused_when_chaos_is_disabled(monkeypatch):
    module = _load(monkeypatch, "false")

    assert module.chaos_pause() == {"error": "chaos disabled"}
    assert module.PAUSED is False


def test_pause_works_when_chaos_is_enabled(monkeypatch):
    module = _load(monkeypatch, "true")

    assert module.chaos_pause() == {"paused": True}
    assert module.PAUSED is True


def test_resume_is_never_gated(monkeypatch):
    module = _load(monkeypatch, "false")
    module.PAUSED = True

    assert module.chaos_resume() == {"paused": False}
    assert module.PAUSED is False
