"""A run that stops learning with nothing two sources agree on ends as insufficient evidence.

That is a correct outcome, kept apart from "escalated" (a spent budget, a failed
action): the agent investigated, found no cause it could stand behind, and said
so without acting. Nothing here names a scenario; hypotheses are generic
categories and evidence is the shape the real tools return."""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from phoenix.graph import graph, investigation, nodes, scoring
from phoenix.graph import remediation_dispatch as dispatch
from phoenix.graph.llm_client import HypothesisDecision, ToolCallDecision
from phoenix.graph.schemas import DiagnoserOutput, Hypothesis, ScoredHypothesis
from phoenix.graph.state import AgentState

SERVICE = "svc"
OVERLOAD = Hypothesis(description="the service is overloaded", category="overload")
CRASH = Hypothesis(description="the service is crashing", category="crash")

PROM_ERRORS = "HighErrorRate firing, error rate above threshold"
# The log line Scenario 5's blip writes; it matches no category keyword.
LOKI_GENERIC = "payment request failed, please retry"
LOKI_PANIC = "panic: index out of range, process exit 1"


def _ev(source: str, text: str, ok: bool = True) -> dict:
    raw = {"status": "success", "text": text} if ok else {"status": "error", "error": "boom"}
    return {"iteration": 1, "source": source, "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": f"{source}({text})", "raw_data": raw}


def _state(evidence, *hypotheses, **fields) -> AgentState:
    state = AgentState(incident_id=1, service_name=SERVICE, evidence=evidence, **fields)
    state.hypotheses = [
        ScoredHypothesis(hypothesis=h, score=s, score_breakdown=b)
        for h, s, b in scoring.score_all(evidence, list(hypotheses))
    ]
    return state


# ---- the classifier ----------------------------------------------------------


def test_a_stagnant_run_with_one_supporting_source_reports_what_was_tried():
    evidence = [_ev("query_prometheus", PROM_ERRORS), _ev("query_loki", LOKI_GENERIC)]
    state = _state(evidence, OVERLOAD, stagnant_passes=2, iteration=3)

    report = investigation.insufficient_evidence_report(state)

    assert report["reason"].startswith("investigation_stagnant:")
    assert report["hypotheses_considered"][0]["category"] == "overload"
    assert report["hypotheses_considered"][0]["supporting_sources"] == ["query_prometheus"]
    assert report["sources_supporting_any_hypothesis"] == ["query_prometheus"]
    assert report["sources_answered_without_support"] == ["query_loki"]
    assert report["sources_failed"] == []
    assert report["sources_never_read"] == ["get_container_state", "get_recent_deployments", "inspect_health"]
    assert report["queries_run"] == 2 and report["iterations"] == 3
    assert "none supported by two independent sources" in report["summary"]


def test_a_run_that_is_still_learning_has_no_report():
    state = _state([_ev("query_prometheus", PROM_ERRORS)], OVERLOAD, stagnant_passes=1)

    assert investigation.insufficient_evidence_report(state) is None


def test_two_agreeing_sources_below_the_threshold_is_not_insufficient_evidence():
    """Partial agreement is a weak finding, not an absence of evidence."""
    evidence = [_ev("query_prometheus", "up==0 ServiceDown"), _ev("query_loki", LOKI_PANIC)]
    state = _state(evidence, CRASH, stagnant_passes=2)

    assert investigation.insufficient_evidence_report(state) is None


def test_a_stagnant_run_with_no_hypotheses_is_still_insufficient_evidence():
    state = AgentState(incident_id=1, service_name=SERVICE, stagnant_passes=2)

    report = investigation.insufficient_evidence_report(state)

    assert report["hypotheses_considered"] == []
    assert report["sources_never_read"] == sorted(scoring.SOURCE_WEIGHTS)


def test_a_failed_read_is_reported_as_failed_not_as_answered():
    evidence = [_ev("query_prometheus", PROM_ERRORS), _ev("query_loki", "", ok=False)]
    state = _state(evidence, OVERLOAD, stagnant_passes=2)

    report = investigation.insufficient_evidence_report(state)

    assert report["sources_failed"] == ["query_loki"]
    assert report["sources_answered_without_support"] == []


# ---- the router ----------------------------------------------------------------


def _route(monkeypatch, **fields):
    rows: list[tuple] = []
    monkeypatch.setattr(graph, "record_audit", lambda incident, node, event, detail, text: rows.append((event, detail, text)))
    state = _state([_ev("query_prometheus", PROM_ERRORS), _ev("query_loki", LOKI_GENERIC)], OVERLOAD, confidence=0.4, **fields)
    return graph.should_continue(state), rows


BUDGET = "token budget exhausted (25624/20000 tokens)"


def test_a_budget_stop_with_no_two_source_hypothesis_is_insufficient_evidence(monkeypatch):
    """The live case: the budget ran out at 0.30 with one source behind the leader. The
    run took no action and found nothing it could stand behind, whether or not its last
    pass happened to move the leader."""
    command, rows = _route(monkeypatch, stagnant_passes=0, tokens_spent=25624, token_budget=20000)

    assert command.update["status"] == "insufficient_evidence"
    assert command.update["escalation_reason"] == BUDGET
    assert command.update["evidence_report"]["reason"] == BUDGET
    assert rows[0][0] == "insufficient_evidence"


def test_a_stagnant_budget_stop_names_the_budget_as_the_reason(monkeypatch):
    command, _ = _route(monkeypatch, stagnant_passes=2, tokens_spent=25624, token_budget=20000)

    assert command.update["status"] == "insufficient_evidence"
    assert command.update["escalation_reason"] == BUDGET
    assert command.update["evidence_report"]["reason"] == BUDGET


def test_the_iteration_cap_with_no_two_source_hypothesis_is_insufficient_evidence(monkeypatch):
    command, rows = _route(monkeypatch, stagnant_passes=0, iteration=5, max_iterations=5, tokens_spent=100)

    assert command.update["status"] == "insufficient_evidence"
    assert command.update["escalation_reason"] == "iteration cap reached (5/5)"
    assert rows[0][0] == "insufficient_evidence"


def _weak_finding_state(**fields) -> AgentState:
    """Two independent sources agree on a cause but the score is below the threshold."""
    evidence = [_ev("query_prometheus", "up==0 ServiceDown"), _ev("query_loki", LOKI_PANIC)]
    return _state(evidence, CRASH, confidence=0.6, **fields)


def test_a_budget_stop_with_a_two_source_hypothesis_stays_escalated(monkeypatch):
    """Two sources agreeing is a finding, however weak, so the run did not find nothing."""
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)

    command = graph.should_continue(_weak_finding_state(tokens_spent=20000, token_budget=20000))

    assert command.update["status"] == "escalated"
    assert "evidence_report" not in command.update


def test_the_iteration_cap_with_a_two_source_hypothesis_stays_escalated(monkeypatch):
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)

    command = graph.should_continue(_weak_finding_state(iteration=5, max_iterations=5, tokens_spent=100))

    assert command.update["status"] == "escalated"
    assert command.update["escalation_reason"] == "iteration cap reached (5/5)"


def test_a_run_that_read_nothing_before_the_budget_ran_out_is_a_budget_problem(monkeypatch):
    """It never looked, so there is no absence of evidence to report."""
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)

    command = graph.should_continue(AgentState(incident_id=1, service_name=SERVICE, tokens_spent=20000, token_budget=20000))

    assert command.update["status"] == "escalated"
    assert "evidence_report" not in command.update


def test_a_run_whose_every_read_failed_before_the_budget_ran_out_is_a_budget_problem(monkeypatch):
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)
    state = AgentState(
        incident_id=1, service_name=SERVICE, tokens_spent=20000, token_budget=20000,
        evidence=[_ev("query_loki", "", ok=False)],
    )

    command = graph.should_continue(state)

    assert command.update["status"] == "escalated"


def test_a_stagnant_run_with_nothing_read_is_still_insufficient_evidence(monkeypatch):
    """Stagnation is itself proof the run went round without learning."""
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)

    command = graph.should_continue(AgentState(incident_id=1, service_name=SERVICE, stagnant_passes=2))

    assert command.update["status"] == "insufficient_evidence"


# ---- through the compiled graph, shaped like the ambiguous scenario ----------------


def test_an_underdetermined_incident_ends_with_a_report_and_no_action(monkeypatch):
    reached: list[str] = []

    def forbidden(name):
        reached.append(name)
        raise AssertionError(f"{name} dispatched by a run with insufficient evidence")

    for action in list(dispatch.REMEDIATION_DISPATCH):
        monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, action, forbidden)

    rows: list[tuple] = []
    monkeypatch.setattr(graph, "record_audit", lambda incident, node, event, detail, text: rows.append((event, detail)))
    monkeypatch.setattr(
        nodes, "decide_tool_calls",
        lambda service, evidence, requests, evidence_state=None: ToolCallDecision(
            [{"name": "query_prometheus", "arguments": {"promql": "up"}}], 1
        ),
    )
    monkeypatch.setattr(
        nodes, "decide_hypotheses",
        lambda service, evidence: HypothesisDecision(DiagnoserOutput(hypotheses=[OVERLOAD]), 10),
    )
    for name in ("record_evidence", "record_audit"):
        monkeypatch.setattr(nodes, name, lambda *a: None)
    monkeypatch.setattr(nodes, "record_hypotheses", lambda *a, **k: None)
    monkeypatch.setattr(nodes, "get_incident_first_seen", lambda incident: None)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: {"status": "success", "text": PROM_ERRORS})
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: {"status": "success", "text": LOKI_GENERIC})

    result = graph.build_graph().invoke(AgentState(incident_id=1, service_name=SERVICE, token_budget=100000))
    final = result if isinstance(result, dict) else dict(result)

    assert final["status"] == "insufficient_evidence"
    assert final["evidence_report"]["hypotheses_considered"][0]["category"] == "overload"
    assert final["confidence"] < final["confidence_threshold"]
    assert reached == []
    assert [event for event, _ in rows][-1] == "insufficient_evidence"
