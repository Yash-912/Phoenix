"""Unit tests for the router — no network, no LLM, no database.

should_continue is pure code over AgentState, so these drive it directly and
assert both halves of the Command it returns: the destination it routes to and
the state update it hands back. The two exits that existed before the budget
guard — the confidence threshold and the iteration cap — are regression-proofed
here. The last four tests drive the compiled graph instead, because a router
that returns the right Command is worth nothing if the run's final state cannot
prove it: those are the tests that catch a dropped escalation, and the one that
catches a loop-back which never comes home. A router that only ever ends a run
can be perfectly wrong about looping, so the loop-back is proved by the node
order a real run produces, not by the value the router returned in isolation.
"""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from langgraph.graph import END

from phoenix.graph import graph, nodes, verification
from phoenix.graph import remediation_dispatch as dispatch
from phoenix.graph.llm_client import HypothesisDecision, ToolCallDecision
from phoenix.graph.schemas import DiagnoserOutput, Hypothesis
from phoenix.graph.state import AgentState

SERVICE = "checkout-service"

SERVICE_DOWN = {"status": "success", "text": "ServiceDown firing, up==0"}
PANIC = {"status": "success", "text": "panic: index out of range, process exit 1"}
SNAPSHOT = {"bytes": 900_000_000, "slope": 3000.0}


def _state(**overrides) -> AgentState:
    return AgentState(incident_id=1, service_name=SERVICE, **overrides)


def _settled_remediation(monkeypatch, outcome: str = "pass", **detail) -> None:
    """Neutralize everything downstream of the router's confidence branch.

    That branch now routes to the remediator instead of to END, so a test which
    only means to exercise routing would otherwise restart a real container and
    then sit out the verification delay. Every door the remediator and verifier
    can reach is replaced here rather than in each test, so a future node added
    to that path fails loudly in one place instead of quietly reaching the
    Docker socket from a test that never meant to touch it.
    """
    monkeypatch.setitem(
        dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: {"status": "ok"}
    )
    monkeypatch.setattr(verification, "read_signal", lambda category, service: SNAPSHOT)
    monkeypatch.setattr(
        verification,
        "run_check",
        lambda *a: (outcome, {"reason": "stubbed", **detail}),
    )
    monkeypatch.setattr(nodes.time, "sleep", lambda seconds: None)


def _final(result) -> dict:
    return result if isinstance(result, dict) else dict(result)


def test_a_run_that_has_spent_its_whole_budget_ends():
    command = graph.should_continue(_state(tokens_spent=20000))

    assert command.goto == END


def test_a_run_that_is_past_its_budget_ends():
    command = graph.should_continue(_state(tokens_spent=20001))

    assert command.goto == END


def test_the_budget_stop_returns_the_spend_that_exhausted_the_budget():
    command = graph.should_continue(_state(tokens_spent=24100, token_budget=24000))

    assert command.goto == END
    assert command.update == {
        "status": "escalated",
        "escalation_reason": "token budget exhausted (24100/24000 tokens)",
    }


def test_the_router_hands_the_escalation_back_rather_than_writing_it_onto_its_copy():
    state = _state(tokens_spent=25000)

    graph.should_continue(state)

    assert state.status == "investigating"
    assert state.escalation_reason is None


def test_a_run_one_token_short_of_its_budget_still_loops():
    command = graph.should_continue(_state(tokens_spent=19999))

    assert command.goto == "observer"
    assert not command.update


def test_the_budget_guard_never_asks_the_llm_anything(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the router consulted the LLM")

    monkeypatch.setattr(nodes, "decide_tool_calls", boom)
    monkeypatch.setattr(nodes, "decide_hypotheses", boom)

    command = graph.should_continue(_state(tokens_spent=25000))

    assert command.goto == END
    assert command.update["status"] == "escalated"
    assert command.update["escalation_reason"] is not None


def test_a_confident_run_that_overspent_is_a_finding_not_an_escalation():
    command = graph.should_continue(
        _state(confidence=0.75, confidence_threshold=0.75, tokens_spent=25000)
    )

    assert command.goto == "remediator"
    assert "escalation_reason" not in (command.update or {})


def test_the_confidence_threshold_stops_investigating_and_goes_to_the_remediator():
    """The threshold is where a run stops asking questions and starts trying to
    do something, so it routes onward rather than ending."""
    command = graph.should_continue(_state(confidence=0.9, tokens_spent=100))

    assert command.goto == "remediator"
    assert command.update == {"status": "confident"}


def test_the_iteration_cap_records_its_own_escalation():
    command = graph.should_continue(_state(iteration=5, max_iterations=5, tokens_spent=100))

    assert command.goto == END
    assert command.update == {
        "status": "escalated",
        "escalation_reason": "iteration cap reached (5/5)",
    }


def test_an_under_budget_run_below_the_cap_still_loops_back_to_the_observer():
    command = graph.should_continue(_state(iteration=1, max_iterations=5, tokens_spent=100))

    assert command.goto == "observer"
    assert not command.update


def test_the_compiled_graph_stops_itself_once_the_budget_is_spent(monkeypatch):
    calls = []

    def fake_tool_calls(service_name, evidence_so_far, evidence_requests):
        calls.append("observer")
        return ToolCallDecision([], 1)

    def fake_hypotheses(service_name, evidence_so_far):
        calls.append("diagnoser")
        return HypothesisDecision(
            DiagnoserOutput(hypotheses=[Hypothesis(description="it crashed", category="crash")]),
            1,
        )

    monkeypatch.setattr(nodes, "decide_tool_calls", fake_tool_calls)
    monkeypatch.setattr(nodes, "decide_hypotheses", fake_hypotheses)

    final = _final(graph.build_graph().invoke(_state(token_budget=2, max_iterations=5)))

    assert calls == ["observer", "diagnoser"]
    assert final["status"] == "escalated"
    assert final["escalation_reason"] == "token budget exhausted (2/2 tokens)"


def test_the_compiled_graph_records_the_iteration_cap_in_its_final_state(monkeypatch):
    def fake_tool_calls(service_name, evidence_so_far, evidence_requests):
        return ToolCallDecision([], 1)

    def fake_hypotheses(service_name, evidence_so_far):
        return HypothesisDecision(
            DiagnoserOutput(hypotheses=[Hypothesis(description="it crashed", category="crash")]),
            1,
        )

    monkeypatch.setattr(nodes, "decide_tool_calls", fake_tool_calls)
    monkeypatch.setattr(nodes, "decide_hypotheses", fake_hypotheses)

    final = _final(
        graph.build_graph().invoke(_state(token_budget=100000, max_iterations=1))
    )

    assert final["status"] == "escalated"
    assert final["escalation_reason"] == "iteration cap reached (1/1)"


def test_the_compiled_graph_ends_a_confident_run_with_no_escalation(monkeypatch):
    def fake_tool_calls(service_name, evidence_so_far, evidence_requests):
        return ToolCallDecision(
            [
                {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
                {"name": "query_loki", "arguments": {"logql": '{container="checkout-service"}'}},
            ],
            1,
        )

    def fake_hypotheses(service_name, evidence_so_far):
        return HypothesisDecision(
            DiagnoserOutput(hypotheses=[Hypothesis(description="it crashed", category="crash")]),
            1,
        )

    monkeypatch.setattr(nodes, "decide_tool_calls", fake_tool_calls)
    monkeypatch.setattr(nodes, "decide_hypotheses", fake_hypotheses)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: SERVICE_DOWN)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: PANIC)
    _settled_remediation(monkeypatch)

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert final["confidence"] == 0.9
    assert final["status"] == "resolved"
    assert final.get("escalation_reason") is None


def test_the_compiled_graph_returns_to_the_observer_when_the_router_loops_back(monkeypatch):
    calls = []
    observer_passes = 0

    def fake_tool_calls(service_name, evidence_so_far, evidence_requests):
        nonlocal observer_passes
        calls.append("observer")
        observer_passes += 1
        if observer_passes == 1:
            return ToolCallDecision([], 1)
        return ToolCallDecision(
            [
                {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
                {"name": "query_loki", "arguments": {"logql": '{container="checkout-service"}'}},
            ],
            1,
        )

    def fake_hypotheses(service_name, evidence_so_far):
        calls.append("diagnoser")
        return HypothesisDecision(
            DiagnoserOutput(hypotheses=[Hypothesis(description="it crashed", category="crash")]),
            1,
        )

    monkeypatch.setattr(nodes, "decide_tool_calls", fake_tool_calls)
    monkeypatch.setattr(nodes, "decide_hypotheses", fake_hypotheses)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: SERVICE_DOWN)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: PANIC)
    _settled_remediation(monkeypatch)

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert calls == ["observer", "diagnoser", "observer", "diagnoser"]
    assert final["iteration"] == 2
    assert final["confidence"] == 0.9
    assert final["status"] == "resolved"
    assert len(final["evidence"]) == 2
