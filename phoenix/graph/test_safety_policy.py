"""The execution policy as configuration and as rules, with no graph and no database.

What these pin: the default is guarded; an unknown mode or a weaker-than-floor
setting refuses to start; lab mode waits for nothing; Tier 2 always waits in
guarded mode; Tier 1 waits only when the user asks; and Tier 3 has no path past
a pull request. The graph-level behaviour is in test_remediator_approval.py.
"""

import pytest

from phoenix.graph import safety_policy
from phoenix.graph.safety_policy import PolicyConfigError, load_config
from phoenix.tools import github_tool


def test_an_empty_environment_runs_guarded_with_the_default_ttl():
    config = load_config({})

    assert config.execution_policy == "guarded"
    assert config.approval_ttl_seconds == safety_policy.DEFAULT_APPROVAL_TTL_SECONDS
    assert config.approvers == frozenset()
    assert config.tier1_requires_approval is False


def test_the_lab_mode_is_only_entered_by_naming_it():
    config = load_config({"PHOENIX_EXECUTION_POLICY": "autonomous_lab"})

    assert config.execution_policy == "autonomous_lab"


def test_a_misspelt_mode_refuses_to_start_rather_than_falling_back_to_lab():
    with pytest.raises(PolicyConfigError, match="PHOENIX_EXECUTION_POLICY"):
        load_config({"PHOENIX_EXECUTION_POLICY": "autonomous-lab"})


def test_the_approver_list_is_split_trimmed_and_empty_entries_dropped():
    config = load_config({"PHOENIX_APPROVERS": " yash@example.com , ,ops@example.com,"})

    assert config.approvers == frozenset({"yash@example.com", "ops@example.com"})


def test_a_ttl_must_be_a_positive_whole_number():
    with pytest.raises(PolicyConfigError):
        load_config({"PHOENIX_APPROVAL_TTL_SECONDS": "soon"})
    with pytest.raises(PolicyConfigError):
        load_config({"PHOENIX_APPROVAL_TTL_SECONDS": "0"})

    assert load_config({"PHOENIX_APPROVAL_TTL_SECONDS": "120"}).approval_ttl_seconds == 120


def test_the_tier_1_setting_is_read_as_a_boolean():
    assert load_config({"PHOENIX_TIER1_REQUIRES_APPROVAL": "true"}).tier1_requires_approval is True
    with pytest.raises(PolicyConfigError, match="PHOENIX_TIER1_REQUIRES_APPROVAL"):
        load_config({"PHOENIX_TIER1_REQUIRES_APPROVAL": "sometimes"})


def test_turning_tier_2_approval_off_is_refused_because_it_is_weaker_than_the_floor():
    with pytest.raises(PolicyConfigError, match="weaker than the Tier 2 floor"):
        load_config({"PHOENIX_TIER2_REQUIRES_APPROVAL": "false"})


def test_asking_for_tier_2_approval_explicitly_is_accepted():
    load_config({"PHOENIX_TIER2_REQUIRES_APPROVAL": "true"})


def test_restart_is_tier_1_and_the_rollbacks_are_tier_2():
    assert safety_policy.action_tier("restart_service") == 1
    assert safety_policy.action_tier("rollback_deployment") == 2
    assert safety_policy.action_tier("rollback_config") == 2


def test_an_action_the_table_does_not_place_is_held_as_tier_2():
    """Unknown means the stricter tier, so a new action added without a policy
    entry waits for a person rather than running in the open."""
    assert safety_policy.action_tier("pause_worker") == 2


def test_lab_mode_waits_for_nothing_at_any_tier():
    config = load_config({"PHOENIX_TIER1_REQUIRES_APPROVAL": "true"})

    assert safety_policy.approval_required("autonomous_lab", 1, config) is False
    assert safety_policy.approval_required("autonomous_lab", 2, config) is False


def test_guarded_mode_auto_approves_tier_1_by_default():
    config = load_config({})

    assert safety_policy.approval_required("guarded", 1, config) is False


def test_guarded_mode_holds_tier_1_when_the_user_tightens_it():
    config = load_config({"PHOENIX_TIER1_REQUIRES_APPROVAL": "true"})

    assert safety_policy.approval_required("guarded", 1, config) is True


def test_guarded_mode_always_holds_tier_2():
    config = load_config({})

    assert safety_policy.approval_required("guarded", 2, config) is True


def test_a_mode_the_rules_do_not_recognise_is_treated_as_guarded():
    """Fail closed: a value that slipped past validation still asks before it acts."""
    config = load_config({})

    assert safety_policy.approval_required("yolo", 2, config) is True
    assert safety_policy.approval_required("yolo", 1, config) is False


def test_tier_3_may_only_open_a_pull_request_or_comment_on_one():
    safety_policy.assert_tier3_action("open_pull_request")
    safety_policy.assert_tier3_action("comment_on_pull_request")


@pytest.mark.parametrize("action", ["merge_pull_request", "approve_pull_request", "push_to_main", "restart_service"])
def test_tier_3_refuses_anything_past_the_pull_request(action):
    with pytest.raises(PermissionError, match="stops at a pull request"):
        safety_policy.assert_tier3_action(action)


def test_the_github_tool_has_no_merge_approve_or_push_operation():
    """Phoenix never merges. The github tool is where such an operation would have
    to live, so its public names are held to that rather than to a list."""
    public = {
        name for name, value in vars(github_tool).items()
        if callable(value) and not name.startswith("_")
        and getattr(value, "__module__", "") == github_tool.__name__
    }
    forbidden = ("merge", "approve", "push", "delete", "close")

    assert public
    assert not [name for name in public if any(word in name for word in forbidden)]
