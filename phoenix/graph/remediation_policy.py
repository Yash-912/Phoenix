"""Which action, if any, a confident diagnosis is allowed to take.

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

**Why deploy goes straight to Tier 2.** Phase 3 left it empty, and the reason
recorded here was that restarting a service whose root cause was a bad deploy is
the "unhealthy means restart" reflex PRD section 2 says Phoenix must be
distinguishable from real understanding. Phase 4 keeps that reasoning and acts
on it: the fix is to stop guessing a Tier 1 action, not to try a useless one.
PRD section 8 lists Scenario 1 as Tier 2 directly, with no Tier 1 step, so
restarting here would be inventing a round trip the spec does not ask for. The
proof that Tier 1 was insufficient comes from the evidence, not from performing
it -- v18 is baked into the image, so a restart cannot clear it.

**Why an unmapped category is still a good outcome.** `config` and `network`
remain empty, and an honest "the right action is Tier 2 rollback_config or Tier 3
code fix, which this phase does not implement" is a better artifact than a wrong
action. The table stays a table so adding them later is a data change.

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

from phoenix.graph.rollback_target import resolve_rollback_target
from phoenix.graph.state import AgentState
from phoenix.tools.docker_tool import get_container_state

CATEGORY_ACTIONS: dict[str, tuple[str, ...]] = {
    "crash": ("restart_service",),
    "overload": ("restart_service",),
    "deploy": ("rollback_deployment",),
    "config": (),
    "network": (),
    "unknown": (),
}

# Actions that replace an artifact rather than perturbing a running one. They
# are Tier 2 by construction: not idempotent, not reversible by a second
# invocation, and destructive if aimed at the wrong target. Keeping them
# separate from ALLOWED_DOCKER_ACTIONS is what stops "allowed" from quietly
# meaning "safe to repeat".
TIER_2_ACTIONS = {"rollback_deployment"}

NO_ACTION_REASON = (
    "a {category} root cause has no action in this phase; the correct action is "
    "Tier 2 (rollback_config) or Tier 3 (code fix), which are later phases"
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

    `args` carries whatever the chosen action needs beyond the container name.
    For Tier 1 that is empty; for rollback it is the target version, which the
    policy resolves from deployment history. Keeping it here rather than at the
    call site means the dispatch table stays a flat name -> callable mapping and
    the target version is decided in one place, by code that never reads model
    output.
    """

    available: bool
    action: str | None
    container: str | None
    check: str | None
    reasoning: str
    args: tuple[str, ...] = ()


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
    action = actions[0] if actions else None

    if action is None:
        return ActionPlan(
            available=False,
            action=None,
            container=None,
            check=None,
            reasoning=NO_ACTION_REASON.format(category=category),
        )

    if action in TIER_2_ACTIONS:
        target = _resolve_tier_2_target(action, state.service_name)
        if target is None:
            return ActionPlan(
                available=False,
                action=None,
                container=None,
                check=None,
                reasoning=(
                    f"top hypothesis is {category}, which maps to {action}, but deployment "
                    f"history for {state.service_name} offers no known-good artifact to "
                    f"restore; refusing rather than guessing a version"
                ),
            )
        return ActionPlan(
            available=True,
            action=action,
            container=state.service_name,
            check=category,
            reasoning=f"top hypothesis is {category}, which maps to {action}; {target.reasoning}",
            args=(target.version, target.from_marker or ""),
        )

    return ActionPlan(
        available=True,
        action=action,
        container=state.service_name,
        check=category,
        reasoning=f"top hypothesis is {category}, which maps to {action}",
    )


def _resolve_tier_2_target(action: str, service_name: str):
    """Read live container identity, then ask history for a version to restore.

    The running version is fetched rather than assumed so the already-rolled-back
    case is detected: once v17 is serving, the stale v18 evidence would otherwise
    justify a second rollback of a healthy service. Returns None when there is no
    safe target, which plan_action turns into an honest refusal.
    """
    running_version = None
    container = get_container_state(service_name)
    if isinstance(container, dict) and container.get("status") != "error":
        labels = (container.get("Config") or {}).get("Labels") or {}
        running_version = labels.get("app.version")

    return resolve_rollback_target(service_name, running_version=running_version)
