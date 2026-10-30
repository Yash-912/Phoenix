"""An LLM outage ends the run as its own audited outcome.

The model being unreachable is not "no hypotheses" and not "ambiguous evidence": the
evidence was never weighed. Each LLM-calling node turns the failure into a terminal
llm_unavailable status with an audit row, the router stops on it, and no later node
spends another call. No network, no LLM, no database.
"""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

import pytest
from langgraph.graph import END

from phoenix.graph import graph, nodes, tier3_nodes
from phoenix.graph.llm_client import (
    HypothesisDecision,
    LLMRequestRejectedError,
    LLMUnavailableError,
    ToolCallDecision,
)
from phoenix.graph.schemas import DiagnoserOutput, Hypothesis
from phoenix.graph.state import AgentState

SERVICE = "checkout-service"


class _Down(Exception):
    pass


def _outage(operation: str) -> LLMUnavailableError:
    return LLMUnavailableError(operation, _Down("connection refused"))


def _state(**overrides) -> AgentState:
    return AgentState(incident_id=7, service_name=SERVICE, **overrides)


@pytest.fixture
def audit(monkeypatch):
    rows = []
    for module in (nodes, graph, tier3_nodes):
        monkeypatch.setattr(
            module, "record_audit",
            lambda incident, node, event, detail, text, _rows=rows: _rows.append((node, event, detail, text)),
            raising=False,
        )
    return rows


def _final(result):
    return result if isinstance(result, dict) else result.model_dump()


# --- the nodes ----------------------------------------------------------------


def test_an_outage_in_the_observer_ends_the_state_as_llm_unavailable(monkeypatch, audit):
    def down(*a, **k):
        raise _outage("decide_tool_calls")

    monkeypatch.setattr(nodes, "decide_tool_calls", down)

    state = nodes.observer_node(_state())

    assert state.status == "llm_unavailable"
    assert "decide_tool_calls" in state.escalation_reason
    assert "connection refused" in state.escalation_reason
    node, event, detail, _text = audit[-1]
    assert (node, event) == ("observer", "llm_unavailable")
    assert detail["operation"] == "decide_tool_calls"


def test_an_outage_in_the_diagnoser_is_not_recorded_as_no_hypotheses(monkeypatch, audit):
    def down(*a, **k):
        raise _outage("decide_hypotheses")

    monkeypatch.setattr(nodes, "decide_hypotheses", down)

    state = nodes.diagnoser_node(_state())

    assert state.status == "llm_unavailable"
    assert [(n, e) for n, e, _d, _t in audit] == [("diagnoser", "llm_unavailable")]


def test_the_diagnoser_makes_no_call_for_a_run_already_marked_unavailable(monkeypatch, audit):
    monkeypatch.setattr(nodes, "decide_hypotheses", lambda *a, **k: pytest.fail("called the LLM again"))

    state = nodes.diagnoser_node(_state(status="llm_unavailable", escalation_reason="down"))

    assert state.status == "llm_unavailable"
    assert audit == []


def test_the_router_stops_a_run_that_is_marked_unavailable(audit):
    command = graph.should_continue(_state(status="llm_unavailable", escalation_reason="down", confidence=0.0))

    assert command.goto == END
    assert command.update["status"] == "llm_unavailable"


def test_an_unavailable_status_wins_over_the_other_stops(audit):
    command = graph.should_continue(
        _state(status="llm_unavailable", escalation_reason="down", iteration=9, max_iterations=5, tokens_spent=10**9)
    )

    assert command.goto == END
    assert command.update["status"] == "llm_unavailable"


def test_the_compiled_graph_ends_llm_unavailable_after_one_failed_call(monkeypatch, audit):
    calls = []

    def down(*a, **k):
        calls.append("observer")
        raise _outage("decide_tool_calls")

    monkeypatch.setattr(nodes, "decide_tool_calls", down)
    monkeypatch.setattr(nodes, "decide_hypotheses", lambda *a, **k: calls.append("diagnoser") or pytest.fail("called"))

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert calls == ["observer"]
    assert final["status"] == "llm_unavailable"
    assert not final.get("evidence_report")
    assert not [e for _n, e, _d, _t in audit if e == "insufficient_evidence"]


def test_an_outage_after_some_evidence_is_still_llm_unavailable_not_insufficient_evidence(monkeypatch, audit):
    runs = {"n": 0}

    def tools(*a, **k):
        return ToolCallDecision([], 1)

    def hypotheses(*a, **k):
        runs["n"] += 1
        if runs["n"] == 1:
            return HypothesisDecision(DiagnoserOutput(hypotheses=[Hypothesis(description="x", category="unknown")]), 1)
        raise _outage("decide_hypotheses")

    monkeypatch.setattr(nodes, "decide_tool_calls", tools)
    monkeypatch.setattr(nodes, "decide_hypotheses", hypotheses)

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert final["status"] == "llm_unavailable"


def test_an_unavailable_run_is_not_written_to_incident_memory():
    from phoenix.graph import memory

    assert "llm_unavailable" not in memory.TERMINAL_STATUSES


# --- Tier 3 -------------------------------------------------------------------


def _hypothesis_state(**overrides) -> AgentState:
    return _state(
        hypotheses=[{
            "hypothesis": Hypothesis(description="payment /charge is slow", category="slow_query"),
            "score": 0.8, "score_breakdown": {},
        }],
        **overrides,
    )


def test_an_outage_while_the_code_investigator_asks_for_tools_ends_the_run_unavailable(monkeypatch, audit):
    def down(*a, **k):
        raise _outage("decide_code_investigation_calls")

    monkeypatch.setattr(tier3_nodes, "decide_code_investigation_calls", down)

    command = tier3_nodes.code_investigator_node(_hypothesis_state())

    assert command.goto == END
    assert command.update["status"] == "llm_unavailable"
    assert "decide_code_investigation_calls" in command.update["escalation_reason"]
    assert audit[-1][:2] == ("code_investigator", "llm_unavailable")


def test_an_outage_while_the_code_investigator_concludes_ends_the_run_unavailable(monkeypatch, audit):
    monkeypatch.setattr(tier3_nodes, "decide_code_investigation_calls", lambda *a: ToolCallDecision([], 1))

    def down(*a, **k):
        raise _outage("decide_defect")

    monkeypatch.setattr(tier3_nodes, "decide_defect", down)

    command = tier3_nodes.code_investigator_node(_hypothesis_state())

    assert command.update["status"] == "llm_unavailable"
    assert command.update.get("tier3_status") != "no_defect_found"


def test_an_outage_in_the_patch_generator_ends_the_run_unavailable_not_rejected(monkeypatch, audit):
    def down(*a, **k):
        raise _outage("decide_patch")

    monkeypatch.setattr(tier3_nodes, "decide_patch", down)
    state = _hypothesis_state(tier3_defect={
        "defect_found": True, "file_path": "services/payment-service/app.py", "function_name": "_find_charge_slow",
        "description": "slow query", "fix_approach": "fix it",
    })

    command = tier3_nodes.patch_generator_node(state)

    assert command.goto == END
    assert command.update["status"] == "llm_unavailable"
    assert audit[-1][:2] == ("patch_generator", "llm_unavailable")


# --- a rejected request ends the run as an escalation, once --------------------------


def _rejected(operation: str) -> LLMRequestRejectedError:
    return LLMRequestRejectedError(operation, _Down("maximum context length"))


def test_a_rejected_observer_request_escalates_and_is_not_marked_unavailable(monkeypatch, audit):
    def rejected(*a, **k):
        raise _rejected("decide_tool_calls")

    monkeypatch.setattr(nodes, "decide_tool_calls", rejected)

    state = nodes.observer_node(_state())

    assert state.status == "escalated"
    assert "maximum context length" in state.escalation_reason
    assert audit[-1][:2] == ("observer", "llm_request_rejected")


def test_the_compiled_graph_ends_escalated_after_a_rejected_request_without_another_call(monkeypatch, audit):
    calls = []

    def rejected(*a, **k):
        calls.append("observer")
        raise _rejected("decide_tool_calls")

    monkeypatch.setattr(nodes, "decide_tool_calls", rejected)
    monkeypatch.setattr(nodes, "decide_hypotheses", lambda *a, **k: calls.append("diagnoser") or pytest.fail("called"))

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert calls == ["observer"]
    assert final["status"] == "escalated"


def test_a_rejected_diagnoser_request_escalates(monkeypatch, audit):
    def rejected(*a, **k):
        raise _rejected("decide_hypotheses")

    monkeypatch.setattr(nodes, "decide_hypotheses", rejected)

    state = nodes.diagnoser_node(_state())

    assert state.status == "escalated"
    assert audit[-1][:2] == ("diagnoser", "llm_request_rejected")


def test_a_rejected_tier3_request_escalates_and_is_not_marked_unavailable(monkeypatch, audit):
    def rejected(*a, **k):
        raise _rejected("decide_code_investigation_calls")

    monkeypatch.setattr(tier3_nodes, "decide_code_investigation_calls", rejected)

    command = tier3_nodes.code_investigator_node(_hypothesis_state())

    assert command.goto == END
    assert command.update["status"] == "escalated"
    assert audit[-1][:2] == ("code_investigator", "llm_request_rejected")
