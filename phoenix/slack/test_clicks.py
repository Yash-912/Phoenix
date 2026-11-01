"""The Slack side of a click: ack first, then what the listener does with the answer.

Slack, the API and the notification table are fakes sharing one event list, so the order
things happened in is part of what is asserted. The rule that matters most is the first
one: the click is acked before anything else, because Slack gives three seconds.
"""

import pytest
from slack_bolt.request import BoltRequest
from slack_sdk import WebClient
from slack_sdk.web import SlackResponse

from phoenix.slack import bridge, messages
from phoenix.slack.api_client import DecisionResult
from phoenix.slack.config import load_config
from phoenix.slack.store import StoreError

CONFIG = load_config({
    "SLACK_APP_TOKEN": "xapp-1-A0123456789-1234567890-abcdef",
    "SLACK_BOT_TOKEN": "xoxb-1234567890-abcdefghijkl",
    "PHOENIX_SLACK_CHANNEL": "C0123456789",
    "PHOENIX_SLACK_APPROVERS": "U012ABCDEF=alice",
    "PHOENIX_APPROVAL_TOKEN": "approval-token-for-tests",
    "PHOENIX_DASHBOARD_URL": "https://phoenix.example.com",
    "DATABASE_URL": "postgresql://slack_bridge:test@postgres:5432/phoenix",
})

APPROVAL = {
    "id": 7, "incident_id": 12, "service_name": "checkout-service", "action": "rollback_deployment",
    "container": "checkout-service", "args": ["v17"], "tier": 2, "incident_summary": "bad_deploy",
    "evidence": [], "expires_at": "2026-11-01T12:15:00+00:00",
}
PENDING_BLOCKS = messages.pending_message(APPROVAL, CONFIG.dashboard_url)["blocks"]


class Events:
    """One list that ack, the API, Slack and the table all append to."""

    def __init__(self):
        self.order: list[str] = []


class Api:
    def __init__(self, events, result):
        self.events, self.result, self.calls = events, result, []

    def decide(self, approval_id, decision, approver):
        self.events.order.append("decide")
        self.calls.append((approval_id, decision, approver))
        return self.result


class Client:
    def __init__(self, events, error=None):
        self.events, self.error, self.updates = events, error, []

    def chat_update(self, **kwargs):
        self.events.order.append("update")
        if self.error:
            raise self.error
        self.updates.append(kwargs)
        return {"ok": True}


class Store:
    def __init__(self, events, settle_error=None):
        self.events, self.settle_error, self.settled = events, settle_error, []

    def settle(self, approval_id):
        self.events.order.append("settle")
        if self.settle_error:
            raise self.settle_error
        self.settled.append(approval_id)


def _payload(user="U012ABCDEF", action="phoenix_approve", value="7", channel="C0123456789", ts="1730000001.000100"):
    return {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": channel},
        "message": {"ts": ts, "blocks": PENDING_BLOCKS},
        "actions": [{"action_id": action, "value": value, "block_id": "approval:7"}],
        "response_url": "http://localhost:1/unused",
    }


def _click(result=DecisionResult("recorded", "approved"), *, payload=None, client_error=None, settle_error=None, respond_error=None):
    events = Events()
    api, client, store = Api(events, result), Client(events, client_error), Store(events, settle_error)
    replies = []

    def ack():
        events.order.append("ack")

    def respond(**kwargs):
        events.order.append("respond")
        if respond_error:
            raise respond_error
        replies.append(kwargs)

    listener = bridge.make_click_listener(CONFIG, api, store)
    listener(ack=ack, body=_payload() if payload is None else payload, respond=respond, client=client)
    return events, api, client, store, replies


def test_the_click_is_acked_before_anything_else_happens():
    events, *_ = _click()

    assert events.order == ["ack", "decide", "update", "settle"]


def test_a_recorded_approval_edits_the_message_and_settles_its_row():
    _, api, client, store, replies = _click()

    assert api.calls == [(7, "approve", "alice")]
    update = client.updates[0]
    assert update["channel"] == "C0123456789" and update["ts"] == "1730000001.000100"
    assert not [block for block in update["blocks"] if block["type"] == "actions"]
    outcome = update["blocks"][-1]["text"]["text"]
    assert "Approved" in outcome and "alice" in outcome and "via slack" in outcome
    assert store.settled == [7]
    assert replies == []


def test_a_recorded_denial_edits_the_message_as_denied():
    _, api, client, store, _ = _click(DecisionResult("recorded", "denied"), payload=_payload(action="phoenix_deny"))

    assert api.calls == [(7, "deny", "alice")]
    assert "Denied" in client.updates[0]["blocks"][-1]["text"]["text"]
    assert store.settled == [7]


def test_losing_the_race_tells_only_the_clicker_and_leaves_the_message_and_row_alone():
    events, _, client, store, replies = _click(DecisionResult("already_decided", "approved"))

    assert client.updates == [] and store.settled == []
    assert events.order == ["ack", "decide", "respond"]
    assert replies[0]["response_type"] == "ephemeral" and replies[0]["replace_original"] is False
    assert "Already approved" in replies[0]["text"]


def test_an_unmapped_user_is_answered_ephemerally_with_no_api_call():
    events, api, client, _, replies = _click(payload=_payload(user="U999ZZZZZZ"))

    assert api.calls == [] and client.updates == []
    assert events.order == ["ack", "respond"]
    assert "approver list" in replies[0]["text"]


def test_a_click_that_is_not_ours_is_acked_and_nothing_else_happens():
    events, api, client, _, replies = _click(payload=_payload(channel="C0OTHERCHAN"))

    assert events.order == ["ack"]
    assert api.calls == [] and client.updates == [] and replies == []


@pytest.mark.parametrize("payload", [
    {},
    {"actions": []},
    {"user": {"id": "U012ABCDEF"}},
    {"actions": [{"action_id": "phoenix_approve"}], "user": None},
    {"actions": "nope", "user": {"id": "U012ABCDEF"}},
])
def test_a_payload_missing_what_a_click_needs_is_acked_and_dropped(payload, capsys):
    events, api, *_ = _click(payload=payload)

    assert events.order == ["ack"]
    assert api.calls == []
    assert "malformed" in capsys.readouterr().out


def test_if_the_message_cannot_be_edited_the_decision_stands_and_the_clicker_is_told():
    events, api, _, store, replies = _click(client_error=RuntimeError("slack is down"))

    assert api.calls == [(7, "approve", "alice")]
    assert store.settled == []
    assert "recorded" in replies[0]["text"].lower() and "message" in replies[0]["text"].lower()


def test_if_the_row_cannot_be_settled_it_is_logged_and_nothing_crashes(capsys):
    _click(settle_error=StoreError("disk full"))

    assert "disk full" in capsys.readouterr().out


def test_a_failure_to_send_the_ephemeral_reply_is_logged_and_does_not_crash(capsys):
    _click(DecisionResult("unreachable"), respond_error=RuntimeError("response_url unreachable"))

    assert "response_url unreachable" in capsys.readouterr().out


def test_no_click_log_line_contains_a_token(capsys):
    _click(client_error=RuntimeError(f"auth failed for {CONFIG.bot_token}"))
    _click(DecisionResult("unreachable"), respond_error=RuntimeError(f"bad {CONFIG.app_token}"))

    out = capsys.readouterr().out
    assert CONFIG.bot_token not in out and CONFIG.app_token not in out and CONFIG.approval_token not in out


def test_bolt_routes_both_buttons_to_the_listener_and_ignores_other_buttons(monkeypatch):
    # Bolt builds its own Slack client for every request, so the fake goes on the class.
    # auth.test and chat.update are the only calls this path makes, and neither leaves the process.
    updates = []
    auth = SlackResponse(
        client=None, http_verb="POST", api_url="https://slack.com/api/auth.test", req_args={}, status_code=200, headers={},
        data={"ok": True, "user_id": "UBOT", "team_id": "T1", "bot_id": "B1", "url": "https://example.slack.com/"},
    )
    monkeypatch.setattr(WebClient, "auth_test", lambda self, **kw: auth)
    monkeypatch.setattr(WebClient, "chat_update", lambda self, **kw: updates.append(kw) or {"ok": True})
    events = Events()
    api = Api(events, DecisionResult("recorded", "approved"))
    app = bridge.build_app(CONFIG, api, Store(events), process_before_response=True)

    approve = app.dispatch(BoltRequest(body=_payload(action="phoenix_approve"), mode="socket_mode"))
    deny = app.dispatch(BoltRequest(body=_payload(action="phoenix_deny", ts="1730000002.000100"), mode="socket_mode"))
    app.dispatch(BoltRequest(body=_payload(action="somebody_elses_button"), mode="socket_mode"))

    assert approve.status == 200 and deny.status == 200
    assert [call[1] for call in api.calls] == ["approve", "deny"]
    assert [update["ts"] for update in updates] == ["1730000001.000100", "1730000002.000100"]
