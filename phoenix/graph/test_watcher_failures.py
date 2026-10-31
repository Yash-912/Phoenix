"""A failing incident is retried a bounded number of times, with backoff, and then
marked failed on the audit trail -- not retried every poll for ever.

An outage run ends with status llm_unavailable (see test_llm_outage.py) and already has
audit rows, so list_unhandled_incidents no longer returns it; the watcher keeps its own
record of it. A run that raises before writing any row is returned on every poll, and
the same record holds it back. No network, no LLM, no database, and no real clock.
"""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

import pytest

from phoenix.graph import watcher

UNAVAILABLE = {"status": "llm_unavailable", "escalation_reason": "decide_tool_calls: the LLM call failed (x)",
               "remediation_attempts": 0, "service_name": "svc"}
FINISHED = {"status": "insufficient_evidence", "service_name": "svc"}


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _App:
    """Hands out the scripted result for an incident, one per run, and counts runs."""

    def __init__(self, script: dict[int, list], checkpoint=None):
        self.script = {k: list(v) for k, v in script.items()}
        self.runs: list[int] = []
        # incident id -> the state the checkpointer holds after the run raised
        self.checkpoint = checkpoint or {}

    def get_state(self, config):
        incident = int(config["configurable"]["thread_id"])
        value = self.checkpoint.get(incident, {})
        if isinstance(value, Exception):
            raise value
        return type("Snapshot", (), {"values": value})()

    def invoke(self, state, config=None):
        self.runs.append(state.incident_id)
        queue = self.script[state.incident_id]
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def world(monkeypatch):
    clock = _Clock()
    audit = []
    unhandled: list[tuple[int, str]] = []
    monkeypatch.setattr(watcher, "_failures", {})
    monkeypatch.setattr(watcher, "_now", clock)
    monkeypatch.setattr(watcher, "MAX_FAILURES", 3)
    monkeypatch.setattr(watcher, "BACKOFF_BASE_SECONDS", 30.0)
    monkeypatch.setattr(watcher, "BACKOFF_CAP_SECONDS", 100.0)
    monkeypatch.setattr(watcher, "record_audit", lambda incident, node, event, detail, text: audit.append((incident, node, event, detail, text)))
    monkeypatch.setattr(watcher, "remember_run", lambda result: None)
    monkeypatch.setattr(watcher.safety_policy, "load_config", lambda: type("C", (), {"execution_policy": "guarded"})())
    monkeypatch.setattr(watcher, "list_unhandled_incidents", lambda after_id=0: list(unhandled))

    class World:
        pass

    w = World()
    w.clock, w.audit, w.unhandled = clock, audit, unhandled

    def script(results: dict[int, list], checkpoint=None):
        app = _App(results, checkpoint)
        monkeypatch.setattr(watcher, "build_graph", lambda checkpointer=None: app)
        w.app = app

    w.script = script
    return w


def _failed_events(world):
    return [(i, e) for i, _n, e, _d, _t in world.audit if e == "investigation_failed"]


def test_an_outage_run_is_not_retried_before_its_backoff_has_elapsed(world):
    world.unhandled.append((1, "svc"))
    world.script({1: [UNAVAILABLE]})

    assert watcher.poll_once() == [1]
    world.unhandled.clear()

    world.clock.now += 29
    assert watcher.poll_once() == []
    assert world.app.runs == [1]


def test_an_outage_run_is_retried_once_the_backoff_has_elapsed_even_though_it_left_audit_rows(world):
    world.unhandled.append((1, "svc"))
    world.script({1: [UNAVAILABLE, FINISHED]})
    watcher.poll_once()
    world.unhandled.clear()

    world.clock.now += 31
    assert watcher.poll_once() == [1]

    assert world.app.runs == [1, 1]
    assert 1 not in watcher._failures
    assert _failed_events(world) == []


def test_the_backoff_doubles_and_is_capped(world):
    assert watcher._backoff(1) == 30
    assert watcher._backoff(2) == 60
    assert watcher._backoff(3) == 100
    assert watcher._backoff(9) == 100


def test_the_watcher_gives_up_after_the_configured_number_of_failures_and_says_so(world):
    world.unhandled.append((1, "svc"))
    world.script({1: [UNAVAILABLE]})

    for _ in range(3):
        watcher.poll_once()
        world.clock.now += 1000

    assert world.app.runs == [1, 1, 1]
    failed = [row for row in world.audit if row[2] == "investigation_failed"]
    assert len(failed) == 1
    incident, node, _event, detail, text = failed[0]
    assert (incident, node) == (1, "watcher")
    assert detail["attempts"] == 3
    assert "LLM call failed" in detail["error"]
    assert "3" in text


def test_a_failed_incident_is_never_run_again(world):
    world.unhandled.append((1, "svc"))
    world.script({1: [UNAVAILABLE]})
    for _ in range(3):
        watcher.poll_once()
        world.clock.now += 1000

    for _ in range(5):
        assert watcher.poll_once() == []
        world.clock.now += 1000

    assert world.app.runs == [1, 1, 1]
    assert len(_failed_events(world)) == 1


def test_an_exception_before_any_audit_row_is_bounded_the_same_way(world):
    world.unhandled.append((1, "svc"))
    world.script({1: [RuntimeError("db gone")]})

    started = []
    for _ in range(8):
        started += watcher.poll_once()
        world.clock.now += 1000

    assert started == [1, 1, 1]
    failed = [row for row in world.audit if row[2] == "investigation_failed"]
    assert len(failed) == 1 and "db gone" in failed[0][3]["error"]


def test_an_exception_is_not_retried_before_its_backoff_has_elapsed(world):
    world.unhandled.append((1, "svc"))
    world.script({1: [RuntimeError("boom")]})
    watcher.poll_once()

    world.clock.now += 5
    assert watcher.poll_once() == []


def test_a_run_that_already_acted_is_not_retried_after_an_outage(world):
    acted = {**UNAVAILABLE, "remediation_attempts": 1}
    world.unhandled.append((1, "svc"))
    world.script({1: [acted]})

    watcher.poll_once()
    world.clock.now += 1000
    watcher.poll_once()

    assert world.app.runs == [1]
    failed = [row for row in world.audit if row[2] == "investigation_failed"]
    assert len(failed) == 1 and failed[0][3]["retry_safe"] is False


def test_a_run_that_pushed_a_branch_is_not_retried_after_an_outage(world):
    pushed = {**UNAVAILABLE, "worktree_branch": "phoenix/tier3-incident-1-svc-1"}
    world.unhandled.append((1, "svc"))
    world.script({1: [pushed]})

    watcher.poll_once()
    world.clock.now += 1000
    watcher.poll_once()

    assert world.app.runs == [1]


def test_a_failing_incident_does_not_hold_back_the_next_one(world):
    world.unhandled.extend([(1, "svc"), (2, "svc")])
    world.script({1: [UNAVAILABLE], 2: [FINISHED]})

    assert watcher.poll_once() == [1, 2]
    world.unhandled.clear()  # incident 2 finished, so it now has audit rows
    world.clock.now += 5

    assert watcher.poll_once() == []
    assert world.app.runs == [1, 2]


def test_a_successful_run_clears_the_failure_record(world):
    world.unhandled.append((1, "svc"))
    world.script({1: [RuntimeError("once"), FINISHED]})
    watcher.poll_once()
    assert 1 in watcher._failures

    world.clock.now += 31
    watcher.poll_once()

    assert 1 not in watcher._failures


def test_the_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("PHOENIX_WATCH_MAX_FAILURES", "5")
    monkeypatch.setenv("PHOENIX_WATCH_BACKOFF_SECONDS", "7")

    assert watcher._max_failures_from_env() == 5
    assert watcher._backoff_base_from_env() == 7.0

    monkeypatch.setenv("PHOENIX_WATCH_MAX_FAILURES", "0")
    monkeypatch.setenv("PHOENIX_WATCH_BACKOFF_SECONDS", "nope")

    assert watcher._max_failures_from_env() == watcher.DEFAULT_MAX_FAILURES
    assert watcher._backoff_base_from_env() == watcher.DEFAULT_BACKOFF_BASE_SECONDS


# ---- an exception after the run acted is not retried either ----------------------------------


@pytest.mark.parametrize("acted", [
    {"remediation_attempts": 1},
    {"worktree_branch": "phoenix/tier3-incident-1-svc-1"},
    {"pr_result": {"status": "ok"}},
])
def test_an_exception_after_the_run_acted_is_failed_at_once_and_never_retried(world, acted):
    world.unhandled.append((1, "svc"))
    world.script({1: [RuntimeError("verifier blew up")]}, checkpoint={1: acted})

    started = []
    for _ in range(5):
        started += watcher.poll_once()
        world.clock.now += 1000

    assert started == [1]
    failed = [row for row in world.audit if row[2] == "investigation_failed"]
    assert len(failed) == 1
    assert failed[0][3]["retry_safe"] is False
    assert failed[0][3]["attempts"] == 1
    assert "verifier blew up" in failed[0][3]["error"]


def test_an_exception_before_any_action_is_still_retried_with_backoff_and_capped(world):
    world.unhandled.append((1, "svc"))
    world.script({1: [RuntimeError("db gone")]}, checkpoint={1: {"remediation_attempts": 0, "worktree_branch": None, "pr_result": None}})

    assert watcher.poll_once() == [1]
    world.clock.now += 5
    assert watcher.poll_once() == []
    world.clock.now += 1000
    assert watcher.poll_once() == [1]
    world.clock.now += 1000
    assert watcher.poll_once() == [1]
    world.clock.now += 1000

    assert watcher.poll_once() == []
    failed = [row for row in world.audit if row[2] == "investigation_failed"]
    assert len(failed) == 1 and failed[0][3]["attempts"] == 3 and failed[0][3]["retry_safe"] is True


def test_an_exception_when_the_run_state_cannot_be_read_is_treated_as_possibly_acted(world):
    world.unhandled.append((1, "svc"))
    world.script({1: [RuntimeError("boom")]}, checkpoint={1: RuntimeError("checkpointer down")})

    watcher.poll_once()
    world.clock.now += 1000
    watcher.poll_once()

    assert world.app.runs == [1]
    failed = [row for row in world.audit if row[2] == "investigation_failed"]
    assert len(failed) == 1 and failed[0][3]["retry_safe"] is False


def test_a_graph_that_could_not_even_be_built_is_retried(world, monkeypatch):
    world.unhandled.append((1, "svc"))
    calls = []

    def broken_build(checkpointer=None):
        calls.append(1)
        raise RuntimeError("import error")

    monkeypatch.setattr(watcher, "build_graph", broken_build)

    watcher.poll_once()
    world.clock.now += 1000
    watcher.poll_once()

    assert len(calls) == 2
