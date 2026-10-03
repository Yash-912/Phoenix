"""Scenario 2's mechanism: a real query regression, not a sleep().

/charge's idempotency check has two implementations behind the SLOW_QUERY
toggle -- _find_charge_fast (the fix: an indexed WHERE) and _find_charge_slow
(the bug: fetch every row, filter in Python). These tests pin three things a
"fix the toggle" patch could get wrong without this coverage: that both
paths agree on the actual result (the regression is a performance bug, not a
different bug wearing its name), that the slow path really does fetch the
whole table rather than secretly also filtering server-side, and that /charge
itself stays idempotent regardless of which path is active.
"""

import importlib.util
import pathlib
import sys
import types

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("psycopg2")

APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "services" / "payment-service" / "app.py"


class _FakeCursor:
    def __init__(self, rows: dict, queries: list):
        self._rows = rows
        self._queries = queries
        self._result = None

    def execute(self, query, params=()):
        q = " ".join(query.split())
        self._queries.append(q)
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
    def __init__(self, rows: dict | None = None):
        self.rows = rows if rows is not None else {}
        self.queries: list = []
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.rows, self.queries)


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

    spec = importlib.util.spec_from_file_location("payment_service_charge_under_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seeded_conn() -> _FakeConn:
    return _FakeConn(rows={"existing-order": {"amount": "42.00", "status": "charged"}})


def test_fast_path_finds_an_existing_charge_by_indexed_lookup(module):
    conn = _seeded_conn()
    module._get_conn = lambda: conn
    module.SLOW_QUERY = False

    result = module.find_charge("existing-order")

    assert result == ("42.00", "charged")
    assert conn.queries == ["SELECT amount, status FROM charges WHERE order_id = %s"]


def test_slow_path_finds_the_same_charge_but_by_fetching_every_row(module):
    """The regression is purely a performance one: same result, worse query."""
    conn = _seeded_conn()
    module._get_conn = lambda: conn
    module.SLOW_QUERY = True

    result = module.find_charge("existing-order")

    assert result == ("42.00", "charged")
    assert conn.queries == ["SELECT order_id, amount, status FROM charges"]


def test_slow_path_has_no_where_clause_at_all(module):
    """Pins the actual bug mechanism -- not "it's slow", but "it fetches the
    whole table and filters in Python instead of asking Postgres to."""
    conn = _seeded_conn()
    module._get_conn = lambda: conn
    module.SLOW_QUERY = True

    module.find_charge("existing-order")

    assert "where" not in conn.queries[0].lower()


@pytest.mark.parametrize("slow_query", [False, True])
def test_charge_is_idempotent_on_both_paths(module, slow_query):
    """A repeated order_id must return the original charge, not double-insert
    or double-charge, regardless of which query path answered the lookup."""
    conn = _FakeConn()
    module._get_conn = lambda: conn
    module.SLOW_QUERY = slow_query

    first = module.charge(order_id="repeat-me", amount=5.0)
    second = module.charge(order_id="repeat-me", amount=999.0)

    assert first["status"] == "charged"
    assert second["status"] == "charged"
    assert len(conn.rows) == 1
    assert conn.rows["repeat-me"]["amount"] == 5.0


def test_a_new_order_id_gets_inserted_and_charged(module):
    conn = _FakeConn()
    module._get_conn = lambda: conn
    module.SLOW_QUERY = False

    result = module.charge(order_id="brand-new", amount=12.5)

    assert result == {"order_id": "brand-new", "amount": 12.5, "status": "charged"}
    assert conn.rows["brand-new"] == {"amount": 12.5, "status": "charged"}


def test_chaos_slow_enable_and_disable_toggle_the_flag(module):
    module.chaos_slow_enable()
    assert module.SLOW_QUERY is True

    module.chaos_slow_disable()
    assert module.SLOW_QUERY is False


def test_chaos_slow_enable_is_refused_when_chaos_disabled(module, monkeypatch):
    monkeypatch.setattr(module, "CHAOS_ENABLED", False)

    result = module.chaos_slow_enable()

    assert result == {"error": "chaos disabled"}
    assert module.SLOW_QUERY is False
