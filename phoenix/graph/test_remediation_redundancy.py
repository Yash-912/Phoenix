"""A restart is not repeated against a process that is already in the state the
last restart left it in.

The verifier keeps its verdict and its thresholds; what changes is only that the
remediator declines an action that has nothing further to do. Everything here
uses generic restart and memory inputs -- no incident, no particular service
size, and no assumption about which category is asking."""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from langgraph.graph import END
from langgraph.types import Command

from phoenix.graph import graph, nodes, verification
from phoenix.graph import remediation_dispatch as dispatch
from phoenix.graph.llm_client import HypothesisDecision, ToolCallDecision
from phoenix.graph.schemas import DiagnoserOutput, Hypothesis, ScoredHypothesis
from phoenix.graph.state import AgentState

SERVICE = "svc"
MIB = 1024 * 1024
ELEVATED = 100_507_648
FRESH = 51_916_800


def _prior(after, outcome="fail", action="restart_service", check="overload", **extra) -> dict:
    return {
        "outcome": outcome, "check": check, "action": action,
        "detail": {"check": check, "before": ELEVATED, "after": after, "reason": "not the drop a restart should produce"},
        "remediation_attempts": 1, "max_remediation_attempts": 2, **extra,
    }


def _state(prior=None, category="overload", **fields) -> AgentState:
    hypothesis = ScoredHypothesis(
        hypothesis=Hypothesis(description="something is wrong", category=category), score=0.9, score_breakdown={}
    )
    return AgentState(
        incident_id=1, service_name=SERVICE, hypotheses=[hypothesis],
        verification_result=prior, remediation_attempts=1 if prior else 0, **fields,
    )


def _wire(monkeypatch, current_bytes, tier3=False):
    restarts: list[str] = []
    audit: list[tuple] = []
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: (restarts.append(name), {"status": "ok"})[1])
    monkeypatch.setattr(verification, "read_signal", lambda category, service: {"bytes": current_bytes, "slope": 0.0})
    monkeypatch.setattr(nodes, "record_audit", lambda incident, node, event, detail, text: audit.append((node, event, detail, text)))
    monkeypatch.setattr(nodes, "is_tier3_eligible", lambda category: tier3)
    return restarts, audit


# ---- Test 1: already at the level the last restart left ------------------------


def test_a_process_still_at_the_level_the_last_restart_left_is_not_restarted_again(monkeypatch):
    restarts, audit = _wire(monkeypatch, current_bytes=FRESH)

    command = nodes.remediator_node(_state(_prior(FRESH)))

    assert restarts == []
    assert command.goto == END
    assert command.update["status"] == "escalated"
    assert "would repeat the last attempt" in command.update["escalation_reason"]
    assert "remediation_attempts" not in command.update
    assert audit[-1][1] == "action_redundant"
    assert audit[-1][3] == command.update["escalation_reason"]


def test_growth_within_the_verifiers_own_noise_tolerance_still_counts_as_the_same_level(monkeypatch):
    restarts, _ = _wire(monkeypatch, current_bytes=FRESH + verification.NOISE_FLOOR_BYTES)

    command = nodes.remediator_node(_state(_prior(FRESH)))

    assert restarts == []
    assert command.update["status"] == "escalated"


def test_a_process_that_sits_below_the_level_the_last_restart_left_is_also_not_restarted(monkeypatch):
    restarts, _ = _wire(monkeypatch, current_bytes=FRESH - 5 * MIB)

    nodes.remediator_node(_state(_prior(FRESH)))

    assert restarts == []


# ---- Test 2: still elevated, so the retry is unchanged ----------------------------


def test_memory_that_has_grown_past_the_noise_since_is_restarted_as_before(monkeypatch):
    restarts, _ = _wire(monkeypatch, current_bytes=FRESH + verification.NOISE_FLOOR_BYTES + 1)

    command = nodes.remediator_node(_state(_prior(FRESH)))

    assert restarts == [SERVICE]
    assert command.goto == "verifier"
    assert command.update["remediation_attempts"] == 2


def test_a_process_still_substantially_elevated_is_restarted_as_before(monkeypatch):
    restarts, _ = _wire(monkeypatch, current_bytes=ELEVATED)

    command = nodes.remediator_node(_state(_prior(FRESH)))

    assert restarts == [SERVICE]
    assert command.goto == "verifier"


def test_the_first_attempt_is_never_declined(monkeypatch):
    restarts, _ = _wire(monkeypatch, current_bytes=FRESH)

    command = nodes.remediator_node(_state(prior=None))

    assert restarts == [SERVICE]
    assert command.goto == "verifier"


# ---- Test 3: the verifier is untouched ---------------------------------------


def test_the_verifiers_thresholds_are_what_they_were():
    assert verification.MEMORY_DROP_RATIO == 0.5
    assert verification.NOISE_FLOOR_BYTES == MIB
    assert verification.SLOPE_TOLERANCE == 1024


def test_the_verifier_still_fails_a_restart_that_misses_its_strict_drop():
    first, _ = verification._check_overload({"bytes": FRESH, "slope": 7865.0, "window_seconds": 90}, {"bytes": ELEVATED})
    second, _ = verification._check_overload({"bytes": FRESH + 274_432, "slope": 7865.0, "window_seconds": 90}, {"bytes": FRESH})

    assert first == verification.OUTCOME_FAIL
    assert second == verification.OUTCOME_FAIL


def test_the_verifier_still_passes_a_restart_that_makes_the_drop():
    outcome, _ = verification._check_overload({"bytes": 40 * MIB, "slope": 0.0, "window_seconds": 90}, {"bytes": ELEVATED})

    assert outcome == verification.OUTCOME_PASS


def test_declining_a_restart_never_calls_the_run_recovered_or_runs_the_verifier(monkeypatch):
    _wire(monkeypatch, current_bytes=FRESH)
    ran = []
    monkeypatch.setattr(verification, "run_check", lambda *a: ran.append(a) or ("pass", {}))

    command = nodes.remediator_node(_state(_prior(FRESH)))

    assert ran == []
    assert command.update["status"] != "resolved"


# ---- Test 4: generic, not tied to one size or one kind of previous result -----------


def test_the_same_rule_holds_at_any_size(monkeypatch):
    restarts, _ = _wire(monkeypatch, current_bytes=1_200_000_000)

    nodes.remediator_node(_state(_prior(1_200_000_000)))

    assert restarts == []


def test_a_previous_result_that_recorded_no_level_leaves_the_retry_alone(monkeypatch):
    restarts, _ = _wire(monkeypatch, current_bytes=FRESH)
    no_level = _prior(FRESH)
    no_level["detail"] = {"check": "overload", "reason": "could not read it"}

    nodes.remediator_node(_state(no_level))

    assert restarts == [SERVICE]


def test_a_non_numeric_recorded_level_leaves_the_retry_alone(monkeypatch):
    for bad in (None, "51916800", True, [FRESH]):
        restarts, _ = _wire(monkeypatch, current_bytes=FRESH)

        nodes.remediator_node(_state(_prior(bad)))

        assert restarts == [SERVICE], bad


def test_an_unreadable_current_snapshot_leaves_the_retry_alone(monkeypatch):
    restarts, _ = _wire(monkeypatch, current_bytes=FRESH)
    monkeypatch.setattr(verification, "read_signal", lambda category, service: {"status": "error", "error": "no data"})

    nodes.remediator_node(_state(_prior(FRESH)))

    assert restarts == [SERVICE]


def test_only_a_failed_verdict_of_the_same_action_and_check_counts(monkeypatch):
    for prior in (
        _prior(FRESH, outcome="pass"),
        _prior(FRESH, outcome="inconclusive"),
        _prior(FRESH, action="rollback_deploy"),
        _prior(FRESH, check="config"),
    ):
        restarts, _ = _wire(monkeypatch, current_bytes=FRESH)

        nodes.remediator_node(_state(prior))

        assert restarts == [SERVICE], prior


# ---- Test 5: where a declined restart goes ------------------------------------------


def test_a_category_that_policy_sends_to_tier_3_goes_there_with_the_failed_verdict_on_record(monkeypatch):
    restarts, audit = _wire(monkeypatch, current_bytes=FRESH, tier3=True)
    prior = _prior(FRESH)

    command = nodes.remediator_node(_state(prior))

    assert restarts == []
    assert command.goto == "code_investigator"
    assert command.update["status"] == "tier3_investigating"
    assert command.update["tier1_mitigation"] == prior
    assert command.update["tier1_mitigation"]["outcome"] == "fail"
    assert audit[-1][1] == "tier3_handoff"
    assert audit[-1][2]["redundant_action"] == "restart_service"
    assert audit[-1][2]["post_action_bytes"] == FRESH


def test_a_category_policy_does_not_send_to_tier_3_is_escalated_not_resolved(monkeypatch):
    _wire(monkeypatch, current_bytes=FRESH, tier3=False)

    command = nodes.remediator_node(_state(_prior(FRESH)))

    assert command.goto == END
    assert command.update["status"] == "escalated"


# ---- through the compiled graph -------------------------------------------------------


def _compiled(monkeypatch, tier3: bool):
    snapshots = iter([{"bytes": ELEVATED, "slope": 0.0}, {"bytes": FRESH, "slope": 0.0}])
    restarts: list[str] = []
    reached: list[str] = []

    def stub_investigator(state: AgentState) -> Command:
        reached.append("code_investigator")
        return Command(goto=END, update={"status": "tier3_investigating"})

    monkeypatch.setattr(graph, "code_investigator_node", stub_investigator)
    monkeypatch.setattr(nodes, "decide_tool_calls", lambda service, evidence, requests, evidence_state=None: ToolCallDecision(
        [{"name": "query_prometheus", "arguments": {"promql": "up"}}, {"name": "query_loki", "arguments": {"logql": "x"}}], 1))
    monkeypatch.setattr(nodes, "decide_hypotheses", lambda service, evidence: HypothesisDecision(
        DiagnoserOutput(hypotheses=[Hypothesis(description="resource pressure", category="overload")]), 10))
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: {"status": "success", "text": "latency p95 slow"})
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: {"status": "success", "text": "request latency slow"})
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: (restarts.append(name), {"status": "ok"})[1])
    monkeypatch.setattr(verification, "read_signal", lambda category, service: next(snapshots))
    monkeypatch.setattr(
        verification, "run_check",
        lambda *a: (verification.OUTCOME_FAIL, {"check": "overload", "before": ELEVATED, "after": FRESH, "reason": "missed the strict drop"}),
    )
    monkeypatch.setattr(nodes.time, "sleep", lambda seconds: None)
    for name in ("record_evidence", "record_audit", "record_hypotheses"):
        monkeypatch.setattr(nodes, name, lambda *a, **k: None)
    monkeypatch.setattr(nodes, "get_incident_first_seen", lambda incident: None)
    monkeypatch.setattr(nodes, "is_tier3_eligible", lambda category: tier3)
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)

    result = graph.build_graph().invoke(AgentState(incident_id=1, service_name=SERVICE, token_budget=100000, max_iterations=5))
    return (result if isinstance(result, dict) else dict(result)), restarts, reached


def test_the_compiled_run_restarts_once_and_then_escalates_when_the_restart_missed_the_strict_drop(monkeypatch):
    final, restarts, reached = _compiled(monkeypatch, tier3=False)

    assert restarts == [SERVICE]
    assert final["remediation_attempts"] == 1
    assert final["status"] == "escalated"
    assert "would repeat the last attempt" in final["escalation_reason"]
    assert final["verification_result"]["outcome"] == "fail"
    assert reached == []


def test_the_compiled_run_hands_a_tier_3_category_on_after_one_restart(monkeypatch):
    final, restarts, reached = _compiled(monkeypatch, tier3=True)

    assert restarts == [SERVICE]
    assert reached == ["code_investigator"]
    assert final["status"] == "tier3_investigating"
    assert final["tier1_mitigation"]["outcome"] == "fail"
    assert final["verification_result"]["outcome"] == "fail"
