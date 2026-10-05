"""The investigation loop knows when it has stopped learning.

Nothing here is specific to an incident or a scenario: hypotheses are generic
categories, evidence is the shape the real tools return, and what is asserted is
what the scorer can see -- a run that keeps observing without changing it is
stopped for that reason, and one that is making progress is not."""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from types import SimpleNamespace

from langgraph.graph import END

from phoenix.graph import graph, investigation, llm_client, nodes, scoring, verification
from phoenix.graph import remediation_dispatch as dispatch
from phoenix.graph.llm_client import HypothesisDecision, ToolCallDecision
from phoenix.graph.schemas import DiagnoserOutput, Hypothesis, ScoredHypothesis
from phoenix.graph.state import AgentState

SERVICE = "svc"
LEADER = Hypothesis(description="the service is crashing", category="crash")
OTHER = Hypothesis(description="a bad deploy", category="deploy")

PROM_DOWN = "ServiceDown firing, up==0"
LOKI_PANIC = "panic: index out of range, process exit 1"
LOKI_QUIET = "all healthy, nothing to report"


def _ev(source: str, text: str, summary: str | None = None, ok: bool = True) -> dict:
    raw = {"status": "success", "text": text} if ok else {"status": "error", "error": "boom"}
    return {"iteration": 1, "source": source, "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": summary or f"{source}({text})", "raw_data": raw}


def _scored(evidence: list[dict], *hypotheses: Hypothesis) -> list[ScoredHypothesis]:
    return [
        ScoredHypothesis(hypothesis=h, score=s, score_breakdown=b)
        for h, s, b in scoring.score_all(evidence, list(hypotheses))
    ]


def _state(evidence=None, hypotheses=None, **fields) -> AgentState:
    state = AgentState(incident_id=1, service_name=SERVICE, evidence=evidence or [], **fields)
    if hypotheses is not None:
        state.hypotheses = hypotheses
    return state


# ---- the signature and what counts as progress ----------------------------


def test_the_signature_is_the_leaders_score_support_and_contradiction():
    scored = _scored([_ev("query_prometheus", PROM_DOWN), _ev("query_loki", LOKI_QUIET)], LEADER)

    assert investigation.progress_signature(scored) == {
        "leader_category": "crash",
        "leader_score": 0.4,
        "supporting_sources": ["query_prometheus"],
        "contradicted": False,
    }


def test_a_contradicted_leader_is_marked_contradicted():
    scored = _scored([_ev("query_prometheus", LOKI_QUIET), _ev("query_loki", LOKI_QUIET)], LEADER)

    signature = investigation.progress_signature(scored)

    assert signature["contradicted"] is True
    assert signature["supporting_sources"] == []


def test_no_hypotheses_is_a_signature_not_an_error():
    assert investigation.progress_signature([]) == {
        "leader_category": None, "leader_score": 0.0, "supporting_sources": [], "contradicted": False,
    }


def test_only_the_leader_counts_not_what_else_the_model_chose_to_mention():
    evidence = [_ev("query_prometheus", PROM_DOWN)]

    with_one = investigation.progress_signature(_scored(evidence, LEADER))
    with_two = investigation.progress_signature(_scored(evidence, LEADER, OTHER))

    assert with_one == with_two


def test_the_first_diagnosis_is_always_progress():
    progressed, reasons = investigation.assess_progress(None, investigation.progress_signature([]))

    assert progressed is True
    assert reasons == ["first diagnosis"]


def test_an_identical_signature_is_not_progress():
    signature = investigation.progress_signature(_scored([_ev("query_prometheus", PROM_DOWN)], LEADER))

    assert investigation.assess_progress(signature, dict(signature)) == (False, [])


def test_each_change_the_scorer_can_see_is_progress_and_says_what_changed():
    base = {"leader_category": "crash", "leader_score": 0.4, "supporting_sources": ["query_prometheus"], "contradicted": False}

    cases = {
        "leading hypothesis changed": {**base, "leader_category": "deploy"},
        "leader score changed": {**base, "leader_score": 0.9},
        "new supporting source: query_loki": {**base, "supporting_sources": ["query_loki", "query_prometheus"]},
        "supporting source lost: query_prometheus": {**base, "supporting_sources": []},
        "now contradicted": {**base, "contradicted": True},
    }
    for expected, changed in cases.items():
        progressed, reasons = investigation.assess_progress(base, changed)
        assert progressed is True, expected
        assert any(expected in reason for reason in reasons), (expected, reasons)


# ---- the diagnoser counts stagnant passes ---------------------------------


def _diagnose(monkeypatch, state: AgentState, *hypotheses: Hypothesis):
    output = (
        DiagnoserOutput(hypotheses=list(hypotheses))
        if hypotheses
        else DiagnoserOutput.model_construct(hypotheses=[])
    )
    monkeypatch.setattr(nodes, "decide_hypotheses", lambda service, evidence: HypothesisDecision(output, 0))
    monkeypatch.setattr(nodes, "record_hypotheses", lambda *a, **k: None)
    audit: list[dict] = []
    monkeypatch.setattr(nodes, "record_audit", lambda incident, node, event, detail, text: audit.append(detail))
    return nodes.diagnoser_node(state), audit[-1]


def test_case_1_the_first_diagnosis_is_progress_and_not_stagnant(monkeypatch):
    state, audit = _diagnose(monkeypatch, _state([_ev("query_prometheus", PROM_DOWN)]), LEADER)

    assert state.stagnant_passes == 0
    assert state.progress_signature["leader_category"] == "crash"
    assert audit["progress"]["progressed"] is True
    assert audit["progress"]["reasons"] == ["first diagnosis"]


def test_case_3_an_identical_diagnosis_increments_stagnation(monkeypatch):
    state = _state([_ev("query_prometheus", PROM_DOWN)])
    state, _ = _diagnose(monkeypatch, state, LEADER)
    state, audit = _diagnose(monkeypatch, state, LEADER)

    assert state.stagnant_passes == 1
    assert audit["progress"] == {
        "progressed": False, "reasons": [], "stagnant_passes": 1, "signature": state.progress_signature,
    }

    state, _ = _diagnose(monkeypatch, state, LEADER)
    assert state.stagnant_passes == 2


def test_a_different_query_that_returns_nothing_new_is_still_stagnant(monkeypatch):
    state = _state([_ev("query_prometheus", PROM_DOWN)])
    state, _ = _diagnose(monkeypatch, state, LEADER)
    state.evidence.append(_ev("query_loki", LOKI_QUIET, summary="query_loki({'logql': 'a different search'})"))

    state, _ = _diagnose(monkeypatch, state, LEADER)

    assert state.stagnant_passes == 1


def test_case_2_a_new_independent_source_changes_the_state_and_resets_stagnation(monkeypatch):
    state = _state([_ev("query_prometheus", PROM_DOWN)])
    state, _ = _diagnose(monkeypatch, state, LEADER)
    state, _ = _diagnose(monkeypatch, state, LEADER)
    assert state.stagnant_passes == 1

    state.evidence.append(_ev("query_loki", LOKI_PANIC))
    state, audit = _diagnose(monkeypatch, state, LEADER)

    assert state.stagnant_passes == 0
    assert audit["progress"]["progressed"] is True
    assert any("new supporting source: query_loki" in r for r in audit["progress"]["reasons"])
    assert state.confidence == 0.9


def test_a_contradiction_is_progress_and_is_recorded(monkeypatch):
    state = _state([_ev("query_prometheus", PROM_DOWN)])
    state, _ = _diagnose(monkeypatch, state, LEADER)
    state.evidence = [_ev("query_prometheus", LOKI_QUIET), _ev("query_loki", LOKI_QUIET)]

    state, audit = _diagnose(monkeypatch, state, LEADER)

    assert state.stagnant_passes == 0
    assert state.progress_signature["contradicted"] is True
    assert any("contradicted" in r for r in audit["progress"]["reasons"])


def test_a_diagnosis_with_no_hypotheses_that_repeats_is_stagnant_too(monkeypatch):
    state, _ = _diagnose(monkeypatch, _state())
    state, _ = _diagnose(monkeypatch, state)

    assert state.stagnant_passes == 1


# ---- the router -------------------------------------------------------------


def _route(monkeypatch, **fields):
    rows: list[tuple] = []
    monkeypatch.setattr(graph, "record_audit", lambda incident, node, event, detail, text: rows.append((event, detail, text)))
    hypotheses = _scored([_ev("query_prometheus", PROM_DOWN)], LEADER)
    state = _state(hypotheses=hypotheses, confidence=0.4, **fields)
    state.progress_signature = investigation.progress_signature(hypotheses)
    return graph.should_continue(state), rows


def test_case_4_two_stagnant_passes_escalate_with_an_explicit_reason(monkeypatch):
    command, rows = _route(monkeypatch, stagnant_passes=2, iteration=2, tokens_spent=100)

    assert command.goto == END
    reason = command.update["escalation_reason"]
    assert reason.startswith("investigation_stagnant: no change in diagnoser evidence state for 2 consecutive passes")
    assert "leader crash at 0.40" in reason and "query_prometheus" in reason
    assert command.update["status"] == "insufficient_evidence"
    assert rows[0][0] == "insufficient_evidence"
    assert rows[0][1]["escalation_reason"] == reason
    assert rows[0][1]["evidence_report"] == command.update["evidence_report"]
    assert rows[0][2].startswith("Insufficient evidence after")


def test_case_5_one_stagnant_pass_with_nothing_further_to_observe_escalates(monkeypatch):
    command, rows = _route(monkeypatch, stagnant_passes=1, observation_exhausted=True, iteration=2)

    assert command.goto == END
    assert command.update["escalation_reason"].startswith(
        "observation_exhausted: no new observable evidence could be collected after a stagnant pass"
    )
    assert rows[0][0] == "insufficient_evidence"


def test_one_stagnant_pass_alone_still_gets_another_directed_attempt(monkeypatch):
    command, _ = _route(monkeypatch, stagnant_passes=1, observation_exhausted=False, iteration=2)

    assert command.goto == "observer"
    assert not command.update


def test_the_first_diagnosis_never_escalates_even_if_the_observer_found_nothing(monkeypatch):
    command, _ = _route(monkeypatch, stagnant_passes=0, observation_exhausted=True, iteration=1)

    assert command.goto == "observer"


def test_case_6_a_reset_count_means_earlier_stagnation_is_not_held_against_the_run(monkeypatch):
    command, _ = _route(monkeypatch, stagnant_passes=0, observation_exhausted=True, iteration=3)

    assert command.goto == "observer"


def test_a_run_that_reached_the_threshold_is_never_stopped_for_stagnation(monkeypatch):
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)
    state = _state(confidence=0.9, stagnant_passes=5, observation_exhausted=True, iteration=4)

    command = graph.should_continue(state)

    assert command.goto == "remediator"
    assert command.update == {"status": "confident"}


def test_the_token_budget_still_speaks_first_even_when_the_run_is_also_stagnant(monkeypatch):
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)

    command = graph.should_continue(_state(stagnant_passes=3, tokens_spent=20000, token_budget=20000, iteration=2))

    assert command.update["escalation_reason"] == "token budget exhausted (20000/20000 tokens)"


def test_the_iteration_cap_still_speaks_before_stagnation(monkeypatch):
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)

    command = graph.should_continue(_state(stagnant_passes=3, iteration=5, max_iterations=5, tokens_spent=100))

    assert command.update["escalation_reason"] == "iteration cap reached (5/5)"


def test_a_run_below_every_limit_and_still_progressing_loops_back(monkeypatch):
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)

    command = graph.should_continue(_state(confidence=0.4, iteration=1, tokens_spent=100))

    assert command.goto == "observer"


# ---- the evidence-state report ---------------------------------------------


def test_the_report_says_what_supports_the_leader_what_does_not_and_what_is_untried():
    evidence = [
        _ev("query_prometheus", PROM_DOWN, summary="query_prometheus({'promql': 'up'})"),
        _ev("query_loki", LOKI_QUIET, summary="query_loki({'logql': 'a'})"),
        _ev("query_loki", LOKI_QUIET, summary="query_loki({'logql': 'b'})"),
    ]
    state = _state(evidence, _scored(evidence, LEADER), stagnant_passes=1, tokens_spent=7000, token_budget=20000)

    report = investigation.build_evidence_state(state)

    assert report["leader"] == "crash"
    assert report["leader_description"] == LEADER.description
    assert report["leader_score"] == 0.4
    assert report["confidence_threshold"] == 0.75
    assert report["leader_contradicted"] is False
    assert report["supporting_sources"] == ["query_prometheus"]
    assert report["queried_sources_with_no_supporting_evidence"] == {"query_loki": 2}
    assert report["sources_not_yet_queried"] == ["get_container_state", "inspect_health", "get_recent_deployments"]
    assert report["queries_already_run"] == [e["summary"] for e in evidence]
    assert report["stagnant_passes"] == 1
    assert report["remaining_token_budget"] == 13000


def test_a_failed_read_does_not_count_as_a_source_that_was_queried():
    evidence = [_ev("query_prometheus", PROM_DOWN), _ev("get_container_state", "", ok=False)]
    state = _state(evidence, _scored(evidence, LEADER))

    report = investigation.build_evidence_state(state)

    assert "get_container_state" in report["sources_not_yet_queried"]
    assert "get_container_state" not in report["queried_sources_with_no_supporting_evidence"]


def test_the_remaining_budget_never_goes_negative():
    evidence = [_ev("query_prometheus", PROM_DOWN)]
    state = _state(evidence, _scored(evidence, LEADER), tokens_spent=25000, token_budget=20000)

    assert investigation.build_evidence_state(state)["remaining_token_budget"] == 0


def test_there_is_no_report_before_there_is_a_leader():
    assert investigation.build_evidence_state(_state([_ev("query_prometheus", PROM_DOWN)])) is None


def test_the_report_has_the_same_generic_fields_whichever_category_leads():
    keys = None
    for category in ("crash", "overload", "slow_query", "memory_leak", "config"):
        hypothesis = Hypothesis(description="x", category=category)
        evidence = [_ev("query_prometheus", PROM_DOWN)]
        report = investigation.build_evidence_state(_state(evidence, _scored(evidence, hypothesis)))
        assert report["leader"] == category
        keys = keys or set(report)
        assert set(report) == keys


# ---- the observer: reports, repeats and exhaustion --------------------------


def _observer(monkeypatch, requested, state: AgentState, tool=lambda args: {"status": "success", "text": "fresh"}):
    seen = []

    def fake_decide(service, evidence, requests, evidence_state=None):
        seen.append(evidence_state)
        return ToolCallDecision(requested, 0)

    audit: list[dict] = []
    ran: list[dict] = []
    monkeypatch.setattr(nodes, "decide_tool_calls", fake_decide)
    monkeypatch.setattr(nodes, "record_evidence", lambda *a: None)
    monkeypatch.setattr(nodes, "record_audit", lambda incident, node, event, detail, text: audit.append(detail))
    monkeypatch.setattr(nodes, "get_incident_first_seen", lambda incident: None)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: (ran.append(args), tool(args))[1])
    return nodes.observer_node(state), seen, audit, ran


LOKI_A = {"name": "query_loki", "arguments": {"logql": '{container="x"} |~ "a"'}}
LOKI_B = {"name": "query_loki", "arguments": {"logql": '{container="x"} |~ "b"'}}


def test_the_first_observer_pass_is_called_exactly_as_it_always_was(monkeypatch):
    called_with = []

    def strict(service, evidence, requests):
        called_with.append((service, evidence, requests))
        return ToolCallDecision([], 0)

    monkeypatch.setattr(nodes, "decide_tool_calls", strict)
    monkeypatch.setattr(nodes, "record_audit", lambda *a: None)
    monkeypatch.setattr(nodes, "get_incident_first_seen", lambda incident: None)

    nodes.observer_node(_state())

    assert len(called_with) == 1


def test_a_later_pass_hands_the_observer_the_evidence_state(monkeypatch):
    evidence = [_ev("query_prometheus", PROM_DOWN)]
    state = _state(evidence, _scored(evidence, LEADER), stagnant_passes=1)

    _, seen, _, _ = _observer(monkeypatch, [], state)

    assert seen[0]["leader"] == "crash"
    assert seen[0]["stagnant_passes"] == 1


def test_an_exact_repeat_is_not_run_again_and_is_audited(monkeypatch):
    state = _state([_ev("query_loki", LOKI_QUIET, summary=f"query_loki({LOKI_A['arguments']})")])

    state, _, audit, ran = _observer(monkeypatch, [LOKI_A, LOKI_B], state)

    assert ran == [LOKI_B["arguments"]]
    assert audit[0]["skipped_duplicates"] == [f"query_loki({LOKI_A['arguments']})"]
    assert audit[0]["dispatched_tools"] == ["query_loki"]
    assert len(state.evidence) == 2


def test_a_call_repeated_within_one_pass_runs_once(monkeypatch):
    state, _, audit, ran = _observer(monkeypatch, [LOKI_A, LOKI_A], _state())

    assert ran == [LOKI_A["arguments"]]
    assert audit[0]["skipped_duplicates"] == [f"query_loki({LOKI_A['arguments']})"]


def test_observation_is_exhausted_when_nothing_new_ran(monkeypatch):
    repeat = _state([_ev("query_loki", LOKI_QUIET, summary=f"query_loki({LOKI_A['arguments']})")])

    only_repeats, _, audit, _ = _observer(monkeypatch, [LOKI_A], repeat)
    assert only_repeats.observation_exhausted is True
    assert audit[0]["observation_exhausted"] is True

    asked_nothing, _, _, _ = _observer(monkeypatch, [], _state())
    assert asked_nothing.observation_exhausted is True


def test_observation_is_not_exhausted_when_a_new_call_ran_and_the_flag_resets(monkeypatch):
    state = _state(observation_exhausted=True)

    state, _, audit, _ = _observer(monkeypatch, [LOKI_A], state)

    assert state.observation_exhausted is False
    assert audit[0]["observation_exhausted"] is False


def test_a_call_that_raised_still_counts_as_something_that_ran(monkeypatch):
    def boom(args):
        raise RuntimeError("down")

    state, _, _, _ = _observer(monkeypatch, [LOKI_A], _state(), tool=boom)

    assert state.observation_exhausted is False


# ---- the prompt -------------------------------------------------------------


def _stub_client(monkeypatch) -> list[dict]:
    recorded: list[dict] = []

    def create(**kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[]))],
            usage=SimpleNamespace(total_tokens=1),
        )

    monkeypatch.setattr(
        llm_client, "client", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    )
    return recorded


def test_the_report_and_the_instructions_reach_the_observers_prompt(monkeypatch):
    recorded = _stub_client(monkeypatch)
    report = {"leader": "crash", "supporting_sources": ["query_prometheus"], "stagnant_passes": 1}

    llm_client.decide_tool_calls(SERVICE, [], [], report)

    user = recorded[0]["messages"][1]["content"]
    assert '"leader": "crash"' in user
    assert "computed in code" in user
    assert "does not yet support it" in user
    assert "has already been run" in user
    assert "query it again for a different target" in user
    assert "near-identical searches against the same target" in user
    assert "call no tools" in user


def test_without_a_report_the_prompt_is_what_it_was(monkeypatch):
    recorded = _stub_client(monkeypatch)

    llm_client.decide_tool_calls(SERVICE, [], [])

    user = recorded[0]["messages"][1]["content"]
    assert "Where the investigation stands" not in user
    assert user == "Evidence collected so far: []\n\nWhich tool(s) do you want to call next?"


# ---- through the compiled graph ---------------------------------------------


def _forbidden_remediation(monkeypatch) -> list[str]:
    reached: list[str] = []

    def forbidden(name):
        reached.append(name)
        raise AssertionError(f"{name} dispatched by a run that must not act")

    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", forbidden)
    return reached


def _wire(monkeypatch, observer, loki_text=LOKI_QUIET):
    monkeypatch.setattr(nodes, "decide_tool_calls", observer)
    monkeypatch.setattr(
        nodes, "decide_hypotheses",
        lambda service, evidence: HypothesisDecision(DiagnoserOutput(hypotheses=[LEADER]), 10),
    )
    monkeypatch.setattr(nodes, "record_evidence", lambda *a: None)
    monkeypatch.setattr(nodes, "record_audit", lambda *a: None)
    monkeypatch.setattr(nodes, "record_hypotheses", lambda *a, **k: None)
    monkeypatch.setattr(nodes, "get_incident_first_seen", lambda incident: None)
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: {"status": "success", "text": PROM_DOWN})
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: {"status": "success", "text": loki_text})


def _run(**fields) -> dict:
    result = graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5, **fields))
    return result if isinstance(result, dict) else dict(result)


def test_case_5_a_run_with_nothing_further_to_observe_stops_deliberately(monkeypatch):
    reached = _forbidden_remediation(monkeypatch)
    reports = []

    def observer(service, evidence, requests, evidence_state=None):
        reports.append(evidence_state)
        return ToolCallDecision([{"name": "query_prometheus", "arguments": {"promql": "up"}}], 1)

    _wire(monkeypatch, observer)
    final = _run()

    assert final["status"] == "insufficient_evidence"
    assert "query_loki" in final["evidence_report"]["sources_never_read"]
    assert final["evidence_report"]["sources_supporting_any_hypothesis"] == ["query_prometheus"]
    assert final["escalation_reason"].startswith("observation_exhausted:")
    assert final["iteration"] == 2
    assert final["tokens_spent"] < 100000
    assert reached == []
    assert reports[0] is None and reports[1]["leader"] == "crash"


def test_case_4_a_run_that_keeps_reading_without_learning_stops_after_two_stagnant_passes(monkeypatch):
    reached = _forbidden_remediation(monkeypatch)
    pass_number = {"n": 0}

    def observer(service, evidence, requests, evidence_state=None):
        pass_number["n"] += 1
        calls = [{"name": "query_prometheus", "arguments": {"promql": "up"}}] if pass_number["n"] == 1 else []
        calls.append({"name": "query_loki", "arguments": {"logql": f"search number {pass_number['n']}"}})
        return ToolCallDecision(calls, 1)

    _wire(monkeypatch, observer)
    final = _run()

    assert final["status"] == "insufficient_evidence"
    assert final["escalation_reason"].startswith(
        "investigation_stagnant: no change in diagnoser evidence state for 2 consecutive passes"
    )
    assert final["iteration"] == 3
    assert final["stagnant_passes"] == 2
    assert final["confidence"] == 0.4
    assert reached == []


def test_case_6_a_run_that_finds_an_independent_source_reaches_the_threshold_and_acts(monkeypatch):
    acted: list[str] = []

    def observer(service, evidence, requests, evidence_state=None):
        if evidence_state is None:
            return ToolCallDecision([{"name": "query_prometheus", "arguments": {"promql": "up"}}], 1)
        return ToolCallDecision([{"name": "query_loki", "arguments": {"logql": "panic"}}], 1)

    _wire(monkeypatch, observer, loki_text=LOKI_PANIC)
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: (acted.append(name), {"status": "ok"})[1])
    monkeypatch.setattr(verification, "read_signal", lambda category, service: {"bytes": 1, "slope": 0.0})
    monkeypatch.setattr(verification, "run_check", lambda *a: ("pass", {"reason": "stubbed"}))
    monkeypatch.setattr(nodes.time, "sleep", lambda seconds: None)

    final = _run()

    assert final["status"] == "resolved"
    assert final["confidence"] == 0.9
    assert final["iteration"] == 2
    assert final["stagnant_passes"] == 0
    assert acted == [SERVICE]


def test_the_two_source_rule_is_unchanged_one_strong_source_stays_below_the_threshold(monkeypatch):
    """However many passes agree with it, a single supporting source scores 0.4."""
    evidence = [_ev("query_prometheus", PROM_DOWN), _ev("query_loki", LOKI_QUIET)]
    state = _state(evidence, stagnant_passes=7)

    state, _ = _diagnose(monkeypatch, state, LEADER)

    assert state.confidence == 0.4
    assert state.confidence < state.confidence_threshold


def test_a_two_source_diagnosis_of_any_category_still_routes_straight_to_the_remediator(monkeypatch):
    monkeypatch.setattr(graph, "record_audit", lambda *a: None)
    leader = Hypothesis(description="a query regression", category="slow_query")
    measured = _ev("query_prometheus", "http_request_duration_seconds p99 slow")
    measured["raw_data"]["latency_measure"] = {"verdict": "sustained_slow", "sustained_slow": True}
    evidence = [
        measured,
        _ev("query_loki", "duration: 1023.4 ms  statement: SELECT * FROM t"),
    ]
    state = _state(evidence)
    state, _ = _diagnose(monkeypatch, state, leader)

    command = graph.should_continue(state)

    assert state.confidence == 0.9
    assert state.stagnant_passes == 0
    assert command.goto == "remediator"
