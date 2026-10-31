"""api-gateway answers with real HTTP status codes.

The routes used to return ({...}, 500) tuples, which FastAPI does not read as
(body, status): it serialises the tuple as a JSON list and answers 200. A broken gateway
therefore reported success to its callers and to Prometheus, so its 5xx error rate stayed
at zero while it was failing every request.
"""

import importlib.util
import pathlib
import sys
import types

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "services" / "api-gateway" / "app.py"


@pytest.fixture
def gateway(monkeypatch):
    stub = types.ModuleType("prometheus_fastapi_instrumentator")

    class _Instrumentator:
        def instrument(self, app, **kwargs):
            return self

        def expose(self, app, **kwargs):
            return self

    stub.Instrumentator = _Instrumentator
    monkeypatch.setitem(sys.modules, "prometheus_fastapi_instrumentator", stub)
    spec = importlib.util.spec_from_file_location("api_gateway_under_test", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_broken_gateway_answers_500_with_a_json_object(gateway):
    gateway.BROKEN = True

    response = TestClient(gateway.app).get("/route/auth")

    assert response.status_code == 500
    assert response.json() == {"error": "bad gateway deploy v18"}


def test_an_unknown_service_is_a_404_with_a_json_object(gateway):
    response = TestClient(gateway.app).get("/route/nope")

    assert response.status_code == 404
    assert response.json() == {"error": "unknown service nope"}


def test_an_unreachable_upstream_is_a_502_with_a_json_object(gateway, monkeypatch):
    def down(*args, **kwargs):
        raise gateway.requests.ConnectionError("connection refused")

    monkeypatch.setattr(gateway.requests, "get", down)

    response = TestClient(gateway.app).get("/route/auth")

    assert response.status_code == 502
    assert response.json() == {"error": "connection refused"}


def test_a_reachable_upstream_is_still_a_200(gateway, monkeypatch):
    monkeypatch.setattr(
        gateway.requests, "get",
        lambda *a, **k: types.SimpleNamespace(json=lambda: {"status": "ok"}),
    )

    response = TestClient(gateway.app).get("/route/auth")

    assert response.status_code == 200
    assert response.json() == {"service": "auth", "upstream": {"status": "ok"}}


def test_no_route_returns_a_status_tuple():
    import ast

    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    tuples = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
    ]

    assert tuples == []
