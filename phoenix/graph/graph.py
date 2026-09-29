import sys

from langgraph.graph import END, StateGraph

from phoenix.graph.nodes import diagnoser_node, observer_node
from phoenix.graph.state import AgentState


def should_continue(state: AgentState) -> str:
    """Route on deterministic state alone; the LLM is never consulted here.

    Three ways out, in this order. A run that reached the confidence threshold has
    a finding, so it is not an escalation however much it spent. Of the two
    reasons an inconclusive run stops, the budget comes first: it is the harder
    ceiling, and naming it is more useful than naming the iteration cap it
    happens to be sitting under. Looping back is the last resort, so no run that
    has spent its budget can return "observer".
    """
    if state.confidence >= state.confidence_threshold:
        print(f"[router] confidence threshold met ({state.confidence:.2f} >= {state.confidence_threshold}) -> end")
        return "end"
    if state.tokens_spent >= state.token_budget:
        state.status = "escalated"
        state.escalation_reason = (
            f"token budget exhausted ({state.tokens_spent}/{state.token_budget} tokens)"
        )
        print(f"[router] token budget exhausted ({state.tokens_spent}/{state.token_budget}) -> end (escalate)")
        return "end"
    if state.iteration >= state.max_iterations:
        print(f"[router] iteration cap hit ({state.iteration}/{state.max_iterations}) -> end (escalate)")
        return "end"
    print(f"[router] confidence too low ({state.confidence:.2f}) -> loop back to observer")
    return "observer"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("observer", observer_node)
    graph.add_node("diagnoser", diagnoser_node)

    graph.set_entry_point("observer")
    graph.add_edge("observer", "diagnoser")
    graph.add_conditional_edges("diagnoser", should_continue, {"observer": "observer", "end": END})

    return graph.compile()


if __name__ == "__main__":
    incident_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    service_name = sys.argv[2] if len(sys.argv) > 2 else "checkout-service"

    app = build_graph()
    result = app.invoke(AgentState(incident_id=incident_id, service_name=service_name))
    print("\nFinal state:")
    print(result)
