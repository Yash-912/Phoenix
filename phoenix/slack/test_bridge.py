"""The bridge's post pass and its startup: what it posts, what it records, and what it does
when something underneath it is down.

Slack, the API and the notification table are all fakes. The rules under test are the
spec's: a request is posted once, a restart does not repost it, and a failure in any
one dependency posts nothing it cannot record.
"""

import pytest
from slack_sdk.errors import SlackApiError

from phoenix.slack import bridge
from phoenix.slack.api_client import ApiError
from phoenix.slack.config import load_config
from phoenix.slack.store import Notification, StoreError, StoreNotReady

ENV = {
    "SLACK_APP_TOKEN": "xapp-1-A0123456789-1234567890-abcdef",
    "SLACK_BOT_TOKEN": "xoxb-1234567890-abcdefghijkl",
    "PHOENIX_SLACK_CHANNEL": "C0123456789",
    "PHOENIX_SLACK_APPROVERS": "U012ABCDEF=alice",
    "PHOENIX_APPROVAL_TOKEN": "approval-token-for-tests",
    "PHOENIX_DASHBOARD_URL": "https://phoenix.example.com",
    "DATABASE_URL": "postgresql://slack_bridge:test@postgres:5432/phoenix",
}
CONFIG = load_config(ENV)


def _approval(approval_id, **changes):
    row = {
        "id": approval_id, "incident_id": 12, "service_name": "checkout-service",
        "action": "rollback_deployment", "container": "checkout-service", "args": ["v17"], "tier": 2,
        "status": "pending", "incident_summary": "bad_deploy", "plan_reasoning": "",
        "evidence": [], "expires_at": "2026-11-01T12:15:00+00:00",
    }
    row.update(changes)
    return row


class FakeApi:
    def __init__(self, *approvals, error=None):
        self.approvals = list(approvals)
        self.error = error

    def list_pending(self):
        if self.error:
            raise self.error
        return list(self.approvals)

    def get_approval(self, approval_id):
        return None


class FakeStore:
    def __init__(self, *, notified=(), read_error=None, record_error=None):
        self.rows: dict[int, Notification] = {n: Notification(n, "C0123456789", "1.1") for n in notified}
        self.read_error = read_error
        self.record_error = record_error
        self.ready_checks = 0

    def check_ready(self):
        self.ready_checks += 1
        if self.read_error:
            raise self.read_error

    def unsettled(self):
        return []

    def notified_ids(self, ids):
        if self.read_error:
            raise self.read_error
        return {i for i in ids if i in self.rows}

    def record(self, approval_id, channel_id, message_ts):
        if self.record_error:
            raise self.record_error
        self.rows.setdefault(approval_id, Notification(approval_id, channel_id, message_ts))


class FakeSlack:
    def __init__(self, *, fail_for=(), error=None):
        self.posted: list[dict] = []
        self.fail_for = set(fail_for)
        self.error = error
        self._clock = 1730000000

    def chat_postMessage(self, **kwargs):
        approval_id = int(kwargs["blocks"][-1]["elements"][0]["value"])
        if approval_id in self.fail_for:
            raise self.error or RuntimeError("slack is down")
        self.posted.append(kwargs)
        self._clock += 1
        return {"ok": True, "ts": f"{self._clock}.000100"}


def _rate_limited(retry_after):
    class Response:
        status_code = 429
        headers = {"Retry-After": str(retry_after)}
        data = {"ok": False, "error": "ratelimited"}

        def get(self, key, default=None):
            return self.data.get(key, default)

    return SlackApiError("ratelimited", Response())


def _pass(api, store, slack):
    return bridge.post_pass(CONFIG, api, store, slack)


def test_a_pending_request_is_posted_to_the_configured_channel_and_recorded():
    api, store, slack = FakeApi(_approval(7)), FakeStore(), FakeSlack()

    result = _pass(api, store, slack)

    assert result.posted == 1
    assert len(slack.posted) == 1
    message = slack.posted[0]
    assert message["channel"] == "C0123456789"
    assert "rollback_deployment" in message["text"]
    assert message["blocks"][0]["type"] == "header"
    assert message["unfurl_links"] is False
    assert store.rows[7] == Notification(7, "C0123456789", "1730000001.000100")


def test_a_request_that_already_has_a_message_is_not_posted_again():
    slack = FakeSlack()

    _pass(FakeApi(_approval(7)), FakeStore(notified=[7]), slack)

    assert slack.posted == []


def test_a_second_pass_does_not_repost():
    api, store, slack = FakeApi(_approval(7)), FakeStore(), FakeSlack()

    _pass(api, store, slack)
    result = _pass(api, store, slack)

    assert result.posted == 0
    assert len(slack.posted) == 1


def test_a_restarted_bridge_reading_the_same_table_does_not_repost():
    store = FakeStore()
    first = FakeSlack()
    _pass(FakeApi(_approval(7)), store, first)

    restarted = FakeSlack()
    _pass(FakeApi(_approval(7)), store, restarted)

    assert len(first.posted) == 1
    assert restarted.posted == []


def test_requests_are_posted_oldest_first_whatever_order_the_api_lists_them():
    slack = FakeSlack()

    _pass(FakeApi(_approval(9), _approval(7), _approval(8)), FakeStore(), slack)

    order = [int(m["blocks"][-1]["elements"][0]["value"]) for m in slack.posted]
    assert order == [7, 8, 9]


def test_an_unreachable_api_posts_nothing_and_says_so(capsys):
    slack = FakeSlack()

    result = _pass(FakeApi(error=ApiError("GET /approvals answered 503")), FakeStore(), slack)

    assert result.posted == 0
    assert slack.posted == []
    assert "503" in capsys.readouterr().out


def test_an_unreadable_table_skips_the_pass_without_posting():
    slack = FakeSlack()

    result = _pass(FakeApi(_approval(7)), FakeStore(read_error=StoreError("connection refused")), slack)

    assert result.posted == 0
    assert slack.posted == []


def test_a_slack_error_records_nothing_and_the_next_pass_tries_again(capsys):
    store = FakeStore()
    down = FakeSlack(fail_for=[7])

    first = _pass(FakeApi(_approval(7)), store, down)
    assert first.posted == 0
    assert 7 not in store.rows
    assert "slack is down" in capsys.readouterr().out

    recovered = FakeSlack()
    second = _pass(FakeApi(_approval(7)), store, recovered)
    assert second.posted == 1
    assert 7 in store.rows


def test_one_failed_post_does_not_stop_the_others():
    store, slack = FakeStore(), FakeSlack(fail_for=[7])

    result = _pass(FakeApi(_approval(7), _approval(8)), store, slack)

    assert result.posted == 1
    assert 8 in store.rows and 7 not in store.rows


def test_a_rate_limit_stops_the_pass_and_reports_how_long_to_wait():
    store, slack = FakeStore(), FakeSlack(fail_for=[7], error=_rate_limited(30))

    result = _pass(FakeApi(_approval(7), _approval(8)), store, slack)

    assert result.wait == 30
    assert result.posted == 0
    assert slack.posted == []
    assert store.rows == {}


def test_a_failure_to_record_after_posting_is_logged_and_does_not_crash(capsys):
    slack = FakeSlack()

    result = _pass(FakeApi(_approval(7)), FakeStore(record_error=StoreError("disk full")), slack)

    assert len(slack.posted) == 1
    assert result.posted == 0
    assert "disk full" in capsys.readouterr().out


def test_no_log_line_contains_a_token(capsys):
    _pass(FakeApi(error=ApiError("boom")), FakeStore(), FakeSlack())
    _pass(FakeApi(_approval(7)), FakeStore(), FakeSlack(fail_for=[7]))

    out = capsys.readouterr().out
    assert "xoxb-" not in out and "xapp-" not in out and CONFIG.approval_token not in out


class _Loop:
    """Runs the loop for a set number of passes and records how long it slept between them."""

    def __init__(self, passes):
        self.passes = passes
        self.sleeps: list[float] = []

    def should_stop(self):
        return len(self.sleeps) >= self.passes

    def sleep(self, seconds):
        self.sleeps.append(seconds)


def test_the_loop_checks_the_table_before_it_posts_anything():
    slack = FakeSlack()
    store = FakeStore(read_error=StoreNotReady("apply migrations 009 and 010"))

    with pytest.raises(StoreNotReady, match="009"):
        bridge.run_forever(CONFIG, FakeApi(_approval(7)), store, slack, sleep=lambda s: None, should_stop=lambda: False)

    assert slack.posted == []


def test_the_loop_sleeps_the_poll_interval_and_longer_after_a_rate_limit():
    loop = _Loop(passes=2)
    slack = FakeSlack(fail_for=[7], error=_rate_limited(30))

    bridge.run_forever(CONFIG, FakeApi(_approval(7)), FakeStore(), slack, sleep=loop.sleep, should_stop=loop.should_stop)

    assert loop.sleeps == [30, 30]
    quiet = _Loop(passes=2)
    bridge.run_forever(CONFIG, FakeApi(), FakeStore(), FakeSlack(), sleep=quiet.sleep, should_stop=quiet.should_stop)
    assert quiet.sleeps == [CONFIG.poll_seconds, CONFIG.poll_seconds]


def test_main_exits_with_the_name_of_the_missing_setting(monkeypatch, capsys):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SLACK_APP_TOKEN")

    with pytest.raises(SystemExit) as caught:
        bridge.main()

    assert caught.value.code == 1
    assert "SLACK_APP_TOKEN" in capsys.readouterr().out
