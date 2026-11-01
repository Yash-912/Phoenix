"""The settle pass: closing a Slack message whose request was answered elsewhere or ran out of time.

A message keeps its Approve and Deny buttons until someone edits it. A click edits its own
message, but an answer from the dashboard or the API, and an expiry, leave the buttons up.
This pass reads each unsettled message's approval from the API and edits the ones that are no
longer open. The rules under test are the spec's: whoever won, the message says who, how and
when; a deadline passing says "expired" and never "approved"; a message is edited once; and a
dependency that is down leaves everything as it was, to be retried on the next pass.
"""

import pytest
from slack_sdk.errors import SlackApiError

from phoenix.slack import bridge
from phoenix.slack.api_client import ApiError
from phoenix.slack.config import load_config
from phoenix.slack.store import Notification, StoreError

CONFIG = load_config({
    "SLACK_APP_TOKEN": "xapp-1-A0123456789-1234567890-abcdef",
    "SLACK_BOT_TOKEN": "xoxb-1234567890-abcdefghijkl",
    "PHOENIX_SLACK_CHANNEL": "C0123456789",
    "PHOENIX_SLACK_APPROVERS": "U012ABCDEF=alice",
    "PHOENIX_APPROVAL_TOKEN": "approval-token-for-tests",
    "PHOENIX_DASHBOARD_URL": "https://phoenix.example.com",
    "DATABASE_URL": "postgresql://slack_bridge:test@postgres:5432/phoenix",
})


def _approval(approval_id=7, **changes):
    row = {
        "id": approval_id, "incident_id": 12, "service_name": "checkout-service",
        "action": "rollback_deployment", "container": "checkout-service", "args": ["v17"], "tier": 2,
        "status": "pending", "can_decide": True, "overdue": False,
        "incident_summary": "bad_deploy: v18 raised the 5xx rate", "plan_reasoning": "",
        "evidence": [{"source": "loki", "iteration": 1, "summary": "KeyError in checkout"}],
        "requested_at": "2026-11-01T12:00:00+00:00", "expires_at": "2026-11-01T12:15:00+00:00",
        "decided_at": None, "decided_by": None, "decision_channel": None,
    }
    row.update(changes)
    return row


def _decided(status="approved", by="yash", channel="dashboard", approval_id=7):
    return _approval(approval_id, status=status, can_decide=False, decided_at="2026-11-01T12:05:00+00:00",
                     decided_by=by, decision_channel=channel)


def _expired(approval_id=7):
    return _approval(approval_id, status="expired", can_decide=False, decided_at="2026-11-01T12:15:01+00:00",
                     decided_by="system:expiry", decision_channel="expiry")


def _overdue(approval_id=7):
    return _approval(approval_id, status="pending", can_decide=False, overdue=True)


class Api:
    def __init__(self, approvals=None, error=None):
        self.approvals = {a["id"]: a for a in (approvals or [])}
        self.error = error
        self.asked: list[int] = []

    def get_approval(self, approval_id):
        self.asked.append(approval_id)
        if self.error:
            raise self.error
        return self.approvals.get(approval_id)


class Store:
    def __init__(self, *approval_ids, read_error=None, settle_error=None):
        self.rows = [Notification(n, "C0123456789", f"17300000{n:02d}.000100") for n in approval_ids]
        self.read_error, self.settle_error, self.settled = read_error, settle_error, []

    def unsettled(self):
        if self.read_error:
            raise self.read_error
        return [row for row in self.rows if row.approval_id not in self.settled]

    def settle(self, approval_id):
        if self.settle_error:
            raise self.settle_error
        self.settled.append(approval_id)


class Slack:
    def __init__(self, error=None, fail_for=()):
        self.updates: list[dict] = []
        self.error, self.fail_for = error, set(fail_for)

    def chat_update(self, **kwargs):
        if self.error or kwargs["ts"] in self.fail_for:
            raise self.error or RuntimeError("slack is down")
        self.updates.append(kwargs)
        return {"ok": True}


def _rate_limited(retry_after):
    class Response:
        status_code = 429
        headers = {"Retry-After": str(retry_after)}
        data = {"ok": False, "error": "ratelimited"}

        def get(self, key, default=None):
            return self.data.get(key, default)

    return SlackApiError("ratelimited", Response())


def _api_error(code):
    class Response:
        status_code = 200
        headers = {}
        data = {"ok": False, "error": code}

        def get(self, key, default=None):
            return self.data.get(key, default)

    return SlackApiError(code, Response())


def _settle(api, store, slack):
    return bridge.settle_pass(CONFIG, api, store, slack)


def _outcome(update):
    return update["blocks"][-1]["text"]["text"]


def test_an_answer_from_the_dashboard_closes_the_message_with_who_how_and_when():
    api, store, slack = Api([_decided("approved", "yash", "dashboard")]), Store(7), Slack()

    result = _settle(api, store, slack)

    assert result.settled == 1
    update = slack.updates[0]
    assert update["channel"] == "C0123456789" and update["ts"] == "1730000007.000100"
    assert not [b for b in update["blocks"] if b["type"] == "actions"]
    outcome = _outcome(update)
    assert "Approved" in outcome and "yash" in outcome and "via dashboard" in outcome
    assert "<!date^1793534700^" in outcome and "|2026-11-01T12:05:00+00:00>" in outcome
    assert store.settled == [7]


def test_the_closed_message_keeps_the_header_the_diagnosis_and_the_link():
    slack = Slack()

    _settle(Api([_decided()]), Store(7), slack)

    body = str(slack.updates[0]["blocks"])
    assert "Approval needed: rollback_deployment on checkout-service" in body
    assert "bad_deploy: v18 raised the 5xx rate" in body
    assert "https://phoenix.example.com/dashboard/#/incidents/12" in body


@pytest.mark.parametrize("status, word", [("approved", "Approved"), ("denied", "Denied")])
def test_each_answer_is_named_as_what_it_was(status, word):
    slack = Slack()

    _settle(Api([_decided(status, "bob", "api")]), Store(7), slack)

    assert word in _outcome(slack.updates[0]) and "via api" in _outcome(slack.updates[0])


def test_a_slack_answer_whose_edit_never_landed_is_closed_with_the_same_wording():
    slack = Slack()

    _settle(Api([_decided("approved", "alice", "slack")]), Store(7), slack)

    assert "Approved by alice via slack" in _outcome(slack.updates[0]).replace("*", "")


def test_an_expired_request_closes_as_expired_and_never_as_an_approval():
    slack = Slack()

    _settle(Api([_expired()]), Store(7), slack)

    outcome = _outcome(slack.updates[0])
    assert "Expired" in outcome and "never an approval" in outcome
    assert "Approved" not in outcome and "system:expiry" not in outcome
    assert "|2026-11-01T12:15:00+00:00>" in outcome


def test_a_request_past_its_deadline_that_the_watcher_has_not_swept_yet_closes_as_expired():
    slack, store = Slack(), Store(7)

    _settle(Api([_overdue()]), store, slack)

    assert "Expired" in _outcome(slack.updates[0])
    assert store.settled == [7]


def test_a_request_that_is_still_open_is_left_alone():
    api, store, slack = Api([_approval()]), Store(7), Slack()

    result = _settle(api, store, slack)

    assert result.settled == 0
    assert slack.updates == [] and store.settled == []


def test_a_closed_message_is_not_edited_again_on_the_next_pass():
    api, store, slack = Api([_decided()]), Store(7), Slack()

    _settle(api, store, slack)
    again = _settle(api, store, slack)

    assert len(slack.updates) == 1
    assert again.settled == 0
    assert api.asked == [7]


def test_with_nothing_to_settle_the_api_is_not_asked():
    api = Api([_decided()])

    assert _settle(api, Store(), Slack()).settled == 0
    assert api.asked == []


def test_messages_are_settled_oldest_first_and_only_the_closed_ones():
    slack, store = Slack(), Store(9, 7, 8)

    result = _settle(Api([_decided(approval_id=7), _approval(8), _expired(9)]), store, slack)

    assert [u["ts"] for u in slack.updates] == ["1730000007.000100", "1730000009.000100"]
    assert result.settled == 2 and store.settled == [7, 9]


def test_an_unreachable_api_changes_nothing_and_the_next_pass_succeeds(capsys):
    store, slack = Store(7), Slack()

    first = _settle(Api(error=ApiError("GET /approvals/7 failed: ConnectError")), store, slack)
    assert first.settled == 0 and slack.updates == [] and store.settled == []
    assert "ConnectError" in capsys.readouterr().out

    second = _settle(Api([_decided()]), store, slack)
    assert second.settled == 1 and store.settled == [7]


def test_a_dead_api_stops_the_pass_at_the_first_failure_instead_of_asking_for_every_message():
    api = Api(error=ApiError("down"))

    _settle(api, Store(7, 8, 9), Slack())

    assert api.asked == [7]


def test_a_request_the_api_does_not_know_is_logged_and_skipped_without_blocking_the_rest(capsys):
    slack, store = Slack(), Store(7, 8)

    _settle(Api([_decided(approval_id=8)]), store, slack)

    assert store.settled == [8]
    assert "approval 7" in capsys.readouterr().out


def test_a_slack_error_leaves_the_row_open_and_the_next_pass_retries():
    store = Store(7)

    first = _settle(Api([_decided()]), store, Slack(error=RuntimeError("slack is down")))
    assert first.settled == 0 and store.settled == []

    slack = Slack()
    second = _settle(Api([_decided()]), store, slack)
    assert second.settled == 1 and len(slack.updates) == 1


def test_one_failed_edit_does_not_stop_the_others():
    slack, store = Slack(fail_for=["1730000007.000100"]), Store(7, 8)

    _settle(Api([_decided(approval_id=7), _decided(approval_id=8)]), store, slack)

    assert store.settled == [8]


def test_a_rate_limit_stops_the_pass_and_reports_how_long_to_wait():
    slack, store = Slack(error=_rate_limited(30)), Store(7, 8)

    result = _settle(Api([_decided(approval_id=7), _decided(approval_id=8)]), store, slack)

    assert result.wait == 30 and result.settled == 0 and store.settled == []


@pytest.mark.parametrize("code", ["message_not_found", "channel_not_found", "cant_update_message"])
def test_a_message_that_can_never_be_edited_is_closed_in_the_table_so_it_is_not_retried_forever(code, capsys):
    store = Store(7)

    result = _settle(Api([_decided()]), store, Slack(error=_api_error(code)))

    assert store.settled == [7]
    assert result.settled == 0
    assert code in capsys.readouterr().out


def test_an_unreadable_table_skips_the_pass():
    api = Api([_decided()])

    result = _settle(api, Store(7, read_error=StoreError("connection refused")), Slack())

    assert result.settled == 0 and api.asked == []


def test_a_failure_to_mark_the_row_settled_is_logged_and_the_edit_is_repeated_harmlessly_next_pass(capsys):
    store, slack = Store(7, settle_error=StoreError("disk full")), Slack()

    _settle(Api([_decided()]), store, slack)
    assert "disk full" in capsys.readouterr().out

    store.settle_error = None
    _settle(Api([_decided()]), store, slack)
    assert store.settled == [7]
    assert [_outcome(u) for u in slack.updates][0] == [_outcome(u) for u in slack.updates][1]


def test_an_approver_name_with_markup_cannot_ping_the_channel_through_the_closed_message():
    slack = Slack()

    _settle(Api([_decided("approved", "<!channel>", "dashboard")]), Store(7), slack)

    assert "<!channel>" not in str(slack.updates[0])


def test_no_log_line_contains_a_token(capsys):
    _settle(Api(error=ApiError("boom")), Store(7), Slack())
    _settle(Api([_decided()]), Store(7), Slack(error=RuntimeError(f"auth failed for {CONFIG.bot_token}")))

    out = capsys.readouterr().out
    assert CONFIG.bot_token not in out and CONFIG.app_token not in out and CONFIG.approval_token not in out
