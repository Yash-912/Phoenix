"""The incident list reports two things: the alert state and the investigation outcome.

incidents.status is the Alertmanager lifecycle. The investigation's own result is
stored in incident_memory, so the list joins it in and returns it under its own name.
The database call is not made here; these tests read the statement and the column
list the function returns.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://phoenix_app:test@localhost:1/phoenix")

import db


def test_list_joins_the_investigation_outcome_without_dropping_unfinished_incidents():
    # LEFT JOIN, so an incident with no memory row yet still appears, with a null outcome.
    assert "LEFT JOIN incident_memory m ON m.incident_id = i.id" in db.LIST_INCIDENTS
    assert "m.status AS investigation_status" in db.LIST_INCIDENTS


def test_list_keeps_the_alert_status_separate_from_the_outcome():
    assert "i.status" in db.LIST_INCIDENTS
    assert "investigation_status" in db.LIST_INCIDENTS


def test_list_returns_the_outcome_as_its_own_column(monkeypatch):
    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, *args, **kwargs):
            class Result:
                def fetchall(self_inner):
                    return [
                        (8, "payment-service", "HighLatency", "high", "active",
                         "pr_exists", None, None, 1),
                        (7, "worker-service", "WorkerServiceMemoryGrowth", "high", "resolved",
                         None, None, None, 1),
                    ]
            return Result()

    monkeypatch.setattr(db.pool, "connection", lambda: FakeConn())
    rows = db.list_incidents()

    assert rows[0]["status"] == "active"
    assert rows[0]["investigation_status"] == "pr_exists"
    assert rows[1]["status"] == "resolved"
    assert rows[1]["investigation_status"] is None
