import sys

from langgraph.graph import END, StateGraph
from langgraph.types import Command

from phoenix.graph.nodes import diagnoser_node, observer_node
from phoenix.graph.state import AgentState


def should_continue(state: AgentState) -> Command:
    """Route on deterministic state alone; the LLM is never consulted here.

    Returns a Command rather than a bare destination name because a decision that
    both routes and records has to travel home as a Command. A bare string can
    only name a destination, and langgraph hands the function a copy of the
    state, so an escalation written on that copy is an escalation nobody can
    read. The update is the part that reaches the run's final state.

    Registered as a node rather than as add_conditional_edges because langgraph
    1.1.10 cannot honour a Command from a conditional edge: attach_branch writes
    every packet as a branch:<destination> channel, so with a path_map a Command
    raises TypeError: unhashable type: 'dict' while the branch indexes its ends,
    and without one the Command is written to a branch:to:Command(...) channel
    that does not exist and the update is dropped with only a warning. Nodes are
    the supported route-and-update path, and Command carries its own
    destination, so the router needs no path_map.

    The return annotation stays a bare Command on purpose: a Command[Literal]
    annotation makes langgraph read the literals as the node's destinations and
    then reject "end" at compile time, since END is the sentinel "__end__".

    Three ways out, in this order. A run that reached the confidence threshold
    has a finding, so it is not an escalation however much it spent. Of the two
    reasons an inconclusive run stops, the budget comes first: it is the harder
    ceiling, and naming it is more useful than naming the iteration cap it
    happens to be sitting under. Looping back is the last resort, so no run
    that has spent its budget can return to the observer.
    """
    if state.confidence >= state.confidence_threshold:
        print(f"[router] confidence threshold met ({state.confidence:.2f} >= {state.confidence_threshold}) -> end")
        return Command(goto=END)
    if state.tokens_spent >= state.token_budget:
        reason = f"token budget exhausted ({state.tokens_spent}/{state.token_budget} tokens)"
        print(f"[router] {reason} -> end (escalate)")
        return Command(
            goto=END,
            update={"status": "escalated", "escalation_reason": reason},
        )
    if state.iteration >= state.max_iterations:
        print(f"[router] iteration cap hit ({state.iteration}/{state.max_iterations}) -> end (escalate)")
        return Command(
            goto=END,
            update={
                "status": "escalated",
                "escalation_reason": (
                    f"iteration cap reached ({state.iteration}/{state.max_iterations})"
                ),
            },
        )
    print(f"[router] confidence too low ({state.confidence:.2f}) -> loop back to observer")
    return Command(goto="observer")


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("observer", observer_node)
    graph.add_node("diagnoser", diagnoser_node)
    graph.add_node("router", should_continue)

    graph.set_entry_point("observer")
    graph.add_edge("observer", "diagnoser")
    graph.add_edge("diagnoser", "router")

    return graph.compile()


if __name__ == "__main__":
    incident_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    service_name = sys.argv[2] if len(sys.argv) > 2 else "checkout-service"

    app = build_graph()
    result = app.invoke(AgentState(incident_id=incident_id, service_name=service_name))
    print("\nFinal state:")
    print(result)
