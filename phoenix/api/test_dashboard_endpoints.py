"""The dashboard's read endpoints, over fixture rows in place of the database.

The database calls are replaced with the fixture incidents (fixtures/README.md). What
is checked here is the HTTP surface: what each route returns, what it refuses, that
nothing leaks the approval token, and that every statement the dashboard sends the
database is a read.
"""

import os
import re

os.environ.setdefault("DATABASE_URL", "postgresql://phoenix_app:test@localhost:1/phoenix")

import psycopg
import pytest
from fastapi.testclient import TestClient

import db
import fixture_store
import main

FIXTURES = fixture_store.load()
TOKEN = "approval-token-for-tests"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PHOENIX_APPROVAL_TOKEN", TOKEN)
    monkeypatch.setenv("PHOENIX_APPROVERS", "tester")
    monkeypatch.setattr(db, "overview", lambda limit: fixture_store.overview_rows(FIXTURES)[:limit])
    monkeypatch.setattr(db, "incident_rows", lambda incident_id: FIXTURES.get(incident_id))
    monkeypatch.setattr(db, "pending_approvals", lambda: [
        {**a, "incident_id": rows["incident"]["id"], "service_name": rows["incident"]["service_name"]}
        for rows in FIXTURES.values() for a in rows["approvals"] if a["status"] == "pending"
    ])
    return TestClient(main.app)


def test_the_overview_lists_every_incident_with_alert_status_and_outcome_apart(client):
    body = client.get("/incidents/overview").json()
    rows = {row["id"]: row for row in body["incidents"]}
    assert len(rows) == len(FIXTURES)
    assert rows[33]["alert_status"] == "resolved" and rows[33]["outcome"]["key"] == "resolved"
    assert rows[38]["alert_status"] == "active" and rows[38]["outcome"]["key"] == "llm_unavailable"
    assert rows[39]["outcome"]["key"] == "awaiting_approval" and rows[39]["pending_approvals"] == 1


def test_the_overview_limit_is_bounded(client):
    assert len(client.get("/incidents/overview?limit=3").json()["incidents"]) == 3
    assert len(client.get("/incidents/overview?limit=0").json()["incidents"]) == 1


def test_the_trace_for_an_incident_is_the_assembled_view(client):
    body = client.get("/incidents/33/trace").json()
    assert body["incident"]["outcome"]["key"] == "resolved"
    assert body["hypotheses"]["leader"]["category"] == "deploy"
    assert body["approvals"][0]["decided_by"] == "tester"


def test_an_unknown_incident_is_a_404_not_an_empty_trace(client):
    assert client.get("/incidents/9999/trace").status_code == 404
    assert client.get("/incidents/9999/audit").status_code == 404


def test_a_non_numeric_incident_id_is_rejected_before_the_database(client):
    assert client.get("/incidents/abc/trace").status_code == 422


def test_the_audit_layer_returns_the_raw_rows_in_order(client):
    rows = client.get("/incidents/33/audit").json()["rows"]
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    assert rows[0]["event_type"] == "observer_pass" and "requested_tools" in rows[0]["detail"]


def test_the_pending_queue_holds_only_requests_still_open(client):
    approvals = client.get("/approvals").json()["approvals"]
    assert [a["id"] for a in approvals] == [15]
    assert approvals[0]["incident_id"] == 39 and approvals[0]["service_name"] == "auth-service"


def test_the_pending_queue_refuses_other_statuses(client):
    assert client.get("/approvals?status=approved").status_code == 422


def test_the_config_names_every_outcome_and_never_the_token(client):
    response = client.get("/dashboard-config")
    body = response.json()
    assert body["approvals_enabled"] is True
    assert {"llm_unavailable", "insufficient_evidence", "escalated", "action_unavailable", "pr_opened", "pr_exists"} <= body["outcomes"].keys()
    assert TOKEN not in response.text


def test_approvals_are_reported_disabled_when_the_token_or_the_list_is_missing(client, monkeypatch):
    monkeypatch.delenv("PHOENIX_APPROVAL_TOKEN")
    assert client.get("/dashboard-config").json()["approvals_enabled"] is False
    monkeypatch.setenv("PHOENIX_APPROVAL_TOKEN", TOKEN)
    monkeypatch.setenv("PHOENIX_APPROVERS", "")
    assert client.get("/dashboard-config").json()["approvals_enabled"] is False


def test_an_unreachable_database_is_a_503_with_no_connection_detail(client, monkeypatch):
    def down(*args):
        raise psycopg.OperationalError("connection to server at 10.0.0.5 failed: password authentication failed for user phoenix_app")

    monkeypatch.setattr(db, "incident_rows", down)
    response = client.get("/incidents/33/trace")
    assert response.status_code == 503
    assert "10.0.0.5" not in response.text and "password" not in response.text


def test_the_trace_response_carries_no_planted_secret(client, monkeypatch):
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    rows = {**FIXTURES[33], "audit": [{**FIXTURES[33]["audit"][0], "detail": {"note": f"saw {secret}"}}]}
    monkeypatch.setattr(db, "incident_rows", lambda incident_id: rows)
    assert secret not in client.get("/incidents/33/trace").text
    assert secret not in client.get("/incidents/33/audit").text


def test_every_dashboard_statement_is_a_read():
    statements = [db.OVERVIEW, db.TRACE_INCIDENT, db.TRACE_EVIDENCE, db.TRACE_HYPOTHESES, db.TRACE_AUDIT,
                  db.TRACE_APPROVALS, db.TRACE_MEMORY, db.PENDING_APPROVALS]
    for sql in statements:
        assert sql.lstrip().upper().startswith("SELECT")
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT)\b", sql, re.IGNORECASE)


def test_the_pending_statement_skips_requests_past_their_deadline():
    assert "p.status = 'pending' AND p.expires_at > now()" in db.PENDING_APPROVALS


def test_incident_rows_reads_the_six_tables_and_returns_none_for_no_incident(monkeypatch):
    seen = []

    class Cursor:
        def __init__(self, columns, rows):
            self.description = [type("C", (), {"name": c}) for c in columns]
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            seen.append(sql)
            if sql == db.TRACE_INCIDENT:
                return Cursor(["id", "service_name"], [(5, "auth-service")] if params["id"] == 5 else [])
            return Cursor(["id"], [(1,)])

    monkeypatch.setattr(db.pool, "connection", lambda: FakeConn())
    assert db.incident_rows(404) is None
    assert seen == [db.TRACE_INCIDENT]
    rows = db.incident_rows(5)
    assert rows["incident"] == {"id": 5, "service_name": "auth-service"}
    assert set(rows) == {"incident", "evidence", "hypotheses", "audit", "approvals", "memory"}
    assert rows["memory"] == {"id": 1}


def test_the_decision_endpoint_is_still_behind_its_token(client):
    response = client.post("/approvals/15/decision", json={"decision": "approve", "approver": "tester"})
    assert response.status_code == 401


def test_the_image_carries_every_module_main_imports_and_the_page():
    from pathlib import Path

    dockerfile = (Path(main.__file__).parent / "Dockerfile").read_text()
    for name in ("main.py", "db.py", "schemas.py", "incident_trace.py", "redact.py", "COPY dashboard ./dashboard"):
        assert name in dockerfile


def test_the_test_only_files_are_kept_out_of_the_image():
    from pathlib import Path

    ignored = (Path(main.__file__).parent / ".dockerignore").read_text()
    for name in ("dashboard/test", "fixtures", "dev_server.py", "fixture_store.py"):
        assert name in ignored


def test_the_page_is_served_from_the_same_app_without_shadowing_the_api(client):
    page = client.get("/dashboard/")
    assert page.status_code == 200 and "<title>Phoenix</title>" in page.text
    assert client.get("/dashboard/js/main.js").status_code == 200
    assert client.get("/incidents/overview").status_code == 200


def test_the_page_files_are_revalidated_so_a_deploy_never_pairs_new_and_old_modules(client):
    for path in ("/dashboard/", "/dashboard/js/main.js", "/dashboard/styles/tokens.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
    assert client.get("/dashboard/js/main.js", headers={"if-none-match": client.get("/dashboard/js/main.js").headers["etag"]}).status_code == 304
