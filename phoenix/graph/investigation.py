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


def insufficient_evidence_report(state) -> dict | None:
    """A report of why a run ended with no finding, or None when that is not the case.

    A run is insufficient-evidence when it stopped because it stopped learning
    (stagnation_reason) and no hypothesis was supported by two independent
    sources. That is a different fact from a run that hit the budget or the cap
    while still changing its leader: that run ran out of room, this one ran out
    of evidence. The caller has already ruled out the threshold, the budget and
    the cap, as for stagnation_reason.

    Computed from the scorer's own breakdowns and the evidence list, never by a
    model, so the trail can say what was tried and why nothing was conclusive
    without a second opinion about it.
    """
    reason = stagnation_reason(state)
    if reason is None:
        return None
    if any(h.score_breakdown.get("sources_supporting", 0) >= 2 for h in state.hypotheses):
        return None

    usable: dict[str, int] = {}
    seen: set[str] = set()
    for item in state.evidence:
        source = item.get("source", "")
        if source not in scoring.SOURCE_WEIGHTS:
            continue
        seen.add(source)
        if scoring._is_usable(item):
            usable[source] = usable.get(source, 0) + 1

    considered = [
        {
            "category": h.hypothesis.category,
            "description": h.hypothesis.description,
            "score": h.score,
            "supporting_sources": _supporting_sources(h.score_breakdown),
            "contradicted": h.score_breakdown.get("contradiction_penalty", 0) > 0,
        }
        for h in state.hypotheses
    ]
    supporting = sorted({source for entry in considered for source in entry["supporting_sources"]})
    answered_without_support = sorted(source for source in usable if source not in supporting)
    failed = sorted(source for source in seen if source not in usable)
    never_read = sorted(source for source in scoring.SOURCE_WEIGHTS if source not in seen)

    summary = (
        f"Insufficient evidence after {state.iteration} pass(es): {len(considered)} hypothesis(es) "
        f"considered, none supported by two independent sources. "
        f"Answered without support: {', '.join(answered_without_support) or 'none'}. "
        f"Read failed: {', '.join(failed) or 'none'}. "
        f"Never read: {', '.join(never_read) or 'none'}."
    )
    return {
        "reason": reason,
        "summary": summary,
        "hypotheses_considered": considered,
        "sources_supporting_any_hypothesis": supporting,
        "sources_answered_without_support": answered_without_support,
        "sources_failed": failed,
        "sources_never_read": never_read,
        "queries_run": len(state.evidence),
        "queries": [item["summary"][:MAX_QUERY_LENGTH] for item in state.evidence[-MAX_LISTED_QUERIES:]],
        "iterations": state.iteration,
        "tokens_spent": state.tokens_spent,
        "confidence": state.confidence,
        "confidence_threshold": state.confidence_threshold,
    }


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
