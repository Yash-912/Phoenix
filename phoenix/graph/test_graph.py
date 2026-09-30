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


def _settled_remediation(monkeypatch, outcome="pass", **detail) -> None:
    """Neutralize everything downstream of the router's confidence branch.

    That branch now routes to the remediator instead of to END, so a test which
    only means to exercise routing would otherwise restart a real container and
    then sit out the verification delay. Every door the remediator and verifier
    can reach is replaced here rather than in each test, so a future node added
    to that path fails loudly in one place instead of quietly reaching the
    Docker socket from a test that never meant to touch it.

    outcome may be a single verdict or a list of them consumed in order, which
    is how a run that fails its first check and passes its second is described.
    """
    verdicts = list(outcome) if isinstance(outcome, list) else None

    def run_check(*args):
        if verdicts is None:
            return outcome, {"reason": "stubbed", **detail}
        verdict = verdicts.pop(0) if verdicts else outcome
        return verdict, {"reason": "stubbed", "outcome": verdict, **detail}

    monkeypatch.setitem(
        dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: {"status": "ok"}
    )
    monkeypatch.setattr(verification, "read_signal", lambda category, service: SNAPSHOT)
    monkeypatch.setattr(verification, "run_check", run_check)
    monkeypatch.setattr(nodes.time, "sleep", lambda seconds: None)


def _no_action_reachable(monkeypatch) -> list[str]:
    """A dispatch table whose only action fails the test if it is ever reached."""
    reached: list[str] = []

    def forbidden(name: str) -> dict:
        reached.append(name)
        raise AssertionError(f"{name} was dispatched by a run that must not act")

    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", forbidden)
    monkeypatch.setattr(nodes.time, "sleep", lambda seconds: None)
    return reached


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


# --- the whole remediate-and-verify loop, through the compiled graph -----------
#
# Everything above drives the router and the nodes separately. These drive the
# compiled graph, because the failure they exist to catch cannot happen in a
# unit test: a node that mutates state in place instead of returning it in its
# Command.update passes every test in isolation and drops the whole effect on the
# way to the final state. In particular a dropped remediation_attempts would let
# the run restart a service forever, and a dropped planned_action would leave the
# verifier with nothing to check. Neither is visible until the nodes are joined
# by real edges.

DEPLOY_ROLLOUT = {"status": "success", "text": "image checkout:v18, rollout complete"}
PROBE_FAILING = {"status": "success", "text": "readiness probe failing, cpu at 98%"}


def _confident_crash(monkeypatch, calls: list[str] | None = None, passes: int = 1):
    """A run that reaches the confidence threshold on its first pass."""

    def fake_tool_calls(service_name, evidence_so_far, evidence_requests):
        if calls is not None:
            calls.append("observer")
        return ToolCallDecision(
            [
                {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
                {"name": "query_loki", "arguments": {"logql": '{container="x"}'}},
            ],
            1,
        )

    def fake_hypotheses(service_name, evidence_so_far):
        if calls is not None:
            calls.append("diagnoser")
        return HypothesisDecision(
            DiagnoserOutput(hypotheses=[Hypothesis(description="it crashed", category="crash")]),
            1,
        )

    monkeypatch.setattr(nodes, "decide_tool_calls", fake_tool_calls)
    monkeypatch.setattr(nodes, "decide_hypotheses", fake_hypotheses)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: SERVICE_DOWN)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: PANIC)


def _confident_deploy(monkeypatch):
    """A run whose finding is a deploy, which no Tier 1 action can fix."""

    def fake_tool_calls(service_name, evidence_so_far, evidence_requests):
        return ToolCallDecision(
            [
                {"name": "get_recent_deployments", "arguments": {"service_name": SERVICE}},
                {"name": "inspect_health", "arguments": {"service_name": SERVICE}},
            ],
            1,
        )

    def fake_hypotheses(service_name, evidence_so_far):
        return HypothesisDecision(
            DiagnoserOutput(
                hypotheses=[Hypothesis(description="the v18 rollout broke it", category="deploy")]
            ),
            1,
        )

    monkeypatch.setattr(nodes, "decide_tool_calls", fake_tool_calls)
    monkeypatch.setattr(nodes, "decide_hypotheses", fake_hypotheses)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "get_recent_deployments", lambda args: DEPLOY_ROLLOUT)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "inspect_health", lambda args: PROBE_FAILING)


def test_a_confident_crash_run_restarts_once_and_ends_resolved(monkeypatch):
    calls: list[str] = []
    _confident_crash(monkeypatch, calls)
    _settled_remediation(monkeypatch)

    final = _final(
        graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5))
    )

    assert calls == ["observer", "diagnoser"]
    assert final["remediation_attempts"] == 1
    assert final["planned_action"]["action"] == "restart_service"
    assert final["planned_action"]["check"] == "crash"
    assert final["verification_result"]["outcome"] == "pass"
    assert final["status"] == "resolved"
    assert final.get("escalation_reason") is None


def test_a_deploy_finding_cannot_clear_the_threshold_so_it_never_reaches_the_remediator(
    monkeypatch,
):
    """A pinned gap, kept as a test so it fails loudly when it is closed.

    The spec wants a confident finding with no Tier 1 action to end as
    action_unavailable rather than as an escalation. That outcome is unreachable
    through the real graph today, because the only categories CATEGORY_ACTIONS
    leaves without an action -- deploy, config, network, unknown -- are also the
    ones scoring cannot lift to 0.75: deploy's evidence weights cap it at 0.15.
    The run therefore loops to the iteration cap and escalates for being
    inconclusive, which is the opposite of the finding: the operator is told the
    agent gave up rather than that it diagnosed a deploy and knew it needed a
    human.

    What is asserted here is the safety half, which does hold, plus the fact that
    keeps the gap visible. The action_unavailable branch itself is proven at the
    node level in test_nodes.py. Closing this properly means deciding whether a
    no-action category should be held to the remediation threshold at all, which
    is a scoring-policy change and not a test change -- so it is deliberately not
    papered over here by tuning evidence or the threshold to make it pass.
    """
    reached = _no_action_reachable(monkeypatch)
    _confident_deploy(monkeypatch)

    final = _final(
        graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5))
    )

    assert reached == []
    assert final["confidence"] < final["confidence_threshold"]
    assert final["status"] == "escalated"
    assert final["status"] != "resolved"
    assert final["remediation_attempts"] == 0
    assert final.get("verification_result") is None


def test_a_guarded_run_reaches_the_remediator_and_takes_no_action(monkeypatch):
    reached = _no_action_reachable(monkeypatch)
    _confident_crash(monkeypatch)

    final = _final(
        graph.build_graph().invoke(
            _state(policy_mode="guarded", token_budget=100000, max_iterations=5)
        )
    )

    assert reached == []
    assert final["status"] == "escalated"
    assert "guarded" in final["escalation_reason"]
    assert final["remediation_attempts"] == 0


def test_a_check_it_cannot_make_escalates_and_never_claims_the_service_recovered(monkeypatch):
    """The one property the whole module exists for: an unmeasurable outcome is
    not a success. If this ever passes with a resolved run, the agent is
    reporting recovery it did not observe."""
    _confident_crash(monkeypatch)
    _settled_remediation(monkeypatch, "inconclusive")

    final = _final(
        graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5))
    )

    assert final["status"] == "escalated"
    assert final["status"] != "resolved"
    assert final["verification_result"]["outcome"] == "inconclusive"
    assert final["escalation_reason"]


def test_a_check_that_fails_comes_back_for_another_attempt_and_then_resolves(monkeypatch):
    """Proves remediation_attempts actually survives the graph. If the node
    mutated its copy instead of returning the increment, this run would restart
    the service on every pass and never reach a second, counted attempt."""
    calls: list[str] = []
    passes = {"n": 0}

    def fake_tool_calls(service_name, evidence_so_far, evidence_requests):
        passes["n"] += 1
        calls.append("observer")
        return ToolCallDecision(
            [
                {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
                {"name": "query_loki", "arguments": {"logql": '{container="x"}'}},
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
    _settled_remediation(monkeypatch, ["fail", "pass"])

    final = _final(
        graph.build_graph().invoke(_state(token_budget=100000, max_iterations=10))
    )

    assert passes["n"] == 2
    assert calls == ["observer", "diagnoser", "observer", "diagnoser"]
    assert final["remediation_attempts"] == 2
    assert final["verification_result"]["outcome"] == "pass"
    assert final["status"] == "resolved"
    assert final.get("escalation_reason") is None


def test_a_run_that_keeps_failing_stops_at_the_attempt_cap_instead_of_looping_forever(
    monkeypatch,
):
    """The cap is the thing that bounds how much damage a service can take. If
    the increment were dropped this test would run to the iteration cap instead,
    which is a different failure with the same symptom of taking too long."""
    passes = {"n": 0}

    def fake_tool_calls(service_name, evidence_so_far, evidence_requests):
        passes["n"] += 1
        return ToolCallDecision(
            [
                {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
                {"name": "query_loki", "arguments": {"logql": '{container="x"}'}},
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
    _settled_remediation(monkeypatch, "fail")

    final = _final(
        graph.build_graph().invoke(
            _state(token_budget=100000, max_iterations=50, max_remediation_attempts=2)
        )
    )

    assert passes["n"] == 2
    assert final["remediation_attempts"] == 2
    assert final["status"] == "escalated"
    assert "attempt" in final["escalation_reason"]
