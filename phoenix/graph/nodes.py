from datetime import datetime, timezone

from phoenix.graph.llm_client import decide_tool_calls
from phoenix.graph.state import AgentState
from phoenix.tools.docker_tool import get_container_state
from phoenix.tools.loki_tool import query_loki
from phoenix.tools.prometheus_tool import query_prometheus

# The ONLY tools the LLM's decisions can ever result in executing.
# decide_tool_calls() offers the LLM exactly these three (TOOL_SCHEMAS in
# llm_client.py) — this dict is the enforcement point: even if the LLM
# somehow returned a name outside this set, .get() below would just find
# nothing to call, not execute anything arbitrary.
TOOL_DISPATCH = {
    "query_prometheus": lambda args: query_prometheus(args["promql"]),
    "query_loki": lambda args: query_loki(args["logql"], args.get("minutes", 15)),
    "get_container_state": lambda args: get_container_state(args["container_name"]),
}


def observer_node(state: AgentState) -> AgentState:
    """Slice 2.3, completed: the LLM decides which tool(s) to call next;
    this code executes exactly what it decides and nothing else.
    """
    state.iteration += 1

    requested_calls = decide_tool_calls(state.service_name, state.evidence)

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
    """Stub for Slice 2.4 — will generate LLM hypotheses scored against real evidence.

    Confidence is simulated here (grows with evidence count) purely so the
    conditional edge below has something real to route on. This is NOT the
    evidence-weighted scoring FR-4 requires — that's Slice 2.4's job.
    """
    state.confidence = min(1.0, len(state.evidence) * 0.2)
    print(f"[diagnoser] iteration {state.iteration}: confidence={state.confidence:.2f}")
    return state
