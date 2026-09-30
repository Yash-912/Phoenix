from pathlib import Path
from typing import get_args

from phoenix.graph.remediation_policy import CATEGORY_ACTIONS, plan_action
from phoenix.graph.schemas import Hypothesis, ScoredHypothesis
from phoenix.graph.state import AgentState

SERVICE = "checkout-service"

NO_ACTION_CATEGORIES = ("deploy", "config", "network", "unknown")


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


def test_a_deploy_category_plans_no_action_and_says_which_tier_would():
    plan = plan_action(_state("deploy"))

    assert plan.available is False
    assert plan.action is None
    assert "Tier 2" in plan.reasoning


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
