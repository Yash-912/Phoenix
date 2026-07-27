from state import AgentState


def observer_node(state: AgentState) -> AgentState:
    """Stub for Slice 2.3 — will query Prometheus/Loki/Docker for real evidence."""
    state.iteration += 1
    state.evidence.append(
        {
            "iteration": state.iteration,
            "source": "stub",
            "summary": f"placeholder evidence #{state.iteration}",
        }
    )
    print(f"[observer] iteration {state.iteration}: collected evidence (total={len(state.evidence)})")
    return state


def diagnoser_node(state: AgentState) -> AgentState:
    """Stub for Slice 2.4 — will generate LLM hypotheses scored against real evidence.

    Confidence is simulated here (grows with evidence count) purely so the
    conditional edge below has something real to route on. This is NOT the
    evidence-weighted scoring FR-4 requires — that's Slice 2.4's job.
    """
    state.confidence = min(1.0, len(state.evidence) * 0.3)
    print(f"[diagnoser] iteration {state.iteration}: confidence={state.confidence:.2f}")
    return state
