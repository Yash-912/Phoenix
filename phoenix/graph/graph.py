from langgraph.graph import END, StateGraph

from nodes import diagnoser_node, observer_node
from state import AgentState


def should_continue(state: AgentState) -> str:
    if state.confidence >= state.confidence_threshold:
        print(f"[router] confidence threshold met ({state.confidence:.2f} >= {state.confidence_threshold}) -> end")
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
    app = build_graph()
    result = app.invoke(AgentState(incident_id=1))
    print("\nFinal state:")
    print(result)
