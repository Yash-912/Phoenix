"""Whether an investigation pass changed anything the scorer can see.

The router bounds a run by the confidence threshold, the token budget and the
iteration cap. None of them says anything about whether the last pass learned
something, so a run whose leading hypothesis is stuck below the threshold keeps
observing until the budget is gone. This module is the missing deterministic
notion of progress, kept to pure functions of the state so it can be tested and
audited without a model.

A pass made progress when the scorer's picture of the leading hypothesis moved:
a different leader, a different score, a different set of sources supporting it,
or it became contradicted. Anything else -- the same sources answering the same
way, a different query that returned nothing -- changed nothing the decision
rests on, whatever it added to the evidence list. The first diagnosis always
counts, because it is the baseline there is nothing to compare with.

Nothing here knows about a category or an incident. Supporting sources come from
the scorer's own per-source signals, so a new category or a new source is
covered without an entry here.
"""

from __future__ import annotations

from phoenix.graph import scoring

# Two consecutive passes with an unchanged picture: the first can be an unlucky
# query, and by the second the observer has been shown what was missing and what
# had already been tried.
MAX_STAGNANT_PASSES = 2

MAX_LISTED_QUERIES = 20
MAX_QUERY_LENGTH = 160


def _supporting_sources(breakdown: dict) -> list[str]:
    return sorted(
        source for source, key in scoring.SOURCE_SIGNAL_KEYS.items() if breakdown.get(key) == 1
    )


def progress_signature(hypotheses: list) -> dict:
    """The part of a diagnosis that a decision rests on, for the leading hypothesis.

    `hypotheses` is the state's list, best first. Only the leader is read: the
    others are proposed afresh by the model each pass, so a change in what it
    chose to mention below the leader says nothing about what was learned.
    """
    if not hypotheses:
        return {"leader_category": None, "leader_score": 0.0, "supporting_sources": [], "contradicted": False}
    leader = hypotheses[0]
    breakdown = leader.score_breakdown
    return {
        "leader_category": leader.hypothesis.category,
        "leader_score": leader.score,
        "supporting_sources": _supporting_sources(breakdown),
        "contradicted": breakdown.get("contradiction_penalty", 0) > 0,
    }


def assess_progress(previous: dict | None, current: dict) -> tuple[bool, list[str]]:
    """Whether `current` differs from `previous`, and in what way."""
    if previous is None:
        return True, ["first diagnosis"]

    reasons = []
    if current["leader_category"] != previous["leader_category"]:
        reasons.append(f"leading hypothesis changed from {previous['leader_category']} to {current['leader_category']}")
    if current["leader_score"] != previous["leader_score"]:
        reasons.append(f"leader score changed from {previous['leader_score']} to {current['leader_score']}")
    if current["supporting_sources"] != previous["supporting_sources"]:
        gained = sorted(set(current["supporting_sources"]) - set(previous["supporting_sources"]))
        lost = sorted(set(previous["supporting_sources"]) - set(current["supporting_sources"]))
        if gained:
            reasons.append(f"new supporting source: {', '.join(gained)}")
        if lost:
            reasons.append(f"supporting source lost: {', '.join(lost)}")
    if current["contradicted"] != previous["contradicted"]:
        reasons.append("leader is now contradicted by the evidence" if current["contradicted"] else "leader is no longer contradicted")
    return bool(reasons), reasons


def build_evidence_state(state) -> dict | None:
    """What the leading hypothesis rests on and what has not been tried, or None
    when there is no leader yet (the first pass has nothing to report).

    Computed from the scorer's own breakdown and the evidence list, never by a
    model, and handed to the observer so its next read is aimed at a source that
    does not yet support the leader instead of at another keyword search of one
    that has already returned nothing.
    """
    if not state.hypotheses:
        return None
    leader = state.hypotheses[0]
    signature = progress_signature(state.hypotheses)
    supporting = signature["supporting_sources"]

    reads: dict[str, int] = {}
    for item in state.evidence:
        if scoring._is_usable(item):
            reads[item.get("source", "")] = reads.get(item.get("source", ""), 0) + 1

    return {
        "leader": leader.hypothesis.category,
        "leader_description": leader.hypothesis.description,
        "leader_score": leader.score,
        "confidence_threshold": state.confidence_threshold,
        "leader_contradicted": signature["contradicted"],
        "supporting_sources": supporting,
        "queried_sources_with_no_supporting_evidence": {
            source: count for source, count in reads.items() if source in scoring.SOURCE_WEIGHTS and source not in supporting
        },
        "sources_not_yet_queried": [source for source in scoring.SOURCE_WEIGHTS if source not in reads],
        "queries_already_run": [
            item["summary"][:MAX_QUERY_LENGTH] for item in state.evidence[-MAX_LISTED_QUERIES:]
        ],
        "stagnant_passes": state.stagnant_passes,
        "remaining_token_budget": max(0, state.token_budget - state.tokens_spent),
    }


def _leader_phrase(state) -> str:
    signature = state.progress_signature or {}
    sources = ", ".join(signature.get("supporting_sources") or []) or "no source"
    return (
        f"leader {signature.get('leader_category')} at {signature.get('leader_score', 0.0):.2f}, "
        f"supported by {sources}"
    )


def stagnation_reason(state) -> str | None:
    """Why the router should stop a run that is below the threshold, or None.

    The caller has already ruled out the threshold, the budget and the cap, so
    this only ever ends a run that has not found its finding. It cannot fire on
    the first diagnosis: stagnation needs a pass that was compared with an
    earlier one.
    """
    if state.stagnant_passes >= MAX_STAGNANT_PASSES:
        return (
            f"investigation_stagnant: no change in diagnoser evidence state for "
            f"{state.stagnant_passes} consecutive passes ({_leader_phrase(state)})"
        )
    if state.stagnant_passes >= 1 and state.observation_exhausted:
        return (
            f"observation_exhausted: no new observable evidence could be collected "
            f"after a stagnant pass ({_leader_phrase(state)})"
        )
    return None
