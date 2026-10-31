"""The approve/deny endpoint: who may answer, and what an answer does.

The database call is replaced, so this checks the gate in front of it: the token,
the approver list, the request shape, and the status code each outcome gets. The
first-answer-wins rule itself is a SQL WHERE clause, tested in db.decide_approval's
statement text below and by the graph-side tests that read the result.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://phoenix_app:test@localhost:1/phoenix")

import pytest
from fastapi.testclient import TestClient

import db
import main

TOKEN = "approval-token-for-tests"
APPROVER = "yash@example.com"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PHOENIX_APPROVAL_TOKEN", TOKEN)
    monkeypatch.setenv("PHOENIX_APPROVERS", f"{APPROVER}, ops@example.com")
    calls: list[tuple] = []
    outcome = {"value": ("decided", {"id": 7, "incident_id": 1, "action": "rollback_deployment",
                                     "container": "checkout-service", "status": "approved",
                                     "decided_by": APPROVER, "decided_at": None})}

    def decide_approval(approval_id, status, approver, channel):
        calls.append((approval_id, status, approver, channel))
        return outcome["value"]

    monkeypatch.setattr(db, "decide_approval", decide_approval)
    test_client = TestClient(main.app)
    test_client.calls = calls
    test_client.outcome = outcome
    return test_client


def _post(client, approval_id=7, body=None, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    payload = body if body is not None else {"decision": "approve", "approver": APPROVER}
    return client.post(f"/approvals/{approval_id}/decision", json=payload, headers=headers)


def test_with_no_token_configured_the_endpoint_refuses_everything(client, monkeypatch):
    monkeypatch.delenv("PHOENIX_APPROVAL_TOKEN")

    response = _post(client)

    assert response.status_code == 503
    assert client.calls == []


def test_a_missing_or_wrong_token_is_refused(client):
    assert _post(client, token=None).status_code == 401
    assert _post(client, token="guessed").status_code == 401
    assert client.calls == []


def test_an_identity_not_on_the_approver_list_cannot_answer(client):
    response = _post(client, body={"decision": "approve", "approver": "mallory@example.com"})

    assert response.status_code == 403
    assert client.calls == []


def test_an_approval_by_a_listed_approver_is_recorded_once(client):
    response = _post(client)

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert client.calls == [(7, "approved", APPROVER, "api")]


def test_a_denial_is_recorded_as_denied(client):
    client.outcome["value"] = ("decided", {"id": 7, "status": "denied", "decided_by": APPROVER})

    response = _post(client, body={"decision": "deny", "approver": APPROVER})

    assert response.status_code == 200
    assert client.calls == [(7, "denied", APPROVER, "api")]


def test_a_second_answer_is_ignored_and_says_which_answer_won(client):
    client.outcome["value"] = ("already_decided", {"status": "approved"})

    response = _post(client, body={"decision": "deny", "approver": APPROVER})

    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "approved"
    assert "first answer already won" in response.json()["detail"]["result"]


def test_an_answer_after_the_deadline_is_refused_not_approved(client):
    client.outcome["value"] = ("expired", None)

    response = _post(client)

    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "expired"


def test_an_unknown_request_is_a_404(client):
    client.outcome["value"] = ("not_found", None)

    assert _post(client, approval_id=999).status_code == 404


def test_a_decision_outside_approve_or_deny_is_rejected_before_the_database(client):
    response = _post(client, body={"decision": "maybe", "approver": APPROVER})

    assert response.status_code == 422
    assert client.calls == []


@pytest.mark.parametrize("channel", ["slack", "dashboard", "api"])
def test_the_channel_the_caller_names_is_the_one_recorded(client, channel):
    response = _post(client, body={"decision": "approve", "approver": APPROVER, "channel": channel})

    assert response.status_code == 200
    assert client.calls == [(7, "approved", APPROVER, channel)]


def test_a_denial_from_slack_is_recorded_as_a_slack_denial(client):
    client.outcome["value"] = ("decided", {"id": 7, "status": "denied", "decided_by": APPROVER})

    response = _post(client, body={"decision": "deny", "approver": APPROVER, "channel": "slack"})

    assert response.status_code == 200
    assert client.calls == [(7, "denied", APPROVER, "slack")]


@pytest.mark.parametrize("channel", ["email", "SLACK", "", "slack ", None, 7, ["slack"]])
def test_a_channel_outside_the_known_three_is_rejected_before_the_database(client, channel):
    response = _post(client, body={"decision": "approve", "approver": APPROVER, "channel": channel})

    assert response.status_code == 422
    assert client.calls == []


def test_naming_a_channel_does_not_skip_the_token_or_the_approver_checks(client):
    body = {"decision": "approve", "approver": "mallory@example.com", "channel": "slack"}

    assert _post(client, body=body).status_code == 403
    assert _post(client, body={**body, "approver": APPROVER}, token="guessed").status_code == 401
    assert client.calls == []


def test_the_decision_statement_only_moves_a_pending_unexpired_row():
    assert "WHERE id = %(id)s AND status = 'pending' AND expires_at > now()" in db.DECIDE_APPROVAL
