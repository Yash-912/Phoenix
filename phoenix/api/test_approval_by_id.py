"""GET /approvals/{id}: one approval in any status, in the redacted shape the dashboard uses.

The Slack bridge reads this to find out whether a request it posted has since been decided
or has expired, whichever channel decided it. The database call is replaced, so this checks
the HTTP surface: the shape for each status, the redaction, the refusals, and that the
statement behind it is a read.
"""

import os
import re
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://phoenix_app:test@localhost:1/phoenix")

import psycopg
import pytest
from fastapi.testclient import TestClient

import db
import main

NOW = datetime.now(timezone.utc)


def _row(approval_id=7, **changes):
    row = {
        "id": approval_id, "incident_id": 12, "service_name": "checkout-service",
        "action": "rollback_deployment", "container": "checkout-service", "args": ["v17"], "tier": 2,
        "status": "pending", "incident_summary": "bad_deploy: v18 raised the 5xx rate",
        "plan_reasoning": "roll back to the last good artifact",
        "evidence": [{"source": "loki", "iteration": 1, "summary": "KeyError in the checkout handler"}],
        "requested_at": NOW - timedelta(minutes=5), "expires_at": NOW + timedelta(minutes=10),
        "decided_at": None, "decided_by": None, "decision_channel": None,
    }
    row.update(changes)
    return row


@pytest.fixture
def client(monkeypatch):
    rows = {}
    monkeypatch.setattr(db, "approval_by_id", lambda approval_id: rows.get(approval_id))
    test_client = TestClient(main.app)
    test_client.rows = rows
    return test_client


def _get(client, approval_id=7):
    return client.get(f"/approvals/{approval_id}")


def test_an_open_request_is_pending_and_can_still_be_decided(client):
    client.rows[7] = _row()

    body = _get(client).json()["approval"]

    assert body["id"] == 7 and body["status"] == "pending"
    assert body["can_decide"] is True and body["overdue"] is False
    assert body["decided_by"] is None and body["decision_channel"] is None


def test_a_request_past_its_deadline_but_not_yet_swept_is_overdue_and_cannot_be_decided(client):
    client.rows[7] = _row(expires_at=NOW - timedelta(minutes=1))

    body = _get(client).json()["approval"]

    assert body["status"] == "pending"
    assert body["can_decide"] is False and body["overdue"] is True


@pytest.mark.parametrize("status, who, channel", [
    ("approved", "alice", "slack"),
    ("approved", "yash", "dashboard"),
    ("denied", "bob", "api"),
    ("expired", "system:expiry", "expiry"),
])
def test_a_decided_request_says_who_decided_it_how_and_when(client, status, who, channel):
    decided = NOW - timedelta(minutes=2)
    client.rows[7] = _row(status=status, decided_at=decided, decided_by=who, decision_channel=channel)

    body = _get(client).json()["approval"]

    assert body["status"] == status
    assert (body["decided_by"], body["decision_channel"]) == (who, channel)
    assert body["decided_at"] == decided.isoformat()
    assert body["can_decide"] is False and body["overdue"] is False


def test_the_incident_and_service_come_with_the_approval(client):
    client.rows[7] = _row(incident_id=33, service_name="auth-service")

    body = _get(client).json()["approval"]

    assert body["incident_id"] == 33 and body["service_name"] == "auth-service"
    assert body["action"] == "rollback_deployment" and body["container"] == "checkout-service"
    assert body["tier"] == 2 and body["args"] == ["v17"]


def test_the_shape_matches_what_the_pending_list_returns_for_the_same_row(client, monkeypatch):
    client.rows[7] = _row()
    monkeypatch.setattr(db, "pending_approvals", lambda: [client.rows[7]])

    from_list = client.get("/approvals").json()["approvals"][0]
    from_id = _get(client).json()["approval"]

    assert from_id == from_list


def test_planted_secrets_are_redacted_in_every_free_text_field(client):
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    client.rows[7] = _row(
        incident_summary=f"saw {secret} in the log",
        plan_reasoning="connect with postgresql://phoenix:hunter2pass@db:5432/x then roll back",
        evidence=[{"source": "loki", "iteration": 1, "summary": f"Authorization: Bearer {secret}"},
                  {"source": "env", "iteration": 1, "summary": "password=correcthorsebattery"}],
        args=[secret],
    )

    response = _get(client)

    assert response.status_code == 200
    for planted in (secret, "hunter2pass", "correcthorsebattery"):
        assert planted not in response.text


def test_an_unknown_request_is_a_404(client):
    response = _get(client, 999)

    assert response.status_code == 404
    assert "999" in response.json()["detail"]


def test_an_id_that_is_not_a_number_is_a_422_and_never_reaches_the_database(client, monkeypatch):
    def boom(approval_id):
        raise AssertionError("the database must not be asked")

    monkeypatch.setattr(db, "approval_by_id", boom)

    assert client.get("/approvals/abc").status_code == 422
    assert client.get("/approvals/7;drop").status_code == 422


@pytest.mark.parametrize("approval_id", ["0", "-1", str(2**63), str(10**30)])
def test_an_id_no_row_could_have_is_a_422_not_a_database_error(client, monkeypatch, approval_id):
    def boom(approval_id):
        raise AssertionError("the database must not be asked")

    monkeypatch.setattr(db, "approval_by_id", boom)

    assert client.get(f"/approvals/{approval_id}").status_code == 422


def test_the_largest_valid_id_is_still_looked_up(client):
    client.rows[2**63 - 1] = _row(2**63 - 1)

    assert _get(client, 2**63 - 1).status_code == 200


def test_an_unreachable_database_is_a_503_with_no_connection_detail(client, monkeypatch):
    def down(approval_id):
        raise psycopg.OperationalError("connection to server at 10.0.0.5 failed: password authentication failed")

    monkeypatch.setattr(db, "approval_by_id", down)

    response = _get(client)

    assert response.status_code == 503
    assert "10.0.0.5" not in response.text and "password" not in response.text


def test_it_is_an_open_read_like_the_other_dashboard_reads_and_changes_nothing(client):
    client.rows[7] = _row()

    assert _get(client).status_code == 200
    assert client.get("/approvals/7", headers={"Authorization": "Bearer wrong"}).status_code == 200


def test_the_statement_is_a_single_read_by_id_with_no_status_filter():
    sql = db.APPROVAL_BY_ID

    assert sql.lstrip().upper().startswith("SELECT")
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT)\b", sql, re.IGNORECASE)
    assert "WHERE p.id = %(id)s" in sql
    assert "status" not in sql.split("WHERE", 1)[1]
    assert "JOIN incidents" in sql


def test_approval_by_id_returns_the_row_as_a_dict_or_none(monkeypatch):
    class Cursor:
        def __init__(self, rows):
            self.description = [type("C", (), {"name": name}) for name in ("id", "status")]
            self._rows = rows

        def fetchall(self):
            return self._rows

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            assert sql == db.APPROVAL_BY_ID
            return Cursor([(7, "approved")] if params["id"] == 7 else [])

    monkeypatch.setattr(db.pool, "connection", lambda: Conn())

    assert db.approval_by_id(7) == {"id": 7, "status": "approved"}
    assert db.approval_by_id(8) is None
