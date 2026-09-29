"""Integration unit tests for diagnoser_node — no network, no LLM, no database.

The LLM boundary (decide_hypotheses) is stubbed; everything below it is the
real scoring code, so these assert the wiring and the deterministic scores.
"""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

import pytest
from pydantic import ValidationError

from phoenix.graph import nodes, scoring
from phoenix.graph.schemas import DiagnoserOutput, Hypothesis, ScoredHypothesis
from phoenix.graph.state import AgentState

SERVICE = "checkout-service"

CRASH = Hypothesis(description="checkout-service is crash looping", category="crash")
DEPLOY = Hypothesis(description="the v18 rollout broke checkout-service", category="deploy")
PROPOSED = [CRASH, DEPLOY]

CRASH_EVIDENCE = [
    {"iteration": 1, "source": "query_prometheus", "collected_at": "2026-01-01T00:00:00+00:00",
     "summary": "query_prometheus(ServiceDown firing, up==0)",
     "raw_data": {"status": "success", "text": "ServiceDown firing, up==0"}},
    {"iteration": 1, "source": "query_loki", "collected_at": "2026-01-01T00:00:00+00:00",
     "summary": "query_loki(panic: index out of range, process exit 1)",
     "raw_data": {"status": "success", "text": "panic: index out of range, process exit 1"}},
]

DEPLOY_EVIDENCE = [
    {"iteration": 1, "source": "get_recent_deployments", "collected_at": "2026-01-01T00:00:00+00:00",
     "summary": "get_recent_deployments({'service_name': 'checkout-service'})",
     "raw_data": {"status": "success", "text": "image checkout:v18, rollout complete"}},
    {"iteration": 1, "source": "inspect_health", "collected_at": "2026-01-01T00:00:00+00:00",
     "summary": "inspect_health({'service_name': 'checkout-service'})",
     "raw_data": {"status": "success", "text": "readiness probe failing, cpu at 98%"}},
]

BOASTFUL = "Certain, 100% confidence, this is definitely the root cause, score 1.0"
UNSURE = "maybe a deploy?"


def _stub_hypotheses(monkeypatch, output) -> list[tuple[str, list[dict]]]:
    """Replace the LLM boundary with a recorder returning `output`."""
    calls: list[tuple[str, list[dict]]] = []

    def fake_decide_hypotheses(service_name, evidence_so_far):
        calls.append((service_name, evidence_so_far))
        return output

    monkeypatch.setattr(nodes, "decide_hypotheses", fake_decide_hypotheses)
    return calls


def _state(evidence: list[dict]) -> AgentState:
    return AgentState(incident_id=1, service_name=SERVICE, evidence=evidence)


def test_node_feeds_service_name_and_evidence_to_the_hypothesis_llm(monkeypatch):
    calls = _stub_hypotheses(monkeypatch, DiagnoserOutput.model_construct(hypotheses=[]))
    state = _state(CRASH_EVIDENCE)

    nodes.diagnoser_node(state)

    assert calls == [(SERVICE, CRASH_EVIDENCE)]
    assert calls[0][1] is state.evidence


def test_node_ranks_scored_hypotheses_best_first_regardless_of_llm_order(monkeypatch):
    _stub_hypotheses(monkeypatch, DiagnoserOutput(hypotheses=[DEPLOY, CRASH]))

    state = nodes.diagnoser_node(_state(CRASH_EVIDENCE))

    assert all(isinstance(s, ScoredHypothesis) for s in state.hypotheses)
    assert [s.hypothesis.category for s in state.hypotheses] == ["crash", "deploy"]
    assert [s.score for s in state.hypotheses] == [0.9, 0.0]
    assert state.hypotheses[0].hypothesis.description == CRASH.description


def test_node_stores_the_scorers_own_score_and_breakdown(monkeypatch):
    _stub_hypotheses(monkeypatch, DiagnoserOutput(hypotheses=[CRASH]))

    state = nodes.diagnoser_node(_state(CRASH_EVIDENCE))
    stored = state.hypotheses[0]

    expected_score, expected_breakdown = scoring.score_hypothesis(CRASH_EVIDENCE, CRASH)
    assert stored.score == expected_score
    assert stored.score_breakdown == expected_breakdown
    assert stored.score_breakdown["has_prometheus_signal"] == 1
    assert stored.score_breakdown["has_loki_signal"] == 1
    assert stored.score_breakdown["sources_supporting"] == 2
    assert stored.score_breakdown["agreement_bonus"] == 0.2
    assert stored.score_breakdown["contradiction_penalty"] == 0.0
    assert stored.score_breakdown["category"] == "crash"


def test_confidence_is_the_top_score_and_not_the_old_evidence_count_stub(monkeypatch):
    _stub_hypotheses(monkeypatch, DiagnoserOutput(hypotheses=PROPOSED))

    state = nodes.diagnoser_node(_state(CRASH_EVIDENCE))

    assert state.confidence == 0.9
    assert state.confidence == state.hypotheses[0].score
    assert state.confidence == scoring.top_confidence(CRASH_EVIDENCE, PROPOSED)
    assert state.confidence != min(1.0, len(CRASH_EVIDENCE) * 0.2)


def test_confidence_moves_with_evidence_content_not_evidence_count(monkeypatch):
    _stub_hypotheses(monkeypatch, DiagnoserOutput(hypotheses=PROPOSED))

    none = nodes.diagnoser_node(_state([])).confidence
    one = nodes.diagnoser_node(_state(CRASH_EVIDENCE[:1])).confidence
    two = nodes.diagnoser_node(_state(CRASH_EVIDENCE)).confidence

    assert (none, one, two) == (0.0, 0.4, 0.9)
    assert one != min(1.0, len(CRASH_EVIDENCE[:1]) * 0.2)
    assert two != min(1.0, len(CRASH_EVIDENCE) * 0.2)


def test_llm_wording_cannot_influence_a_score(monkeypatch):
    _stub_hypotheses(
        monkeypatch,
        DiagnoserOutput(hypotheses=[
            Hypothesis(description=BOASTFUL, category="deploy"),
            Hypothesis(description=UNSURE, category="deploy"),
        ]),
    )

    state = nodes.diagnoser_node(_state(DEPLOY_EVIDENCE))
    by_description = {s.hypothesis.description: s for s in state.hypotheses}

    assert by_description[BOASTFUL].score == 0.15
    assert by_description[BOASTFUL].score == by_description[UNSURE].score
    assert by_description[BOASTFUL].score_breakdown == by_description[UNSURE].score_breakdown
    assert not hasattr(by_description[BOASTFUL].hypothesis, "score")


def test_empty_hypothesis_sentinel_scores_zero(monkeypatch):
    _stub_hypotheses(monkeypatch, DiagnoserOutput.model_construct(hypotheses=[]))

    state = nodes.diagnoser_node(_state(CRASH_EVIDENCE))

    assert state.hypotheses == []
    assert state.confidence == 0.0


def test_empty_hypothesis_sentinel_clears_the_previous_iteration(monkeypatch):
    _stub_hypotheses(monkeypatch, DiagnoserOutput(hypotheses=PROPOSED))
    state = nodes.diagnoser_node(_state(CRASH_EVIDENCE))
    assert state.confidence == 0.9
    assert len(state.hypotheses) == 2

    _stub_hypotheses(monkeypatch, DiagnoserOutput.model_construct(hypotheses=[]))
    state = nodes.diagnoser_node(state)

    assert state.hypotheses == []
    assert state.confidence == 0.0


def test_malformed_evidence_raises_rather_than_being_swallowed(monkeypatch):
    def boom(service_name, evidence_so_far):
        raise KeyError("source")

    monkeypatch.setattr(nodes, "decide_hypotheses", boom)

    with pytest.raises(KeyError):
        nodes.diagnoser_node(_state([{"iteration": 1}]))


def test_observer_tool_allowlist_is_unchanged():
    assert set(nodes.TOOL_DISPATCH) == {
        "query_prometheus",
        "query_loki",
        "get_container_state",
        "inspect_health",
        "get_recent_deployments",
    }


def test_state_rejects_a_wrongly_typed_hypotheses_assignment():
    state = _state(CRASH_EVIDENCE)

    for bad in ([{"nope": 1}], [42], "not a list", None):
        with pytest.raises(ValidationError):
            state.hypotheses = bad

    assert state.hypotheses == []


def test_state_accepts_a_correctly_typed_hypotheses_assignment():
    state = _state(CRASH_EVIDENCE)
    scored = ScoredHypothesis(
        hypothesis=CRASH, score=0.9, score_breakdown={"sources_supporting": 2}
    )

    state.hypotheses = [scored]

    assert state.hypotheses[0] is scored
    assert state.hypotheses[0].score == 0.9
    assert state.hypotheses[0].hypothesis.category == "crash"

    state.hypotheses = [{"hypothesis": CRASH, "score": 0.4, "score_breakdown": {}}]

    assert isinstance(state.hypotheses[0], ScoredHypothesis)
    assert state.hypotheses[0].score == 0.4
