"""Scenario 5's mechanism: a bounded, self-clearing, category-neutral failure.

Mirrors test_demo_checkout_logging.py's approach -- load the service module
fresh, read what it actually logs -- for a different property: not that the
failure is findable, but that it is findable as *nothing in particular*. The
log line is checked against scoring.CATEGORY_KEYWORDS directly rather than by
eyeballing the string, so a future edit to either the message or the keyword
lists cannot silently reintroduce a match.
"""

import importlib.util
import io
import logging
import pathlib
import sys
import types

import pytest

pytest.importorskip("fastapi")

from phoenix.graph.scoring import CATEGORY_KEYWORDS

APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "services" / "payment-service" / "app.py"


class _FakeCursor:
    """Stands in for a psycopg2 cursor against an empty charges table --
    enough for charge() to run its real logic (check, miss, insert) without
    a live Postgres connection. See test_payment_service_charge.py for the
    tests that actually exercise the fast/slow query logic itself."""

    def __init__(self, rows: dict):
        self._rows = rows
        self._result = None

    def execute(self, query, params=()):
        q = " ".join(query.split())
        if q.startswith("SELECT amount, status FROM charges WHERE order_id"):
            row = self._rows.get(params[0])
            self._result = (row["amount"], row["status"]) if row else None
        elif q.startswith("SELECT order_id, amount, status FROM charges"):
            self._result = [(oid, r["amount"], r["status"]) for oid, r in self._rows.items()]
        elif q.startswith("INSERT INTO charges"):
            order_id, amount = params
            self._rows.setdefault(order_id, {"amount": amount, "status": "charged"})
        else:
            raise AssertionError(f"unexpected query: {query!r}")

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.rows: dict = {}
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.rows)


@pytest.fixture
def service(monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [], raising=False)
    monkeypatch.setenv("CHAOS_ENABLED", "true")

    stub = types.ModuleType("prometheus_fastapi_instrumentator")

    class _Instrumentator:
        def instrument(self, app):
            return self

        def expose(self, app, **kwargs):
            return self

    stub.Instrumentator = _Instrumentator
    monkeypatch.setitem(sys.modules, "prometheus_fastapi_instrumentator", stub)

    spec = importlib.util.spec_from_file_location("payment_service_under_test", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._get_conn = lambda: _FakeConn()

    emitted = io.StringIO()
    installed = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    if not installed:
        pytest.fail("the service installed no log handler, so a blip would log nowhere searchable")
    installed[0].stream = emitted

    return module, emitted


def test_outside_a_blip_charge_succeeds_normally(service):
    module, emitted = service

    result = module.charge()

    assert result["status"] == "charged"
    assert emitted.getvalue() == ""


def test_a_started_blip_makes_charge_fail(service):
    module, emitted = service

    module.chaos_blip_start(duration_seconds=45)
    with pytest.raises(Exception):
        module.charge()

    assert "ERROR" in emitted.getvalue()


def test_the_blip_disabled_by_chaos_enabled_does_nothing(monkeypatch):
    """The same CHAOS_ENABLED gate every other chaos endpoint on this service
    respects -- a blip must not be startable in an environment that disabled
    chaos."""
    monkeypatch.setenv("CHAOS_ENABLED", "false")
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [], raising=False)
    stub = types.ModuleType("prometheus_fastapi_instrumentator")

    class _Instrumentator:
        def instrument(self, app):
            return self

        def expose(self, app, **kwargs):
            return self

    stub.Instrumentator = _Instrumentator
    monkeypatch.setitem(sys.modules, "prometheus_fastapi_instrumentator", stub)
    spec = importlib.util.spec_from_file_location("payment_service_disabled", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._get_conn = lambda: _FakeConn()

    result = module.chaos_blip_start(duration_seconds=45)

    assert result == {"error": "chaos disabled"}
    assert module.charge()["status"] == "charged"


def test_the_blip_self_clears_once_its_duration_has_elapsed(service, monkeypatch):
    """Bounded by time, not by a second call to /chaos/blip/stop -- a chaos
    script that crashed mid-run must not leave the service broken forever."""
    module, _emitted = service
    module.chaos_blip_start(duration_seconds=45)

    monkeypatch.setattr(module.time, "time", lambda: module.BLIP_UNTIL + 1)

    result = module.charge()

    assert result["status"] == "charged"


def test_stop_clears_the_blip_immediately(service):
    module, _emitted = service
    module.chaos_blip_start(duration_seconds=45)

    module.chaos_blip_stop()

    assert module.charge()["status"] == "charged"


def test_the_blip_log_line_matches_no_category_keyword(service):
    """The property Scenario 5 actually depends on: scoring must find nothing
    to support any hypothesis from this evidence. Checked against the live
    keyword lists, not a copy of them, so the two cannot drift apart."""
    module, emitted = service

    module.chaos_blip_start(duration_seconds=45)
    with pytest.raises(Exception):
        module.charge()

    line = emitted.getvalue().lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            assert keyword not in line, (
                f"blip log line unexpectedly matches {category!r} via {keyword!r}: {line!r}"
            )


def test_the_blip_writes_no_deployment_or_config_marker(service, monkeypatch, tmp_path):
    """The other half of ambiguity: nothing in deployment history should even
    exist for correlation.py to read, let alone point at a cause."""
    monkeypatch.setenv("PHOENIX_DEPLOYMENTS_ROOT", str(tmp_path))
    module, _emitted = service

    module.chaos_blip_start(duration_seconds=45)
    with pytest.raises(Exception):
        module.charge()
    module.chaos_blip_stop()

    assert list(tmp_path.rglob("*.json")) == []
