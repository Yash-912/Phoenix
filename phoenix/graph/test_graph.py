"""Unit tests for the router â€” no network, no LLM, no database.

should_continue is pure code over AgentState, so these drive it directly and
assert both halves of the Command it returns: the destination it routes to and
the state update it hands back. The two exits that existed before the budget
guard â€” the confidence threshold and the iteration cap â€” are regression-proofed
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
from phoenix.graph import remediation_policy
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

    def fake_tool_calls(service_name, evidence_so_far, evidence_requests, evidence_state=None):
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


def _confident_crash(monkeypatch, calls: list[str] | None = None):
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
# --- Phase 4: the Tier 2 deploy path ----------------------------------------
#
# Phase 3 pinned the opposite outcome as a known gap: a deploy finding could not
# clear the threshold, so it never reached the remediator and escalated for being
# inconclusive. That gap is closed, and closed by making the deployment real
# rather than by changing the scorer. The fixtures below are the payloads the
# tools return for a genuine v18 rollout.


def _v18_evidence_payloads(history: list[dict] | None = None) -> dict:
    """What the read-only tools return for a genuine v18 rollout.

    Phase 3's fixtures for this scenario were two summary strings, and neither
    carried anything scoring could weigh. "image checkout:v18, rollout complete"
    is a single keyword hit against a 0.15 deploy weight and a 0.75 threshold,
    which is why the scenario could not reach the remediator at all.

    These are the real payload shapes: the container carries app.version because
    the deployer labelled it, the log line names v18 because a release build
    prints which build raised the error, and the history holds the rollout that
    put it there. Scoring weighs that at 0.95 on its own, with no scoring change.
    """
    rollout = [
        {"service": SERVICE, "timestamp": "2026-10-02T08:00:00+00:00",
         "image_tag": "v18", "git_commit": "bad-v18",
         "config": {"regression": True}, "deployed_by": "chaos/deploy_bad_v18.py",
         "correlation": "before_incident", "delta_seconds": -30.0, "in_window": True},
        {"service": SERVICE, "timestamp": "2026-10-01T08:00:00+00:00",
         "image_tag": "v17", "git_commit": "good-v17",
         "config": {"regression": False}, "deployed_by": "chaos/deploy_bad_v18.py --reset",
         "correlation": "before_incident", "delta_seconds": -86430.0, "in_window": False},
    ]
    return {
        "get_container_state": {
            "Image": "sha256:2432102350f3",
            "State": {"Status": "running", "StartedAt": "2026-10-02T08:00:00.000000000Z"},
            "Config": {"Image": "agentic/checkout-service:v18",
                       "Labels": {"app.version": "v18"}},
        },
        "query_loki": {
            "status": "ok",
            "data": {"result": [
                {"line": "2026-10-02 08:00:03 ERROR checkout-service v18 "
                         "order rejected: inventory reservation expired"}]},
        },
        "query_prometheus": {
            "status": "success",
            "data": {"resultType": "vector", "result": [
                {"metric": {"__name__": "http_requests_total",
                            "job": SERVICE, "status": "500"},
                 "value": [1759000000.0, "1847"]}]},
        },
        "inspect_health": {"container": {"status": "running"},
                           "app": {"status": "ok", "version": "v18"}},
        "get_recent_deployments": {
            "status": "ok",
            "service": SERVICE,
            "incident_started_at": "2026-10-02T08:00:30+00:00",
            "deployments": rollout if history is None else history,
        },
    }


def _deploy_run(monkeypatch, *, running_version="v18", history=None) -> list[dict]:
    """Drive the compiled graph over real v18 evidence, stubbing only the
    mutation and the HTTP probe.

    Returns the collector for rollback calls so a test can assert what was
    deployed, or that nothing was. The verification stub echoes the version it
    was handed back into the detail, which is how the tests below check that the
    restored version survives the trip from the resolver to the check.
    """
    payloads = _v18_evidence_payloads(history)
    rolled_back: list[dict] = []

    def fake_tool_calls(service_name, evidence_so_far, evidence_requests):
        return ToolCallDecision(
            [
                {"name": "get_recent_deployments", "arguments": {"service_name": SERVICE}},
                {"name": "get_container_state", "arguments": {"container_name": SERVICE}},
                {"name": "query_loki", "arguments": {"logql": '{container="checkout-service"}'}},
                {"name": "query_prometheus", "arguments": {"promql": "http_requests_total"}},
            ],
            1,
        )

    def fake_hypotheses(service_name, evidence_so_far):
        return HypothesisDecision(
            DiagnoserOutput(
                hypotheses=[Hypothesis(description="the v18 rollout broke it",
                                       category="deploy")]
            ),
            1,
        )

    monkeypatch.setattr(nodes, "decide_tool_calls", fake_tool_calls)
    monkeypatch.setattr(nodes, "decide_hypotheses", fake_hypotheses)
    for name, payload in payloads.items():
        monkeypatch.setitem(nodes.TOOL_DISPATCH, name, lambda args, _p=payload: _p)

    monkeypatch.setattr(nodes, "get_incident_first_seen",
                        lambda incident_id: "2026-10-02T08:00:30+00:00")
    # Policy reads the live container directly rather than through nodes, so the
    # stub belongs where the import landed. Miss this and the real container
    # answers: a lab left on v17 makes the already-restored guard fire against a
    # run that is supposed to be about v18, which is a confusing way to fail.
    monkeypatch.setattr(remediation_policy, "get_container_state",
                        lambda name: {"Config": {"Labels": {"app.version": running_version}}})
    monkeypatch.setattr(
        "phoenix.graph.rollback_target.get_recent_deployments",
        lambda service, limit=10: {
            "status": "ok",
            "deployments": payloads["get_recent_deployments"]["deployments"]},
    )
    monkeypatch.setitem(
        dispatch.TIER_2_DISPATCH, "rollback_deployment",
        lambda service, *args: rolled_back.append({"service": service, "args": args})
        or {"status": "ok", "to_version": args[0], "image_digest": "sha256:abc"},
    )
    monkeypatch.setattr(
        verification, "run_check",
        lambda category, service_name, before, action_at, expect_version=None:
            ("pass", {"check": category, "expected_version": expect_version,
                      "running_version": running_version}),
    )
    monkeypatch.setattr(nodes.time, "sleep", lambda seconds: None)
    return rolled_back


def test_a_confident_deploy_finding_rolls_back_to_the_version_before_the_incident(monkeypatch):
    """The Phase 4 path end to end through the compiled graph: score, route,
    resolve the target from history, roll back, verify."""
    rolled_back = _deploy_run(monkeypatch)

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert final["confidence"] >= final["confidence_threshold"], final["confidence"]
    assert final["planned_action"]["action"] == "rollback_deployment"
    assert final["planned_action"]["check"] == "deploy"
    # v17, and the v18 marker being undone. Hand-derived from the fixture rather
    # than read back out of the resolver that produced it.
    assert rolled_back == [{"service": SERVICE,
                            "args": ("v17", "2026-10-02T08:00:00+00:00")}]
    assert final["verification_result"]["outcome"] == "pass"
    assert final["status"] == "resolved"


def test_a_deploy_rollback_is_checked_against_the_version_it_restored(monkeypatch):
    """Without the restored version the deploy check has no identity to demand,
    and an identity-less check passes a container still running the bad release,
    reporting a recovery that never happened."""
    _deploy_run(monkeypatch)

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert final["verification_result"]["detail"]["expected_version"] == "v17"
    assert final["status"] == "resolved"


def test_a_deploy_run_with_nothing_safe_to_restore_takes_no_action(monkeypatch):
    """The honest outcome is a refusal, not a rollback to an arbitrary tag."""
    only_the_bad_release = [
        {"service": SERVICE, "timestamp": "2026-10-02T08:00:00+00:00",
         "image_tag": "v18", "config": {"regression": True}},
    ]
    rolled_back = _deploy_run(monkeypatch, history=only_the_bad_release)
    monkeypatch.setitem(
        dispatch.TIER_2_DISPATCH, "rollback_deployment",
        lambda *a: pytest.fail("no safe target existed, so nothing may be deployed"),
    )

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert rolled_back == []
    assert final["status"] == "action_unavailable"
    assert final["remediation_attempts"] == 0
    assert final.get("verification_result") is None


def test_a_service_already_serving_the_good_artifact_is_not_rolled_back_again(monkeypatch):
    """Scoring keys on the version appearing in labels and logs, so the stale v18
    evidence still clears the threshold after a successful rollback. Without this
    guard a converged run keeps reverting a healthy service on later iterations."""
    rolled_back = _deploy_run(monkeypatch, running_version="v17")
    monkeypatch.setitem(
        dispatch.TIER_2_DISPATCH, "rollback_deployment",
        lambda *a: pytest.fail("the service is already serving v17; reverting again would break it"),
    )

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert rolled_back == []
    assert final["remediation_attempts"] == 0


def test_a_guarded_run_with_a_deploy_finding_takes_no_action(monkeypatch):
    """Tier 2 is the first tier the safety policy gates, so the gate has to apply
    before the rollback rather than after it."""
    rolled_back = _deploy_run(monkeypatch)
    monkeypatch.setitem(
        dispatch.TIER_2_DISPATCH, "rollback_deployment",
        lambda *a: pytest.fail("guarded mode must not reach a Tier 2 action"),
    )

    final = _final(
        graph.build_graph().invoke(
            _state(policy_mode="guarded", token_budget=100000, max_iterations=5)
        )
    )

    assert rolled_back == []
    assert final["status"] != "resolved"
    assert final["remediation_attempts"] == 0


def test_an_observer_asking_for_a_rollback_is_ignored(monkeypatch):
    """The boundary Phase 4 must not erode: the model may reach a deploy
    diagnosis, but it has no tool with which to change an artifact. Asserted by
    behaviour, since the model is free to ask for anything at all."""
    rolled_back = _deploy_run(monkeypatch)

    def forge_a_tool_call(service_name, evidence_so_far, evidence_requests, evidence_state=None):
        return ToolCallDecision(
            [{"name": "rollback_deployment",
              "arguments": {"service_name": SERVICE, "target_version": "latest"}}],
            1,
        )

    monkeypatch.setattr(nodes, "decide_tool_calls", forge_a_tool_call)
    monkeypatch.setitem(
        dispatch.TIER_2_DISPATCH, "rollback_deployment",
        lambda *a: pytest.fail("the observer must not be able to invoke a mutation"),
    )

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert rolled_back == []
    # The forged call is dropped as unrecognized, so no evidence ever claims a
    # deployment was inspected. The diagnoser still proposes its hypothesis --
    # it always may -- but with nothing to support it, nothing is deployed.
    assert final["evidence"] == []
    assert [hypothesis.score for hypothesis in final["hypotheses"]] == [0.0]
    assert final["status"] != "resolved"


# --- Phase 4: the Tier 2 config path -----------------------------------------
#
# Same shape as the deploy section above, for the other Tier 2 action: a shrunk
# connection pool is a value, not an artifact, so there is no image to roll back
# and no app.version label to read -- the running state is the service's own
# /health response instead.

AUTH_SERVICE = "auth-service"


def _pool_evidence_payloads(history: list[dict] | None = None) -> dict:
    """What the read-only tools return for a genuine pool-size regression.

    "pool" has to land in the primary sources' content for scoring to clear the
    confidence threshold, the same requirement _v18_evidence_payloads meets for
    "v18": a metric name, a log line, and a container env entry, each carrying
    it for a different, real reason.
    """
    history = history if history is not None else [
        {"service": AUTH_SERVICE, "timestamp": "2026-10-02T08:00:00+00:00",
         "image_tag": "same", "config": {"DB_POOL_SIZE": "1"},
         "deployed_by": "chaos/config_pool.py",
         "correlation": "before_incident", "delta_seconds": -30.0, "in_window": True},
    ]
    return {
        "get_container_state": {
            "Image": "sha256:auth",
            "State": {"Status": "running"},
            "Config": {"Env": ["DB_POOL_SIZE=1"], "Labels": {}},
        },
        "query_loki": {
            "status": "ok",
            "data": {"result": [
                {"line": "2026-10-02 08:00:03 ERROR auth-service "
                         "connection pool exhausted, cannot acquire connection"}]},
        },
        "query_prometheus": {
            "status": "success",
            "data": {"resultType": "vector", "result": [
                {"metric": {"__name__": "db_pool_size", "job": AUTH_SERVICE},
                 "value": [1759000000.0, "1"]}]},
        },
        "inspect_health": {"container": {"status": "running"},
                           "app": {"status": "ok", "db_pool_size": "1"}},
        "get_recent_deployments": {
            "status": "ok",
            "service": AUTH_SERVICE,
            "incident_started_at": "2026-10-02T08:00:30+00:00",
            "deployments": history,
        },
    }


def _config_run(monkeypatch, *, running_value="1", history=None) -> list[dict]:
    """Drive the compiled graph over real pool-regression evidence, stubbing
    only the mutation and the health probes the policy/verifier read live."""
    payloads = _pool_evidence_payloads(history)
    rolled_back: list[dict] = []

    def fake_tool_calls(service_name, evidence_so_far, evidence_requests):
        return ToolCallDecision(
            [
                {"name": "get_recent_deployments", "arguments": {"service_name": AUTH_SERVICE}},
                {"name": "get_container_state", "arguments": {"container_name": AUTH_SERVICE}},
                {"name": "query_loki", "arguments": {"logql": '{container="auth-service"}'}},
                {"name": "query_prometheus", "arguments": {"promql": "db_pool_size"}},
            ],
            1,
        )

    def fake_hypotheses(service_name, evidence_so_far):
        return HypothesisDecision(
            DiagnoserOutput(
                hypotheses=[Hypothesis(description="the auth pool was shrunk",
                                       category="config")]
            ),
            1,
        )

    monkeypatch.setattr(nodes, "decide_tool_calls", fake_tool_calls)
    monkeypatch.setattr(nodes, "decide_hypotheses", fake_hypotheses)
    for name, payload in payloads.items():
        monkeypatch.setitem(nodes.TOOL_DISPATCH, name, lambda args, _p=payload: _p)

    monkeypatch.setattr(nodes, "get_incident_first_seen",
                        lambda incident_id: "2026-10-02T08:00:30+00:00")
    # Policy reads the live /health response directly rather than through nodes,
    # the config equivalent of the deploy section's get_container_state stub.
    monkeypatch.setattr(
        remediation_policy, "inspect_health",
        lambda name: {"app": {"status": "ok", "db_pool_size": running_value}},
    )
    monkeypatch.setattr(
        "phoenix.graph.config_rollback_target.get_recent_deployments",
        lambda service, limit=10: {
            "status": "ok",
            "deployments": payloads["get_recent_deployments"]["deployments"]},
    )
    monkeypatch.setitem(
        dispatch.TIER_2_DISPATCH, "rollback_config",
        lambda service, *args: rolled_back.append({"service": service, "args": args})
        or {"status": "ok", "to_value": args[1]},
    )
    monkeypatch.setattr(
        verification, "run_check",
        lambda category, service_name, before, action_at, expect_version=None:
            ("pass", {"check": category, "expected_value": expect_version,
                      "reported_value": running_value}),
    )
    monkeypatch.setattr(nodes.time, "sleep", lambda seconds: None)
    return rolled_back


def test_a_confident_config_finding_rolls_back_to_the_known_good_pool_size(monkeypatch):
    """The Phase 4 config path end to end through the compiled graph: score,
    route, resolve the target from the declared known-good value, roll back,
    verify."""
    rolled_back = _config_run(monkeypatch)

    final = _final(
        graph.build_graph().invoke(AgentState(incident_id=1, service_name=AUTH_SERVICE, token_budget=100000, max_iterations=5))
    )

    assert final["confidence"] >= final["confidence_threshold"], final["confidence"]
    assert final["planned_action"]["action"] == "rollback_config"
    assert final["planned_action"]["check"] == "config"
    assert rolled_back == [{"service": AUTH_SERVICE,
                            "args": ("DB_POOL_SIZE", "10", "2026-10-02T08:00:00+00:00")}]
    assert final["verification_result"]["outcome"] == "pass"
    assert final["status"] == "resolved"


def test_a_config_run_with_no_configurable_key_for_the_service_takes_no_action(monkeypatch):
    """A service with no declared key is as valid a refusal as history offering
    nothing safe -- this run targets checkout-service, which has none."""
    rolled_back = _config_run(monkeypatch)
    monkeypatch.setitem(
        dispatch.TIER_2_DISPATCH, "rollback_config",
        lambda *a: pytest.fail("checkout-service has no configurable key"),
    )

    final = _final(
        graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5))
    )

    assert rolled_back == []
    assert final["status"] == "action_unavailable"
    assert final["remediation_attempts"] == 0


def test_a_service_already_reporting_the_good_pool_size_is_not_rolled_back_again(monkeypatch):
    """Mirrors the deploy guard: stale evidence must not justify reverting a
    service that already reports the known-good value."""
    rolled_back = _config_run(monkeypatch, running_value="10")
    monkeypatch.setitem(
        dispatch.TIER_2_DISPATCH, "rollback_config",
        lambda *a: pytest.fail("the service already reports DB_POOL_SIZE=10"),
    )

    final = _final(
        graph.build_graph().invoke(AgentState(incident_id=1, service_name=AUTH_SERVICE, token_budget=100000, max_iterations=5))
    )

    assert rolled_back == []
    assert final["remediation_attempts"] == 0


def test_a_guarded_run_with_a_config_finding_takes_no_action(monkeypatch):
    """Tier 2 is gated for config the same as for deploy: the gate applies
    before the rollback, not after it."""
    rolled_back = _config_run(monkeypatch)
    monkeypatch.setitem(
        dispatch.TIER_2_DISPATCH, "rollback_config",
        lambda *a: pytest.fail("guarded mode must not reach a Tier 2 action"),
    )

    final = _final(
        graph.build_graph().invoke(
            AgentState(incident_id=1, service_name=AUTH_SERVICE, policy_mode="guarded",
                       token_budget=100000, max_iterations=5)
        )
    )

    assert rolled_back == []
    assert final["status"] != "resolved"
    assert final["remediation_attempts"] == 0
