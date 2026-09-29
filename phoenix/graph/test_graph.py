"""Unit tests for the router — no network, no LLM, no database.

should_continue is pure code over AgentState, so these drive it directly and
assert the route it picks and the reason it records. The two exits that existed
before the budget guard — the confidence threshold and the iteration cap — are
regression-proofed here, and the last test runs the compiled graph to show the
guard actually ends the loop rather than only returning "end".
"""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from phoenix.graph import graph, nodes
from phoenix.graph.llm_client import HypothesisDecision, ToolCallDecision
from phoenix.graph.schemas import DiagnoserOutput, Hypothesis
from phoenix.graph.state import AgentState

SERVICE = "checkout-service"


def _state(**overrides) -> AgentState:
    return AgentState(incident_id=1, service_name=SERVICE, **overrides)


def test_a_run_that_has_spent_its_whole_budget_ends():
    state = _state(tokens_spent=20000)

    assert graph.should_continue(state) == "end"


def test_a_run_that_is_past_its_budget_ends():
    state = _state(tokens_spent=20001)

    assert graph.should_continue(state) == "end"


def test_the_budget_stop_records_the_spend_that_exhausted_the_budget():
    state = _state(tokens_spent=24100, token_budget=24000)

    assert graph.should_continue(state) == "end"
    assert state.status == "escalated"
    assert "budget" in state.escalation_reason
    assert "24100" in state.escalation_reason
    assert "24000" in state.escalation_reason


def test_a_run_one_token_short_of_its_budget_still_loops():
    state = _state(tokens_spent=19999)

    assert graph.should_continue(state) == "observer"
    assert state.status == "investigating"
    assert state.escalation_reason is None


def test_the_budget_guard_never_asks_the_llm_anything(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the router consulted the LLM")

    monkeypatch.setattr(nodes, "decide_tool_calls", boom)
    monkeypatch.setattr(nodes, "decide_hypotheses", boom)
    state = _state(tokens_spent=25000)

    assert graph.should_continue(state) == "end"
    assert state.escalation_reason is not None


def test_a_confident_run_that_overspent_is_a_finding_not_an_escalation():
    state = _state(confidence=0.75, confidence_threshold=0.75, tokens_spent=25000)

    assert graph.should_continue(state) == "end"
    assert state.status == "investigating"
    assert state.escalation_reason is None


def test_the_confidence_threshold_still_ends_a_run_under_budget():
    state = _state(confidence=0.9, tokens_spent=100)

    assert graph.should_continue(state) == "end"
    assert state.escalation_reason is None


def test_the_iteration_cap_still_ends_an_under_budget_run():
    state = _state(iteration=5, max_iterations=5, tokens_spent=100)

    assert graph.should_continue(state) == "end"
    assert state.status == "investigating"
    assert state.escalation_reason is None


def test_an_under_budget_run_below_the_cap_still_loops_back_to_the_observer():
    state = _state(iteration=1, max_iterations=5, tokens_spent=100)

    assert graph.should_continue(state) == "observer"
    assert state.status == "investigating"
    assert state.escalation_reason is None


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

    graph.build_graph().invoke(_state(token_budget=2, max_iterations=5))

    assert calls == ["observer", "diagnoser"]
