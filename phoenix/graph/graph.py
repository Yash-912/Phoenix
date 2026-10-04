import sys

from langgraph.graph import END, StateGraph
from langgraph.types import Command

from phoenix.graph import investigation
from phoenix.graph.nodes import (
    diagnoser_node,
    observer_node,
    remediator_node,
    verifier_node,
)
from phoenix.graph.persist import close_persistence, record_audit
from phoenix.graph.state import AgentState
from phoenix.graph.tier3_nodes import (
    code_investigator_node,
    patch_generator_node,
    patch_validator_node,
    pr_opener_node,
)

ROUTER_DESTINATIONS: tuple[str, ...] = ("observer", "remediator", END)


def _record_route(
    state: AgentState,
    event_type: str,
    destination: str,
    reasoning_text: str,
    escalation_reason: str | None = None,
) -> None:
    """Append this router pass's decision to the trail, then let it route.

    One row per pass, not one per run: a trail that only holds the last decision
    cannot show what an investigation did to reach it, and the passes in between
    are the whole of the investigation.

    The row is written here, inside the router, because the router is the last
    thing a run passes through and every exit path it can take goes straight to
    END -- there is no node downstream that could see the end of a run, and
    adding one would mean changing the destinations this function returns.
    Recording a row is not a routing decision: the Command comes back exactly as
    it would have, and the state the row describes is the copy the router decided
    on, which is the state the decision was actually made against.

    The escalation is a separate field from the destination because the two are
    different facts. Reaching the confidence threshold is a successful end and
    records no escalation, however much the run spent; a row that called it
    escalated would file a finding as a failure. escalation_reason is the same
    string the Command carries into the final state, so the row and the state
    cannot disagree about why a run stopped.
    """
    record_audit(
        state.incident_id,
        "router",
        event_type,
        {
            "iteration": state.iteration,
            "max_iterations": state.max_iterations,
            "confidence": state.confidence,
            "confidence_threshold": state.confidence_threshold,
            "tokens_spent": state.tokens_spent,
            "token_budget": state.token_budget,
            "destination": destination,
            "escalation_reason": escalation_reason,
        },
        reasoning_text,
    )


def should_continue(state: AgentState) -> Command:
    """Route on deterministic state alone; the LLM is never consulted here.

    Returns a Command rather than a bare destination name because a decision that
    both routes and records has to travel home as a Command. A bare string can
    only name a destination, and langgraph hands the function a copy of the
    state, so an escalation written on that copy is an escalation nobody can
    read. The update is the part that reaches the run's final state.

    Registered as a node rather than as add_conditional_edges, measured on
    langgraph 1.1.10 -- the version this module is pinned to, so this is a
    statement about the dependency rather than a note on a local skew -- where
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
    has a finding, so it is not an escalation however much it spent -- but a
    finding is not the end of the run either. It is the one state that means
    something can be done about it, so it routes to the remediator rather than
    to END, and the remediator decides whether the policy allows an action and
    then hands off to the verifier. Of the two reasons an inconclusive run
    stops, the budget comes first: it is the harder ceiling, and naming it is
    more useful than naming the iteration cap it happens to be sitting under.
    Looping back is the last resort, so no run that has spent its budget can
    return to the observer. A fourth stop sits after the other three: a run
    still below the threshold whose last passes changed nothing the scorer sees
    (investigation.stagnation_reason) ends with that stated as its reason, so a
    run is not left to spend its budget on observations that cannot move it.

    Each of the four decisions is written to audit_log on its way out. That is
    the same audit table the observer and the diagnoser append to, so a run's
    end reads as one trail in order rather than a state field nobody queried.
    """
    if state.confidence >= state.confidence_threshold:
        print(f"[router] confidence threshold met ({state.confidence:.2f} >= {state.confidence_threshold}) -> remediator")
        _record_route(
            state,
            "threshold_reached",
            "remediator",
            f"confidence threshold met ({state.confidence:.2f} >= {state.confidence_threshold})",
        )
        return Command(goto="remediator", update={"status": "confident"})
    if state.tokens_spent >= state.token_budget:
        reason = f"token budget exhausted ({state.tokens_spent}/{state.token_budget} tokens)"
        print(f"[router] {reason} -> end (escalate)")
        _record_route(state, "escalated", END, reason, reason)
        return Command(
            goto=END,
            update={"status": "escalated", "escalation_reason": reason},
        )
    if state.iteration >= state.max_iterations:
        print(f"[router] iteration cap hit ({state.iteration}/{state.max_iterations}) -> end (escalate)")
        reason = f"iteration cap reached ({state.iteration}/{state.max_iterations})"
        _record_route(state, "escalated", END, reason, reason)
        return Command(
            goto=END,
            update={
                "status": "escalated",
                "escalation_reason": (
                    f"iteration cap reached ({state.iteration}/{state.max_iterations})"
                ),
            },
        )
    # Last of the stops, after the threshold, the budget and the cap have all
    # been ruled out: a run still below the threshold whose passes have stopped
    # changing what the scorer sees. Without it such a run keeps observing until
    # the budget is gone, and says only that the budget ran out.
    stagnation = investigation.stagnation_reason(state)
    if stagnation is not None:
        print(f"[router] {stagnation} -> end (escalate)")
        _record_route(state, "escalated", END, stagnation, stagnation)
        return Command(
            goto=END,
            update={"status": "escalated", "escalation_reason": stagnation},
        )
    print(f"[router] confidence too low ({state.confidence:.2f}) -> loop back to observer")
    _record_route(
        state,
        "continuing",
        "observer",
        f"confidence too low ({state.confidence:.2f})",
    )
    return Command(goto="observer")


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("observer", observer_node)
    graph.add_node("diagnoser", diagnoser_node)
    graph.add_node("router", should_continue, destinations=ROUTER_DESTINATIONS)
    graph.add_node("remediator", remediator_node)
    graph.add_node("verifier", verifier_node)
    # Tier 3 subgraph. Reached only via a Command from remediator_node
    # (slow_query, no Tier 1/2 action) or verifier_node (memory_leak, after a
    # passing Tier 1 check) -- never a static edge, so ordinary Tier 1/2
    # incidents never pass through any of these four nodes.
    graph.add_node("code_investigator", code_investigator_node)
    graph.add_node("patch_generator", patch_generator_node)
    graph.add_node("patch_validator", patch_validator_node)
    graph.add_node("pr_opener", pr_opener_node)

    graph.set_entry_point("observer")
    graph.add_edge("observer", "diagnoser")
    graph.add_edge("diagnoser", "router")
    # No static edge out of remediator, verifier, or any Tier 3 node. Every
    # one of them returns a Command that names its own destination, and a
    # static edge alongside a Command would be a second, competing claim
    # about where the run goes next.

    return graph.compile()


if __name__ == "__main__":
    incident_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    service_name = sys.argv[2] if len(sys.argv) > 2 else "checkout-service"

    app = build_graph()
    try:
        result = app.invoke(AgentState(incident_id=incident_id, service_name=service_name))
    finally:
        close_persistence()
    print("\nFinal state:")
    print(result)
