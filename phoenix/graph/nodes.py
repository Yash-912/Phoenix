from datetime import datetime, timezone

from phoenix.graph import scoring
from phoenix.graph.llm_client import decide_hypotheses, decide_tool_calls
from phoenix.graph.schemas import ScoredHypothesis
from phoenix.graph.state import AgentState
from phoenix.tools.deploy_tool import get_recent_deployments
from phoenix.tools.docker_tool import get_container_state
from phoenix.tools.health_tool import inspect_health
from phoenix.tools.loki_tool import query_loki
from phoenix.tools.prometheus_tool import query_prometheus

# The ONLY tools the LLM's decisions can ever result in executing.
# Observer stays read-only: remediation actions (restart/pause/cache)
# live in remediation_tool.py and are executor-only, never dispatched here.
TOOL_DISPATCH = {
    "query_prometheus": lambda args: query_prometheus(args["promql"]),
    "query_loki": lambda args: query_loki(args["logql"], args.get("minutes", 15)),
    "get_container_state": lambda args: get_container_state(args["container_name"]),
    "inspect_health": lambda args: inspect_health(args["service_name"]),
    "get_recent_deployments": lambda args: get_recent_deployments(
        args["service_name"], args.get("limit", 10)
    ),
}


def _pending_evidence_requests(hypotheses: list[ScoredHypothesis]) -> list[str]:
    """The confirm/refute signals the surviving hypotheses still want, best-ranked first.

    Several hypotheses routinely ask for the same next read; a repeated request is
    prompt noise, so keep the first occurrence and drop blanks.
    """
    requests: list[str] = []
    for scored in hypotheses:
        for requested in scored.hypothesis.needs_evidence:
            text = requested.strip()
            if text and text not in requests:
                requests.append(text)
    return requests


def observer_node(state: AgentState) -> AgentState:
    """Slice 2.3, completed: the LLM decides which tool(s) to call next;
    this code executes exactly what it decides and nothing else. The diagnoser's
    outstanding evidence requests ride along into that decision, so the loop
    investigates what the hypotheses actually want confirmed.
    """
    state.iteration += 1

    requested_calls = decide_tool_calls(
        state.service_name, state.evidence, state.needs_evidence
    )

    if not requested_calls:
        print(f"[observer] iteration {state.iteration}: LLM requested no tool calls")
        return state

    for call in requested_calls:
        tool_name = call["name"]
        tool_fn = TOOL_DISPATCH.get(tool_name)
        if tool_fn is None:
            print(f"[observer] iteration {state.iteration}: ignoring unrecognized tool '{tool_name}'")
            continue

        result = tool_fn(call["arguments"])
        state.evidence.append(
            {
                "iteration": state.iteration,
                "source": tool_name,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "summary": f"{tool_name}({call['arguments']})",
                "raw_data": result,
            }
        )
        print(f"[observer] iteration {state.iteration}: called {tool_name}({call['arguments']})")

    return state


def diagnoser_node(state: AgentState) -> AgentState:
    """The LLM describes candidate root causes; scoring.py alone decides how much
    to believe them. Confidence is the top ranked hypothesis' deterministic score,
    so it moves with the evidence the observer collected, never with a count of
    evidence items and never with anything the model asserted about itself. The
    ranked hypotheses' needs_evidence entries become the state's outstanding
    requests, which the observer's next prompt is steered by.
    """
    proposed = decide_hypotheses(state.service_name, state.evidence).hypotheses

    if not proposed:
        print(f"[diagnoser] iteration {state.iteration}: LLM proposed no hypotheses")
        state.hypotheses = []
        state.needs_evidence = []
        state.confidence = 0.0
        return state

    state.hypotheses = [
        ScoredHypothesis(hypothesis=hypothesis, score=score, score_breakdown=breakdown)
        for hypothesis, score, breakdown in scoring.score_all(state.evidence, proposed)
    ]
    state.needs_evidence = _pending_evidence_requests(state.hypotheses)
    state.confidence = scoring.top_confidence(state.evidence, proposed)

    for scored in state.hypotheses:
        print(
            f"[diagnoser] iteration {state.iteration}: {scored.hypothesis.category} "
            f"scores {scored.score:.2f}: {scored.hypothesis.description}"
        )
    print(f"[diagnoser] iteration {state.iteration}: confidence={state.confidence:.2f}")
    return state
