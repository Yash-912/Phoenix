"""Never remediate a symptom that is no longer observed.

The verifier refuses to report a recovery it did not observe. This is the
inverse: the remediator refuses to act on a symptom nobody can observe now. A
stale finding with a healthy service takes no action and opens no PR; a finding
whose symptom is still there is acted on exactly as before."""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

import pytest
from langgraph.graph import END

from phoenix.graph import nodes, scoring, verification
from phoenix.graph import remediation_dispatch as dispatch
from phoenix.graph.schemas import Hypothesis, ScoredHypothesis
from phoenix.graph.state import AgentState

SERVICE = "svc"
SLOW = Hypothesis(description="a slow query", category="slow_query")
LEAK = Hypothesis(description="memory keeps growing", category="memory_leak")
CRASH = Hypothesis(description="the service is crashing", category="crash")

SLOW_MEASURE = {"verdict": "elevated_ongoing", "sustained_slow": True}


def _item(source: str, raw: dict) -> dict:
    return {"iteration": 1, "source": source, "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": f"{source}(...)", "raw_data": raw}


def _confident(hypothesis: Hypothesis, *evidence: dict) -> AgentState:
    state = AgentState(incident_id=1, service_name=SERVICE, evidence=list(evidence), confidence=0.9)
    state.hypotheses = [
        ScoredHypothesis(hypothesis=h, score=s, score_breakdown=b)
        for h, s, b in scoring.score_all(list(evidence), [hypothesis])
    ]
    return state


def _slow_state() -> AgentState:
    return _confident(
        SLOW,
        _item("query_prometheus", {"status": "success", "latency_measure": SLOW_MEASURE}),
        _item("query_loki", {"status": "success", "text": "duration: 1203.4 ms  statement: SELECT * FROM charges"}),
    )


@pytest.fixture
def audit(monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setattr(nodes, "record_audit", lambda incident, node, event, detail, text: rows.append((event, detail, text)))
    return rows


def _symptom(monkeypatch, state: str, calls: list | None = None):
    def fake(category, service):
        if calls is not None:
            calls.append((category, service))
        return {"state": state}

    monkeypatch.setattr(nodes, "current_symptom", fake)


# ---- a finding that goes to Tier 3 (slow_query has no Tier 1/2 action) ------------------


def test_a_symptom_still_present_is_handed_to_tier_3_as_before(monkeypatch, audit):
    _symptom(monkeypatch, "present")

    command = nodes.remediator_node(_slow_state())

    assert command.goto == "code_investigator"
    assert command.update == {"status": "tier3_investigating"}


def test_a_symptom_that_cannot_be_observed_does_not_block_the_handoff(monkeypatch, audit):
    """No traffic is not recovery: absence of an observation is not an observation of absence."""
    _symptom(monkeypatch, "unknown")

    assert nodes.remediator_node(_slow_state()).goto == "code_investigator"


def test_a_symptom_that_is_gone_stops_the_run_before_any_tier_3_work(monkeypatch, audit):
    _symptom(monkeypatch, "absent")

    command = nodes.remediator_node(_slow_state())

    assert command.goto == END
    assert command.update["status"] == "escalated"
    assert "no longer observed" in command.update["escalation_reason"]
    assert "slow_query" in command.update["escalation_reason"]


def test_a_cleared_symptom_is_recorded_on_the_audit_trail(monkeypatch, audit):
    _symptom(monkeypatch, "absent")

    nodes.remediator_node(_slow_state())

    events = [event for event, _, _ in audit]
    assert "symptom_cleared" in events
    assert "tier3_handoff" not in events


def test_the_check_is_made_for_the_top_hypothesis_of_the_run(monkeypatch, audit):
    calls: list = []
    _symptom(monkeypatch, "present", calls)

    nodes.remediator_node(_slow_state())

    assert calls == [("slow_query", SERVICE)]


# ---- a finding that gets a Tier 1 action ------------------------------------------------


def _leak_state() -> AgentState:
    return _confident(
        LEAK,
        _item("query_prometheus", {"status": "success", "memory_trend": {"verdict": "sustained_growth", "sustained_growth": True}}),
        _item("query_loki", {"status": "success", "text": "job result cache is growing, nothing is ever evicted"}),
    )


def _watch_restarts(monkeypatch) -> list:
    restarted: list = []
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service",
                        lambda name: restarted.append(name) or {"status": "ok"})
    monkeypatch.setattr(verification, "read_signal", lambda check, service: {"bytes": 1.0})
    return restarted


def test_a_symptom_still_present_is_restarted_as_before(monkeypatch, audit):
    restarted = _watch_restarts(monkeypatch)
    _symptom(monkeypatch, "present")

    command = nodes.remediator_node(_leak_state())

    assert command.goto == "verifier"
    assert restarted == [SERVICE]


def test_a_symptom_that_is_gone_is_not_restarted(monkeypatch, audit):
    restarted = _watch_restarts(monkeypatch)
    _symptom(monkeypatch, "absent")

    command = nodes.remediator_node(_leak_state())

    assert command.goto == END
    assert restarted == []
    assert command.update["status"] == "escalated"


def test_the_check_comes_before_the_pre_action_snapshot(monkeypatch, audit):
    """A restart that is not taken must cost the world nothing, so nothing is read for it either."""
    _watch_restarts(monkeypatch)
    monkeypatch.setattr(verification, "read_signal", lambda *a: (_ for _ in ()).throw(AssertionError("snapshot read")))
    _symptom(monkeypatch, "absent")

    nodes.remediator_node(_leak_state())


# ---- categories with no probe behave exactly as they did -----------------------------------


def test_a_category_with_no_probe_is_acted_on_without_a_check(monkeypatch, audit):
    restarted = _watch_restarts(monkeypatch)
    monkeypatch.setattr(nodes, "current_symptom", lambda category, service: {"state": "not_checked"})
    state = _confident(CRASH, _item("query_prometheus", {"status": "success", "text": "up==0 ServiceDown"}),
                       _item("query_loki", {"status": "success", "text": "panic: process exit 1"}))

    command = nodes.remediator_node(state)

    assert command.goto == "verifier"
    assert restarted == [SERVICE]


def test_the_attempt_cap_still_speaks_before_the_symptom_check(monkeypatch, audit):
    calls: list = []
    _symptom(monkeypatch, "absent", calls)
    state = _slow_state()
    state.remediation_attempts = state.max_remediation_attempts

    command = nodes.remediator_node(state)

    assert "remediation attempts exhausted" in command.update["escalation_reason"]
    assert calls == []
