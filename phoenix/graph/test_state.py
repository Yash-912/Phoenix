import pytest
from pydantic import ValidationError

from phoenix.graph.state import AgentState


def _state(**overrides) -> AgentState:
    return AgentState(incident_id=1, service_name="checkout-service", **overrides)


def test_a_fresh_state_has_taken_no_action_and_acts_under_the_default_policy():
    state = _state()

    assert state.remediation_attempts == 0
    assert state.max_remediation_attempts == 2
    assert state.policy_mode == "autonomous_lab"
    assert state.planned_action is None
    assert state.verification_result is None
    assert state.verification_delay_seconds == 15
    assert state.status == "investigating"


def test_a_policy_mode_outside_the_two_literals_is_rejected():
    with pytest.raises(ValidationError):
        _state(policy_mode="yolo")


def test_a_status_outside_the_five_literals_is_rejected():
    with pytest.raises(ValidationError):
        _state(status="rebooting")


def test_the_attempt_cap_can_be_lowered_but_not_negative():
    with pytest.raises(ValidationError):
        _state(max_remediation_attempts=-1)
