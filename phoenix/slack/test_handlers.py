"""What a click on Approve or Deny does, as a function of who clicked and what the API said.

The API is a fake that records what it was asked. The rules under test are the spec's: only
a mapped Slack user reaches the API, only the approver name goes with them, a click that
is not ours is ignored, and every answer the API can give is one plain reply.
"""

import pytest

from phoenix.slack.api_client import DecisionResult
from phoenix.slack.config import load_config
from phoenix.slack.handlers import ClickOutcome, Settle, handle_click

CONFIG = load_config({
    "SLACK_APP_TOKEN": "xapp-1-A0123456789-1234567890-abcdef",
    "SLACK_BOT_TOKEN": "xoxb-1234567890-abcdefghijkl",
    "PHOENIX_SLACK_CHANNEL": "C0123456789",
    "PHOENIX_SLACK_APPROVERS": "U012ABCDEF=alice,U034DEFGHI=bob",
    "PHOENIX_APPROVAL_TOKEN": "approval-token-for-tests",
    "PHOENIX_DASHBOARD_URL": "https://phoenix.example.com",
    "DATABASE_URL": "postgresql://slack_bridge:test@postgres:5432/phoenix",
})


class FakeApi:
    def __init__(self, result=DecisionResult("recorded", "approved")):
        self.result = result
        self.calls: list[tuple] = []

    def decide(self, approval_id, decision, approver):
        self.calls.append((approval_id, decision, approver))
        return self.result


def _click(api, *, user="U012ABCDEF", action="phoenix_approve", channel="C0123456789", value="7"):
    return handle_click(CONFIG, api, user_id=user, action_id=action, channel_id=channel, value=value)


def test_an_approve_click_from_a_mapped_user_sends_an_approve_under_their_approver_name():
    api = FakeApi(DecisionResult("recorded", "approved"))

    outcome = _click(api)

    assert api.calls == [(7, "approve", "alice")]
    assert outcome == ClickOutcome(settle=Settle(7, "approved", "alice"))
    assert outcome.reply is None


def test_a_deny_click_sends_a_deny_and_settles_as_denied():
    api = FakeApi(DecisionResult("recorded", "denied"))

    outcome = _click(api, action="phoenix_deny", user="U034DEFGHI")

    assert api.calls == [(7, "deny", "bob")]
    assert outcome.settle == Settle(7, "denied", "bob")


def test_the_slack_user_id_never_reaches_the_api():
    api = FakeApi()

    _click(api)

    assert "U012ABCDEF" not in repr(api.calls)


def test_a_slack_user_who_is_not_in_the_map_is_refused_without_calling_the_api():
    api = FakeApi()

    outcome = _click(api, user="U999ZZZZZZ")

    assert api.calls == []
    assert outcome.settle is None
    assert not outcome.ignored
    assert "approver list" in outcome.reply


@pytest.mark.parametrize("kwargs", [
    {"channel": "C0OTHERCHAN"},
    {"channel": ""},
    {"action": "phoenix_delete"},
    {"action": "something_else"},
    {"value": None},
    {"value": ""},
    {"value": "abc"},
    {"value": "7; DROP TABLE approvals"},
    {"value": "-1"},
    {"value": "1e3"},
    {"value": "7 "},
    {"value": "٧"},
    {"value": "9" * 40},
])
def test_a_click_that_is_not_ours_is_ignored_and_makes_no_call(kwargs):
    api = FakeApi()

    outcome = _click(api, **kwargs)

    assert api.calls == []
    assert outcome.ignored
    assert outcome.reply is None and outcome.settle is None


@pytest.mark.parametrize("result, expected", [
    (DecisionResult("already_decided", "approved"), "already approved"),
    (DecisionResult("already_decided", "denied"), "already denied"),
    (DecisionResult("expired", "expired"), "deadline passed"),
    (DecisionResult("not_on_list"), "PHOENIX_APPROVERS"),
    (DecisionResult("not_configured"), "not set up"),
    (DecisionResult("not_found"), "no longer exists"),
    (DecisionResult("unreachable"), "Could not reach Phoenix"),
    (DecisionResult("error"), "unexpected"),
])
def test_every_answer_the_api_can_give_is_one_plain_reply_and_settles_nothing(result, expected):
    outcome = _click(FakeApi(result))

    assert expected.lower() in outcome.reply.lower()
    assert outcome.settle is None
    assert not outcome.ignored


def test_an_expired_reply_says_expiry_is_never_an_approval():
    reply = _click(FakeApi(DecisionResult("expired", "expired"))).reply

    assert "never an approval" in reply


def test_every_failure_reply_points_at_the_dashboard_when_it_helps():
    for result in (DecisionResult("unreachable"), DecisionResult("not_configured")):
        assert "dashboard" in _click(FakeApi(result)).reply.lower()
