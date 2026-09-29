import sys

from langgraph.graph import END, StateGraph
from langgraph.types import Command

from phoenix.graph.nodes import diagnoser_node, observer_node
from phoenix.graph.state import AgentState

ROUTER_DESTINATIONS: tuple[str, ...] = ("observer", END)


def should_continue(state: AgentState) -> Command:
    """Route on deterministic state alone; the LLM is never consulted here.

    Returns a Command rather than a bare destination name because a decision that
    both routes and records has to travel home as a Command. A bare string can
    only name a destination, and langgraph hands the function a copy of the
    state, so an escalation written on that copy is an escalation nobody can
    read. The update is the part that reaches the run's final state.

    Registered as a node rather than as add_conditional_edges, measured on
    langgraph 1.1.10 -- the version installed here, against a 0.2.39 pin -- where
    attach_branch writes every packet as a branch:<destination> channel and
    understands only str or Send. With a path_map a Command raises TypeError:
    unhashable type: 'dict' while the branch indexes its ends; without one the
    Command is written to a branch:to:Command(...) channel that does not exist
    and the update is dropped with only a warning. That is version-scoped
    evidence, not a general claim about langgraph, but the node shape is
    langgraph's own route-and-update path and is the shape that does not change
    meaning with the installed version. A Command also carries its own
    destination, so the router needs no path_map.

    ROUTER_DESTINATIONS is declared on the node so langgraph still validates the
    destination names at compile() -- the check the path map used to do for
    free, now that the destinations are strings this function returns rather
    than keys of a mapping. It is a promise about what the router may name, not a
    second source of truth: nothing enforces that the Command agrees with it,
    which is why the loop-back is proved by running the compiled graph.

    The return annotation stays a bare Command on purpose. A Command[Literal]
    annotation makes langgraph read the literals as the node's destinations, so
    spelling the terminal end would mean writing END's sentinel value
    "__end__" into the type instead of the name the code uses.

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
    graph.add_node("router", should_continue, destinations=ROUTER_DESTINATIONS)

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
