"""Which Tier 1 action, if any, a confident diagnosis is allowed to take.

This module is the boundary the whole mutating path leans on. The LLM proposes a
root cause and names its category; the scorer turns evidence into a confidence;
and from here on nothing the model said can reach a container. The category picks
a row, the row picks an action, and an empty row is a first-class answer rather
than a failure to find one.

**Why the table is two rows wide.** Overload is genuinely ambiguous: memory
exhaustion wants restart_service, queue saturation wants pause_worker, and nothing
in today's evidence separates the two. Adding pause as a fallback would be a
guess, and a guess here is exactly the LLM-influenced decision this module
exists to prevent. So pause_worker, resume_worker, and clear_approved_cache stay
implemented and allowlisted in remediation_tool.py but unrouted. They enter
CATEGORY_ACTIONS as a data change when a scenario demands them, which is the
point of keeping the mapping a table rather than a branch.

**Why an unmapped category is a good outcome.** A deploy-category root cause has
a confident diagnosis and no valid Tier 1 action -- the right action is a
rollback, which is Tier 2 and does not exist until Phase 4. Restarting a service
because its root cause was a bad deploy is the "unhealthy means restart" reflex
PRD section 2 says Phoenix must be distinguishable from real understanding. The
honest answer is to end with the diagnosis and say the correct tier is out of
scope, which is a better artifact than a wrong restart.

**Where the model's influence still reaches.** The LLM picks the category, and the
category selects this row, so category -> action is inside the model's reach.
That is the boundary the spec records, not a violation of it: the model chooses
among families it already has evidence for, and it cannot reach confidence,
routing, the attempt cap, the policy gate, or the verification. Those are the
decisions with consequences nobody can undo from a log.

Entries are ordered tuples whose first element is the least invasive action that
could suffice. Later elements are fallbacks on retry, and there are none yet.
"""

from dataclasses import dataclass

from phoenix.graph.state import AgentState

CATEGORY_ACTIONS: dict[str, tuple[str, ...]] = {
    "crash": ("restart_service",),
    "overload": ("restart_service",),
    "deploy": (),
    "config": (),
    "network": (),
    "unknown": (),
}

NO_TIER_1_REASON = (
    "a {category} root cause has no Tier 1 action; the correct action is Tier 2 "
    "(rollback), which is Phase 4"
)


@dataclass(frozen=True)
class ActionPlan:
    """What the policy decided, and the reasoning a reader of the trail needs.

    Frozen because a plan that a later step could edit is not a decision: the
    remediator executes what this says or refuses, and nothing in between is
    allowed to substitute a different action.

    check is the category, not an action name, because verification is keyed on
    what was wrong rather than on what was done. Two actions that were meant to
    fix the same failure family are verified the same way, and one action used
    for two families would need the family to verify it correctly.
    """

    available: bool
    action: str | None
    container: str | None
    check: str | None
    reasoning: str


def plan_action(state: AgentState) -> ActionPlan:
    """The one Tier 1 action this diagnosis is allowed to take, or none at all.

    Reads the top-scoring hypothesis only. A lower-ranked hypothesis is not a
    fallback to try when the top one has no action: that would let the agent act
    on a cause it ranked below another one it could not act on, which is the
    guess this module refuses to make.
    """
    if not state.hypotheses:
        return ActionPlan(
            available=False,
            action=None,
            container=None,
            check=None,
            reasoning="no hypothesis survived scoring, so there is nothing to act on",
        )

    category = state.hypotheses[0].hypothesis.category
    actions = CATEGORY_ACTIONS.get(category, ())

    if not actions:
        return ActionPlan(
            available=False,
            action=None,
            container=None,
            check=None,
            reasoning=NO_TIER_1_REASON.format(category=category),
        )

    return ActionPlan(
        available=True,
        action=actions[0],
        container=state.service_name,
        check=category,
        reasoning=f"top hypothesis is {category}, which maps to {actions[0]}",
    )
