from pathlib import Path
from typing import get_args

import pytest

from phoenix.graph import remediation_policy
from phoenix.graph.config_rollback_target import ConfigRollbackTarget
from phoenix.graph.remediation_policy import CATEGORY_ACTIONS, plan_action
from phoenix.graph.rollback_target import RollbackTarget
from phoenix.graph.schemas import Hypothesis, ScoredHypothesis
from phoenix.graph.state import AgentState

SERVICE = "checkout-service"

# Phase 4 routes deploy to rollback_deployment and config to rollback_config;
# network and unknown still have no action, which is the honest answer while
# their tiers are unbuilt.
NO_ACTION_CATEGORIES = ("network", "unknown")

_DEPLOY_TARGET = RollbackTarget(
    version="v17",
    from_marker="2026-10-02T08:00:00+00:00",
    from_version="v18",
    reasoning="newest non-regression artifact is v17",
)

_CONFIG_TARGET = ConfigRollbackTarget(
    key="DB_POOL_SIZE",
    value="10",
    from_marker="2026-10-02T08:00:00+00:00",
    from_value="1",
    reasoning="known-good DB_POOL_SIZE is 10",
)

_TARGETS_BY_ACTION = {
    "rollback_deployment": _DEPLOY_TARGET,
    "rollback_config": _CONFIG_TARGET,
}


@pytest.fixture(autouse=True)
def _known_good_target(monkeypatch):
    """Give each Tier 2 action a resolvable target so policy tests exercise
    routing, not Docker or the live service.

    The alternative -- letting these tests reach the live container, the real
    deployment history, and the live /health response -- would make the policy
    suite depend on whatever the lab last deployed or was last configured to,
    so a test would pass or fail depending on lab state.
    """
    monkeypatch.setattr(
        remediation_policy,
        "_resolve_tier_2_target",
        lambda action, service: _TARGETS_BY_ACTION.get(action),
    )


def _state(category: str | None, **overrides) -> AgentState:
    hypotheses = (
        [
            ScoredHypothesis(
                hypothesis=Hypothesis(description="something is wrong", category=category),
                score=0.9,
                score_breakdown={},
            )
        ]
        if category
        else []
    )
    return AgentState(
        incident_id=1, service_name=SERVICE, hypotheses=hypotheses, **overrides
    )


def test_a_crash_category_plans_a_restart_of_the_service_container():
    plan = plan_action(_state("crash"))

    assert plan.available is True
    assert plan.action == "restart_service"
    assert plan.container == SERVICE
    assert plan.check == "crash"
    assert "crash" in plan.reasoning


def test_an_overload_category_plans_a_restart():
    assert plan_action(_state("overload")).action == "restart_service"


def test_a_deploy_category_plans_a_rollback_of_the_last_good_artifact():
    """Phase 4 change: deploy maps to Tier 2 rather than to no action at all.

    It deliberately does not plan a restart first. v18's regression is compiled
    into the image, so restarting it cannot help, and PRD section 8 lists
    Scenario 1 as Tier 2 directly. The version is resolved from history, never
    from the hypothesis.
    """
    plan = plan_action(_state("deploy"))

    assert plan.available is True
    assert plan.action == "rollback_deployment"
    assert plan.container == SERVICE
    assert plan.check == "deploy"
    assert plan.args == ("v17", "2026-10-02T08:00:00+00:00")
    assert "v17" in plan.reasoning


def test_a_deploy_category_with_nothing_safe_to_restore_refuses_to_act(monkeypatch):
    monkeypatch.setattr(remediation_policy, "_resolve_tier_2_target", lambda action, service: None)

    plan = plan_action(_state("deploy"))

    assert plan.available is False
    assert plan.action is None
    assert "no known-good artifact" in plan.reasoning


def test_a_config_category_plans_a_rollback_of_the_known_good_value():
    """Phase 4 change: config maps to rollback_config rather than to no action.

    No Tier 1 action precedes it, on the same reasoning as deploy: the pool
    size is a value the process rereads at connection time, not a process state
    a restart would clear, and a restart under the regressed value would just
    come back up exhausted again.
    """
    plan = plan_action(_state("config"))

    assert plan.available is True
    assert plan.action == "rollback_config"
    assert plan.container == SERVICE
    assert plan.check == "config"
    assert plan.args == ("DB_POOL_SIZE", "10", "2026-10-02T08:00:00+00:00")
    assert "DB_POOL_SIZE" in plan.reasoning


def test_a_config_category_with_nothing_safe_to_restore_refuses_to_act(monkeypatch):
    monkeypatch.setattr(remediation_policy, "_resolve_tier_2_target", lambda action, service: None)

    plan = plan_action(_state("config"))

    assert plan.available is False
    assert plan.action is None
    assert "no known-good value" in plan.reasoning


def test_an_unmapped_category_says_a_later_phase_is_correct():
    plan = plan_action(_state("network"))

    assert plan.available is False
    assert plan.action is None
    assert "later phase" in plan.reasoning


def test_no_action_is_available_without_a_hypothesis():
    plan = plan_action(_state(None))

    assert plan.available is False
    assert plan.action is None


def test_every_category_the_schema_allows_either_maps_to_an_action_or_maps_to_nothing():
    allowed = get_args(Hypothesis.model_fields["category"].annotation)

    assert set(allowed) == set(CATEGORY_ACTIONS)


def test_the_categories_with_no_tier_1_action_are_exactly_the_ones_named():
    unmapped = {c for c, actions in CATEGORY_ACTIONS.items() if not actions}

    assert unmapped == set(NO_ACTION_CATEGORIES)


def test_planning_twice_gives_the_same_plan():
    state = _state("crash")

    assert plan_action(state) == plan_action(state)


def test_the_policy_module_never_reaches_the_llm_client():
    source = (Path(__file__).parent / "remediation_policy.py").read_text(encoding="utf-8")

    assert "llm_client" not in source
