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

**Why config now routes too.** `config` maps to rollback_config on the same
reasoning deploy maps to rollback_deployment: Scenario 4's root cause is a
value, not a process state, and no Tier 1 action changes a value a restart
would just reread unchanged. `network` remains empty -- there is no Tier 2
action for it yet, and an honest "the right action is a later phase" is a
better artifact than a wrong one. The table stays a table so adding it later is
a data change.

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

from phoenix.graph.config_rollback_target import resolve_config_rollback_target
from phoenix.graph.rollback_target import resolve_rollback_target
from phoenix.graph.state import AgentState
from phoenix.tools.docker_tool import get_container_state
from phoenix.tools.health_tool import inspect_health

CATEGORY_ACTIONS: dict[str, tuple[str, ...]] = {
    "crash": ("restart_service",),
    "overload": ("restart_service",),
    "deploy": ("rollback_deployment",),
    "config": ("rollback_config",),
    "network": (),
    # memory_leak keeps a Tier 1 action: a restart genuinely mitigates a
    # growing working set, right now, for real -- the PRD's own Scenario 3
    # shape is Tier 1 then Tier 3, not Tier 3 instead of Tier 1. The
    # permanent fix is a separate decision made in nodes.py/graph.py after
    # this action verifies, not by this table.
    "memory_leak": ("restart_service",),
    # slow_query has no Tier 1/2 action: restarting payment-service does not
    # make an unindexed query fast again, and there is no config value or
    # deployment to roll back to. Mapping it to () is what makes it route
    # straight to Tier 3 rather than this module inventing an action to take.
    "slow_query": (),
    "unknown": (),
}

# Categories whose root cause is application code, not infrastructure state.
# Belonging to this set never authorizes a mutation by itself -- it only
# tells remediator_node/verifier_node to hand the run to the Tier 3 subgraph
# instead of ending it, the same way TIER_2_ACTIONS only tells dispatch which
# signature to use. The LLM picks the category; this table, not the LLM,
# decides what that category is allowed to trigger next.
TIER3_CATEGORIES: frozenset[str] = frozenset({"slow_query", "memory_leak"})


def is_tier3_eligible(category: str | None) -> bool:
    return category in TIER3_CATEGORIES

# Actions that replace an artifact or a config value rather than perturbing a
# running one. They are Tier 2 by construction: not idempotent, not reversible
# by a second invocation, and destructive if aimed at the wrong target. Keeping
# them separate from ALLOWED_DOCKER_ACTIONS is what stops "allowed" from
# quietly meaning "safe to repeat".
TIER_2_ACTIONS = {"rollback_deployment", "rollback_config"}

# Which config key a rollback_config plan targets, by service. One key per
# service today; the config dict markers already support more if a future
# scenario needs it.
CONFIG_KEYS_BY_SERVICE: dict[str, str] = {
    "auth-service": "DB_POOL_SIZE",
}

# Where a config key's live value is reported, inside inspect_health's app
# payload. A separate map from CONFIG_KEYS_BY_SERVICE on purpose: the key name
# used across the policy/dispatch/tool boundary (DB_POOL_SIZE, matching the
# compose env var) is not the field name the service's own /health JSON uses
# (db_pool_size) -- conflating them would make a rename of either look like a
# rename of both.
CONFIG_HEALTH_FIELDS: dict[str, str] = {
    "DB_POOL_SIZE": "db_pool_size",
}

NO_ACTION_REASON = (
    "a {category} root cause has no action in this phase; the correct action is "
    "a later phase"
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
                reasoning=_tier_2_refusal_reason(action, category, state.service_name),
            )
        return ActionPlan(
            available=True,
            action=action,
            container=state.service_name,
            check=category,
            reasoning=f"top hypothesis is {category}, which maps to {action}; {target.reasoning}",
            args=_tier_2_args(action, target),
        )

    return ActionPlan(
        available=True,
        action=action,
        container=state.service_name,
        check=category,
        reasoning=f"top hypothesis is {category}, which maps to {action}",
    )


def _resolve_tier_2_target(action: str, service_name: str):
    """The target this Tier 2 action would restore, or None if there is nothing
    safe to do.

    One seam for both actions rather than one each, because plan_action's
    Tier-2 branch is itself one piece of code: it resolves whichever target the
    action needs, then hands the result to _tier_2_args without caring which
    kind of target it got. Tests patch this function directly to give policy
    tests a resolvable target without reaching the live container or real
    deployment history.
    """
    if action == "rollback_deployment":
        return resolve_rollback_target(
            service_name, running_version=_read_running_version(service_name)
        )
    if action == "rollback_config":
        key = CONFIG_KEYS_BY_SERVICE.get(service_name)
        if key is None:
            return None
        running_value = _read_running_config_value(service_name, key)
        return resolve_config_rollback_target(service_name, key, running_value=running_value)
    return None


def _read_running_version(service_name: str) -> str | None:
    """The live app.version label, read from the container -- or None.

    Fetched rather than assumed so the already-rolled-back case is detected:
    once v17 is serving, stale v18 evidence would otherwise justify a second
    rollback of a healthy service.
    """
    container = get_container_state(service_name)
    if isinstance(container, dict) and container.get("status") != "error":
        labels = (container.get("Config") or {}).get("Labels") or {}
        return labels.get("app.version")
    return None


def _read_running_config_value(service_name: str, key: str) -> str | None:
    """The live value of `key`, read from the service's own /health response.

    Mirrors _read_running_version's reasoning for config: both exist so the
    already-healed case is detectable before a second rollback is planned
    against evidence that has not caught up yet.
    """
    field = CONFIG_HEALTH_FIELDS.get(key)
    if field is None:
        return None
    health = inspect_health(service_name)
    app = health.get("app") if isinstance(health, dict) else None
    if not isinstance(app, dict) or app.get("status") != "ok":
        return None
    value = app.get(field)
    return str(value) if value is not None else None


def _tier_2_args(action: str, target) -> tuple[str, ...]:
    """The dispatch args this target implies, keyed to rollback_config/
    rollback_deployment's own signatures in remediation_tool.py."""
    if action == "rollback_deployment":
        return (target.version, target.from_marker or "")
    if action == "rollback_config":
        return (target.key, target.value, target.from_marker or "")
    return ()


def _tier_2_refusal_reason(action: str, category: str, service_name: str) -> str:
    """Why plan_action refused this Tier 2 action, named precisely enough to
    read in the audit trail without the code behind it."""
    if action == "rollback_deployment":
        return (
            f"top hypothesis is {category}, which maps to {action}, but deployment "
            f"history for {service_name} offers no known-good artifact to "
            f"restore; refusing rather than guessing a version"
        )
    if action == "rollback_config":
        return (
            f"top hypothesis is {category}, which maps to {action}, but there is no "
            f"known-good value to restore for {service_name}, or it already reports "
            f"one; refusing rather than guessing a value"
        )
    return f"top hypothesis is {category}, which maps to {action}, but no target could be resolved"
