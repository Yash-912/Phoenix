"""The approval gate on the remediator, run through a checkpointed graph.

The store is an in-memory stand-in with the same rules as the real table: the
first answer wins, a request cannot be answered after its deadline, and the same
request key returns the same row. Nothing here touches Postgres or Docker. The
deploy evidence is the one test_graph.py already uses (tg._deploy_run), so the
flip test drives the real router, remediator and verifier over a real incident
shape.

Exit criterion (plan, Phase 7): the same incident pauses for a Tier 2 approval
in guarded mode and proceeds at once in lab mode. That is the first test below.
"""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command

from phoenix.graph import approvals, graph, nodes, verification
from phoenix.graph import remediation_dispatch as dispatch
from phoenix.graph import test_graph as tg
from phoenix.graph.state import AgentState

SERVICE = tg.SERVICE
APPROVER = "yash@example.com"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
DEPLOY_ARGS = ("v17", "2026-10-02T08:00:00+00:00")


class FakeApprovalStore:
    """Same rules as the approvals table. Installed over approvals.* by _install_store."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self._by_key: dict[str, int] = {}
        self._next_id = 1
        self.fail_requests = False

    def request(self, **kwargs) -> dict:
        if self.fail_requests:
            raise approvals.ApprovalStoreError("the approvals table is unreachable")
        key = kwargs["request_key"]
        if key in self._by_key:
            return dict(self.rows[self._by_key[key]])
        row_id = self._next_id
        self._next_id += 1
        row = {
            "id": row_id,
            "incident_id": kwargs["incident_id"],
            "request_key": key,
            "status": "pending",
            "action": kwargs["action"],
            "container": kwargs["container"],
            "args": list(kwargs["args"]),
            "tier": kwargs["tier"],
            "incident_summary": kwargs["incident_summary"],
            "plan_reasoning": kwargs["plan_reasoning"],
            "evidence": kwargs["evidence"],
            "requested_at": NOW,
            "expires_at": NOW + timedelta(seconds=kwargs["ttl_seconds"]),
            "decided_at": None,
            "decided_by": None,
            "decision_channel": None,
        }
        self.rows[row_id] = row
        self._by_key[key] = row_id
        return dict(row)

    def read(self, row_id: int) -> dict | None:
        row = self.rows.get(row_id)
        return dict(row) if row else None

    def decide(self, row_id: int, status: str, by: str, channel: str = "dashboard") -> str:
        row = self.rows[row_id]
        if row["status"] != "pending":
            return "ignored"
        if row["expires_at"] <= NOW:
            return "expired"
        row.update(status=status, decided_by=by, decided_at=NOW, decision_channel=channel)
        return "decided"

    def expire(self, row_id: int) -> None:
        row = self.rows[row_id]
        if row["status"] == "pending":
            row.update(status="expired", decided_at=NOW, decided_by="system:expiry", decision_channel="expiry")

    def pending(self) -> list[dict]:
        return [dict(row) for row in self.rows.values() if row["status"] == "pending"]


def _install_store(monkeypatch) -> FakeApprovalStore:
    store = FakeApprovalStore()
    monkeypatch.setattr(approvals, "request_approval", store.request)
    monkeypatch.setattr(approvals, "read_approval", store.read)
    return store


def _audit(monkeypatch) -> list[dict]:
    rows: list[dict] = []

    def record_audit(incident_id, node, event_type, detail, reasoning_text):
        rows.append({"node": node, "event_type": event_type, "detail": detail, "reasoning_text": reasoning_text})

    # The graph's router writes its own rows through its own import of the writer.
    monkeypatch.setattr(nodes, "record_audit", record_audit)
    monkeypatch.setattr(graph, "record_audit", record_audit)
    return rows


def _set_policy_env(monkeypatch, **extra) -> None:
    monkeypatch.setenv("PHOENIX_APPROVERS", APPROVER)
    monkeypatch.delenv("PHOENIX_EXECUTION_POLICY", raising=False)
    monkeypatch.delenv("PHOENIX_TIER1_REQUIRES_APPROVAL", raising=False)
    monkeypatch.delenv("PHOENIX_TIER2_REQUIRES_APPROVAL", raising=False)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def _deploy_app(monkeypatch, policy_mode: str, *, checkpointer: bool = True):
    """Compile the graph over the real deploy evidence, run it, and return what it did."""
    rolled_back = tg._deploy_run(monkeypatch)
    app = graph.build_graph(checkpointer=MemorySaver() if checkpointer else None)
    config = {"configurable": {"thread_id": "1"}}
    result = app.invoke(
        AgentState(
            incident_id=1, service_name=SERVICE, token_budget=100000, max_iterations=5,
            policy_mode=policy_mode,
        ),
        config,
    )
    return app, config, result, rolled_back


def _resume(app, config, approval_id: int) -> dict:
    return tg._final(app.invoke(Command(resume=approval_id), config))


def test_flipping_the_policy_changes_what_the_same_incident_does(monkeypatch):
    """The Phase 7 exit criterion. Same evidence, same rollback target, one flag."""
    _set_policy_env(monkeypatch)
    store = _install_store(monkeypatch)
    _audit(monkeypatch)

    app, config, paused, rolled_back = _deploy_app(monkeypatch, "guarded")

    assert rolled_back == [], "guarded mode must not roll back before a person answers"
    assert "__interrupt__" in paused
    [request] = store.pending()
    assert request["action"] == "rollback_deployment"
    assert request["args"] == list(DEPLOY_ARGS)

    store.decide(request["id"], "approved", APPROVER)
    resumed = _resume(app, config, request["id"])

    assert rolled_back == [{"service": SERVICE, "args": DEPLOY_ARGS}]
    assert resumed["status"] == "resolved"

    lab_store = _install_store(monkeypatch)
    _, _, lab, lab_rolled_back = _deploy_app(monkeypatch, "autonomous_lab")

    assert lab_rolled_back == [{"service": SERVICE, "args": DEPLOY_ARGS}]
    assert lab["status"] == "resolved"
    assert lab_store.rows == {}, "lab mode must not ask anyone"


def test_a_guarded_request_records_the_incident_summary_evidence_action_and_target(monkeypatch):
    _set_policy_env(monkeypatch)
    store = _install_store(monkeypatch)
    audit = _audit(monkeypatch)

    _deploy_app(monkeypatch, "guarded")

    [request] = store.pending()
    assert request["incident_summary"].startswith("deploy:")
    assert request["plan_reasoning"]
    assert request["evidence"] and all({"source", "summary"} <= set(item) for item in request["evidence"])
    assert request["container"] == SERVICE
    assert request["tier"] == 2
    requested = [row for row in audit if row["event_type"] == "approval_requested"]
    assert [row["detail"]["approval_id"] for row in requested] == [request["id"]]
    assert not [row for row in audit if row["event_type"] == "action_started"]


def test_a_denied_request_escalates_and_takes_no_action(monkeypatch):
    _set_policy_env(monkeypatch)
    store = _install_store(monkeypatch)
    _audit(monkeypatch)
    app, config, _, rolled_back = _deploy_app(monkeypatch, "guarded")

    store.decide(store.pending()[0]["id"], "denied", APPROVER)
    final = _resume(app, config, 1)

    assert rolled_back == []
    assert final["status"] == "escalated"
    assert "denied" in final["escalation_reason"]


def test_an_expired_request_escalates_and_is_never_an_approval(monkeypatch):
    _set_policy_env(monkeypatch)
    store = _install_store(monkeypatch)
    _audit(monkeypatch)
    app, config, _, rolled_back = _deploy_app(monkeypatch, "guarded")

    store.expire(1)
    final = _resume(app, config, 1)

    assert rolled_back == []
    assert final["status"] == "escalated"
    assert "expired" in final["escalation_reason"]
    assert "never an approval" in final["escalation_reason"]


def test_a_decision_by_someone_no_longer_on_the_list_does_not_release_the_action(monkeypatch):
    _set_policy_env(monkeypatch)
    store = _install_store(monkeypatch)
    _audit(monkeypatch)
    app, config, _, rolled_back = _deploy_app(monkeypatch, "guarded")

    store.decide(1, "approved", "mallory@example.com")
    final = _resume(app, config, 1)

    assert rolled_back == []
    assert final["status"] == "escalated"
    assert "no longer an approver" in final["escalation_reason"]


def test_an_approval_for_a_different_target_does_not_cover_the_live_plan(monkeypatch):
    """The target is re-derived on resume. If it moved while the run waited, the
    approval is for something else and nothing is rolled back."""
    _set_policy_env(monkeypatch)
    store = _install_store(monkeypatch)
    _audit(monkeypatch)
    app, config, _, rolled_back = _deploy_app(monkeypatch, "guarded")

    store.rows[1]["args"] = ["v16", DEPLOY_ARGS[1]]
    store.decide(1, "approved", APPROVER)
    final = _resume(app, config, 1)

    assert rolled_back == []
    assert final["status"] == "escalated"
    assert "does not cover this action" in final["escalation_reason"]


def test_the_first_answer_wins_and_a_second_is_ignored(monkeypatch):
    _set_policy_env(monkeypatch)
    store = _install_store(monkeypatch)
    _audit(monkeypatch)
    app, config, _, rolled_back = _deploy_app(monkeypatch, "guarded")

    assert store.decide(1, "approved", APPROVER) == "decided"
    assert store.decide(1, "denied", APPROVER, channel="slack") == "ignored"
    final = _resume(app, config, 1)

    assert store.rows[1]["status"] == "approved"
    assert rolled_back == [{"service": SERVICE, "args": DEPLOY_ARGS}]
    assert final["status"] == "resolved"


def test_a_request_the_store_cannot_record_takes_no_action(monkeypatch):
    _set_policy_env(monkeypatch)
    store = _install_store(monkeypatch)
    store.fail_requests = True
    _audit(monkeypatch)

    _, _, result, rolled_back = _deploy_app(monkeypatch, "guarded")

    assert "__interrupt__" not in result
    assert rolled_back == []
    assert result["status"] == "escalated"
    assert "could not be recorded" in result["escalation_reason"]


def test_a_run_that_cannot_pause_escalates_instead_of_waiting_for_a_resume_that_never_comes(monkeypatch):
    _set_policy_env(monkeypatch)
    store = _install_store(monkeypatch)
    _audit(monkeypatch)

    _, _, result, rolled_back = _deploy_app(monkeypatch, "guarded", checkpointer=False)

    assert rolled_back == []
    assert store.rows == {}, "no request is written when the run has no way to wait for it"
    assert result["status"] == "escalated"
    assert "cannot pause" in result["escalation_reason"]


def test_a_safety_configuration_that_does_not_parse_stops_the_action(monkeypatch):
    _set_policy_env(monkeypatch, PHOENIX_TIER2_REQUIRES_APPROVAL="false")
    store = _install_store(monkeypatch)
    _audit(monkeypatch)

    _, _, result, rolled_back = _deploy_app(monkeypatch, "guarded")

    assert rolled_back == []
    assert store.rows == {}
    assert result["status"] == "escalated"
    assert "safety configuration is invalid" in result["escalation_reason"]


def test_the_action_is_framed_by_started_and_finished_records_in_lab_mode(monkeypatch):
    _set_policy_env(monkeypatch)
    _install_store(monkeypatch)
    audit = _audit(monkeypatch)

    _deploy_app(monkeypatch, "autonomous_lab")

    events = [row["event_type"] for row in audit]
    assert events.index("action_started") < events.index("action_finished")
    finished = next(row for row in audit if row["event_type"] == "action_finished")
    assert finished["detail"]["ok"] is True
    assert finished["detail"]["action"] == "rollback_deployment"


# --- Tier 1 under a tightened setting. A small graph around the remediator alone,
# so the crash path can be exercised without the observer and diagnoser around it.

def _tier1_app(monkeypatch, restarts: list[str]):
    monkeypatch.setattr(verification, "read_signal", lambda category, service: {"bytes": 1})
    monkeypatch.setitem(
        dispatch.REMEDIATION_DISPATCH, "restart_service",
        lambda name: restarts.append(name) or {"status": "ok"},
    )

    def verifier(state):
        return Command(goto=END, update={"status": "resolved"})

    builder = StateGraph(AgentState)
    builder.add_node("remediator", nodes.remediator_node)
    builder.add_node("verifier", verifier)
    builder.set_entry_point("remediator")
    return builder.compile(checkpointer=MemorySaver())


def _crash_state(policy_mode: str) -> AgentState:
    from phoenix.graph.schemas import Hypothesis, ScoredHypothesis

    return AgentState(
        incident_id=1,
        service_name=SERVICE,
        policy_mode=policy_mode,
        hypotheses=[ScoredHypothesis(hypothesis=Hypothesis(description="crash looping", category="crash"),
                                     score=0.9, score_breakdown={})],
    )


def test_a_tightened_tier_1_setting_holds_the_restart_for_approval(monkeypatch):
    _set_policy_env(monkeypatch, PHOENIX_TIER1_REQUIRES_APPROVAL="true")
    store = _install_store(monkeypatch)
    _audit(monkeypatch)
    monkeypatch.setattr(nodes, "current_symptom", lambda category, service: {"state": "present"})
    restarts: list[str] = []
    app = _tier1_app(monkeypatch, restarts)
    config = {"configurable": {"thread_id": "1"}}

    paused = app.invoke(_crash_state("guarded"), config)

    assert restarts == []
    assert "__interrupt__" in paused
    assert store.pending()[0]["action"] == "restart_service"
    assert store.pending()[0]["tier"] == 1

    store.decide(1, "approved", APPROVER)
    app.invoke(Command(resume=1), config)

    assert restarts == [SERVICE]


def test_an_approved_action_is_checked_against_the_live_system_again_before_it_runs(monkeypatch):
    """The approval does not carry over a stale reading. If the symptom has cleared
    while the run waited, the approved restart is not run."""
    _set_policy_env(monkeypatch, PHOENIX_TIER1_REQUIRES_APPROVAL="true")
    store = _install_store(monkeypatch)
    _audit(monkeypatch)
    observed = {"state": "present"}
    monkeypatch.setattr(nodes, "current_symptom", lambda category, service: observed)
    restarts: list[str] = []
    app = _tier1_app(monkeypatch, restarts)
    config = {"configurable": {"thread_id": "1"}}

    app.invoke(_crash_state("guarded"), config)
    observed["state"] = "absent"
    store.decide(1, "approved", APPROVER)
    final = tg._final(app.invoke(Command(resume=1), config))

    assert restarts == []
    assert final["status"] == "escalated"
    assert "no longer observed" in final["escalation_reason"]
