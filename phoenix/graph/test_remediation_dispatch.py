import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from phoenix.graph import nodes
from phoenix.graph.llm_client import TOOL_SCHEMAS
from phoenix.graph.remediation_dispatch import REMEDIATION_DISPATCH

# Spelled out as a literal rather than derived from either table it checks.
# Deriving it would make the assertions tautological: a mutating action added to
# TOOL_DISPATCH would widen the expected set along with it, and the boundary
# would pass while broken. This is the same set test_nodes.py pins, kept here so
# the boundary this module exists to enforce is checked in the file that owns it.
OBSERVER_TOOLS = {
    "query_prometheus",
    "query_loki",
    "get_container_state",
    "inspect_health",
    "get_recent_deployments",
}


def test_no_mutating_action_is_reachable_from_the_observer():
    assert set(REMEDIATION_DISPATCH) & set(nodes.TOOL_DISPATCH) == set()


def test_every_action_the_policy_can_plan_is_dispatchable():
    from phoenix.graph.remediation_policy import CATEGORY_ACTIONS

    for actions in CATEGORY_ACTIONS.values():
        for action in actions:
            assert action in REMEDIATION_DISPATCH, action


def test_the_llm_is_offered_exactly_the_five_read_only_tools():
    assert {schema["function"]["name"] for schema in TOOL_SCHEMAS} == OBSERVER_TOOLS
    assert set(nodes.TOOL_DISPATCH) == OBSERVER_TOOLS


def test_no_observer_tool_name_collides_with_a_remediation_action():
    assert OBSERVER_TOOLS & set(REMEDIATION_DISPATCH) == set()
