"""Scenario 2: the payment-service charge lookup.

Two kinds of test live here, kept apart on purpose.

NORMAL APPLICATION TESTS describe how /charge must behave once the query
regression is fixed: the idempotency lookup finds the right row (or none)
whichever implementation answers it, and it asks the database for one row
rather than pulling the whole table across the wire. None of them depends on
the injected defect existing, so a correct fix to the slow lookup keeps them
green.

CHAOS ROUTING TESTS cover only the injector's wiring: SLOW_QUERY must dispatch
find_charge to _find_charge_slow, and clearing it must dispatch back to
_find_charge_fast. They deliberately say nothing about what _find_charge_slow
does inside, so they hold before and after the defect is repaired -- and they
fail a "fix" that merely disconnects the injector instead of repairing the
query. That the injected regression is genuinely slow against real Postgres is
proven by chaos/test_live_slow_query.py, not here.
"""

import importlib.util
import pathlib
import re
import sys
import types

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("psycopg2")

APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "services" / "payment-service" / "app.py"


class _FakeCursor:
    """Just enough SQL for this service's statements. Counts the rows it hands
    back to the client (stats["rows_sent"]) so a test can assert how much of
    the table a lookup dragged across the wire."""

    _SELECT = re.compile(r"^select (?P<cols>.+?) from charges", re.IGNORECASE)

    def __init__(self, rows: dict, queries: list, stats: dict):
        self._rows = rows
        self._queries = queries
        self._stats = stats
        self._result = None

    def execute(self, query, params=()):
        q = " ".join(query.split())
        self._queries.append(q)
        select = self._SELECT.match(q)
        if select:
            cols = [c.strip() for c in select.group("cols").split(",")]
            filtered = "where" in q.lower()
            self._result = [
                tuple(oid if c == "order_id" else row[c] for c in cols)
                for oid, row in self._rows.items()
                if not filtered or oid == params[0]
            ]
        elif q.startswith("INSERT INTO charges"):
            order_id, amount = params
            self._rows.setdefault(order_id, {"amount": amount, "status": "charged"})
        else:
            raise AssertionError(f"unexpected query: {query!r}")

    def fetchone(self):
        if not self._result:
            return None
        self._stats["rows_sent"] += 1
        return self._result[0]

    def fetchall(self):
        self._stats["rows_sent"] += len(self._result)
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, rows: dict | None = None):
        self.rows = rows if rows is not None else {}
        self.queries: list = []
        self.stats = {"rows_sent": 0}
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.rows, self.queries, self.stats)


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

    spec = importlib.util.spec_from_file_location("payment_service_charge_under_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seeded_conn() -> _FakeConn:
    return _FakeConn(rows={"existing-order": {"amount": "42.00", "status": "charged"}})


def _many_orders_conn(count: int = 500) -> _FakeConn:
    return _FakeConn(rows={f"order-{i}": {"amount": f"{i}.00", "status": "charged"} for i in range(count)})


# ---- normal application behaviour -----------------------------------------


def test_slow_query_chaos_is_off_by_default(module):
    assert module.SLOW_QUERY is False


def test_find_charge_returns_amount_and_status_of_an_existing_order(module):
    module._get_conn = lambda: _seeded_conn()

    assert module.find_charge("existing-order") == ("42.00", "charged")


def test_find_charge_returns_none_for_an_unknown_order(module):
    module._get_conn = lambda: _seeded_conn()

    assert module.find_charge("never-charged") is None


def test_find_charge_picks_the_right_order_among_many(module):
    conn = _many_orders_conn()
    module._get_conn = lambda: conn

    assert module.find_charge("order-17") == ("17.00", "charged")
    assert module.find_charge("order-499") == ("499.00", "charged")
    assert module.find_charge("order-500") is None


def test_find_charge_does_not_pull_the_table_to_find_one_order(module):
    """The performance contract behind Scenario 2: answering "has this order
    been charged?" must cost one row, not every row in a ~1M-row table."""
    conn = _many_orders_conn(500)
    module._get_conn = lambda: conn

    result = module.find_charge("order-250")

    assert result == ("250.00", "charged")
    assert conn.stats["rows_sent"] <= 1


@pytest.mark.parametrize("slow_query", [False, True])
def test_charge_is_idempotent_whichever_lookup_answers(module, slow_query):
    """A repeated order_id must return the original charge, not double-insert
    or double-charge. Holds whichever lookup implementation is active -- the
    toggle may change how fast the lookup is, never what it answers."""
    conn = _FakeConn()
    module._get_conn = lambda: conn
    module.SLOW_QUERY = slow_query

    first = module.charge(order_id="repeat-me", amount=5.0)
    second = module.charge(order_id="repeat-me", amount=999.0)

    assert first["status"] == "charged"
    assert second["status"] == "charged"
    assert len(conn.rows) == 1
    assert conn.rows["repeat-me"]["amount"] == 5.0


@pytest.mark.parametrize("slow_query", [False, True])
def test_both_lookups_agree_on_found_and_missing_orders(module, slow_query):
    conn = _many_orders_conn(20)
    module._get_conn = lambda: conn
    module.SLOW_QUERY = slow_query

    assert module.find_charge("order-7") == ("7.00", "charged")
    assert module.find_charge("order-404") is None


def test_a_new_order_id_gets_inserted_and_charged(module):
    conn = _FakeConn()
    module._get_conn = lambda: conn
    module.SLOW_QUERY = False

    result = module.charge(order_id="brand-new", amount=12.5)

    assert result == {"order_id": "brand-new", "amount": 12.5, "status": "charged"}
    assert conn.rows["brand-new"] == {"amount": 12.5, "status": "charged"}


# ---- chaos injector wiring -------------------------------------------------


def test_chaos_enabled_routes_the_lookup_through_the_slow_implementation(module):
    module._find_charge_slow = lambda order_id: ("via-slow", order_id)
    module._find_charge_fast = lambda order_id: ("via-fast", order_id)
    module.SLOW_QUERY = True

    assert module.find_charge("x") == ("via-slow", "x")


def test_chaos_disabled_routes_the_lookup_through_the_fast_implementation(module):
    module._find_charge_slow = lambda order_id: ("via-slow", order_id)
    module._find_charge_fast = lambda order_id: ("via-fast", order_id)
    module.SLOW_QUERY = False

    assert module.find_charge("x") == ("via-fast", "x")


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
