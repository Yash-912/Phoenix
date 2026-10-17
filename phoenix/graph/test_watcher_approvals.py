"""The watcher's side of an approval: holding a paused run, resuming it when it is
decided or expired, and leaving it alone while it is still pending.

As in test_watcher.py, nothing here reaches the database or the graph. The
approval store and the audit writer are stubbed; the compiled graph is a fake that
records what it was resumed with.
"""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from langgraph.types import Command

from phoenix.graph import approvals, watcher
from phoenix.graph.state import AgentState


class _Interrupt:
    def __init__(self, approval_id):
        self.value = {"approval_id": approval_id, "action": "rollback_deployment", "container": "checkout-service"}


class _PausingApp:
    """First invoke pauses on an approval. A resume returns the final result."""

    def __init__(self, approval_id, final):
        self.approval_id = approval_id
        self.final = final
        self.invocations: list[tuple[object, dict | None]] = []

    def invoke(self, state_or_command, config=None):
        self.invocations.append((state_or_command, config))
        if isinstance(state_or_command, Command):
            return self.final
        return {"__interrupt__": [_Interrupt(self.approval_id)]}


class _Stores:
    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.audit: list[dict] = []
        self.expired: list[dict] = []
        self.read_error = None
        self.sweep_error = None

    def read(self, approval_id):
        if self.read_error:
            raise self.read_error
        return self.rows.get(approval_id)

    def sweep(self):
        if self.sweep_error:
            raise self.sweep_error
        rows, self.expired = self.expired, []
        return rows

    def record_audit(self, incident_id, node, event_type, detail, reasoning_text):
        self.audit.append({"incident_id": incident_id, "event_type": event_type, "detail": detail})


def _install(monkeypatch) -> _Stores:
    stores = _Stores()
    monkeypatch.setattr(approvals, "read_approval", stores.read)
    monkeypatch.setattr(approvals, "expire_stale_approvals", stores.sweep)
    monkeypatch.setattr(watcher, "record_audit", stores.record_audit)
    watcher._paused.clear()
    return stores


def _pause(monkeypatch, approval_id=7, incident_id=1, final=None) -> _PausingApp:
    """Run one incident through poll_once, with the graph stubbed to pause."""
    app = _PausingApp(approval_id, final or {"status": "resolved"})
    monkeypatch.setattr(watcher, "list_unhandled_incidents", lambda after_id=0: [(incident_id, "checkout-service")])
    monkeypatch.setattr(watcher, "build_graph", lambda checkpointer=None: app)
    watcher.poll_once()
    return app


def test_a_run_that_asks_for_approval_is_held_under_its_approval_id(monkeypatch, capsys):
    _install(monkeypatch)

    app = _pause(monkeypatch)

    assert list(watcher._paused) == [7]
    assert watcher._paused[7][0] == 1
    assert "paused, waiting on approval 7" in capsys.readouterr().out
    assert len(app.invocations) == 1


def test_each_incident_runs_with_a_checkpointer_and_its_own_thread(monkeypatch):
    _install(monkeypatch)
    seen = {}

    class _App(_PausingApp):
        def invoke(self, state_or_command, config=None):
            seen["config"] = config
            return super().invoke(state_or_command, config)

    app = _App(7, {"status": "resolved"})
    monkeypatch.setattr(watcher, "list_unhandled_incidents", lambda after_id=0: [(4, "checkout-service")])

    def build(checkpointer=None):
        seen["checkpointer"] = checkpointer
        return app

    monkeypatch.setattr(watcher, "build_graph", build)
    watcher.poll_once()

    assert seen["checkpointer"] is not None
    assert seen["config"] == {"configurable": {"thread_id": "4"}}


def test_the_run_takes_its_policy_from_configuration(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setenv("PHOENIX_EXECUTION_POLICY", "autonomous_lab")
    states = []

    class _App(_PausingApp):
        def invoke(self, state_or_command, config=None):
            states.append(state_or_command)
            return {"status": "resolved"}

    monkeypatch.setattr(watcher, "list_unhandled_incidents", lambda after_id=0: [(4, "checkout-service")])
    monkeypatch.setattr(watcher, "build_graph", lambda checkpointer=None: _App(7, None))
    watcher.poll_once()

    assert isinstance(states[0], AgentState)
    assert states[0].policy_mode == "autonomous_lab"


def test_a_pending_request_leaves_its_run_paused(monkeypatch):
    stores = _install(monkeypatch)
    app = _pause(monkeypatch)
    stores.rows[7] = {"id": 7, "status": "pending", "incident_id": 1}

    resumed = watcher.resume_answered()

    assert resumed == []
    assert list(watcher._paused) == [7]
    assert len(app.invocations) == 1


def test_an_approved_request_resumes_the_run_it_belongs_to(monkeypatch, capsys):
    stores = _install(monkeypatch)
    app = _pause(monkeypatch, final={"status": "resolved"})
    stores.rows[7] = {"id": 7, "status": "approved", "incident_id": 1}

    resumed = watcher.resume_answered()

    assert resumed == [7]
    assert watcher._paused == {}
    resume_call, config = app.invocations[-1]
    assert isinstance(resume_call, Command)
    assert resume_call.resume == 7
    assert config == {"configurable": {"thread_id": "1"}}
    assert "approval 7 is approved -- resuming incident 1" in capsys.readouterr().out


def test_an_expired_request_resumes_into_an_escalation_and_is_audited(monkeypatch):
    stores = _install(monkeypatch)
    app = _pause(monkeypatch, final={"status": "escalated", "escalation_reason": "expired"})
    stores.expired = [{"id": 7, "incident_id": 1, "action": "rollback_deployment", "container": "checkout-service"}]
    stores.rows[7] = {"id": 7, "status": "expired", "incident_id": 1}

    resumed = watcher.resume_answered()

    assert resumed == [7]
    assert [row["event_type"] for row in stores.audit] == ["approval_expired"]
    assert stores.audit[0]["incident_id"] == 1
    assert isinstance(app.invocations[-1][0], Command)


def test_an_unreadable_store_keeps_the_run_paused_for_the_next_pass(monkeypatch, capsys):
    stores = _install(monkeypatch)
    app = _pause(monkeypatch)
    stores.read_error = approvals.ApprovalStoreError("connection refused")

    resumed = watcher.resume_answered()

    assert resumed == []
    assert list(watcher._paused) == [7]
    assert len(app.invocations) == 1
    assert "will retry" in capsys.readouterr().out


def test_a_failed_expiry_sweep_does_not_stop_resumes(monkeypatch, capsys):
    stores = _install(monkeypatch)
    _pause(monkeypatch)
    stores.sweep_error = approvals.ApprovalStoreError("timeout")
    stores.rows[7] = {"id": 7, "status": "denied", "incident_id": 1}

    resumed = watcher.resume_answered()

    assert resumed == [7]
    assert "could not expire overdue approvals" in capsys.readouterr().out


def test_a_bad_safety_configuration_stops_the_watcher_before_it_starts_a_run(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setenv("PHOENIX_EXECUTION_POLICY", "yolo")
    monkeypatch.setattr(watcher, "list_unhandled_incidents", lambda after_id=0: [(4, "checkout-service")])
    monkeypatch.setattr(watcher, "build_graph", lambda checkpointer=None: (_ for _ in ()).throw(AssertionError("built")))

    try:
        watcher.poll_once()
    except Exception as exc:  # the config error is the one thing allowed out of poll_once
        assert "PHOENIX_EXECUTION_POLICY" in str(exc)
    else:
        raise AssertionError("poll_once started a run under an invalid policy")
