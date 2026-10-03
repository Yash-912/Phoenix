"""The watcher's own loop: what it starts, in what order, and how one
incident's failure does not take the rest of the batch down with it.

No network, no LLM, no database -- list_unhandled_incidents and build_graph
are both monkeypatched, so this tests watcher.py's own logic, not the graph it
drives (that is test_graph.py's job) or the query it polls (test_persist.py's).
"""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

import pytest

from phoenix.graph import watcher


class _FakeApp:
    def __init__(self, results_by_incident: dict[int, object]):
        self._results = results_by_incident
        self.invoked_with: list[tuple[int, str]] = []

    def invoke(self, state):
        self.invoked_with.append((state.incident_id, state.service_name))
        result = self._results[state.incident_id]
        if isinstance(result, Exception):
            raise result
        return result


def _patch(monkeypatch, incidents: list[tuple[int, str]], results_by_incident: dict[int, object]):
    monkeypatch.setattr(watcher, "list_unhandled_incidents", lambda after_id=0: incidents)
    fake_app = _FakeApp(results_by_incident)
    monkeypatch.setattr(watcher, "build_graph", lambda: fake_app)
    return fake_app


def test_poll_once_forwards_after_id_to_list_unhandled_incidents(monkeypatch):
    seen = []
    monkeypatch.setattr(watcher, "list_unhandled_incidents", lambda after_id=0: seen.append(after_id) or [])

    watcher.poll_once(after_id=22)

    assert seen == [22]


def test_no_unhandled_incidents_calls_the_graph_not_at_all(monkeypatch):
    calls = []
    monkeypatch.setattr(watcher, "list_unhandled_incidents", lambda after_id=0: [])
    monkeypatch.setattr(watcher, "build_graph", lambda: calls.append("built") or pytest.fail("must not build"))

    started = watcher.poll_once()

    assert started == []
    assert calls == []


def test_one_unhandled_incident_is_run_against_its_own_state(monkeypatch):
    fake_app = _patch(
        monkeypatch,
        [(22, "checkout-service")],
        {22: {"status": "resolved"}},
    )

    started = watcher.poll_once()

    assert started == [22]
    assert fake_app.invoked_with == [(22, "checkout-service")]


def test_every_unhandled_incident_is_run_in_the_order_polled(monkeypatch):
    fake_app = _patch(
        monkeypatch,
        [(7, "auth-service"), (9, "payment-service")],
        {7: {"status": "resolved"}, 9: {"status": "escalated"}},
    )

    started = watcher.poll_once()

    assert started == [7, 9]
    assert fake_app.invoked_with == [(7, "auth-service"), (9, "payment-service")]


def test_one_incidents_exception_does_not_cancel_the_rest_of_the_batch(monkeypatch):
    """The failure mode this module exists to avoid: one bad run silently
    starving every incident behind it in the same poll."""
    fake_app = _patch(
        monkeypatch,
        [(1, "checkout-service"), (2, "auth-service")],
        {1: RuntimeError("the LLM endpoint is down"), 2: {"status": "resolved"}},
    )

    started = watcher.poll_once()

    assert started == [1, 2]
    assert fake_app.invoked_with == [(1, "checkout-service"), (2, "auth-service")]


def test_a_result_that_is_not_a_dict_does_not_raise_while_logging_status(monkeypatch):
    """invoke() on a compiled StateGraph returns a dict-like AddableValuesDict in
    practice, but the status line must not assume that -- it only ever reads it
    to print, never to route on."""

    class _ObjectResult:
        status = "resolved"

    _patch(monkeypatch, [(5, "worker-service")], {5: _ObjectResult()})

    started = watcher.poll_once()

    assert started == [5]


def test_run_forever_polls_then_sleeps_then_polls_again(monkeypatch):
    """Pins the loop shape without actually looping forever: the third sleep
    call raises to end the test, so the assertion is that two polls happened
    first, not that the loop never terminates."""
    monkeypatch.setattr(watcher, "max_incident_id", lambda: 0)
    poll_calls = []
    monkeypatch.setattr(watcher, "poll_once", lambda after_id=0: poll_calls.append(after_id) or [])

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(watcher.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        watcher.run_forever()

    assert poll_calls == [0, 0]
    assert sleeps == [watcher.POLL_INTERVAL_SECONDS, watcher.POLL_INTERVAL_SECONDS]


def test_run_forever_polls_with_the_floor_it_read_at_startup(monkeypatch):
    """The floor is read once, not re-read per poll -- an incident that gets
    investigated between polls must not need the floor raised to stay excluded;
    it is excluded because it now has an audit_log row."""
    monkeypatch.setattr(watcher, "max_incident_id", lambda: 22)
    poll_calls = []
    monkeypatch.setattr(watcher, "poll_once", lambda after_id=0: poll_calls.append(after_id) or [])
    monkeypatch.setattr(watcher.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        watcher.run_forever()

    assert poll_calls == [22]


def test_the_poll_interval_is_configurable_by_environment(monkeypatch):
    monkeypatch.setenv("PHOENIX_WATCH_INTERVAL_SECONDS", "3")
    import importlib

    reloaded = importlib.reload(watcher)
    try:
        assert reloaded.POLL_INTERVAL_SECONDS == 3
    finally:
        monkeypatch.delenv("PHOENIX_WATCH_INTERVAL_SECONDS", raising=False)
        importlib.reload(watcher)
