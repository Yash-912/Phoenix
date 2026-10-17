"""The graph's side of the approval record: what it writes, what it reads, and how a
failure is reported. A fake pool stands in for Postgres, so these tests check the
statements and the error mapping, not the table itself (007_approvals.sql has not
been run against a live database from here).
"""

from datetime import datetime, timezone

import pytest

from phoenix.graph import approvals

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
ROW = (7, 1, "1:rollback_deployment:checkout-service:1:0", "pending", "rollback_deployment",
       "checkout-service", ["v17", "2026-10-02T08:00:00+00:00"], 2, NOW, NOW, None, None, None)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    """Answers each execute with the next scripted result set, and records the statements."""

    def __init__(self, results, error=None):
        self._results = list(results)
        self._error = error
        self.executed: list[tuple[str, dict | None]] = []
        self.commits = 0

    def execute(self, sql, params=None):
        if self._error:
            raise self._error
        self.executed.append((sql, params))
        return _Cursor(self._results.pop(0) if self._results else [])

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


def _use_pool(monkeypatch, conn):
    monkeypatch.setattr(approvals.persist, "_get_pool", lambda: _Pool(conn))


def _request(**overrides):
    kwargs = dict(
        incident_id=1,
        request_key="1:rollback_deployment:checkout-service:1:0",
        action="rollback_deployment",
        container="checkout-service",
        args=("v17", "2026-10-02T08:00:00+00:00"),
        tier=2,
        incident_summary="deploy: the v18 rollout broke it",
        plan_reasoning="top hypothesis is deploy",
        evidence=[{"source": "loki", "iteration": 1, "summary": "panics"}],
        ttl_seconds=900,
    )
    kwargs.update(overrides)
    return kwargs


def test_a_new_request_is_inserted_and_returned_as_a_row(monkeypatch):
    conn = _Conn([[ROW]])
    _use_pool(monkeypatch, conn)

    row = approvals.request_approval(**_request())

    assert row["id"] == 7
    assert row["status"] == "pending"
    assert row["args"] == ["v17", "2026-10-02T08:00:00+00:00"]
    assert conn.commits == 1
    sql, params = conn.executed[0]
    assert "ON CONFLICT (request_key) DO NOTHING" in sql
    assert params["ttl_seconds"] == 900


def test_a_repeated_request_returns_the_row_it_already_has(monkeypatch):
    """A node that resumes asks for the same request again. The insert conflicts
    and the existing row, decided or not, comes back instead of a second one."""
    decided = ROW[:3] + ("approved",) + ROW[4:10] + (NOW, "yash@example.com", "dashboard")
    conn = _Conn([[], [decided]])
    _use_pool(monkeypatch, conn)

    row = approvals.request_approval(**_request())

    assert row["status"] == "approved"
    assert row["decided_by"] == "yash@example.com"
    assert "WHERE request_key" in conn.executed[1][0]


def test_no_database_means_no_approval_and_so_no_action(monkeypatch):
    monkeypatch.setattr(approvals.persist, "_get_pool", lambda: None)

    with pytest.raises(approvals.ApprovalStoreError, match="no database connection"):
        approvals.request_approval(**_request())


def test_a_driver_error_on_write_is_an_approval_store_error(monkeypatch):
    _use_pool(monkeypatch, _Conn([], error=RuntimeError("connection reset")))

    with pytest.raises(approvals.ApprovalStoreError, match="connection reset"):
        approvals.request_approval(**_request())


def test_a_driver_error_on_read_is_an_approval_store_error(monkeypatch):
    _use_pool(monkeypatch, _Conn([], error=RuntimeError("server closed the connection")))

    with pytest.raises(approvals.ApprovalStoreError):
        approvals.read_approval(7)


def test_a_missing_approval_reads_as_none_not_as_pending(monkeypatch):
    _use_pool(monkeypatch, _Conn([[]]))

    assert approvals.read_approval(999) is None


def test_expiry_touches_only_pending_rows_past_their_deadline(monkeypatch):
    expired = ROW[:3] + ("expired",) + ROW[4:10] + (NOW, "system:expiry", "expiry")
    conn = _Conn([[expired]])
    _use_pool(monkeypatch, conn)

    rows = approvals.expire_stale_approvals()

    assert [row["status"] for row in rows] == ["expired"]
    sql = conn.executed[0][0]
    assert "WHERE status = 'pending' AND expires_at <= now()" in sql
    assert "system:expiry" in sql


def test_a_failed_expiry_sweep_is_reported_so_the_watcher_can_retry(monkeypatch):
    _use_pool(monkeypatch, _Conn([], error=RuntimeError("timeout")))

    with pytest.raises(approvals.ApprovalStoreError, match="could not be swept"):
        approvals.expire_stale_approvals()
