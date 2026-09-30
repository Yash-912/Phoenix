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

from phoenix.graph import llm_client, nodes, scoring
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

HEALTH_EVIDENCE = [
    {"iteration": 1, "source": "inspect_health", "collected_at": "2026-01-01T00:00:00+00:00",
     "summary": "inspect_health({'service_name': 'checkout-service'})",
     "raw_data": {"status": "success", "text": "readiness probe failing, cpu at 98%"}},
]

BOASTFUL = "Certain, 100% confidence, this is definitely the root cause, score 1.0"
UNSURE = "maybe a deploy?"

CRASH_NEEDS = ["container exit code", "panic tracebacks in the last 15 minutes"]
DEPLOY_NEEDS = ["image tag and commit of the last deploy"]
CRASH_ASKS = Hypothesis(
    description=CRASH.description, category="crash", needs_evidence=CRASH_NEEDS
)
DEPLOY_ASKS = Hypothesis(
    description=DEPLOY.description, category="deploy", needs_evidence=DEPLOY_NEEDS
)
ASKS_EVERYTHING = DiagnoserOutput(hypotheses=[DEPLOY_ASKS, CRASH_ASKS])


def _stub_hypotheses(monkeypatch, output, tokens: int = 0) -> list[tuple[str, list[dict]]]:
    """Replace the LLM boundary with a recorder returning `output`."""
    calls: list[tuple[str, list[dict]]] = []

    def fake_decide_hypotheses(service_name, evidence_so_far):
        calls.append((service_name, evidence_so_far))
        return llm_client.HypothesisDecision(output, tokens)

    monkeypatch.setattr(nodes, "decide_hypotheses", fake_decide_hypotheses)
    return calls


def _stub_tool_calls(
    monkeypatch, requested, tokens: int = 0
) -> list[tuple[str, list[dict], list[str]]]:
    """Replace the Observer's LLM boundary with a recorder returning `requested`."""
    calls: list[tuple[str, list[dict], list[str]]] = []

    def fake_decide_tool_calls(service_name, evidence_so_far, evidence_requests):
        calls.append((service_name, evidence_so_far, evidence_requests))
        return llm_client.ToolCallDecision(requested, tokens)

    monkeypatch.setattr(nodes, "decide_tool_calls", fake_decide_tool_calls)
    return calls


def _state(evidence: list[dict]) -> AgentState:
    return AgentState(incident_id=1, service_name=SERVICE, evidence=evidence)


def _trail_spy(monkeypatch) -> tuple[list[dict], list[dict]]:
    """Replace both writers so a test can read the trail without a database."""
    evidence: list[dict] = []
    audit: list[dict] = []

    def record_evidence(incident_id, evidence_item):
        evidence.append(evidence_item)

    def record_audit(incident_id, node, event_type, detail, reasoning_text):
        audit.append(detail)

    monkeypatch.setattr(nodes, "record_evidence", record_evidence)
    monkeypatch.setattr(nodes, "record_audit", record_audit)
    return evidence, audit


def _loki_wanting_an_int(monkeypatch, received: list[tuple]) -> None:
    """Stand in for query_loki, refusing a minutes it cannot multiply out.

    The real tool fails on a string by computing "15" * 60 * 1_000_000_000, which
    asks the process for 120 GB before it can raise; this reproduces the refusal
    it ends up making, without the allocation that ends the test run instead.
    """
    def query_loki(logql, minutes=15):
        received.append((logql, minutes))
        if not isinstance(minutes, int):
            raise TypeError(
                f"unsupported operand type(s) for -: 'int' and '{type(minutes).__name__}'"
            )
        return {"status": "success", "text": f"logs for {logql}"}

    monkeypatch.setattr(nodes, "query_loki", query_loki)


def _tool_failing_with(monkeypatch, exc: BaseException) -> None:
    """Stand in for a tool that reached nothing and said so in the raised error.

    A refused connection is the shape that matters: its text names the network,
    so a failure that was mistaken for a reading would corroborate a network
    hypothesis instead of being discarded.
    """
    def query_loki(logql, minutes=15):
        raise exc

    monkeypatch.setattr(nodes, "query_loki", query_loki)


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


def test_the_llm_is_offered_exactly_those_five_read_only_tools():
    offered = [schema["function"]["name"] for schema in llm_client.TOOL_SCHEMAS]

    assert offered == [
        "query_prometheus",
        "query_loki",
        "get_container_state",
        "inspect_health",
        "get_recent_deployments",
    ]
    assert set(offered) == set(nodes.TOOL_DISPATCH)


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


def test_diagnoser_stores_the_surviving_hypotheses_requests_best_ranked_first(monkeypatch):
    _stub_hypotheses(monkeypatch, ASKS_EVERYTHING)

    state = nodes.diagnoser_node(_state(CRASH_EVIDENCE))

    assert [s.hypothesis.category for s in state.hypotheses] == ["crash", "deploy"]
    assert state.needs_evidence == [*CRASH_NEEDS, *DEPLOY_NEEDS]


def test_requests_never_move_a_score_or_the_confidence(monkeypatch):
    _stub_hypotheses(monkeypatch, DiagnoserOutput(hypotheses=[CRASH_ASKS, DEPLOY_ASKS]))

    plain = nodes.diagnoser_node(_state(CRASH_EVIDENCE))

    _stub_hypotheses(monkeypatch, DiagnoserOutput(hypotheses=[CRASH, DEPLOY]))
    silent = nodes.diagnoser_node(_state(CRASH_EVIDENCE))

    assert plain.needs_evidence == [*CRASH_NEEDS, *DEPLOY_NEEDS]
    assert silent.needs_evidence == []
    assert [s.score for s in plain.hypotheses] == [s.score for s in silent.hypotheses]
    assert plain.confidence == silent.confidence


def test_repeated_and_blank_evidence_requests_collapse(monkeypatch):
    shared = "container exit code"
    _stub_hypotheses(
        monkeypatch,
        DiagnoserOutput(hypotheses=[
            Hypothesis(description=CRASH.description, category="crash", needs_evidence=[shared]),
            Hypothesis(description=DEPLOY.description, category="deploy",
                       needs_evidence=[f"  {shared}  ", "  ", "deploy markers"]),
        ]),
    )

    state = nodes.diagnoser_node(_state(CRASH_EVIDENCE))

    assert state.needs_evidence == [shared, "deploy markers"]


def test_a_request_the_evidence_already_answers_is_retired_and_never_re_issued(monkeypatch):
    answered = "panic index out of range"
    still_open = "an unasked signal nobody read"
    output = DiagnoserOutput(hypotheses=[
        Hypothesis(description=CRASH.description, category="crash",
                   needs_evidence=[answered, still_open]),
    ])
    _stub_hypotheses(monkeypatch, output)

    state = nodes.diagnoser_node(_state(CRASH_EVIDENCE))
    assert state.needs_evidence == [still_open]

    state = nodes.diagnoser_node(state)

    assert state.needs_evidence == [still_open]
    assert answered not in state.needs_evidence


def test_a_tool_name_alone_never_retires_a_request(monkeypatch):
    _stub_hypotheses(
        monkeypatch,
        DiagnoserOutput(hypotheses=[
            Hypothesis(description=DEPLOY.description, category="deploy",
                       needs_evidence=["recent deployments"]),
        ]),
    )
    deployments = [
        {"iteration": 1, "source": "get_recent_deployments",
         "collected_at": "2026-01-01T00:00:00+00:00",
         "summary": "get_recent_deployments({'service_name': 'checkout-service'})",
         "raw_data": {"status": "success", "text": "no deployment markers found"}},
    ]

    state = nodes.diagnoser_node(_state(deployments))

    assert state.needs_evidence == ["recent deployments"]


def test_a_call_argument_never_retires_a_request(monkeypatch):
    _stub_hypotheses(
        monkeypatch,
        DiagnoserOutput(hypotheses=[
            Hypothesis(description=CRASH.description, category="crash",
                       needs_evidence=["service name"]),
        ]),
    )

    state = nodes.diagnoser_node(_state(HEALTH_EVIDENCE))

    assert state.needs_evidence == ["service name"]


def test_a_request_the_returned_result_answers_is_retired(monkeypatch):
    _stub_hypotheses(
        monkeypatch,
        DiagnoserOutput(hypotheses=[
            Hypothesis(description=CRASH.description, category="crash",
                       needs_evidence=["readiness probe", "image tag of the last deploy"]),
        ]),
    )

    state = nodes.diagnoser_node(_state(HEALTH_EVIDENCE))

    assert state.needs_evidence == ["image tag of the last deploy"]


def test_a_payload_field_name_never_retires_a_request(monkeypatch):
    _stub_hypotheses(
        monkeypatch,
        DiagnoserOutput(hypotheses=[
            Hypothesis(description=CRASH.description, category="crash",
                       needs_evidence=["status text"]),
        ]),
    )

    state = nodes.diagnoser_node(_state(HEALTH_EVIDENCE))

    assert state.needs_evidence == ["status text"]


def test_a_request_retires_only_against_whole_words(monkeypatch):
    _stub_hypotheses(
        monkeypatch,
        DiagnoserOutput(hypotheses=[
            Hypothesis(description=CRASH.description, category="crash",
                       needs_evidence=["exit code"]),
        ]),
    )
    container = [
        {"iteration": 1, "source": "get_container_state",
         "collected_at": "2026-01-01T00:00:00+00:00",
         "summary": "get_container_state({'container_name': 'checkout-service'})",
         "raw_data": {"status": "success", "text": "exited normally, code 0"}},
    ]

    state = nodes.diagnoser_node(_state(container))

    assert state.needs_evidence == ["exit code"]


def test_the_request_list_is_capped_in_count_and_in_length(monkeypatch):
    requests = [f"signal-{i} " + "detail " * 40 for i in range(nodes.MAX_EVIDENCE_REQUESTS + 4)]
    _stub_hypotheses(
        monkeypatch,
        DiagnoserOutput(hypotheses=[
            Hypothesis(description=CRASH.description, category="crash", needs_evidence=requests),
        ]),
    )

    state = nodes.diagnoser_node(_state([]))

    assert len(state.needs_evidence) == nodes.MAX_EVIDENCE_REQUESTS
    assert all(len(text) == nodes.MAX_REQUEST_LENGTH for text in state.needs_evidence)
    assert state.needs_evidence == [
        text[: nodes.MAX_REQUEST_LENGTH] for text in requests[: nodes.MAX_EVIDENCE_REQUESTS]
    ]


def test_empty_hypothesis_sentinel_clears_the_outstanding_requests(monkeypatch):
    _stub_hypotheses(monkeypatch, ASKS_EVERYTHING)
    state = nodes.diagnoser_node(_state(CRASH_EVIDENCE))
    assert state.needs_evidence == [*CRASH_NEEDS, *DEPLOY_NEEDS]

    _stub_hypotheses(monkeypatch, DiagnoserOutput.model_construct(hypotheses=[]))
    state = nodes.diagnoser_node(state)

    assert state.hypotheses == []
    assert state.needs_evidence == []


def test_observer_gives_the_outstanding_requests_to_its_llm(monkeypatch):
    calls = _stub_tool_calls(monkeypatch, [])
    state = _state(CRASH_EVIDENCE)
    state.needs_evidence = [*CRASH_NEEDS, *DEPLOY_NEEDS]

    nodes.observer_node(state)

    assert calls == [(SERVICE, CRASH_EVIDENCE, [*CRASH_NEEDS, *DEPLOY_NEEDS])]
    assert calls[0][2] is state.needs_evidence


def test_observer_asks_for_nothing_specific_before_a_diagnoser_has_run(monkeypatch):
    calls = _stub_tool_calls(monkeypatch, [])

    nodes.observer_node(_state(CRASH_EVIDENCE))

    assert calls == [(SERVICE, CRASH_EVIDENCE, [])]


def test_observer_executes_what_the_llm_decides_and_nothing_outside_the_allowlist(monkeypatch):
    _stub_tool_calls(monkeypatch, [
        {"name": "restart_service", "arguments": {"service_name": SERVICE}},
        {"name": "pause_deployments", "arguments": {"service_name": SERVICE}},
        {"name": "inspect_health", "arguments": {"service_name": SERVICE}},
    ])
    monkeypatch.setitem(
        nodes.TOOL_DISPATCH, "inspect_health", lambda args: {"status": "success", "text": "ok"}
    )

    state = nodes.observer_node(_state([]))

    assert [e["source"] for e in state.evidence] == ["inspect_health"]
    assert state.evidence[0]["raw_data"] == {"status": "success", "text": "ok"}


def test_a_string_where_an_int_is_expected_fails_that_call_and_ends_the_run_nothing(
    monkeypatch, capsys
):
    _trail_spy(monkeypatch)
    _loki_wanting_an_int(monkeypatch, [])
    _stub_tool_calls(monkeypatch, [
        {"name": "query_loki", "arguments": {"logql": '{container="x"}', "minutes": "15"}},
    ])

    state = nodes.observer_node(_state([]))

    printed = capsys.readouterr().out
    assert len(state.evidence) == 1
    assert state.evidence[0]["source"] == "query_loki"
    assert state.evidence[0]["raw_data"] == {
        "status": "error",
        "error": "TypeError: unsupported operand type(s) for -: 'int' and 'str'",
    }
    assert "[observer] iteration 1: query_loki(" in printed
    assert "failed: TypeError:" in printed


def test_a_call_missing_a_required_key_fails_that_call_and_ends_the_run_nothing(
    monkeypatch, capsys
):
    _trail_spy(monkeypatch)
    _stub_tool_calls(monkeypatch, [
        {"name": "query_loki", "arguments": {"minutes": 15}},
    ])

    state = nodes.observer_node(_state([]))

    printed = capsys.readouterr().out
    assert [e["raw_data"] for e in state.evidence] == [
        {"status": "error", "error": "KeyError: 'logql'"}
    ]
    assert "failed: KeyError: 'logql'" in printed


def test_an_unknown_argument_name_is_left_alone_rather_than_called_a_failure(monkeypatch):
    written, audit = _trail_spy(monkeypatch)
    received: list[tuple] = []
    _loki_wanting_an_int(monkeypatch, received)
    _stub_tool_calls(monkeypatch, [
        {"name": "query_loki", "arguments": {"logql": '{container="x"}', "minutess": 15}},
    ])

    state = nodes.observer_node(_state([]))

    assert received == [('{container="x"}', 15)]
    assert state.evidence[0]["raw_data"] == {"status": "success", "text": 'logs for {container="x"}'}
    assert written == state.evidence
    assert audit[0]["failed_tools"] == []


def test_one_failed_call_does_not_cost_the_pass_the_calls_after_it(monkeypatch):
    written, audit = _trail_spy(monkeypatch)
    _loki_wanting_an_int(monkeypatch, [])
    _stub_tool_calls(monkeypatch, [
        {"name": "query_loki", "arguments": {"logql": '{container="x"}', "minutes": "15"}},
        {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
        {"name": "query_loki", "arguments": {"logql": '{container="y"}', "minutes": 5}},
    ])
    monkeypatch.setitem(
        nodes.TOOL_DISPATCH,
        "query_prometheus",
        lambda args: {"status": "success", "text": "ServiceDown firing"},
    )

    state = nodes.observer_node(_state([]))

    assert [e["source"] for e in state.evidence] == [
        "query_loki", "query_prometheus", "query_loki"
    ]
    assert state.evidence[1]["raw_data"] == {"status": "success", "text": "ServiceDown firing"}
    assert state.evidence[2]["raw_data"] == {"status": "success", "text": 'logs for {container="y"}'}
    assert written == state.evidence
    assert audit[0]["dispatched_tools"] == [
        "query_loki", "query_prometheus", "query_loki"
    ]
    assert audit[0]["failed_tools"] == ["query_loki"]
    assert audit[0]["evidence_collected"] == 3


def test_a_failed_call_is_not_something_the_scorer_can_score(monkeypatch):
    _trail_spy(monkeypatch)
    _loki_wanting_an_int(monkeypatch, [])
    _stub_tool_calls(monkeypatch, [
        {"name": "query_loki", "arguments": {"logql": '{container="x"}', "minutes": "15"}},
    ])

    state = nodes.observer_node(_state([]))

    score, breakdown = scoring.score_hypothesis(
        state.evidence, Hypothesis(description="the network dropped", category="network")
    )
    assert breakdown["sources_supporting"] == 0
    assert score == 0.0


def test_a_failure_whose_text_names_the_category_is_still_not_evidence(monkeypatch):
    """A failed read must not score, even when its error text matches the category.

    A refused connection reads "Connection refused", which is a network keyword.
    If the failure envelope were ever treated as usable, this exact text would
    satisfy a network hypothesis, raise sources_supporting, and suppress the
    contradiction penalty -- so a read that reached nothing would look like
    better evidence than no read at all. The text is chosen to make that
    specific wrong answer reachable.
    """
    _trail_spy(monkeypatch)
    _tool_failing_with(monkeypatch, ConnectionError("Connection refused by peer"))
    _stub_tool_calls(monkeypatch, [
        {"name": "query_loki", "arguments": {"logql": '{container="x"}', "minutes": 15}},
    ])

    state = nodes.observer_node(_state([]))

    assert state.evidence[0]["raw_data"]["error"] == "ConnectionError: Connection refused by peer"

    score, breakdown = scoring.score_hypothesis(
        state.evidence, Hypothesis(description="the network dropped", category="network")
    )
    assert breakdown["sources_supporting"] == 0
    assert score == 0.0


def test_a_failure_cannot_suppress_the_contradiction_penalty(monkeypatch):
    """A failure must not rescue a guess that the real reads all contradict.

    The penalty only bites at two or more distinct sources that support nothing,
    so a test with a single failure cannot see it. Two real reads that match
    nothing, plus a failure whose text would match if it were counted, is the
    shape that matters: counting the failure lifts sources_supporting to one and
    the penalty vanishes, turning a contradicted guess into a corroborated one.
    """
    _trail_spy(monkeypatch)
    _tool_failing_with(monkeypatch, ConnectionError("Connection refused by peer"))
    _stub_tool_calls(monkeypatch, [
        {"name": "query_loki", "arguments": {"logql": '{container="x"}', "minutes": 15}},
    ])

    def _clean_read(source: str, text: str) -> dict:
        return {
            "iteration": 1,
            "source": source,
            "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": f"{source}()",
            "raw_data": {"status": "success", "text": text},
        }

    contradicted = [
        _clean_read("get_container_state", "container up, restart count 0"),
        _clean_read("get_recent_deployments", "no releases in the last 24 hours"),
    ]
    network = Hypothesis(description="the network dropped", category="network")

    without = scoring.score_hypothesis(contradicted, network)
    with_failure = nodes.observer_node(_state(contradicted))
    after = scoring.score_hypothesis(with_failure.evidence, network)

    assert without[1]["contradiction_penalty"] > 0.0
    assert after[1]["contradiction_penalty"] == without[1]["contradiction_penalty"]
    assert after[1]["sources_supporting"] == 0
    assert after[0] == without[0]


def test_state_rejects_a_wrongly_typed_needs_evidence_assignment():
    state = _state(CRASH_EVIDENCE)

    for bad in ([42], "container exit code", None):
        with pytest.raises(ValidationError):
            state.needs_evidence = bad

    assert state.needs_evidence == []


def test_state_accepts_a_correctly_typed_needs_evidence_assignment():
    state = _state(CRASH_EVIDENCE)

    state.needs_evidence = list(CRASH_NEEDS)

    assert state.needs_evidence == CRASH_NEEDS


def test_the_token_budget_is_an_int_defaulting_to_twenty_thousand():
    state = _state(CRASH_EVIDENCE)

    assert state.token_budget == 20000
    assert isinstance(state.token_budget, int)
    assert state.tokens_spent == 0
    assert isinstance(state.tokens_spent, int)


def test_the_observer_adds_its_llm_tokens_to_the_state(monkeypatch):
    _stub_tool_calls(monkeypatch, [], tokens=940)
    state = _state(CRASH_EVIDENCE)

    nodes.observer_node(state)

    assert state.tokens_spent == 940


def test_the_observer_bills_its_tokens_even_when_no_tool_was_requested(monkeypatch):
    _stub_tool_calls(monkeypatch, [], tokens=940)
    state = _state([])
    state.tokens_spent = 60

    nodes.observer_node(state)

    assert state.evidence == []
    assert state.tokens_spent == 1000


def test_the_observer_adds_to_the_spend_already_on_the_state(monkeypatch):
    _stub_tool_calls(monkeypatch, [], tokens=940)
    state = _state(CRASH_EVIDENCE)
    state.tokens_spent = 19100

    nodes.observer_node(state)

    assert state.tokens_spent == 20040
    assert state.tokens_spent > state.token_budget


def test_the_diagnoser_adds_its_llm_tokens_to_the_state(monkeypatch):
    _stub_hypotheses(monkeypatch, DiagnoserOutput(hypotheses=[CRASH]), tokens=1350)

    state = nodes.diagnoser_node(_state(CRASH_EVIDENCE))

    assert state.tokens_spent == 1350
    assert state.confidence == 0.9


def test_the_diagnoser_bills_its_tokens_even_when_it_proposed_nothing(monkeypatch):
    _stub_hypotheses(monkeypatch, DiagnoserOutput.model_construct(hypotheses=[]), tokens=760)

    state = nodes.diagnoser_node(_state(CRASH_EVIDENCE))

    assert state.hypotheses == []
    assert state.tokens_spent == 760


def test_both_nodes_accumulate_into_the_same_running_total(monkeypatch):
    _stub_hypotheses(monkeypatch, DiagnoserOutput(hypotheses=[CRASH]), tokens=1350)
    _stub_tool_calls(monkeypatch, [], tokens=940)
    state = _state(CRASH_EVIDENCE)

    nodes.observer_node(state)
    nodes.diagnoser_node(state)
    nodes.observer_node(state)

    assert state.tokens_spent == 940 + 1350 + 940


def test_a_call_with_no_usage_leaves_the_total_where_it_was(monkeypatch):
    _stub_hypotheses(monkeypatch, DiagnoserOutput(hypotheses=[CRASH]), tokens=0)
    _stub_tool_calls(monkeypatch, [], tokens=0)
    state = _state(CRASH_EVIDENCE)
    state.tokens_spent = 4242

    nodes.observer_node(state)
    nodes.diagnoser_node(state)

    assert state.tokens_spent == 4242


def test_state_rejects_a_wrongly_typed_tokens_spent_assignment():
    state = _state(CRASH_EVIDENCE)

    for bad in (None, [940], {"tokens": 940}, 940.5):
        with pytest.raises(ValidationError):
            state.tokens_spent = bad

    assert state.tokens_spent == 0


def test_the_int_field_tolerates_a_whole_float_and_refuses_a_fractional_one():
    state = _state(CRASH_EVIDENCE)

    state.tokens_spent = 940.0

    assert state.tokens_spent == 940
    assert isinstance(state.tokens_spent, int)

    with pytest.raises(ValidationError):
        state.tokens_spent = 940.5


def test_state_rejects_a_wrongly_typed_token_budget_assignment():
    state = _state(CRASH_EVIDENCE)

    for bad in (None, [20000], {"tokens": 20000}, 20000.5):
        with pytest.raises(ValidationError):
            state.token_budget = bad

    assert state.token_budget == 20000
